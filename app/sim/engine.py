"""Daily circle jobs and the demo admin commands (spec sections 9.1 to 9.3).

`DemoEngine.run_day` is the only place a circle's day is processed, whether the scheduler reached
the virtual lesson time or an admin sent /simulate_day. Its steps, in order:

1. Today's lesson: generated (or the pre-generated fallback) and translated into the real members'
   languages. Every LLM call happens here, before the transaction writes anything, because the
   LLM cache writes in its own transaction and SQLite would block it.
2. Claim the day in `daily_runs`. A run that finds the day already claimed rolls back, so the
   scheduler and /simulate_day never process the same circle and virtual day twice.
3. Queue the lesson for every real member.
4. Persona actions in a seeded circle: check-ins and answers, then Chidi's replies to questions.
5. Inactivity check: members first detected as quiet today (`app/core/nudges.py`).
6. Queue each real member's batched circle update, carrying a nudge prompt for each newly quiet
   member to everyone else in the circle.

The spec lists persona actions before the lesson post. Here the lesson comes first because the
personas answer it; nothing is delivered before the transaction commits, so learners still get
the lesson and then an update that already shows who checked in.

/simulate_day advances the clock by 24 hours and runs the day; /simulate_week does that seven
times. Weekly payouts arrive with milestone 7. Admin commands manage their own transactions,
because they advance the clock and generate lessons.
"""
import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.base import Button
from app.core import circles, feed, nudges, outbox
from app.core.clock import Clock, virtual_day
from app.core.conversation import ADMIN_PREFIX
from app.core.embeddings import Embedder
from app.core.i18n import t
from app.core.lessons import LessonService, LessonUnavailable
from app.core.replies import Reply
from app.db import dialect_insert
from app.models import (
    Circle, CircleMember, ClockState, DailyRun, FeedEvent, Goal, Language, Learner, Lesson, Nudge, OutboundDelivery,
    RunTrigger, Simulation, SyllabusTopic,
)
from app.sim import personas

logger = logging.getLogger(__name__)

DEFAULT_SEED = 42
DAYS_PER_WEEK = 7
RESET_YES = f"{ADMIN_PREFIX}reset:yes"
RESET_NO = f"{ADMIN_PREFIX}reset:no"
_SEED = re.compile(r"\d{1,18}")


class _AlreadyRun(Exception):
    pass


@dataclass(frozen=True)
class DayReport:
    circle_id: UUID
    day: date
    lesson_day: int | None
    topic: str | None
    checked_in: list[str]  # first names of members who checked in today, in joining order
    replies: int  # persona replies to circle questions
    quiet: list[str]  # first names of members first detected as quiet today
    lessons_queued: int
    updates_queued: int


class DemoEngine:
    def __init__(self, sessions: sessionmaker[Session], clock: Clock, lessons: LessonService, embedder: Embedder,
                 *, salt: str, min_score: float, goal_code: str = "jamb_bio") -> None:
        self.sessions, self.clock, self.lessons, self.embedder = sessions, clock, lessons, embedder
        self.salt, self.min_score, self.goal_code = salt, min_score, goal_code

    # Daily jobs

    def run_day(self, circle_id: UUID, trigger: RunTrigger) -> DayReport | None:
        """Process the circle's current virtual day once; None if it was already processed."""
        try:
            with self.sessions.begin() as session:
                return self._run_day(session, circle_id, trigger)
        except _AlreadyRun:
            return None
        except IntegrityError:
            # SQLite ignores the circle row lock, so a racing run can insert the same lesson first.
            with self.sessions() as session:
                if session.get(DailyRun, (circle_id, virtual_day(self.clock.now(session)))) is not None:
                    return None
            raise

    def _run_day(self, session: Session, circle_id: UUID, trigger: RunTrigger) -> DayReport:
        now = self.clock.now(session)
        today = virtual_day(now)
        if session.get(DailyRun, (circle_id, today)) is not None:
            raise _AlreadyRun
        circle = session.get(Circle, circle_id)
        if circle is None:
            raise LookupError("No such circle")
        # 1. Today's lesson, with every LLM call made before this transaction writes.
        try:
            lesson, posts = self.lessons.post(session, circle, now)
        except LessonUnavailable as error:
            logger.warning("No lesson for the circle's day: %s", error)
            lesson, posts = None, []
        # 2. Claim the day; if another run claimed it first, roll everything back.
        claimed = session.scalar(dialect_insert(session, DailyRun).values(
            circle_id=circle.id, virtual_day=today, trigger=trigger.value, lesson_id=lesson.id if lesson else None,
            created_at=now,
        ).on_conflict_do_nothing(index_elements=["circle_id", "virtual_day"]).returning(DailyRun.circle_id))
        if claimed is None:
            raise _AlreadyRun
        # 3. The lesson post, for real members only.
        lessons_queued = sum(outbox.enqueue(session, learner, replies, key=f"lesson:{lesson.id}:{learner.id}",
                                            now=self.clock.now(session))
                             for learner, replies in posts)
        # 4. Persona actions.
        replies = self._personas_act(session, circle, lesson, today, now)
        # 5. Inactivity check, after the personas have had today's chance to check in.
        quiet = nudges.detect(session, circle.id, now)
        # 6. The batched circle update, with nudge prompts for the newly quiet.
        updates_queued = feed.queue_digests(session, self.clock, circle, now, quiet)
        statuses = feed.member_statuses(session, circle.id, lesson)
        topic = session.get(SyllabusTopic, lesson.topic_id).title if lesson else None
        return DayReport(circle.id, today, lesson.day_index if lesson else None, topic,
                         [status.first_name for status in statuses if status.last_checkin == today],
                         replies, [member.first_name for member in quiet], lessons_queued, updates_queued)

    def _personas_act(self, session: Session, circle: Circle, lesson: Lesson | None, today: date, now) -> int:
        simulation = session.get(Simulation, circle.id)
        if simulation is None:
            return 0
        members = personas.circle_personas(session, circle, session.get(Goal, circle.goal_id), self.salt)
        if lesson is not None:
            # The circle's processed days, this one included (the claim is already written).
            day = session.scalar(select(func.count()).select_from(DailyRun).where(DailyRun.circle_id == circle.id))
            for persona, learner in members:
                if personas.checks_in(session, persona, learner, day, today):
                    personas.answer_lesson(session, simulation.seed, persona, learner, lesson, day, now)
        return sum(personas.reply_to_questions(session, self.embedder, simulation.seed, persona, learner, circle,
                                               self.min_score, now)
                   for persona, learner in members)

    def run_scheduled(self) -> tuple[list[DayReport], bool]:
        """The scheduler's daily run for every circle with a real member; (reports, whether any circle failed)."""
        with self.sessions() as session:
            circle_ids = session.scalars(
                select(CircleMember.circle_id).join(Learner, Learner.id == CircleMember.learner_id)
                .where(Learner.is_simulated.is_(False)).distinct()
            ).all()
        reports, failed = [], False
        for circle_id in circle_ids:
            try:
                report = self.run_day(circle_id, RunTrigger.SCHEDULER)
            except Exception:  # noqa: BLE001 - one circle's failure must not stop the others
                logger.exception("Daily jobs failed for a circle")
                failed = True
                continue
            if report is not None:
                reports.append(report)
        return reports, failed

    def simulate_day(self, circle_id: UUID) -> tuple[date, DayReport | None]:
        """Advance the virtual clock by 24 hours, then run the circle's day."""
        now = self.clock.advance(timedelta(days=1))
        return virtual_day(now), self.run_day(circle_id, RunTrigger.SIMULATION)

    # Admin commands. Conversation checks ADMIN_IDS before calling these.

    def command(self, command: str, argument: str, user_hash: str) -> list[Reply]:
        if command == "seed_demo":
            return self._seed(user_hash, argument)
        if command == "simulate_day":
            return self._simulate(user_hash, 1)
        if command == "simulate_week":
            return self._simulate(user_hash, DAYS_PER_WEEK)
        if command == "reset_demo":
            language = self._language(user_hash)
            return [Reply(t("admin.reset_confirm", language), [Button(t("admin.reset_yes", language), RESET_YES),
                                                                Button(t("delete.cancel", language), RESET_NO)])]
        if command == "demo_status":
            return self._status(user_hash)
        raise ValueError(f"Unknown admin command {command!r}")

    def button(self, payload: str, user_hash: str) -> list[Reply]:
        if payload == RESET_YES:
            return self._reset(user_hash)
        language = self._language(user_hash)
        if payload == RESET_NO:
            return [Reply(t("admin.reset_cancelled", language))]
        return [Reply(t("button.expired", language))]

    def _admin(self, session: Session, user_hash: str) -> tuple[Learner | None, Goal | None, Language]:
        """The admin's own learner row if they have onboarded, the goal to demo, and the reply language."""
        learner = session.scalar(select(Learner).where(Learner.channel_user_hash == user_hash))
        if learner is not None and learner.goal_id is not None:
            goal = session.get(Goal, learner.goal_id)
        else:
            goal = session.scalar(select(Goal).where(Goal.code == self.goal_code))
        return learner, goal, learner.language if learner is not None else Language.EN

    def _language(self, user_hash: str) -> Language:
        with self.sessions() as session:
            return self._admin(session, user_hash)[2]

    def _demo_circle(self, session: Session, goal: Goal | None) -> Circle | None:
        return session.get(Circle, circles.demo_circle_id(goal)) if goal is not None else None

    def _seed(self, user_hash: str, argument: str) -> list[Reply]:
        if argument and _SEED.fullmatch(argument) is None:
            return [Reply(t("admin.seed_usage", self._language(user_hash)))]
        with self.sessions.begin() as session:
            admin, goal, language = self._admin(session, user_hash)
            if goal is None:
                return [Reply(t("admin.no_goal", language))]
            now = self.clock.now(session)
            circle = circles.demo_circle(session, goal, now)
            seed = int(argument) if argument else None
            session.execute(dialect_insert(session, Simulation).values(
                circle_id=circle.id, seed=DEFAULT_SEED if seed is None else seed, created_at=now,
            ).on_conflict_do_nothing(index_elements=["circle_id"]))
            simulation = session.get(Simulation, circle.id)
            if seed is not None:
                simulation.seed = seed  # an explicit seed replaces the previous one
            personas.seed_personas(session, circle, goal, self.salt, now)
            joined = admin is not None and admin.goal_id == goal.id
            if joined:
                circles.join(session, circle, admin, now)
            replies = [Reply(t("admin.seeded", language, circle=circle.name,
                               names=", ".join(circles.member_first_names(session, circle.id)), seed=simulation.seed))]
            # The admin becomes a learner only through onboarding, so their consent is recorded like anyone's.
            return replies if joined else [*replies, Reply(t("admin.join_first", language))]

    def _seeded_circle(self, session: Session, user_hash: str) -> tuple[Circle | None, Language]:
        _, goal, language = self._admin(session, user_hash)
        circle = self._demo_circle(session, goal)
        if circle is None or session.get(Simulation, circle.id) is None:
            return None, language
        return circle, language

    def _simulate(self, user_hash: str, days: int) -> list[Reply]:
        with self.sessions() as session:
            circle, language = self._seeded_circle(session, user_hash)
            circle_id = circle.id if circle is not None else None
        if circle_id is None:
            return [Reply(t("admin.not_seeded", language))]
        blocks = [self._report_text(*self.simulate_day(circle_id), language) for _ in range(days)]
        if days == DAYS_PER_WEEK:
            blocks.append(t("admin.payout_pending", language))
        return [Reply("\n\n".join(blocks))]

    def _report_text(self, day: date, report: DayReport | None, language: Language) -> str:
        shown = day.strftime("%a %d %b %Y")
        if report is None:
            return t("admin.already_ran", language, date=shown)
        lesson = (t("admin.day_lesson", language, day=report.lesson_day, topic=report.topic)
                  if report.lesson_day is not None else t("admin.no_lesson", language))
        text = t("admin.day_done", language, date=shown, lesson=lesson,
                 names=", ".join(report.checked_in) or t("admin.nobody", language),
                 lessons=report.lessons_queued, updates=report.updates_queued)
        return f"{text}\n{t('admin.day_quiet', language, names=', '.join(report.quiet))}" if report.quiet else text

    def _status(self, user_hash: str) -> list[Reply]:
        with self.sessions() as session:
            _, goal, language = self._admin(session, user_hash)
            circle = self._demo_circle(session, goal)
            if circle is None:
                return [Reply(t("admin.not_seeded", language))]
            now = self.clock.now(session)
            today = virtual_day(now)
            offset = session.get(ClockState, 1).offset_seconds
            simulation = session.get(Simulation, circle.id)
            days = session.scalar(select(func.count()).select_from(DailyRun).where(DailyRun.circle_id == circle.id))
            lesson = feed.todays_lesson(session, circle.id, today)
            rows = session.execute(select(Learner, CircleMember).join(CircleMember, CircleMember.learner_id == Learner.id)
                                   .where(CircleMember.circle_id == circle.id)
                                   .order_by(CircleMember.joined_at, Learner.first_name, Learner.id)).all()
            members = "\n".join(t("admin.status_member", language, name=learner.first_name,
                                  persona=t("admin.persona_marker", language) if learner.is_simulated else "",
                                  last=_day_text(member.last_checkin_at, language), streak=member.streak_days)
                                for learner, member in rows)
            topic = session.get(SyllabusTopic, lesson.topic_id).title if lesson else None
            return [Reply(t("admin.status", language, time=now.strftime("%a %d %b %Y, %H:%M UTC"),
                            offset=f"+{offset // 86400}d {offset % 86400 // 3600}h", circle=circle.name,
                            seed=simulation.seed if simulation else "-", days=days,
                            lesson=t("admin.day_lesson", language, day=lesson.day_index, topic=topic) if lesson else "-",
                            members=members))]

    def _reset(self, user_hash: str) -> list[Reply]:
        """Wipe the demo circle's state and the clock offset; keep learners, their QA history and the corpus."""
        with self.sessions.begin() as session:
            _, goal, language = self._admin(session, user_hash)
            circle = self._demo_circle(session, goal)
            if circle is None:
                return [Reply(t("admin.not_seeded", language))]
            now = self.clock.reset(session)
            member_ids = list(session.scalars(select(CircleMember.learner_id)
                                              .where(CircleMember.circle_id == circle.id)))
            persona_ids = list(session.scalars(select(Learner.id).where(Learner.id.in_(member_ids),
                                                                        Learner.is_simulated.is_(True))))
            for model in (DailyRun, Lesson, FeedEvent, Nudge, Simulation):  # lessons cascade to questions and answers
                session.execute(delete(model).where(model.circle_id == circle.id))
            session.execute(delete(OutboundDelivery).where(OutboundDelivery.learner_id.in_(member_ids)))
            session.execute(delete(Learner).where(Learner.id.in_(persona_ids)))
            session.execute(update(CircleMember).where(CircleMember.circle_id == circle.id)
                            .values(last_checkin_at=None, streak_days=0, feed_delivered_at=None, feed_cursor_id=None,
                                    joined_at=now))
            return [Reply(t("admin.reset_done", language))]


def _day_text(moment, language: Language) -> str:
    return virtual_day(moment).strftime("%a %d %b") if moment is not None else t("admin.never", language)

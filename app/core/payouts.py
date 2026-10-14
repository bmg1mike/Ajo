"""Weekly payouts, /progress and /pack (spec section 7.7).

A circle's week is seven processed days: its payout is the last step of every seventh daily run
(`DemoEngine.run_day`), so the scheduler and /simulate_day share one boundary and a week is paid
out once. The `weekly_payouts` row, inserted with ON CONFLICT DO NOTHING, is a second guard and
records the period. Week N covers the lessons posted after the end of week N - 1 up to and
including its seventh run, so every lesson belongs to exactly one week. Week N - 1 ended at its
payout, or, if it was never paid out (days processed before payouts existed), at the circle's
7(N - 1)th daily run. Answers count towards the week of the lesson they answer, whenever they
were given.

Every real member gets one queued batch:

1. A private progress summary: days checked in (lesson days with at least one answer), main
   questions answered, accuracy, topics covered and the weakest topic. Only the learner sees it.
2. The circle summary, the same for everyone: total check-ins and how many members checked in.
   It holds counts only, never a name with a score.
3. One verified opportunity for the circle's goal and country (`app/core/opportunities.py`).
4. The study pack as a text file under 500 KB (`app/core/pack.py`), when the week had lessons.

The payout runs at the morning lesson time of day 7, so day 7's lesson is in it but answers given
later that day are not; they still show in /progress and /pack, which describe the current week
so far, or the week just paid out until the next week's first lesson arrives.
"""
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import opportunities, outbox
from app.core.clock import Clock, virtual_day
from app.core.i18n import t
from app.core.lessons import learner_circle
from app.core.pack import build as build_pack
from app.core.replies import Attachment, Reply
from app.db import dialect_insert
from app.models import (
    Answer, Circle, CircleMember, DailyRun, Goal, Language, Learner, Lesson, Question, SyllabusTopic, WeeklyPayout,
)

DAYS_PER_WEEK = 7


@dataclass(frozen=True)
class Week:
    index: int
    start: datetime | None  # exclusive: the previous week's end; None for week 1
    end: datetime | None  # inclusive; None while the week is still running

    @property
    def so_far(self) -> bool:
        return self.end is None


def week_lessons(session: Session, circle_id: UUID, week: Week, now: datetime | None = None) -> list[Lesson]:
    query = select(Lesson).where(Lesson.circle_id == circle_id)
    if week.start is not None:
        query = query.where(Lesson.posted_at > week.start)
    if week.end is not None or now is not None:
        query = query.where(Lesson.posted_at <= (week.end or now))
    return list(session.scalars(query.order_by(Lesson.day_index)))


def _processed_days(session: Session, circle_id: UUID) -> int:
    return session.scalar(select(func.count()).select_from(DailyRun).where(DailyRun.circle_id == circle_id))


def week_end(session: Session, circle_id: UUID, index: int) -> datetime | None:
    """When week `index` ended: its payout, or else its seventh daily run; None if it has not ended."""
    if index < 1:
        return None
    paid = session.get(WeeklyPayout, (circle_id, index))
    if paid is not None:
        return paid.period_end
    # A week that was never paid out, such as days processed before payouts existed.
    return session.scalar(select(DailyRun.created_at).where(DailyRun.circle_id == circle_id)
                          .order_by(DailyRun.virtual_day).offset(index * DAYS_PER_WEEK - 1).limit(1))


def current_week(session: Session, circle_id: UUID, now: datetime) -> tuple[Week, list[Lesson]]:
    """The running week if it has a lesson yet, otherwise the week that just ended (or an empty week 1)."""
    index = _processed_days(session, circle_id) // DAYS_PER_WEEK + 1
    running = Week(index, week_end(session, circle_id, index - 1), None)
    lessons = week_lessons(session, circle_id, running, now)
    if lessons or index == 1:
        return running, lessons
    ended = Week(index - 1, week_end(session, circle_id, index - 2), running.start)
    return ended, week_lessons(session, circle_id, ended)


# Progress


@dataclass(frozen=True)
class TopicResult:
    title: str
    correct: int
    answered: int


@dataclass(frozen=True)
class Progress:
    lesson_days: int
    days_checked_in: int
    questions: int  # main questions in the week's lessons; easier follow-ups are not counted
    answered: int
    correct: int
    topics: list[str]  # topics of the lessons the learner answered, in lesson order
    weakest: TopicResult | None  # the topic with the lowest accuracy below 100%


def _answers(session: Session, lessons: Sequence[Lesson], learner_ids: Sequence[UUID] | None = None):
    """(learner, lesson, topic, main question?, correct?) for every answer to the lessons' questions."""
    if not lessons:
        return []
    query = (select(Answer.learner_id, Question.lesson_id, Lesson.topic_id, Question.parent_question_id.is_(None),
                    Answer.is_correct)
             .join(Question, Question.id == Answer.question_id).join(Lesson, Lesson.id == Question.lesson_id)
             .where(Question.lesson_id.in_([lesson.id for lesson in lessons])))
    if learner_ids is not None:
        query = query.where(Answer.learner_id.in_(learner_ids))
    return session.execute(query).all()


def progress(session: Session, learner: Learner, lessons: Sequence[Lesson]) -> Progress:
    rows = _answers(session, lessons, [learner.id])
    lesson_ids = [lesson.id for lesson in lessons]
    questions = session.scalar(select(func.count()).select_from(Question)
                               .where(Question.lesson_id.in_(lesson_ids), Question.parent_question_id.is_(None))) \
        if lesson_ids else 0
    mains = [row for row in rows if row[3]]
    by_topic: dict[UUID, list[bool]] = {}
    for _, _, topic_id, _, is_correct in mains:
        by_topic.setdefault(topic_id, []).append(is_correct)
    answered_lessons = {row[1] for row in rows}
    topic_order = list(dict.fromkeys(lesson.topic_id for lesson in lessons if lesson.id in answered_lessons))
    titles = dict(session.execute(select(SyllabusTopic.id, SyllabusTopic.title)
                                  .where(SyllabusTopic.id.in_(topic_order))).all()) if topic_order else {}
    results = [TopicResult(titles[topic_id], sum(by_topic[topic_id]), len(by_topic[topic_id]))
               for topic_id in topic_order if topic_id in by_topic]
    # Lowest accuracy first; between equals, the topic with more evidence, then the earlier one.
    weak = sorted((result for result in results if result.correct < result.answered),
                  key=lambda result: (result.correct / result.answered, -result.answered))
    return Progress(lesson_days=len(lessons), days_checked_in=len(answered_lessons), questions=questions,
                    answered=len(mains), correct=sum(row[4] for row in mains),
                    topics=[titles[topic_id] for topic_id in topic_order], weakest=weak[0] if weak else None)


def progress_text(result: Progress, week: Week, language: Language) -> str:
    header = "progress.header_so_far" if week.so_far else "progress.header"
    lines = [t(header, language, week=week.index), t("progress.private", language),
             t("progress.days", language, days=result.days_checked_in, total=result.lesson_days),
             t("progress.questions", language, answered=result.answered, total=result.questions)]
    if result.answered:
        lines.append(t("progress.accuracy", language, percent=round(100 * result.correct / result.answered),
                       correct=result.correct, answered=result.answered))
    else:
        lines.append(t("progress.accuracy_none", language))
    lines.append(t("progress.topics", language, topics=", ".join(result.topics)) if result.topics
                 else t("progress.topics_none", language))
    if result.weakest is not None:
        lines.append(t("progress.weakest", language, topic=result.weakest.title, correct=result.weakest.correct,
                       answered=result.weakest.answered))
    elif result.answered:
        lines.append(t("progress.no_weakest", language))
    return "\n".join(lines)


# The circle summary


def circle_summary(session: Session, circle_id: UUID, lessons: Sequence[Lesson]) -> dict:
    """Counts only: check-ins (member and lesson day pairs with an answer), lesson days and members."""
    member_ids = list(session.scalars(select(CircleMember.learner_id).where(CircleMember.circle_id == circle_id)))
    checkins = {(row[0], row[1]) for row in _answers(session, lessons, member_ids)}
    return {"checkins": len(checkins), "lesson_days": len(lessons), "members": len(member_ids),
            "active_members": len({learner_id for learner_id, _ in checkins})}


def circle_summary_text(summary: dict, week: int, language: Language) -> str:
    return t("circle_summary.text", language, week=week, checkins=summary["checkins"], days=summary["lesson_days"],
             active=summary["active_members"], members=summary["members"])


# Payout


@dataclass(frozen=True)
class PayoutReport:
    week: int
    queued: int  # members whose payout was queued


def _pack_reply(session: Session, learner: Learner, lessons: Sequence[Lesson], week: Week, goal: Goal) -> Reply:
    pack = build_pack(session, learner, lessons, goal_title=goal.title, week=week.index, so_far=week.so_far)
    caption = "pack.caption_so_far" if week.so_far else "pack.caption"
    return Reply(t(caption, learner.language, week=week.index, size=pack.size_kb),
                 file=Attachment(pack.filename, pack.data))


def payout_replies(session: Session, learner: Learner, lessons: Sequence[Lesson], week: Week, goal: Goal,
                   summary: dict, opportunity) -> list[Reply]:
    language = learner.language
    replies = [Reply(f"{t('payout.intro', language, week=week.index)}\n\n"
                     f"{progress_text(progress(session, learner, lessons), week, language)}"),
               Reply(circle_summary_text(summary, week.index, language)),
               opportunities.opportunity_reply(opportunity, language)]
    if lessons:
        replies.append(_pack_reply(session, learner, lessons, week, goal))
    return replies


def queue_weekly(session: Session, clock: Clock, circle: Circle, now: datetime, day: int) -> PayoutReport | None:
    """On every seventh processed day, record the week and queue each real member's payout.

    `day` counts the circle's processed days, this one included. Returns None on other days, or if
    the week was already paid out.
    """
    if day <= 0 or day % DAYS_PER_WEEK:
        return None
    index = day // DAYS_PER_WEEK
    week = Week(index, week_end(session, circle.id, index - 1), now)
    lessons = week_lessons(session, circle.id, week)
    summary = circle_summary(session, circle.id, lessons)
    # RETURNING, not rowcount: psycopg reports -1 for a single-row ON CONFLICT insert.
    claimed = session.scalar(dialect_insert(session, WeeklyPayout).values(
        circle_id=circle.id, week_index=index, period_start=week.start, period_end=now, summary=summary,
        created_at=now,
    ).on_conflict_do_nothing(index_elements=["circle_id", "week_index"]).returning(WeeklyPayout.week_index))
    if claimed is None:
        return None
    goal = session.get(Goal, circle.goal_id)
    opportunity = opportunities.choose(session, goal, virtual_day(now))
    members = session.scalars(select(Learner).join(CircleMember, CircleMember.learner_id == Learner.id)
                              .where(CircleMember.circle_id == circle.id, Learner.is_simulated.is_(False))
                              .order_by(CircleMember.joined_at, Learner.id)).all()
    queued = sum(outbox.enqueue(session, learner, payout_replies(session, learner, lessons, week, goal, summary,
                                                                 opportunity),
                                key=f"payout:{circle.id}:{index}:{learner.id}", now=clock.now(session))
                 for learner in members)
    return PayoutReport(index, queued)


# Learner commands


def progress_command(session: Session, learner: Learner, now: datetime) -> Reply:
    """/progress: the learner's private summary for the current week so far, or the week just paid out."""
    circle = learner_circle(session, learner)
    if circle is None:
        return Reply(t("lesson.not_in_circle", learner.language))
    week, lessons = current_week(session, circle.id, now)
    if not lessons:
        return Reply(t("progress.no_lessons", learner.language))
    return Reply(progress_text(progress(session, learner, lessons), week, learner.language))


def pack_command(session: Session, learner: Learner, now: datetime) -> Reply:
    """/pack: the study pack for the same week /progress describes."""
    circle = learner_circle(session, learner)
    if circle is None:
        return Reply(t("lesson.not_in_circle", learner.language))
    week, lessons = current_week(session, circle.id, now)
    if not lessons:
        return Reply(t("pack.empty", learner.language))
    return _pack_reply(session, learner, lessons, week, session.get(Goal, circle.goal_id))

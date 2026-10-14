"""The batched circle feed (spec section 7.5).

Once a day every real member is sent one "Circle update": each other member's first name and
check-in status, followed by what happened in the circle since their previous update (questions
posted to the circle, replies to them, members leaving). Scores appear only for members who turned
on /share_scores, and are worked out from `answers` when the update is built; feed events never hold
them. The update is queued in the outbox under a key per learner and virtual day, so a second update
on the same day is never queued, and its events wait for the next one. An update is kept under
FEED_MAX_CHARS: long questions are shortened, and events that still do not fit stay after the
member's (created_at, id) cursor for the next day's update. A score line is queued in its private
form and shows the score only if its owner still shares scores when it is sent. Nudges (milestone
6) are delivered immediately and are not part of this batch.
"""
import dataclasses
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.adapters.base import Button
from app.core import outbox
from app.core.clock import Clock, virtual_day
from app.core.i18n import t
from app.core.lessons import lesson_questions
from app.core.qa import SHOW_SOURCE, excerpt
from app.core.replies import Reply
from app.core.retrieval import ChunkRef, load_chunks
from app.models import Answer, Circle, CircleMember, FeedEvent, FeedType, Language, Learner, Lesson

QUIET_WEEKDAY_LIMIT = 7  # beyond a week, "since Monday" would be ambiguous
FEED_MAX_CHARS = 3500  # comfortably under Telegram's 4,096-character message limit
QUESTION_PREVIEW_CHARS = 280  # circle questions can be 1,000 characters; the update shows the start
MORE_RESERVE = 80  # room for the "…and N more" line
EVENT_TYPES = (FeedType.QUESTION, FeedType.REPLY, FeedType.MEMBER_LEFT)


@dataclass(frozen=True)
class MemberStatus:
    learner_id: UUID
    first_name: str
    share_scores: bool
    answered: bool  # every main question of today's lesson
    correct: int
    total: int
    last_checkin: date | None


def todays_lesson(session: Session, circle_id: UUID, today: date) -> Lesson | None:
    latest = session.scalar(select(Lesson).where(Lesson.circle_id == circle_id)
                            .order_by(Lesson.day_index.desc()).limit(1))
    return latest if latest is not None and virtual_day(latest.posted_at) == today else None


def member_statuses(session: Session, circle_id: UUID, lesson: Lesson | None) -> list[MemberStatus]:
    """Every member in joining order, with today's progress taken from `answers`."""
    rows = session.execute(select(Learner, CircleMember).join(CircleMember, CircleMember.learner_id == Learner.id)
                           .where(CircleMember.circle_id == circle_id)
                           .order_by(CircleMember.joined_at, Learner.first_name, Learner.id)).all()
    mains = [q.id for q in lesson_questions(session, lesson.id) if q.parent_question_id is None] if lesson else []
    results: dict[UUID, list[bool]] = {}
    if mains:
        for learner_id, is_correct in session.execute(select(Answer.learner_id, Answer.is_correct)
                                                      .where(Answer.question_id.in_(mains))):
            results.setdefault(learner_id, []).append(is_correct)
    return [MemberStatus(learner.id, learner.first_name, learner.share_scores,
                         answered=bool(mains) and len(results.get(learner.id, [])) == len(mains),
                         correct=sum(results.get(learner.id, [])), total=len(mains),
                         last_checkin=virtual_day(member.last_checkin_at) if member.last_checkin_at else None)
            for learner, member in rows]


def status_line(status: MemberStatus, today: date, language: Language) -> str:
    name = status.first_name
    if status.answered:
        if status.share_scores:  # explicit opt-in only (spec section 16)
            return t("feed.answered_score", language, name=name, correct=status.correct, total=status.total)
        return t("feed.answered", language, name=name)
    if status.last_checkin == today:
        return t("feed.checked_in", language, name=name)
    if status.last_checkin is None:
        return t("feed.not_started", language, name=name)
    if status.last_checkin == today - timedelta(days=1):
        # Real learners answer after the morning update, so yesterday's check-in is not "quiet".
        return t("feed.checked_in_yesterday", language, name=name)
    if (today - status.last_checkin).days >= QUIET_WEEKDAY_LIMIT:
        return t("feed.quiet_long", language, name=name)
    return t("feed.quiet_since", language, name=name,
             weekday=t(f"weekday.{status.last_checkin.weekday()}", language))


def _event_line(session: Session, event: FeedEvent, recipient: Learner) -> tuple[str, ChunkRef | None] | None:
    """One event as the recipient reads it, with the cited chunk of a reply; None if it is not shown to them."""
    language = recipient.language
    if event.actor_learner_id == recipient.id:
        return None  # their own question needs no relaying back
    if event.type == FeedType.MEMBER_LEFT:
        return t("circle.member_left", language), None
    actor = session.get(Learner, event.actor_learner_id)
    if actor is None:
        return None
    if event.type == FeedType.QUESTION:
        question = excerpt(event.payload["question"], QUESTION_PREVIEW_CHARS)
        return t("feed.question", language, name=actor.first_name, question=question), None
    if session.get(FeedEvent, UUID(event.payload["question_event_id"])) is None:
        return None  # a reply whose asker deleted their data, and their question with it
    cited = load_chunks(session, [UUID(event.payload["chunk_id"])])
    if not cited:
        return None  # the chunk was pruned from the library
    return (f"{t('feed.reply', language, name=actor.first_name)}\n"
            f"{t('qa.source_line', language, citation=cited[0].citation)}"), cited[0]


def _events_after(session: Session, circle_id: UUID, member: CircleMember, now: datetime) -> list[FeedEvent]:
    """Circle events after the member's cursor, up to now, in (created_at, id) order."""
    since_at = member.feed_delivered_at or member.joined_at
    after = FeedEvent.created_at > since_at
    if member.feed_delivered_at is not None and member.feed_cursor_id is not None:
        after = or_(after, and_(FeedEvent.created_at == since_at, FeedEvent.id > member.feed_cursor_id))
    return list(session.scalars(
        select(FeedEvent).where(FeedEvent.circle_id == circle_id, FeedEvent.type.in_(EVENT_TYPES), after,
                                FeedEvent.created_at <= now)
        .order_by(FeedEvent.created_at, FeedEvent.id)
    ))


@dataclass(frozen=True)
class Digest:
    reply: Reply
    scores: list[outbox.ScoreLine]  # applied at delivery, only if the member still shares scores
    cursor: tuple[datetime, UUID | None]  # the member's new feed cursor


def digest(session: Session, circle: Circle, recipient: Learner, member: CircleMember, statuses: list[MemberStatus],
           today: date, now: datetime) -> Digest | None:
    """The recipient's circle update: the other members' status lines, then events since their cursor.

    The update stays under FEED_MAX_CHARS, counting every score as shown. Events that do not fit are
    left after the new cursor for the next update, and a closing line says how many are waiting.
    """
    language = recipient.language
    lines, scores, longest = [t("feed.header", language)], [], 0
    for status in statuses:
        if status.learner_id == recipient.id:
            continue
        lines.append(status_line(dataclasses.replace(status, share_scores=False), today, language))
        if status.share_scores and status.answered:
            shared = status_line(status, today, language)
            scores.append(outbox.ScoreLine(0, len(lines) - 1, status.learner_id, shared))
            longest += len(shared) - len(lines[-1])
    size = len("\n".join(lines)) + longest
    events, chunks, waiting = [], [], 0
    cursor = (now, None)  # everything up to now relayed, unless something has to wait
    handled = (member.feed_delivered_at or member.joined_at, member.feed_cursor_id if member.feed_delivered_at else None)
    for event in _events_after(session, circle.id, member, now):
        shown = _event_line(session, event, recipient)
        if waiting:
            waiting += shown is not None
            continue
        if shown is not None and size + len(shown[0]) + 2 + MORE_RESERVE > FEED_MAX_CHARS:
            waiting, cursor = 1, handled  # this event and everything after it wait for the next update
            continue
        handled = (event.created_at, event.id)
        if shown is None:
            continue
        size += len(shown[0]) + (1 if events else 2)
        events.append(shown[0])
        if shown[1] is not None:
            chunks.append(shown[1])
    if len(lines) == 1 and not events:
        return None
    if waiting:
        events.append(t("feed.more", language, count=waiting))
    if len(chunks) == 1:
        buttons = [Button(t("qa.show_source", language), f"{SHOW_SOURCE}{chunks[0].id}")]
    else:
        buttons = [Button(t("qa.show_source_n", language, number=number), f"{SHOW_SOURCE}{chunk.id}")
                   for number, chunk in enumerate(chunks, start=1)]
    text = "\n".join([*lines, *([""] + events if events else [])])
    return Digest(Reply(text, buttons), scores, cursor)


def queue_digests(session: Session, clock: Clock, circle: Circle, now: datetime) -> int:
    """Queue today's update for every real member who has not had one today; returns how many were queued."""
    today = virtual_day(now)
    statuses = member_statuses(session, circle.id, todays_lesson(session, circle.id, today))
    rows = session.execute(select(Learner, CircleMember).join(CircleMember, CircleMember.learner_id == Learner.id)
                           .where(CircleMember.circle_id == circle.id, Learner.is_simulated.is_(False))
                           .order_by(CircleMember.joined_at, Learner.id)).all()
    queued = 0
    for learner, member in rows:
        update = digest(session, circle, learner, member, statuses, today, now)
        if update is None:
            continue
        key = f"feed:{circle.id}:{learner.id}:{today.isoformat()}"
        if outbox.enqueue(session, learner, [update.reply], key=key, now=clock.now(session), scores=update.scores):
            member.feed_delivered_at, member.feed_cursor_id = update.cursor
            queued += 1
    session.flush()
    return queued

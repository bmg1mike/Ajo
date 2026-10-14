"""Inactivity detection and peer nudges (spec section 7.6).

A member is quiet once two whole virtual days have passed without a check-in. Someone who last
checked in on a Tuesday is detected in Friday's daily run, the third day after that check-in
(spec section 16), because Wednesday and Thursday ended without one. The daily run happens at the
morning lesson time, before real learners act, so the day in progress never counts. A member who
has never checked in counts from the day they joined.

`detect` runs once per circle and virtual day, inside `DemoEngine.run_day`. On the first detection
of a quiet spell it records one `missed` feed event whose ID is derived from the member and the day
of their last check-in, so a spell is detected once however long it lasts, and a new check-in
starts a new one. That day's circle update then carries a prompt with a "Send <name> a nudge"
button for every other real member. The quiet member gets no prompt about themselves, and Ajo
never reminds anyone on its own: the only message a quiet member receives because they are quiet
is a nudge that another member chose to send.

A tap sends the nudge straight away through the outbox, outside the daily batch. The nudge is
attributed to the member who tapped and has buttons to get today's lesson. The
`one_nudge_per_recipient_day` constraint allows one nudge per quiet member and virtual day,
whoever taps. The insert uses ON CONFLICT DO NOTHING after locking the quiet member's circle row,
so concurrent taps are settled one after another and only the first is queued. The queued nudge
expires at the end of its virtual day, so the outbox delivers it on that day or not at all. A
nudge held up by failures, a missing adapter or a /simulate_day therefore never arrives next to
the following day's, and at most one nudge reaches a member per virtual day.
"""
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID, uuid4, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.base import Button
from app.core import circles, outbox
from app.core.clock import virtual_day
from app.core.i18n import t
from app.core.lessons import TODAY
from app.core.replies import Reply
from app.db import dialect_insert
from app.models import CircleMember, FeedEvent, FeedType, Learner, Nudge

QUIET_DAYS = 2  # whole virtual days without a check-in before a member counts as quiet

# Button payloads, well under Telegram's 64-byte callback limit.
PREFIX = "nd:"
SEND = "nd:s:"  # followed by the `missed` event that prompted it
LATER = "nd:later"

_NAMESPACE = UUID("9e2d4b17-5f0c-4a83-b6e1-0c7a3d58f241")


@dataclass(frozen=True)
class QuietMember:
    """A member newly detected as quiet, and the `missed` event their nudge button points at."""
    event_id: UUID
    learner_id: UUID
    first_name: str


def quiet_since(member: CircleMember) -> date:
    """The day the member last checked in, or joined if they never have."""
    return virtual_day(member.last_checkin_at or member.joined_at)


def is_quiet(member: CircleMember, today: date) -> bool:
    return (today - quiet_since(member)).days > QUIET_DAYS


def detect(session: Session, circle_id: UUID, now: datetime) -> list[QuietMember]:
    """Record each member whose quiet spell starts being detected today; returns them in joining order.

    Personas are detected like anyone else; the prompt reaches real members only, through the feed.
    """
    today = virtual_day(now)
    rows = session.execute(select(Learner, CircleMember).join(CircleMember, CircleMember.learner_id == Learner.id)
                           .where(CircleMember.circle_id == circle_id)
                           .order_by(CircleMember.joined_at, Learner.first_name, Learner.id)).all()
    detected = []
    for learner, member in rows:
        if not is_quiet(member, today):
            continue
        since = quiet_since(member)
        event_id = uuid5(_NAMESPACE, f"missed\x00{circle_id}\x00{learner.id}\x00{since.isoformat()}")
        # RETURNING, not rowcount: psycopg reports -1 for a single-row ON CONFLICT insert.
        inserted = session.scalar(dialect_insert(session, FeedEvent).values(
            id=event_id, circle_id=circle_id, type=FeedType.MISSED.value, actor_learner_id=learner.id,
            payload={"since": since.isoformat()}, created_at=now,
        ).on_conflict_do_nothing(index_elements=["id"]).returning(FeedEvent.id))
        if inserted is not None:
            detected.append(QuietMember(event_id, learner.id, learner.first_name))
    return detected


def prompts(recipient: Learner, quiet: Sequence[QuietMember]) -> tuple[list[str], list[Button]]:
    """The nudge prompt lines and buttons for one recipient's circle update, leaving out their own spell."""
    shown = [member for member in quiet if member.learner_id != recipient.id]
    language = recipient.language
    return ([t("nudge.prompt", language, name=member.first_name) for member in shown],
            [Button(t("nudge.button", language, name=member.first_name), f"{SEND}{member.event_id}")
             for member in shown])


def message(sender: Learner, recipient: Learner) -> Reply:
    """The nudge as the quiet member reads it: from a named person, with a lesson-request button."""
    language = recipient.language
    return Reply(t("nudge.message", language, name=sender.first_name),
                 [Button(t("nudge.yes", language), TODAY), Button(t("nudge.later", language), LATER)])


def send(session: Session, sender: Learner, event_id: UUID, now: datetime) -> Reply:
    """A "Send <name> a nudge" tap: nudge the quiet member unless someone already did today."""
    language = sender.language
    event = session.get(FeedEvent, event_id)
    if (event is None or event.type != FeedType.MISSED or event.actor_learner_id in (None, sender.id)
            or not circles.is_member(session, event.circle_id, sender.id)):
        return Reply(t("button.expired", language))
    recipient = session.get(Learner, event.actor_learner_id)
    # Locked so taps for the same member, and their own check-ins, are settled one at a time.
    member = session.get(CircleMember, (event.circle_id, recipient.id), with_for_update=True)
    if member is None:
        return Reply(t("button.expired", language))
    today = virtual_day(now)
    if not is_quiet(member, today):
        return Reply(t("nudge.not_quiet", language, name=recipient.first_name))
    nudge_id = session.scalar(dialect_insert(session, Nudge).values(
        id=uuid4(), circle_id=event.circle_id, from_learner_id=sender.id, to_learner_id=recipient.id,
        virtual_day=today, created_at=now,
    ).on_conflict_do_nothing(index_elements=["to_learner_id", "virtual_day"]).returning(Nudge.id))
    if nudge_id is None:
        return Reply(t("nudge.already_sent", language, name=recipient.first_name))
    # Not part of the daily batch: the outbox sends it on its next pass, but only within today, so a
    # late nudge can never reach the member beside tomorrow's. Personas are never queued; the
    # simulation reads the nudge row instead.
    outbox.enqueue(session, recipient, [message(sender, recipient)], key=f"nudge:{nudge_id}", now=now,
                   expires_at=end_of_day(today))
    return Reply(t("nudge.sent", language, name=recipient.first_name))


def end_of_day(day: date) -> datetime:
    """The first virtual moment after `day`: a nudge sent on `day` is delivered on `day` or never."""
    return datetime.combine(day + timedelta(days=1), time(), tzinfo=UTC)


def later(learner: Learner) -> Reply:
    return Reply(t("nudge.later_reply", learner.language))

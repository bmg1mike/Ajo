"""Queued outbound messages: the daily lesson post, the batched circle feed and the weekly payout
(spec sections 7.2, 7.5, 7.7).

A reply to a learner's own message goes back through the adapter that received it. Messages the
learner did not ask for are queued here instead, in the same transaction as the work that produced
them, and a background job (`app/scheduler.py`) sends them through the adapter registry. Each row
holds one batch of messages for one learner, sent in order. A message may carry a file (the weekly
study pack), stored base64-encoded and sent after the message's text.

Every row has a deduplication key, so a batch is queued at most once: the feed uses one key per
learner and virtual day, which is what limits ordinary circle updates to one message a day.
Simulated learners have no chat ID and are never queued. A row is retried until it is sent or has
failed MAX_ATTEMPTS times; a failure part-way through a batch resends the whole batch, and holds
back that learner's later batches until the next pass so they never arrive out of order.

Rows are re-read just before each message is sent, and again before a message's file, so a learner
deleted meanwhile receives nothing more, and score lines are rendered from the owner's current
/share_scores choice at that moment.

A row may carry an `expires_at` virtual time. From then on it is never sent, whether it was
waiting for an adapter, retrying after failures or simply not reached yet; it stays unsent and
does not hold back the learner's other batches. Nudges expire at the end of the virtual day they
were sent, so a late one is dropped rather than delivered beside the next day's.
"""
import asyncio
import base64
import logging
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.base import AdapterRegistry, Button
from app.core.clock import Clock
from app.core.replies import Attachment, Reply
from app.db import dialect_insert
from app.models import Learner, OutboundDelivery

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
BATCH_SIZE = 50


@dataclass(frozen=True)
class ScoreLine:
    """A feed line that may show a member's score, decided again when the message is sent.

    The queued text holds the private line; `shared` replaces it only if the member still shares
    scores at that moment, so an opt-out applies to updates already queued. Only members who had
    opted in when the update was queued get one, so no unshared score is ever stored for others.
    """
    message: int
    line: int
    learner_id: UUID
    shared: str


def enqueue(session: Session, learner: Learner, replies: list[Reply], *, key: str, now: datetime,
            scores: list[ScoreLine] = (), expires_at: datetime | None = None) -> bool:
    """Queue `replies` for one real learner; False if the learner can't receive them or `key` was used.

    `now` orders deliveries, so callers pass a fresh clock reading for each batch. A batch with
    `expires_at` is never sent once the virtual clock reaches it.
    """
    if learner.is_simulated or not learner.channel_chat_id or not replies:
        return False
    payload = {"messages": [_message(reply) for reply in replies],
               "scores": [{"message": score.message, "line": score.line, "learner_id": str(score.learner_id),
                           "shared": score.shared} for score in scores]}
    # RETURNING, not rowcount: psycopg reports -1 for a single-row ON CONFLICT insert.
    inserted = session.scalar(
        dialect_insert(session, OutboundDelivery).values(
            id=uuid4(), learner_id=learner.id, channel=learner.channel.value, channel_chat_id=learner.channel_chat_id,
            payload=payload, deduplication_key=key, created_at=now, attempts=0, expires_at=expires_at,
        ).on_conflict_do_nothing(index_elements=["deduplication_key"]).returning(OutboundDelivery.id)
    )
    return inserted is not None


def _message(reply: Reply) -> dict:
    if reply.voice is not None:  # lesson audio is sent only on request, as a reply
        raise ValueError("Voice notes are not queued")
    message = {"text": reply.text,
               "buttons": [{"text": button.text, "payload": button.payload} for button in reply.buttons]}
    if reply.file is not None:
        message["file"] = {"filename": reply.file.filename, "data": base64.b64encode(reply.file.data).decode("ascii")}
    return message


def render(session: Session, delivery: OutboundDelivery, index: int) -> Reply:
    """Message `index` of a batch as it should be sent now: shared scores only for members still sharing them."""
    message = delivery.payload["messages"][index]
    lines = message["text"].split("\n")
    for score in delivery.payload.get("scores", []):
        if score["message"] != index:
            continue
        owner = session.get(Learner, UUID(score["learner_id"]))
        if owner is not None and owner.share_scores:
            lines[score["line"]] = score["shared"]
    file = message.get("file")
    return Reply("\n".join(lines), [Button(button["text"], button["payload"]) for button in message["buttons"]],
                 Attachment(file["filename"], base64.b64decode(file["data"])) if file else None)


def replies_of(session: Session, delivery: OutboundDelivery) -> list[Reply]:
    return [render(session, delivery, index) for index in range(len(delivery.payload["messages"]))]


@dataclass(frozen=True)
class _Pending:
    id: UUID
    learner_id: UUID
    messages: int


def _sendable(now: datetime):
    return (OutboundDelivery.delivered_at.is_(None), OutboundDelivery.failed_at.is_(None),
            or_(OutboundDelivery.expires_at.is_(None), OutboundDelivery.expires_at > now))


def pending(session: Session, channels: frozenset, now: datetime, limit: int = BATCH_SIZE) -> list[OutboundDelivery]:
    """Unsent, unexpired rows for channels that have an adapter, oldest first."""
    if not channels:
        return []
    return list(session.scalars(
        select(OutboundDelivery)
        .where(*_sendable(now), OutboundDelivery.channel.in_([channel.value for channel in channels]))
        .order_by(OutboundDelivery.created_at, OutboundDelivery.id).limit(limit)
    ))


def _load(sessions: sessionmaker[Session], clock: Clock, channels: frozenset, limit: int) -> list[_Pending]:
    with sessions() as session:
        return [_Pending(row.id, row.learner_id, len(row.payload["messages"]))
                for row in pending(session, channels, clock.now(session), limit)]


def _prepare(sessions: sessionmaker[Session], clock: Clock, delivery_id: UUID,
             index: int) -> tuple[Learner, Reply] | None:
    """Re-read the row and its learner just before sending one message; None if either is gone, the row
    is settled, or it has expired.

    Deleting a learner cascades to their rows, so a deletion committed while earlier messages were
    being sent cancels everything not yet handed to the adapter.
    """
    with sessions() as session:
        row = session.execute(select(OutboundDelivery, Learner).join(Learner, Learner.id == OutboundDelivery.learner_id)
                              .where(OutboundDelivery.id == delivery_id,
                                     *_sendable(clock.now(session)))).first()
        if row is None:
            return None
        delivery, learner = row
        return learner, render(session, delivery, index)


def _mark_delivered(sessions: sessionmaker[Session], clock: Clock, delivery_id: UUID) -> None:
    with sessions.begin() as session:
        session.execute(update(OutboundDelivery).where(OutboundDelivery.id == delivery_id)
                        .values(delivered_at=clock.now(session)))


def _record_failure(sessions: sessionmaker[Session], clock: Clock, delivery_id: UUID) -> None:
    with sessions.begin() as session:
        delivery = session.get(OutboundDelivery, delivery_id, with_for_update=True)
        if delivery is None:  # the learner was deleted meanwhile
            return
        delivery.attempts += 1
        if delivery.attempts >= MAX_ATTEMPTS:
            delivery.failed_at = clock.now(session)


async def deliver_pending(sessions: sessionmaker[Session], registry: AdapterRegistry, clock: Clock, *,
                          limit: int = BATCH_SIZE) -> int:
    """Send queued batches through the registry; returns how many batches were sent.

    Only channels with a registered adapter are read, so rows for a channel that is switched off
    wait without blocking the others. Database work runs in threads, off the event loop.
    """
    sent, held = 0, set()
    for item in await asyncio.to_thread(_load, sessions, clock, registry.channels, limit):
        if item.learner_id in held:
            continue  # an earlier batch for this learner failed or was cancelled: keep their messages in order
        try:
            for index in range(item.messages):
                prepared = await asyncio.to_thread(_prepare, sessions, clock, item.id, index)
                if prepared is None:
                    break  # the learner was deleted, the row settled elsewhere, or it expired
                learner, reply = prepared
                if reply.text or reply.buttons:
                    await registry.send_text(learner, reply.text, reply.buttons)
                    if reply.file is not None:
                        # A deletion may have committed while the text was sent: re-read before the private file.
                        prepared = await asyncio.to_thread(_prepare, sessions, clock, item.id, index)
                        if prepared is None:
                            break
                        learner = prepared[0]
                if reply.file is not None:
                    await registry.send_file(learner, reply.file.filename, reply.file.data)
            else:
                await asyncio.to_thread(_mark_delivered, sessions, clock, item.id)
                sent += 1
                continue
            held.add(item.learner_id)
        except Exception as error:  # noqa: BLE001 - one learner's failure must not stop the others
            # The error may carry a chat ID or message text; log its type only.
            logger.warning("Outbound delivery failed (%s); it will be retried", type(error).__name__)
            await asyncio.to_thread(_record_failure, sessions, clock, item.id)
            held.add(item.learner_id)
    return sent

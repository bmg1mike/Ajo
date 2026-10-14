"""Queued outbound messages: the daily lesson post and the batched circle feed (spec sections 7.2, 7.5).

A reply to a learner's own message goes back through the adapter that received it. Messages the
learner did not ask for are queued here instead, in the same transaction as the work that produced
them, and a background job (`app/scheduler.py`) sends them through the adapter registry. Each row
holds one batch of messages for one learner, sent in order.

Every row has a deduplication key, so a batch is queued at most once: the feed uses one key per
learner and virtual day, which is what limits ordinary circle updates to one message a day.
Simulated learners have no chat ID and are never queued. A row is retried until it is sent or has
failed MAX_ATTEMPTS times; a failure part-way through a batch resends the whole batch, and holds
back that learner's later batches until the next pass so they never arrive out of order.

Rows are re-read just before each message is sent, so a learner deleted meanwhile receives nothing
more, and score lines are rendered from the owner's current /share_scores choice at that moment.
"""
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.base import AdapterRegistry, Button
from app.core.clock import Clock
from app.core.replies import Reply
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
            scores: list[ScoreLine] = ()) -> bool:
    """Queue `replies` for one real learner; False if the learner can't receive them or `key` was used.

    `now` orders deliveries, so callers pass a fresh clock reading for each batch.
    """
    if learner.is_simulated or not learner.channel_chat_id or not replies:
        return False
    payload = {"messages": [{"text": reply.text,
                             "buttons": [{"text": button.text, "payload": button.payload} for button in reply.buttons]}
                            for reply in replies],
               "scores": [{"message": score.message, "line": score.line, "learner_id": str(score.learner_id),
                           "shared": score.shared} for score in scores]}
    # RETURNING, not rowcount: psycopg reports -1 for a single-row ON CONFLICT insert.
    inserted = session.scalar(
        dialect_insert(session, OutboundDelivery).values(
            id=uuid4(), learner_id=learner.id, channel=learner.channel.value, channel_chat_id=learner.channel_chat_id,
            payload=payload, deduplication_key=key, created_at=now, attempts=0,
        ).on_conflict_do_nothing(index_elements=["deduplication_key"]).returning(OutboundDelivery.id)
    )
    return inserted is not None


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
    return Reply("\n".join(lines), [Button(button["text"], button["payload"]) for button in message["buttons"]])


def replies_of(session: Session, delivery: OutboundDelivery) -> list[Reply]:
    return [render(session, delivery, index) for index in range(len(delivery.payload["messages"]))]


@dataclass(frozen=True)
class _Pending:
    id: UUID
    learner_id: UUID
    messages: int


def pending(session: Session, channels: frozenset, limit: int = BATCH_SIZE) -> list[OutboundDelivery]:
    """Unsent rows for channels that have an adapter, oldest first."""
    if not channels:
        return []
    return list(session.scalars(
        select(OutboundDelivery)
        .where(OutboundDelivery.delivered_at.is_(None), OutboundDelivery.failed_at.is_(None),
               OutboundDelivery.channel.in_([channel.value for channel in channels]))
        .order_by(OutboundDelivery.created_at, OutboundDelivery.id).limit(limit)
    ))


def _load(sessions: sessionmaker[Session], channels: frozenset, limit: int) -> list[_Pending]:
    with sessions() as session:
        return [_Pending(row.id, row.learner_id, len(row.payload["messages"]))
                for row in pending(session, channels, limit)]


def _prepare(sessions: sessionmaker[Session], delivery_id: UUID, index: int) -> tuple[Learner, Reply] | None:
    """Re-read the row and its learner just before sending one message; None if either is gone or settled.

    Deleting a learner cascades to their rows, so a deletion committed while earlier messages were
    being sent cancels everything not yet handed to the adapter.
    """
    with sessions() as session:
        row = session.execute(select(OutboundDelivery, Learner).join(Learner, Learner.id == OutboundDelivery.learner_id)
                              .where(OutboundDelivery.id == delivery_id, OutboundDelivery.delivered_at.is_(None),
                                     OutboundDelivery.failed_at.is_(None))).first()
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
    for item in await asyncio.to_thread(_load, sessions, registry.channels, limit):
        if item.learner_id in held:
            continue  # an earlier batch for this learner failed or was cancelled: keep their messages in order
        try:
            for index in range(item.messages):
                prepared = await asyncio.to_thread(_prepare, sessions, item.id, index)
                if prepared is None:
                    break  # the learner was deleted, or the row settled elsewhere
                learner, reply = prepared
                await registry.send_text(learner, reply.text, reply.buttons)
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

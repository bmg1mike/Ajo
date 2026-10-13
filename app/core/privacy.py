import hashlib
import json
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Channel, FeedEvent, Learner, LLMCache, OutboundDelivery, QALog


def channel_hash(channel: Channel, user_id: str, salt: str) -> str:
    if len(salt.strip()) < 32:
        raise ValueError("A server salt of at least 32 characters is required")
    if not user_id:
        raise ValueError("A channel user ID is required")
    # Length-safe JSON separates namespaces and prevents ambiguous concatenation.
    value = json.dumps([salt, Channel(channel).value, user_id], separators=(",", ":"))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _contains_identifier(value: object, identifiers: set[str]) -> bool:
    if isinstance(value, dict):
        return any(_contains_identifier(key, identifiers) or _contains_identifier(item, identifiers)
                   for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains_identifier(item, identifiers) for item in value)
    if isinstance(value, str):
        return any(identifier in value for identifier in identifiers)
    return False


def delete_learner(session: Session, learner_id: UUID, *, confirmed: bool = False) -> bool:
    """Delete within the caller's transaction after explicit confirmation.

    Adapters must pass the authenticated learner's ID, never an arbitrary payload
    ID. Cascades remove membership, answers, nudges, QA and outbound identifiers.
    Also remove JSON payloads referencing the learner indirectly. Cache entries
    must contain anonymous content, but are inspected defensively here.
    """
    if confirmed is not True:
        raise ValueError("Learner deletion requires explicit confirmation")
    learner = session.get(Learner, learner_id)
    if learner is None:
        return False
    identifiers = {str(learner.id), learner.id.hex, learner.channel_user_hash, learner.first_name}
    if learner.channel_chat_id:
        identifiers.add(learner.channel_chat_id)
    identifiers.discard("")
    fields_by_model = (
        (FeedEvent, ("payload",)),
        (OutboundDelivery, ("payload", "channel_chat_id", "external_message_id", "deduplication_key")),
        (QALog, ("question_text", "fallback_reason")),
        (LLMCache, ("response",)),
    )
    for model, fields in fields_by_model:
        for row in session.scalars(select(model)):
            if any(_contains_identifier(getattr(row, field), identifiers) for field in fields):
                session.delete(row)
    session.delete(learner)
    session.flush()
    return True

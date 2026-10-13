import hashlib
import json
import re
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


def learner_identifiers(learner: Learner) -> set[str]:
    identifiers = {str(learner.id), learner.id.hex, learner.channel_user_hash, learner.first_name,
                   learner.channel_chat_id or ""}
    identifiers.discard("")
    return identifiers


REDACTED = "[redacted]"
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# A run of digits joined by any whitespace, dashes, slashes, dots or brackets. Any run holding
# 7 or more digits is redacted, with no upper bound: several numbers joined by separators
# ("08000000001. 08000000002") form one longer run, and must not escape as "too long to be a
# phone". No word-boundary checks either, so "Call08031234567" is caught. Grouped numbers like
# 1,000,000 keep their commas and survive.
_PHONE_LIKE = re.compile(r"[+(]*\d(?:[\d\s()./\-\u2010-\u2015]*\d)?")
MIN_PHONE_DIGITS = 7


def redact_contact_details(text: str) -> str:
    """Remove phone numbers, numeric channel IDs and email addresses from free text.

    Phone numbers and channel user IDs are stored only as salted hashes, so they cannot be
    matched verbatim; anything shaped like one is redacted instead.
    """
    text = _EMAIL.sub(REDACTED, text)
    return _PHONE_LIKE.sub(lambda match: REDACTED if sum(c.isdigit() for c in match.group()) >= MIN_PHONE_DIGITS
                           else match.group(), text)


def redact_learner(text: str, learner: Learner) -> str:
    """Remove the learner's own identifiers and any contact details before text leaves for an LLM."""
    for identifier in sorted(learner_identifiers(learner), key=len, reverse=True):
        if len(identifier) >= 2:
            text = re.sub(rf"(?<!\w){re.escape(identifier)}(?!\w)", REDACTED, text, flags=re.IGNORECASE)
    return redact_contact_details(text)


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
    identifiers = learner_identifiers(learner)
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

"""Question answering with citation enforcement (spec section 8).

An answer is accepted only if the model's JSON matches the schema, it is answerable,
it cites at least one chunk, every citation is in the retrieved set, and a second
claim-support check says yes. Anything else becomes the library fallback. Every
outcome is written to qa_log.
"""
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.base import Button
from app.core.clock import Clock
from app.core.embeddings import Embedder
from app.core.i18n import t
from app.core.privacy import redact_contact_details, redact_learner
from app.core.replies import Reply
from app.core.retrieval import TOP_K, ChunkRef, RetrievedChunk, retrieve
from app.llm import prompts
from app.llm.client import LLMClient, LLMError, LLMUnavailable
from app.models import Chunk, CircleMember, FeedEvent, FeedType, Language, Learner, QALog, Source, SyllabusTopic

MAX_QUESTION_CHARS = 1000
EXCERPT_CHARS = 600

# Button payloads stay well under Telegram's 64-byte callback limit.
SHOW_SOURCE = "qa:src:"
ASK_CIRCLE = "qa:ask:"
DISMISS = "qa:no"

# Labels the model leaked into the answer text: bracketed anywhere, or a bare run after the last sentence.
# Bare labels mid-sentence are left alone ("C4 plants" is Biology).
_LEAKED_LABELS = re.compile(r"\s*[\[(]\s*C\d+(?:\s*[,;]\s*C\d+)*\s*[\])]|(?<=[.!?])(?:\s+C\d+)+\s*$")

# The claim-check verdict must be exactly one word, give or take punctuation and markdown.
# A qualified reply ("Yes, but the claim about gold is not") is malformed, never a yes.
_VERDICT = re.compile(r"[\W_]*(yes|no)[\W_]*", re.IGNORECASE)

_NAMESPACE = UUID("0c8c3f8e-5d0a-4f43-9a53-3f6f3c1f2b7d")


class FallbackReason(StrEnum):
    EMPTY_QUESTION = "empty_question"
    QUESTION_TOO_LONG = "question_too_long"
    NO_GOAL = "no_goal"
    NO_CHUNKS = "no_chunks"
    BELOW_THRESHOLD = "below_threshold"
    LLM_UNAVAILABLE = "llm_unavailable"
    LLM_ERROR = "llm_error"
    INVALID_JSON = "invalid_json"
    SCHEMA_MISMATCH = "schema_mismatch"
    NOT_ANSWERABLE = "not_answerable"
    ANSWER_TOO_LONG = "answer_too_long"
    NO_CITATIONS = "no_citations"
    CITATION_OUTSIDE_RETRIEVED = "citation_outside_retrieved"
    CLAIM_CHECK_ERROR = "claim_check_error"
    UNSUPPORTED_CLAIMS = "unsupported_claims"


@dataclass(frozen=True)
class QAResult:
    qa_log_id: UUID
    reply: Reply
    retrieved: list[RetrievedChunk]
    cited: list[RetrievedChunk] = field(default_factory=list)
    answer: str | None = None
    fallback_reason: FallbackReason | None = None

    @property
    def answered(self) -> bool:
        return self.fallback_reason is None


class _Rejected(Exception):
    def __init__(self, reason: FallbackReason) -> None:
        self.reason = reason


class _AnswerSchema(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")
    answerable: bool
    answer: str
    cited_chunk_ids: list[str]


class QAService:
    def __init__(self, embedder: Embedder, llm: LLMClient, clock: Clock, *, min_score: float,
                 top_k: int = TOP_K) -> None:
        self.embedder, self.llm, self.clock = embedder, llm, clock
        self.min_score, self.top_k = min_score, top_k

    def ask(self, session: Session, learner: Learner, question: str) -> QAResult:
        """Answer within the caller's transaction and log the outcome. LLM calls block."""
        question = question.strip()
        log_id = uuid4()
        retrieved: list[RetrievedChunk] = []
        try:
            if not question:
                raise _Rejected(FallbackReason.EMPTY_QUESTION)
            if len(question) > MAX_QUESTION_CHARS:
                raise _Rejected(FallbackReason.QUESTION_TOO_LONG)
            if learner.goal_id is None:
                raise _Rejected(FallbackReason.NO_GOAL)
            retrieved = retrieve(session, self.embedder, learner.goal_id, question, limit=self.top_k)
            if not retrieved:
                raise _Rejected(FallbackReason.NO_CHUNKS)
            if retrieved[0].score < self.min_score:
                raise _Rejected(FallbackReason.BELOW_THRESHOLD)
            answer, cited = self._generate(learner, question, retrieved)
        except _Rejected as rejected:
            result = QAResult(log_id, _fallback_reply(learner, log_id), retrieved, fallback_reason=rejected.reason)
        else:
            result = QAResult(log_id, _answer_reply(learner, answer, cited), retrieved, cited, answer)
        session.add(QALog(
            id=log_id, learner_id=learner.id, question_text=redact_contact_details(question),
            retrieved_chunk_ids=[chunk.id for chunk in retrieved], answered=result.answered,
            fallback_reason=result.fallback_reason, top_score=retrieved[0].score if retrieved else None,
            cited_chunk_ids=[chunk.id for chunk in result.cited] if result.answered else None,
            created_at=self.clock.now(session),
        ))
        session.flush()
        return result

    def _generate(self, learner: Learner, question: str,
                  retrieved: list[RetrievedChunk]) -> tuple[str, list[RetrievedChunk]]:
        # The model sees the learner's language and level only, never who they are.
        messages = prompts.qa_messages(redact_learner(question, learner), retrieved,
                                       language=Language(learner.language).value, level=learner.difficulty)
        output = _parse_answer(self._complete(messages, json_output=True))
        if not output.answerable:
            raise _Rejected(FallbackReason.NOT_ANSWERABLE)
        answer = strip_leaked_labels(output.answer)
        if not answer:
            raise _Rejected(FallbackReason.SCHEMA_MISMATCH)
        if len(answer.split()) > prompts.QA_MAX_WORDS:
            raise _Rejected(FallbackReason.ANSWER_TOO_LONG)
        cited = _resolve_citations(output.cited_chunk_ids, retrieved)

        verdict = parse_verdict(self._complete(prompts.claim_check_messages(answer, cited), json_output=False))
        if verdict is None:
            raise _Rejected(FallbackReason.CLAIM_CHECK_ERROR)
        if not verdict:
            raise _Rejected(FallbackReason.UNSUPPORTED_CLAIMS)
        return answer, cited

    def _complete(self, messages: list[prompts.Message], *, json_output: bool) -> str:
        try:
            return self.llm.complete(messages, json_output=json_output)
        except LLMUnavailable:
            raise _Rejected(FallbackReason.LLM_UNAVAILABLE) from None
        except LLMError:
            raise _Rejected(FallbackReason.LLM_ERROR) from None


def parse_json_object(content: str) -> dict | None:
    """The JSON object in a model reply, tolerating code fences and surrounding prose; None if there is none."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        try:
            data = json.loads(text[start:end + 1]) if 0 <= start < end else None
        except json.JSONDecodeError:
            data = None
    return data if isinstance(data, dict) else None


def strip_leaked_labels(text: str) -> str:
    return _LEAKED_LABELS.sub("", text).strip()


def parse_verdict(content: str) -> bool | None:
    """True for a bare "yes", False for a bare "no", None for anything else (a qualified reply is malformed)."""
    verdict = _VERDICT.fullmatch(content)
    return None if verdict is None else verdict.group(1).lower() == "yes"


def resolve_labels[T: ChunkRef](cited_ids: list[str], passages: list[T]) -> list[T] | None:
    """Map the model's labels (C1..Cn, or full chunk IDs) onto the passages it was shown, without duplicates.

    None if any label is not one of them; an empty list if nothing was cited.
    """
    by_key: dict[str, T] = {}
    for index, chunk in enumerate(passages):
        by_key[prompts.label(index).lower()] = chunk
        by_key[str(chunk.id)] = chunk
    resolved: list[T] = []
    for raw in cited_ids:
        chunk = by_key.get(raw.strip().strip("[]").lower())
        if chunk is None:
            return None
        if chunk not in resolved:
            resolved.append(chunk)
    return resolved


def _parse_answer(content: str) -> _AnswerSchema:
    data = parse_json_object(content)
    if data is None:
        raise _Rejected(FallbackReason.INVALID_JSON)
    try:
        return _AnswerSchema.model_validate(data)
    except ValidationError:
        raise _Rejected(FallbackReason.SCHEMA_MISMATCH) from None


def _resolve_citations(cited_ids: list[str], retrieved: list[RetrievedChunk]) -> list[RetrievedChunk]:
    resolved = resolve_labels(cited_ids, retrieved)
    if resolved is None:
        raise _Rejected(FallbackReason.CITATION_OUTSIDE_RETRIEVED)
    if not resolved:
        raise _Rejected(FallbackReason.NO_CITATIONS)
    return resolved


def source_footer(language: Language | str, cited: list[ChunkRef]) -> tuple[str, list[Button]]:
    """One "Source:" line per distinct citation, and a Show source button for each."""
    distinct: dict[str, ChunkRef] = {}
    for chunk in cited:
        distinct.setdefault(chunk.citation, chunk)
    lines = "\n".join(t("qa.source_line", language, citation=citation) for citation in distinct)
    if len(distinct) == 1:
        buttons = [Button(t("qa.show_source", language), f"{SHOW_SOURCE}{next(iter(distinct.values())).id}")]
    else:
        buttons = [Button(t("qa.show_source_n", language, number=number), f"{SHOW_SOURCE}{chunk.id}")
                   for number, chunk in enumerate(distinct.values(), start=1)]
    return lines, buttons


def _answer_reply(learner: Learner, answer: str, cited: list[RetrievedChunk]) -> Reply:
    lines, buttons = source_footer(learner.language, cited)
    return Reply(f"{answer}\n\n{lines}", buttons)


def _fallback_reply(learner: Learner, log_id: UUID) -> Reply:
    return Reply(t("qa.fallback", learner.language), [
        Button(t("qa.ask_circle", learner.language), f"{ASK_CIRCLE}{log_id}"),
        Button(t("qa.no_thanks", learner.language), DISMISS),
    ])


def excerpt(text: str, limit: int = EXCERPT_CHARS) -> str:
    """Truncate at a word boundary so the result, ellipsis included, is at most `limit` characters."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    if " " in cut:
        cut = cut[:cut.rfind(" ")]
    return cut.rstrip(" ,;:") + "…"


def show_source(session: Session, learner: Learner, chunk_id: UUID) -> Reply:
    """The original, untranslated chunk text behind a citation, truncated to 600 characters."""
    row = session.execute(
        select(Chunk, Source.title).join(Source, Source.id == Chunk.source_id)
        .join(SyllabusTopic, SyllabusTopic.id == Chunk.syllabus_topic_id)
        .where(Chunk.id == chunk_id, SyllabusTopic.goal_id == learner.goal_id)
    ).first()
    if row is None:
        return Reply(t("qa.source_unavailable", learner.language))
    chunk, title = row
    return Reply(f"{title}, {chunk.section}, p. {chunk.page}\n\n{excerpt(chunk.text)}")


def ask_circle(session: Session, learner: Learner, qa_log_id: UUID, now: datetime) -> Reply:
    """Post the learner's own unanswered question to their circle feed, at most once.

    Delivery happens with the batched circle feed. The event ID is derived from the
    QA log ID, so a repeated or concurrent tap cannot post twice.
    """
    log = session.get(QALog, qa_log_id)
    if log is None or log.learner_id != learner.id or log.answered or not log.question_text:
        return Reply(t("qa.not_postable", learner.language))
    circle_id = session.scalar(select(CircleMember.circle_id).where(CircleMember.learner_id == learner.id)
                               .order_by(CircleMember.joined_at).limit(1))
    if circle_id is None:
        return Reply(t("qa.not_in_circle", learner.language))
    event_id = uuid5(_NAMESPACE, f"circle-question\x00{qa_log_id}")
    if session.get(FeedEvent, event_id) is not None:
        return Reply(t("qa.already_posted", learner.language))
    session.add(FeedEvent(id=event_id, circle_id=circle_id, type=FeedType.QUESTION, actor_learner_id=learner.id,
                          payload={"qa_log_id": str(log.id), "question": log.question_text}, created_at=now))
    session.flush()
    return Reply(t("qa.posted_to_circle", learner.language))


def dismiss(learner: Learner) -> Reply:
    return Reply(t("qa.dismissed", learner.language))

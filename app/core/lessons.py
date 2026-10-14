"""Daily lessons (spec sections 7.2, 11 and 14): generation, validation, translation, delivery, pre-generation.

A lesson is generated in English from the six chunks of the day's syllabus topic that best match
the topic, then accepted only if its JSON matches the schema, the lesson has at most 150 words,
both questions (and their easier follow-ups) have four options A to D with one marked answer,
every item cites chunks it was shown, and each item passes a claim check against only the chunks
it cites. A rejected lesson is retried once with the reason; after that the topic's validated
pre-generated lesson is used.

Translations keep the English citations and source excerpts. A translation must keep the lesson's
shape, must not return the lesson or example in English, and passes a fidelity check that sees
the target language and every English string beside its translation. If it fails, the learner
gets the English lesson with a note. Lessons carry no learner details, so no prompt here can
identify anyone.

Pre-generate and validate lessons for every topic before a demo with:

    uv run python -m app.core.lessons --pregenerate
"""
import argparse
import json
import logging
import sys
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.base import Button
from app.core.clock import Clock, virtual_day
from app.core.embeddings import Embedder
from app.core.i18n import t
from app.core.qa import parse_json_object, parse_verdict, resolve_labels, source_footer, strip_leaked_labels
from app.core.replies import Reply
from app.core.retrieval import ChunkRef, load_chunks, retrieve
from app.llm import prompts
from app.llm.client import LLMClient, LLMError, LLMUnavailable
from app.models import Circle, CircleMember, Language, Learner, Lesson, PregeneratedLesson, Question, SyllabusTopic

logger = logging.getLogger(__name__)

LESSON_CHUNKS = 6
QUESTIONS_PER_LESSON = 2
LESSON_DIFFICULTY = 2  # everyone starts at level 2 (spec section 7.1)
FOLLOW_UP_DIFFICULTY = LESSON_DIFFICULTY - 1
# A translation may run longer than its English, but not without bound.
TRANSLATION_MAX_RATIO = 3
# How much of the translation reviewer's notes goes back to the translator on the retry.
MAX_REVIEW_NOTES = 8
MAX_REVIEW_NOTE_CHARS = 200

# Button payloads, well under Telegram's 64-byte callback limit.
PREFIX = "ls:"
START_QUESTIONS = "ls:go:"
ANSWER = "ls:a:"
TODAY = "ls:today"  # today's lesson on request, as /lesson does; offered with a peer nudge
LISTEN = "ls:listen:"  # lesson audio (app/core/voice.py), handled outside the conversation's transaction

_NAMESPACE = UUID("8d7c5e0b-31a4-4f6e-9b0d-2a6c1e4f7a93")


class Rejection(StrEnum):
    NO_CHUNKS = "no_chunks"
    LLM_UNAVAILABLE = "llm_unavailable"
    LLM_ERROR = "llm_error"
    INVALID_JSON = "invalid_json"
    SCHEMA_MISMATCH = "schema_mismatch"
    TOO_LONG = "too_long"
    INVALID_OPTIONS = "invalid_options"
    NO_CITATIONS = "no_citations"
    CITATION_OUTSIDE_RETRIEVED = "citation_outside_retrieved"
    CLAIM_CHECK_ERROR = "claim_check_error"
    UNSUPPORTED_CLAIMS = "unsupported_claims"
    STRUCTURE_MISMATCH = "structure_mismatch"  # a translation changed the shape of the lesson
    UNTRANSLATED = "untranslated"  # a translation returned the English unchanged
    UNFAITHFUL_TRANSLATION = "unfaithful_translation"  # the translation check said no


class Rejected(Exception):
    """Generated content failed validation. `problem` is fixed text fed back to the model on the retry."""

    def __init__(self, reason: Rejection, problem: str = "") -> None:
        super().__init__(reason.value)
        self.reason, self.problem = reason, problem or reason.value.replace("_", " ")


class LessonUnavailable(Exception):
    """Neither a valid generated lesson nor a pre-generated one exists for the day's topic."""


# Content. English drafts hold resolved chunk IDs; translations hold text only.


class QuestionText(BaseModel):
    stem: str
    options: dict[str, str]
    explanation: str


class TranslatedQuestion(QuestionText):
    follow_up: QuestionText | None = None


class LessonText(BaseModel):
    """What a translation changes: every string a learner reads, in the same shape."""
    lesson: str
    example: str
    questions: list[TranslatedQuestion]


class QuestionDraft(QuestionText):
    correct_option: str
    cited_chunk_ids: list[UUID]
    follow_up: "QuestionDraft | None" = None


class LessonDraft(BaseModel):
    lesson: str
    example: str
    cited_chunk_ids: list[UUID]
    questions: list[QuestionDraft]
    translations: dict[Language, LessonText] = {}

    def text(self) -> LessonText:
        return LessonText(lesson=self.lesson, example=self.example, questions=[
            TranslatedQuestion(stem=q.stem, options=q.options, explanation=q.explanation,
                               follow_up=q.follow_up and QuestionText(stem=q.follow_up.stem, options=q.follow_up.options,
                                                                      explanation=q.follow_up.explanation))
            for q in self.questions])

    def chunk_ids(self) -> list[UUID]:
        """Every chunk cited anywhere in the lesson, in first-cited order."""
        ids = list(self.cited_chunk_ids)
        for question in self.questions:
            ids += question.cited_chunk_ids
            if question.follow_up is not None:
                ids += question.follow_up.cited_chunk_ids
        return list(dict.fromkeys(ids))

    def parts(self) -> list[tuple[str, str, list[UUID]]]:
        """(name, claim-check text, the chunks it cites) for the lesson with its example and every question.

        Each part is checked against its own citations only, so a question cannot borrow support
        from a chunk that only another part cites.
        """
        parts = [("the lesson", prompts.lesson_part_material(self.lesson, self.example), self.cited_chunk_ids)]
        for number, question in enumerate(self.questions, start=1):
            parts.append((f"question {number}", prompts.question_material(question, question.correct_option),
                          question.cited_chunk_ids))
            if question.follow_up is not None:
                parts.append((f"the follow-up to question {number}",
                              prompts.question_material(question.follow_up, question.follow_up.correct_option),
                              question.follow_up.cited_chunk_ids))
        return parts


def translation_pairs(english: LessonText, translated: LessonText) -> list[tuple[str, str, str]]:
    """(part, English, translation) for every string a learner reads, for the fidelity check."""
    pairs = [("Lesson", english.lesson, translated.lesson), ("Worked example", english.example, translated.example)]

    def question(name: str, source: QuestionText, shown: QuestionText) -> None:
        pairs.append((name, source.stem, shown.stem))
        pairs.extend((f"{name}, option {key}", source.options[key], shown.options[key]) for key in source.options)
        pairs.append((f"{name}, explanation", source.explanation, shown.explanation))

    for number, (source, shown) in enumerate(zip(english.questions, translated.questions, strict=True), start=1):
        question(f"Question {number}", source, shown)
        if source.follow_up is not None and shown.follow_up is not None:
            question(f"Follow-up to question {number}", source.follow_up, shown.follow_up)
    return pairs


def _same_text(first: str, second: str) -> bool:
    return " ".join(first.split()).casefold() == " ".join(second.split()).casefold()


class _Strict(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")


class _RawQuestion(_Strict):
    stem: str
    options: dict[str, str]
    correct_option: str
    explanation: str
    cited_chunk_ids: list[str]


class _RawMainQuestion(_RawQuestion):
    follow_up: _RawQuestion


class _RawLesson(_Strict):
    lesson: str
    example: str
    cited_chunk_ids: list[str]
    questions: list[_RawMainQuestion]


class _TranslationReview(_Strict):
    faithful: bool
    problems: list[str] = []


# Validation


def _text(value: str, name: str, max_words: int | None = None) -> str:
    text = strip_leaked_labels(value)
    if not text:
        raise Rejected(Rejection.SCHEMA_MISMATCH, f"{name} is empty")
    words = len(text.split())
    if max_words is not None and words > max_words:
        raise Rejected(Rejection.TOO_LONG, f"{name} has {words} words; the limit is {max_words}")
    return text


def _options(options: dict[str, str], name: str, max_words: int | None = prompts.OPTION_MAX_WORDS) -> dict[str, str]:
    normalized = {key.strip().upper(): value for key, value in options.items()}
    if len(normalized) != len(options) or sorted(normalized) != list(prompts.OPTION_KEYS):
        raise Rejected(Rejection.INVALID_OPTIONS, f"{name} must have exactly the options A, B, C and D")
    cleaned = {key: _text(normalized[key], f"option {key} of {name}", max_words) for key in prompts.OPTION_KEYS}
    if len({value.casefold() for value in cleaned.values()}) != len(cleaned):
        raise Rejected(Rejection.INVALID_OPTIONS, f"{name} has two identical options")
    return cleaned


def _citations[T: ChunkRef](labels: list[str], passages: list[T], name: str) -> list[T]:
    resolved = resolve_labels(labels, passages)
    if resolved is None:
        raise Rejected(Rejection.CITATION_OUTSIDE_RETRIEVED,
                       f"{name} cites a label that is not one of C1 to C{len(passages)}")
    if not resolved:
        raise Rejected(Rejection.NO_CITATIONS, f"{name} cites no chunks")
    return resolved


def _question(raw: _RawQuestion, passages: list[ChunkRef], name: str) -> QuestionDraft:
    options = _options(raw.options, name)
    correct = raw.correct_option.strip().strip(".)").upper()
    if correct not in options:
        raise Rejected(Rejection.INVALID_OPTIONS, f"the correct_option of {name} is not one of A, B, C and D")
    return QuestionDraft(stem=_text(raw.stem, f"the stem of {name}", prompts.STEM_MAX_WORDS), options=options,
                         correct_option=correct,
                         explanation=_text(raw.explanation, f"the explanation of {name}",
                                           prompts.EXPLANATION_MAX_WORDS),
                         cited_chunk_ids=[chunk.id for chunk in _citations(raw.cited_chunk_ids, passages, name)])


def validate_lesson(data: dict, passages: list[ChunkRef]) -> LessonDraft:
    """Structural and citation checks on a parsed model reply; the claim check comes after."""
    try:
        raw = _RawLesson.model_validate(data)
    except ValidationError:
        raise Rejected(Rejection.SCHEMA_MISMATCH, "the JSON does not match the required form") from None
    if len(raw.questions) != QUESTIONS_PER_LESSON:
        raise Rejected(Rejection.SCHEMA_MISMATCH,
                       f"it has {len(raw.questions)} questions; exactly {QUESTIONS_PER_LESSON} are required")
    questions = []
    for number, raw_question in enumerate(raw.questions, start=1):
        question = _question(raw_question, passages, f"question {number}")
        follow_up = _question(raw_question.follow_up, passages, f"the follow-up to question {number}")
        if follow_up.stem.casefold() == question.stem.casefold():
            raise Rejected(Rejection.SCHEMA_MISMATCH, f"the follow-up to question {number} repeats the question")
        questions.append(question.model_copy(update={"follow_up": follow_up}))
    if questions[0].stem.casefold() == questions[1].stem.casefold():
        raise Rejected(Rejection.SCHEMA_MISMATCH, "the two questions are the same")
    return LessonDraft(lesson=_text(raw.lesson, "the lesson", prompts.LESSON_MAX_WORDS),
                       example=_text(raw.example, "the example", prompts.EXAMPLE_MAX_WORDS),
                       cited_chunk_ids=[chunk.id for chunk in _citations(raw.cited_chunk_ids, passages, "the lesson")],
                       questions=questions)


def _capped(translated: str, english: str, name: str) -> str:
    text = _text(translated, name)
    if len(text) > TRANSLATION_MAX_RATIO * len(english) + 40:
        raise Rejected(Rejection.TOO_LONG, f"the translation of {name} is far longer than the English")
    return text


def flatten(text: LessonText) -> dict[str, str]:
    """Every string a learner reads, keyed by part name: what the translator receives and must return.

    A flat object of named strings, rather than the nested lesson, so that each value is plainly
    text to translate; in the nested form models left the questions in English.
    """
    return {name: value for name, value, _ in translation_pairs(text, text)}


def _unflatten(english: LessonText, values: dict[str, str]) -> LessonText:
    def question(name: str, source: QuestionText) -> QuestionText:
        return QuestionText(stem=values[name], explanation=values[f"{name}, explanation"],
                            options={key: values[f"{name}, option {key}"] for key in source.options})

    questions = []
    for number, source in enumerate(english.questions, start=1):
        follow_up = source.follow_up and question(f"Follow-up to question {number}", source.follow_up)
        questions.append(TranslatedQuestion(**question(f"Question {number}", source).model_dump(),
                                            follow_up=follow_up))
    return LessonText(lesson=values["Lesson"], example=values["Worked example"], questions=questions)


def validate_translation(data: dict, english: LessonText) -> LessonText:
    """The translation must return exactly the English part names, each translated and of sane length."""
    expected = flatten(english)
    missing, extra = [key for key in expected if key not in data], [key for key in data if key not in expected]
    if missing or extra:
        raise Rejected(Rejection.STRUCTURE_MISMATCH,
                       f"return exactly the keys given; missing: {', '.join(missing) or 'none'}; "
                       f"unexpected: {', '.join(extra) or 'none'}")
    if any(not isinstance(value, str) for value in data.values()):
        raise Rejected(Rejection.SCHEMA_MISMATCH, "every value must be a string")
    values = {key: _capped(data[key], source, key) for key, source in expected.items()}
    # Cheap first line against untranslated text; the fidelity check judges the rest. Options are
    # exempt: one may rightly be just a Biology term, identical in both languages.
    unchanged = [key for key, source in expected.items() if ", option " not in key and _same_text(values[key], source)]
    if unchanged:
        raise Rejected(Rejection.UNTRANSLATED, f"these parts were left in English: {', '.join(unchanged)}")
    text = _unflatten(english, values)
    for number, question in enumerate(text.questions, start=1):
        _options(question.options, f"question {number}", max_words=None)  # translated options stay distinct
        if question.follow_up is not None:
            _options(question.follow_up.options, f"the follow-up to question {number}", max_words=None)
    return text


# Storage


def _localized_body(text: LessonText) -> dict:
    return {"lesson": text.lesson, "example": text.example}


def _localized_question(question: QuestionText) -> dict:
    return {"stem": question.stem, "options": question.options, "explanation": question.explanation}


def lesson_questions(session: Session, lesson_id: UUID, *, refresh: bool = False) -> list[Question]:
    """Each main question followed by its follow-up, by position."""
    query = (select(Question).where(Question.lesson_id == lesson_id)
             .order_by(Question.position, Question.parent_question_id.is_not(None)))
    return list(session.scalars(query.execution_options(populate_existing=refresh)))


def draft_from_rows(lesson: Lesson, questions: Sequence[Question]) -> LessonDraft:
    follow_ups = {question.parent_question_id: question for question in questions if question.parent_question_id}

    def draft(question: Question) -> QuestionDraft:
        return QuestionDraft(stem=question.stem, options=question.options, correct_option=question.correct_option,
                             explanation=question.explanation, cited_chunk_ids=list(question.cited_chunk_ids))

    mains = [question for question in questions if question.parent_question_id is None]
    return LessonDraft(lesson=lesson.body["en"]["lesson"], example=lesson.body["en"]["example"],
                       cited_chunk_ids=list(lesson.cited_chunk_ids),
                       questions=[draft(main).model_copy(update={"follow_up": follow_ups.get(main.id) and
                                                                 draft(follow_ups[main.id])})
                                  for main in mains])


def _apply_translations(lesson: Lesson, questions: Sequence[Question],
                        translations: dict[Language, LessonText]) -> None:
    """Store translations; JSON columns are reassigned, not mutated in place, so the change is tracked."""
    mains = [question for question in questions if question.parent_question_id is None]
    follow_ups = {question.parent_question_id: question for question in questions if question.parent_question_id}
    for language, text in translations.items():
        lesson.body = {**lesson.body, language.value: _localized_body(text)}
        for main, translated in zip(mains, text.questions, strict=True):
            pairs = [(main, translated)]
            if translated.follow_up is not None and main.id in follow_ups:
                pairs.append((follow_ups[main.id], translated.follow_up))
            for row, shown in pairs:
                row.translations = {**(row.translations or {}), language.value: _localized_question(shown)}


def _store(session: Session, circle: Circle, day_index: int, topic: SyllabusTopic, draft: LessonDraft,
           now: datetime, fallback_reason: str | None) -> Lesson:
    # Derived from the circle and day: a racing second insert fails on the key instead of posting twice.
    lesson = Lesson(id=uuid5(_NAMESPACE, f"lesson\x00{circle.id}\x00{day_index}"), circle_id=circle.id,
                    day_index=day_index, topic_id=topic.id, body={"en": _localized_body(draft.text())},
                    cited_chunk_ids=draft.cited_chunk_ids, posted_at=now, fallback_reason=fallback_reason)
    session.add(lesson)
    session.flush()
    questions = []
    for position, item in enumerate(draft.questions, start=1):
        main = Question(id=uuid4(), lesson_id=lesson.id, stem=item.stem, options=item.options,
                        correct_option=item.correct_option, explanation=item.explanation,
                        cited_chunk_ids=item.cited_chunk_ids, difficulty=LESSON_DIFFICULTY, position=position,
                        translations={})
        questions.append(main)
        if item.follow_up is not None:
            follow_up = item.follow_up
            questions.append(Question(id=uuid4(), lesson_id=lesson.id, parent_question_id=main.id,
                                      stem=follow_up.stem, options=follow_up.options,
                                      correct_option=follow_up.correct_option, explanation=follow_up.explanation,
                                      cited_chunk_ids=follow_up.cited_chunk_ids, difficulty=FOLLOW_UP_DIFFICULTY,
                                      position=position, translations={}))
    session.add_all(questions)
    _apply_translations(lesson, questions, draft.translations)
    session.flush()
    return lesson


def topic_for_day(session: Session, goal_id: UUID, day_index: int) -> SyllabusTopic | None:
    """Topics in syllabus order; after the last one the sequence starts again as revision."""
    topics = session.scalars(select(SyllabusTopic).where(SyllabusTopic.goal_id == goal_id)
                             .order_by(SyllabusTopic.order_index)).all()
    return topics[(day_index - 1) % len(topics)] if topics else None


def topic_query(topic: SyllabusTopic) -> str:
    return f"{topic.title}. {topic.objectives}"


def learner_circle(session: Session, learner: Learner) -> Circle | None:
    return session.scalar(select(Circle).join(CircleMember, CircleMember.circle_id == Circle.id)
                          .where(CircleMember.learner_id == learner.id).order_by(CircleMember.joined_at).limit(1))


@dataclass(frozen=True)
class PregenerationResult:
    topic: SyllabusTopic
    draft: LessonDraft | None
    rejection: Rejected | None = None
    translation_failures: dict[Language, Rejection] | None = None


class LessonService:
    def __init__(self, embedder: Embedder, llm: LLMClient, clock: Clock, *, audio: bool = False) -> None:
        self.embedder, self.llm, self.clock = embedder, llm, clock
        self.audio = audio  # offer the Listen button: TTS is on

    # Generation and translation. LLM calls block; callers make them before writing anything,
    # so the client's cache writes never wait on the caller's transaction.

    def generate(self, session: Session, topic: SyllabusTopic) -> LessonDraft:
        """A validated English lesson for the topic, or Rejected after one retry."""
        passages = retrieve(session, self.embedder, topic.goal_id, topic_query(topic), limit=LESSON_CHUNKS,
                            topic_id=topic.id)
        if not passages:
            raise Rejected(Rejection.NO_CHUNKS)

        def accept(content: str) -> LessonDraft:
            draft = validate_lesson(_json(content), passages)
            by_id = {chunk.id: chunk for chunk in passages}
            for name, material, chunk_ids in draft.parts():
                self._check(name, material, [by_id[chunk_id] for chunk_id in chunk_ids])
            return draft

        return self._with_retry(prompts.lesson_messages(topic.title, topic.objectives, passages), accept)

    def translate(self, draft: LessonDraft, language: Language) -> LessonText:
        """A validated translation of the whole lesson, or Rejected after one retry.

        The English was claim-checked part by part, so a faithful translation is supported too. The
        check therefore compares every translated string with its English, in the target language.
        """
        english = draft.text()

        def accept(content: str) -> LessonText:
            text = validate_translation(_json(content), english)
            messages = prompts.translation_check_messages(language.value, translation_pairs(english, text))
            content = self._complete(messages)  # a provider failure stays an LLM error and is retried as is
            try:
                review = _TranslationReview.model_validate(_json(content))
            except (Rejected, ValidationError):
                raise Rejected(Rejection.CLAIM_CHECK_ERROR, "a reviewer could not confirm the translation") from None
            if not review.faithful:
                # The reviewer's notes name parts of this lesson only, so they are safe to send back.
                notes = "; ".join(note.strip()[:MAX_REVIEW_NOTE_CHARS] for note in review.problems[:MAX_REVIEW_NOTES])
                raise Rejected(Rejection.UNFAITHFUL_TRANSLATION,
                               f"a reviewer found parts that are not in {prompts.LANGUAGE_NAMES[language.value]} or "
                               f"that change the meaning of the English ({notes or 'no details given'}); "
                               "translate every part faithfully")
            return text

        material = json.dumps(flatten(english), ensure_ascii=False, indent=1)
        return self._with_retry(prompts.translation_messages(language.value, material), accept)

    def _translations(self, draft: LessonDraft, languages: Collection[Language],
                      ) -> tuple[dict[Language, LessonText], dict[Language, Rejection]]:
        done, failed = {}, {}
        for language in sorted(set(languages) - {Language.EN} - set(draft.translations)):
            try:
                done[language] = self.translate(draft, language)
            except Rejected as rejected:
                logger.warning("Lesson translation to %s rejected: %s", language.value, rejected.reason.value)
                failed[language] = rejected.reason
        return done, failed

    def _with_retry[T](self, messages: list[prompts.Message], accept: Callable[[str], T]) -> T:
        content = None
        try:
            content = self._complete(messages)
            return accept(content)
        except Rejected as rejected:
            # A provider failure is retried as is: failures are never cached. A rejected reply is
            # retried with the reason, which also makes the request differ from the cached one.
            if content is None or rejected.reason in (Rejection.LLM_ERROR, Rejection.LLM_UNAVAILABLE):
                retry = messages
            else:
                retry = prompts.retry_messages(messages, content, rejected.problem)
            logger.info("Retrying rejected lesson content: %s", rejected.reason.value)
        return accept(self._complete(retry))

    def _check(self, name: str, material: str, chunks: list[ChunkRef]) -> None:
        """Claim check of one part against only the chunks it cites."""
        verdict = parse_verdict(self._complete(prompts.lesson_check_messages(material, chunks), json_output=False))
        if verdict is None:
            raise Rejected(Rejection.CLAIM_CHECK_ERROR, f"a reviewer could not confirm {name} against its chunks")
        if not verdict:
            raise Rejected(Rejection.UNSUPPORTED_CLAIMS,
                           f"a reviewer found that the chunks {name} cites do not support it; every item must cite "
                           "the chunks its facts come from, and exactly one option of each question may be correct")

    def _complete(self, messages: list[prompts.Message], *, json_output: bool = True) -> str:
        try:
            return self.llm.complete(messages, json_output=json_output)
        except LLMUnavailable:
            raise Rejected(Rejection.LLM_UNAVAILABLE) from None
        except LLMError:
            raise Rejected(Rejection.LLM_ERROR) from None

    # Lessons for circles

    def draft_for(self, session: Session, topic: SyllabusTopic) -> tuple[LessonDraft, str | None]:
        """A generated lesson, or the pre-generated one with the reason generation failed."""
        try:
            return self.generate(session, topic), None
        except Rejected as rejected:
            reason = rejected.reason.value
        logger.warning("Lesson generation for topic %s rejected (%s); using the pre-generated lesson",
                       topic.order_index, reason)
        stored = session.get(PregeneratedLesson, topic.id)
        if stored is None:
            raise LessonUnavailable(f"generation failed ({reason}) and topic {topic.order_index} has no "
                                    "pre-generated lesson")
        return LessonDraft.model_validate(stored.content), reason

    def todays_lesson(self, session: Session, circle: Circle, now: datetime,
                      languages: Collection[Language] = ()) -> Lesson:
        """The circle's lesson for the current virtual day, creating the next one on the first request.

        Locks the circle row, so concurrent requests produce one lesson; generation and translation
        into `languages` happen before anything is written.
        """
        session.get(Circle, circle.id, with_for_update=True)
        latest = session.scalar(select(Lesson).where(Lesson.circle_id == circle.id)
                                .order_by(Lesson.day_index.desc()).limit(1))
        if latest is not None and virtual_day(latest.posted_at) == virtual_day(now):
            self.localize(session, latest, languages)
            return latest
        day_index = latest.day_index + 1 if latest is not None else 1
        topic = topic_for_day(session, circle.goal_id, day_index)
        if topic is None:
            raise LessonUnavailable("the circle's goal has no syllabus topics")
        draft, fallback_reason = self.draft_for(session, topic)
        translations, _ = self._translations(draft, languages)
        draft = draft.model_copy(update={"translations": {**draft.translations, **translations}})
        return _store(session, circle, day_index, topic, draft, now, fallback_reason)

    def localize(self, session: Session, lesson: Lesson, languages: Collection[Language]) -> None:
        """Translate an existing lesson into any of `languages` it lacks. Failures leave it in English."""
        missing = {Language(language) for language in languages} - {Language.EN}
        missing -= {Language(key) for key in lesson.body}
        if not missing:
            return
        draft = draft_from_rows(lesson, lesson_questions(session, lesson.id))
        translations, _ = self._translations(draft, missing)
        if not translations:
            return
        # Re-read under a lock so a concurrent translation into another language is kept.
        lesson = session.get(Lesson, lesson.id, with_for_update=True, populate_existing=True)
        questions = lesson_questions(session, lesson.id, refresh=True)
        _apply_translations(lesson, questions, {language: text for language, text in translations.items()
                                                if language.value not in lesson.body})
        session.flush()

    def today(self, session: Session, learner: Learner, now: datetime) -> list[Reply]:
        """/lesson: today's lesson for the learner's circle, in their language where possible."""
        circle = learner_circle(session, learner)
        if circle is None:
            return [Reply(t("lesson.not_in_circle", learner.language))]
        try:
            lesson = self.todays_lesson(session, circle, now, {learner.language})
        except LessonUnavailable as error:
            logger.warning("No lesson available: %s", error)
            return [Reply(t("lesson.unavailable", learner.language))]
        return lesson_replies(session, lesson, learner, audio=self.audio)

    def post(self, session: Session, circle: Circle, now: datetime) -> tuple[Lesson, list[tuple[Learner, list[Reply]]]]:
        """Today's lesson for a whole circle, translated into every real member's language, with each real
        member's messages. Simulated personas read nothing, so their languages cost no translation; a
        persona controlled from the web simulator counts as a real member. Delivery is up to the caller."""
        members = session.scalars(select(Learner).join(CircleMember, CircleMember.learner_id == Learner.id)
                                  .where(CircleMember.circle_id == circle.id, Learner.receives_messages)
                                  .order_by(CircleMember.joined_at, Learner.id)).all()
        lesson = self.todays_lesson(session, circle, now, {member.language for member in members})
        return lesson, [(member, lesson_replies(session, lesson, member, audio=self.audio)) for member in members]

    # Pre-generation

    def pregenerate(self, session: Session, topic: SyllabusTopic, languages: Collection[Language],
                    now: datetime) -> PregenerationResult:
        """Generate, validate and store the topic's fallback lesson; keep the old one if generation fails."""
        try:
            draft = self.generate(session, topic)
        except Rejected as rejected:
            return PregenerationResult(topic, None, rejected)
        translations, failed = self._translations(draft, languages)
        draft = draft.model_copy(update={"translations": translations})
        stored = session.get(PregeneratedLesson, topic.id) or PregeneratedLesson(topic_id=topic.id)
        stored.content, stored.cited_chunk_ids, stored.created_at = draft.model_dump(mode="json"), draft.chunk_ids(), now
        session.add(stored)
        session.flush()
        return PregenerationResult(topic, draft, translation_failures=failed)


def _json(content: str) -> dict:
    data = parse_json_object(content)
    if data is None:
        raise Rejected(Rejection.INVALID_JSON, "it is not a JSON object")
    return data


# Rendering


def localized(lesson: Lesson, language: Language) -> dict:
    return lesson.body.get(Language(language).value) or lesson.body["en"]


def lesson_replies(session: Session, lesson: Lesson, learner: Learner, *, audio: bool = False) -> list[Reply]:
    """The lesson message: header, lesson, worked example, untranslated sources and buttons (spec section 7.2).

    With `audio` (TTS is on), a Listen button comes before the answer button.
    """
    language = Language(learner.language)
    note = [] if language.value in lesson.body else [Reply(t("lesson.english_only", language))]
    body = localized(lesson, language)
    topic = session.get(SyllabusTopic, lesson.topic_id)
    sources, buttons = source_footer(language, load_chunks(session, lesson.cited_chunk_ids))
    text = (f"{t('lesson.header', language, day=lesson.day_index, topic=topic.title)}\n{body['lesson']}\n\n"
            f"{t('lesson.example', language, example=body['example'])}\n\n{sources}")
    listen = [Button(t("lesson.listen", language), f"{LISTEN}{lesson.id}")] if audio else []
    return [*note, Reply(text, [*buttons, *listen, Button(t("lesson.answer_questions", language),
                                                          f"{START_QUESTIONS}{lesson.id}")])]


# Command line


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Pre-generate and validate the fallback lesson for every topic.")
    parser.add_argument("--pregenerate", action="store_true", required=True,
                        help="generate, validate and store a lesson per topic")
    parser.add_argument("--goal", default="jamb_bio")
    parser.add_argument("--topics", type=int, nargs="+", metavar="N", help="only these topic order indexes")
    parser.add_argument("--languages", nargs="*", default=[Language.YO.value, Language.PCM.value],
                        choices=[item.value for item in Language if item != Language.EN],
                        help="also store validated translations (default: yo pcm)")
    return parser.parse_args(argv)


def format_result(result: PregenerationResult) -> str:
    head = f"{result.topic.order_index}. {result.topic.title}"
    if result.draft is None:
        return f"- {head}: FAILED ({result.rejection.reason.value}: {result.rejection.problem})"
    draft = result.draft
    line = (f"- {head}: ok (lesson {len(draft.lesson.split())} words, example {len(draft.example.split())} words, "
            f"{len(draft.chunk_ids())} chunks cited)")
    translated = ", ".join(language.value for language in draft.translations)
    failed = ", ".join(f"{language.value} ({reason.value})" for language, reason in
                       (result.translation_failures or {}).items())
    return line + (f"; translated: {translated}" if translated else "") + (f"; FAILED: {failed}" if failed else "")


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    from app.config import Settings
    from app.core.embeddings import E5Embedder
    from app.db import build_engine, session_factory
    from app.models import Goal

    settings = Settings()
    engine = build_engine(settings.database_url)
    sessions = session_factory(engine)
    clock = Clock(sessions)
    llm = LLMClient.from_settings(settings, sessions, clock)
    service = LessonService(E5Embedder(settings.embed_model), llm, clock)
    languages = [Language(value) for value in args.languages]
    complete = True
    try:
        with sessions() as session:
            goal = session.scalar(select(Goal).where(Goal.code == args.goal))
            if goal is None:
                print(f"Goal {args.goal!r} is not ingested; run python -m ingest.ingest first", file=sys.stderr)
                return 2
            topics = session.scalars(select(SyllabusTopic).where(SyllabusTopic.goal_id == goal.id)
                                     .order_by(SyllabusTopic.order_index)).all()
            topic_ids = [topic.id for topic in topics if not args.topics or topic.order_index in args.topics]
        print("# Pre-generated lessons\n")
        for topic_id in topic_ids:
            with sessions.begin() as session:  # one transaction per topic: finished topics are kept
                result = service.pregenerate(session, session.get(SyllabusTopic, topic_id), languages,
                                             clock.now(session))
                stored = session.get(PregeneratedLesson, topic_id)
            print(format_result(result), flush=True)
            if result.draft is None and stored is not None:
                print("  (the previously stored lesson is kept)")
            complete = complete and result.draft is not None and not result.translation_failures
    finally:
        llm.close()
        engine.dispose()
    print("\nPASS" if complete else "\nINCOMPLETE: rerun after fixing the failures above")
    return 0 if complete else 1


if __name__ == "__main__":
    raise SystemExit(main())

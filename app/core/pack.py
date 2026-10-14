"""The weekly study pack (spec sections 2.5 and 7.7): a plain-text file, strictly under 500 KB.

The pack holds the week's lessons and worked examples, every question with its options, the marked
answer, the learner's own answer and the explanation, and the citations, so it can be read offline.
Lessons and questions are in the learner's language where a validated translation is stored, and
in English with a note otherwise; building a pack never calls the LLM. Citations stay untranslated,
and the pack ends with each cited source's publisher, licence and link, which CC BY requires.

It is private: it shows the learner's own answers and names nobody. A pack that would reach
PACK_MAX_BYTES loses lessons from the end, with a note saying so.
"""
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import virtual_day
from app.core.grading import shown_question
from app.core.i18n import t
from app.core.lessons import lesson_questions, localized
from app.core.retrieval import load_chunks
from app.models import Answer, Chunk, Language, Learner, Lesson, Question, Source, SyllabusTopic

# "Strictly under 500 KB" whether a kilobyte is 1,000 or 1,024 bytes.
PACK_MAX_BYTES = 500_000
RULE = "─" * 32


@dataclass(frozen=True)
class Pack:
    filename: str
    data: bytes
    lessons: int  # lessons included
    trimmed: bool  # lessons were left out to stay under PACK_MAX_BYTES

    @property
    def size_kb(self) -> int:
        return max(1, -(-len(self.data) // 1000))


def _lesson_section(session: Session, lesson: Lesson, learner: Learner, answers: dict) -> tuple[str, set]:
    """One lesson as text, and the IDs of every chunk it cites."""
    language = Language(learner.language)
    topic = session.get(SyllabusTopic, lesson.topic_id)
    body = localized(lesson, language)
    lines = [t("lesson.header", language, day=lesson.day_index, topic=topic.title)]
    if language.value not in lesson.body:
        lines.append(t("lesson.english_only", language))
    lines += [body["lesson"], "", t("lesson.example", language, example=body["example"]), ""]
    cited = set(lesson.cited_chunk_ids)
    lines += _source_lines(session, language, lesson.cited_chunk_ids)
    questions = lesson_questions(session, lesson.id)
    total = sum(question.parent_question_id is None for question in questions)
    for question in questions:
        shown = shown_question(question, language)
        header = (t("question.easier", language) if question.parent_question_id is not None
                  else t("question.header", language, number=question.position, total=total))
        lines += ["", header, shown["stem"]]
        lines += [f"  {key}. {shown['options'][key]}" for key in sorted(question.options)]
        answer_text = shown["options"].get(question.correct_option, question.options[question.correct_option])
        lines.append(t("pack.answer", language, option=question.correct_option, answer=answer_text))
        own = answers.get(question.id)
        if own is not None:
            key = "pack.your_answer_correct" if own.is_correct else "pack.your_answer_wrong"
            lines.append(t(key, language, option=own.chosen_option))
        elif question.parent_question_id is None:
            lines.append(t("pack.not_answered", language))
        lines.append(t("pack.explanation", language, explanation=shown["explanation"]))
        lines += _source_lines(session, language, question.cited_chunk_ids)
        cited |= set(question.cited_chunk_ids)
    return "\n".join(lines), cited


def _source_lines(session: Session, language: Language, chunk_ids) -> list[str]:
    citations = dict.fromkeys(chunk.citation for chunk in load_chunks(session, chunk_ids))
    return [t("qa.source_line", language, citation=citation) for citation in citations]


def _licences(session: Session, chunk_ids: set) -> list[str]:
    if not chunk_ids:
        return []
    sources = session.scalars(select(Source).join(Chunk, Chunk.source_id == Source.id)
                              .where(Chunk.id.in_(chunk_ids)).distinct().order_by(Source.title, Source.id)).all()
    return [f"• {source.title}, {source.publisher}. {source.license}. {source.url}" for source in sources]


def build(session: Session, learner: Learner, lessons: Sequence[Lesson], *, goal_title: str, week: int,
          so_far: bool = False) -> Pack:
    """The learner's pack for these lessons, in order; it must have at least one lesson."""
    if not lessons:
        raise ValueError("A study pack needs at least one lesson")
    language = Language(learner.language)
    question_ids = select(Question.id).where(Question.lesson_id.in_([lesson.id for lesson in lessons]))
    answers = {answer.question_id: answer for answer in session.scalars(
        select(Answer).where(Answer.learner_id == learner.id, Answer.question_id.in_(question_ids)))}
    sections = [_lesson_section(session, lesson, learner, answers) for lesson in lessons]
    first, last = (virtual_day(lesson.posted_at).isoformat() for lesson in (lessons[0], lessons[-1]))
    header = [t("pack.title", language, goal=goal_title, week=week), t("pack.period", language, start=first, end=last)]

    def render(count: int) -> bytes:
        cited = set().union(*(chunk_ids for _, chunk_ids in sections[:count]))
        parts = ["\n".join(header)]
        parts += [f"{RULE}\n{text}" for text, _ in sections[:count]]
        if count < len(sections):
            parts.append(f"{RULE}\n{t('pack.trimmed', language)}")
        parts.append("\n".join([RULE, t("pack.sources", language), *_licences(session, cited)]))
        return ("\n\n".join(parts) + "\n").encode("utf-8")

    count = len(sections)
    data = render(count)
    while len(data) >= PACK_MAX_BYTES and count > 1:
        count -= 1
        data = render(count)
    if len(data) >= PACK_MAX_BYTES:  # a single lesson is far smaller; cut at a character boundary all the same
        data = data[:PACK_MAX_BYTES - 1].decode("utf-8", errors="ignore").encode("utf-8")
    suffix = "-so-far" if so_far else ""
    return Pack(f"ajo-week-{week}{suffix}-study-pack.txt", data, count, count < len(sections))

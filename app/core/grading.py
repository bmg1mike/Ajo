"""Answering lesson questions (spec section 7.3).

Questions arrive one at a time with option buttons. Each answer is recorded once and counts as a
check-in. A correct answer gets a short confirmation. A wrong one gets a private follow-up: the
explanation, the cited source and the easier related question that was generated and validated
with the lesson, so grading never waits on the LLM. When a learner has answered both questions the
circle gets an `answered` feed event that holds no score: the feed shows scores only for learners
who opted in, and works them out from `answers` when it renders.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.base import Button
from app.core.clock import virtual_day
from app.core.i18n import t
from app.core.lessons import ANSWER, LessonService, lesson_questions
from app.core.qa import source_footer
from app.core.replies import Reply
from app.core.retrieval import load_chunks
from app.db import dialect_insert
from app.models import Answer, CircleMember, FeedEvent, FeedType, Language, Learner, Lesson, Question

_NAMESPACE = UUID("3b9e6f1c-7a52-4d08-8e4f-5c1d2b9a0e67")


@dataclass(frozen=True)
class Graded:
    question: Question
    is_correct: bool
    completed_lesson: bool  # this answer was the last main question still open


def _member_lesson(session: Session, learner: Learner, lesson_id: UUID) -> Lesson | None:
    lesson = session.get(Lesson, lesson_id)
    if lesson is None or session.get(CircleMember, (lesson.circle_id, learner.id)) is None:
        return None
    return lesson


def _answers(session: Session, learner: Learner, questions: list[Question]) -> dict[UUID, Answer]:
    rows = session.scalars(select(Answer).where(Answer.learner_id == learner.id,
                                                Answer.question_id.in_([question.id for question in questions])))
    return {answer.question_id: answer for answer in rows}


def next_question(session: Session, learner: Learner, lesson: Lesson) -> Question | None:
    """Question 1, its follow-up if question 1 was wrong, question 2, then its follow-up if wrong."""
    questions = lesson_questions(session, lesson.id)
    answers = _answers(session, learner, questions)
    follow_ups = {question.parent_question_id: question for question in questions if question.parent_question_id}
    for question in questions:
        if question.parent_question_id is not None:
            continue
        answer = answers.get(question.id)
        if answer is None:
            return question
        follow_up = follow_ups.get(question.id)
        if not answer.is_correct and follow_up is not None and follow_up.id not in answers:
            return follow_up
    return None


def shown_question(question: Question, language: Language) -> dict:
    """The question in the learner's language when a translation is stored, else English."""
    return (question.translations or {}).get(Language(language).value) or {
        "stem": question.stem, "options": question.options, "explanation": question.explanation}


def question_reply(session: Session, learner: Learner, question: Question) -> Reply:
    language = learner.language
    shown = shown_question(question, language)
    if question.parent_question_id is not None:
        header = t("question.easier", language)
    else:
        total = len(session.scalars(select(Question.id).where(Question.lesson_id == question.lesson_id,
                                                              Question.parent_question_id.is_(None))).all())
        header = t("question.header", language, number=question.position, total=total)
    options = "\n".join(f"{key}. {shown['options'][key]}" for key in sorted(question.options))
    return Reply(f"{header}\n{shown['stem']}\n\n{options}",
                 [Button(key, f"{ANSWER}{question.id}:{key}") for key in sorted(question.options)])


def check_in(session: Session, circle_id: UUID, learner_id: UUID, now: datetime) -> None:
    """Record activity: last_checkin_at moves to now; the streak grows once per consecutive virtual day."""
    member = session.get(CircleMember, (circle_id, learner_id), with_for_update=True)
    if member is None:
        return
    today = virtual_day(now)
    last = virtual_day(member.last_checkin_at) if member.last_checkin_at is not None else None
    if last != today:
        member.streak_days = (member.streak_days or 0) + 1 if last == today - timedelta(days=1) else 1
    member.last_checkin_at = now


def record_answer(session: Session, learner: Learner, question: Question, option: str,
                  now: datetime) -> Graded | None:
    """Store one answer and its check-in; None if the learner already answered this question.

    Channel-free, so simulated personas answer through it too. The insert is ON CONFLICT DO
    NOTHING against one_answer_per_question, so a double tap is graded once.
    """
    lesson = session.get(Lesson, question.lesson_id)
    is_correct = option == question.correct_option
    # RETURNING, not rowcount: psycopg reports -1 for a single-row ON CONFLICT insert.
    inserted = session.scalar(
        dialect_insert(session, Answer).values(id=uuid4(), question_id=question.id, learner_id=learner.id,
                                               chosen_option=option, is_correct=is_correct, answered_at=now)
        .on_conflict_do_nothing(index_elements=["question_id", "learner_id"]).returning(Answer.id)
    )
    if inserted is None:
        return None
    check_in(session, lesson.circle_id, learner.id, now)
    completed = False
    if question.parent_question_id is None:
        mains = [item for item in lesson_questions(session, lesson.id) if item.parent_question_id is None]
        completed = len(_answers(session, learner, mains)) == len(mains)
        if completed:
            _record_answered(session, lesson, learner, now)
    session.flush()
    return Graded(question, is_correct, completed)


def _record_answered(session: Session, lesson: Lesson, learner: Learner, now: datetime) -> None:
    """One score-free `answered` event per learner and lesson; its ID makes a repeat a no-op."""
    event_id = uuid5(_NAMESPACE, f"answered\x00{lesson.id}\x00{learner.id}")
    session.execute(dialect_insert(session, FeedEvent).values(
        id=event_id, circle_id=lesson.circle_id, type=FeedType.ANSWERED.value, actor_learner_id=learner.id,
        payload={"lesson_id": str(lesson.id), "day_index": lesson.day_index}, created_at=now,
    ).on_conflict_do_nothing(index_elements=["id"]))


def _result_reply(session: Session, learner: Learner, graded: Graded) -> Reply:
    language = learner.language
    if graded.is_correct:
        return Reply(t("question.correct", language))
    question = graded.question
    shown = shown_question(question, language)
    answer = shown["options"].get(question.correct_option, question.options[question.correct_option])
    sources, buttons = source_footer(language, load_chunks(session, question.cited_chunk_ids))
    text = (f"{t('question.wrong', language, option=question.correct_option, answer=answer)}\n"
            f"{shown['explanation']}\n\n{sources}")
    return Reply(text, buttons)


def start(session: Session, lessons: LessonService, learner: Learner, lesson_id: UUID) -> list[Reply]:
    """The "Answer questions" button: the next open question of the lesson."""
    lesson = _member_lesson(session, learner, lesson_id)
    if lesson is None:
        return [Reply(t("button.expired", learner.language))]
    lessons.localize(session, lesson, {learner.language})
    question = next_question(session, learner, lesson)
    if question is None:
        return [Reply(t("question.all_answered", learner.language))]
    return [question_reply(session, learner, question)]


def answer(session: Session, lessons: LessonService, learner: Learner, question_id: UUID, option: str,
           now: datetime) -> list[Reply]:
    """An option button: grade it privately, then send the next question or the closing note."""
    language = learner.language
    question = session.get(Question, question_id)
    lesson = _member_lesson(session, learner, question.lesson_id) if question is not None else None
    if lesson is None or option not in question.options:
        return [Reply(t("button.expired", language))]
    if question.parent_question_id is not None:
        # A follow-up is offered only after a wrong answer to the question it follows.
        parent = session.scalar(select(Answer).where(Answer.question_id == question.parent_question_id,
                                                     Answer.learner_id == learner.id))
        if parent is None or parent.is_correct:
            return [Reply(t("button.expired", language))]
    # Any translation (an LLM call) happens before the answer is written.
    lessons.localize(session, lesson, {language})
    graded = record_answer(session, learner, question, option, now)
    replies = [Reply(t("question.already_answered", language))] if graded is None \
        else [_result_reply(session, learner, graded)]
    following = next_question(session, learner, lesson)
    if following is not None:
        replies.append(question_reply(session, learner, following))
    elif graded is not None:
        replies.append(Reply(t("question.done", language)))
    return replies

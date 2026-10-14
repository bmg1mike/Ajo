"""The simulated circle members of spec section 9.2: Chidi, Halima and Tunde.

Personas are learners with `is_simulated` set and no chat ID, so nothing is ever sent to them. They
answer through the same grading code as real learners. Every choice a persona makes is drawn from a
random generator seeded with the circle's seed, the persona and the item being decided (simulated
day and question, or the circle question's text), never from shared state. A seeded run therefore repeats exactly,
whatever order the personas act in and whatever real learners do in between.

A web simulator pane can take control of a persona (`take_control`, milestone 9). Until it lets go,
the persona does nothing on its own: whoever drives the pane answers, asks and nudges for it, and
it is sent what a real member is sent, through the pane. It stays simulated and still has no chat ID.
"""
import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from uuid import UUID, uuid5

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core import circles, grading
from app.core.embeddings import Embedder
from app.core.privacy import channel_hash
from app.core.retrieval import retrieve
from app.db import dialect_insert
from app.models import (
    Channel, Circle, CircleMember, FeedEvent, FeedType, Goal, Language, Learner, Lesson, Nudge, Question,
)

_NAMESPACE = UUID("c4a1f0d2-6b3e-4e8a-9f57-1d2c3b4a5e60")


@dataclass(frozen=True)
class Persona:
    key: str
    first_name: str
    language: Language
    accuracy: float  # chance of answering a question correctly
    active_days: int | None = None  # checks in on lesson days 1..active_days only; None: every day
    reply_rate: float = 0.0  # chance of replying to a circle question with a canned message and a citation


CHIDI = Persona("chidi", "Chidi", Language.EN, accuracy=0.9, reply_rate=0.5)
HALIMA = Persona("halima", "Halima", Language.PCM, accuracy=0.5)
# The spec gives Tunde no accuracy; he is a middling student whose problem is attendance.
TUNDE = Persona("tunde", "Tunde", Language.YO, accuracy=0.7, active_days=2)
PERSONAS = (CHIDI, HALIMA, TUNDE)


def rng(seed: int, *parts: object) -> random.Random:
    """A generator for one decision. String seeds are hashed with SHA-512, so this is stable across processes."""
    return random.Random("\x00".join(str(part) for part in (seed, *parts)))


def persona_user_id(goal: Goal, persona: Persona) -> str:
    """The persona's pseudo channel user ID: personas have no platform account, and the namespace keeps
    their hashes unique per goal. The web simulator speaks for a controlled persona with it."""
    return f"persona:{goal.code}:{persona.key}"


def persona_hash(goal: Goal, persona: Persona, salt: str) -> str:
    return channel_hash(Channel.WEB, persona_user_id(goal, persona), salt)


def user_id_of(session: Session, learner: Learner, salt: str) -> str | None:
    """The pseudo user ID of a persona learner; None for anyone else."""
    goal = session.get(Goal, learner.goal_id) if learner.is_simulated and learner.goal_id is not None else None
    if goal is None:
        return None
    return next((persona_user_id(goal, persona) for persona in PERSONAS
                 if persona_hash(goal, persona, salt) == learner.channel_user_hash), None)


def take_control(session: Session, learner: Learner, now: datetime) -> None:
    """A web simulator pane speaks for the persona from now on, and its automated behavior pauses.

    Its feed cursor moves to now, as for a member who has just joined, so the pane's first circle
    update starts from today rather than replaying everything the persona never read.
    """
    if not learner.is_simulated:
        raise ValueError("Only personas can be controlled")
    if learner.controlled_at is not None:
        return
    learner.controlled_at = now
    session.execute(update(CircleMember).where(CircleMember.learner_id == learner.id)
                    .values(feed_delivered_at=now, feed_cursor_id=None))
    session.flush()


def release_control(learner: Learner) -> None:
    """Hand the persona back to the simulation: it acts on its own again from the next simulated day."""
    learner.controlled_at = None


def release_all(session: Session) -> int:
    """Hand every controlled persona back, for a server without the web simulator; returns how many."""
    return session.execute(update(Learner).where(Learner.controlled_at.is_not(None))
                           .values(controlled_at=None)).rowcount


def seed_personas(session: Session, circle: Circle, goal: Goal, salt: str, now: datetime) -> list[Learner]:
    """Create any missing personas and add them to the circle. Repeating it changes nothing."""
    learners = []
    for persona in PERSONAS:
        user_hash = persona_hash(goal, persona, salt)
        learner = session.scalar(select(Learner).where(Learner.channel_user_hash == user_hash))
        if learner is None:
            learner = Learner(channel=Channel.WEB, channel_user_hash=user_hash, channel_chat_id=None,
                              first_name=persona.first_name, language=persona.language, goal_id=goal.id,
                              is_simulated=True, share_scores=False, difficulty=2, consented_at=None, created_at=now)
            session.add(learner)
            session.flush()
        circles.join(session, circle, learner, now)
        learners.append(learner)
    return learners


def circle_personas(session: Session, circle: Circle, goal: Goal, salt: str) -> list[tuple[Persona, Learner]]:
    """The circle's personas with their learner rows, in the order of PERSONAS, controlled or not."""
    by_hash = {persona_hash(goal, persona, salt): persona for persona in PERSONAS}
    learners = session.scalars(select(Learner).where(Learner.channel_user_hash.in_(by_hash),
                                                     Learner.is_simulated.is_(True))).all()
    found = {by_hash[learner.channel_user_hash]: learner for learner in learners
             if circles.is_member(session, circle.id, learner.id)}
    return [(persona, found[persona]) for persona in PERSONAS if persona in found]


def checks_in(session: Session, persona: Persona, learner: Learner, day: int, today: date) -> bool:
    """Chidi and Halima every day; Tunde on days 1 and 2, and on the day after someone nudges him.

    `day` counts the circle's processed days, not lessons: a learner's /lesson can create a lesson
    the personas never saw, and that must not shift their schedule.
    """
    if persona.active_days is None or day <= persona.active_days:
        return True
    yesterday = today - timedelta(days=1)
    return session.scalar(select(Nudge.id).where(Nudge.to_learner_id == learner.id,
                                                 Nudge.virtual_day == yesterday).limit(1)) is not None


def choose_option(seed: int, persona: Persona, day: int, question: Question) -> str:
    label = f"q{question.position}{'f' if question.parent_question_id else ''}"
    draw = rng(seed, persona.key, "answer", day, label)
    if draw.random() < persona.accuracy:
        return question.correct_option
    return draw.choice(sorted(key for key in question.options if key != question.correct_option))


def answer_lesson(session: Session, seed: int, persona: Persona, learner: Learner, lesson: Lesson, day: int,
                  now: datetime) -> int:
    """Answer every question the persona is offered, follow-ups included, as a learner would; returns the count."""
    answered = 0
    while (question := grading.next_question(session, learner, lesson)) is not None:
        option = choose_option(seed, persona, day, question)
        if grading.record_answer(session, learner, question, option, now) is None:
            break  # already answered: nothing left to do
        answered += 1
    return answered


def reply_to_questions(session: Session, embedder: Embedder, seed: int, persona: Persona, learner: Learner,
                       circle: Circle, min_score: float, now: datetime) -> int:
    """Occasionally reply to circle questions with a canned message and the library's best match.

    No LLM is involved: the reply is a fixed UI string plus a citation, and it is posted only when
    retrieval clears the QA threshold, so an off-topic question never gets a Biology source.
    Returns the number of replies posted.
    """
    if persona.reply_rate <= 0:
        return 0
    questions = session.scalars(select(FeedEvent).where(FeedEvent.circle_id == circle.id,
                                                        FeedEvent.type == FeedType.QUESTION,
                                                        FeedEvent.actor_learner_id != learner.id,
                                                        FeedEvent.created_at <= now)
                                .order_by(FeedEvent.created_at, FeedEvent.id)).all()
    posted = 0
    for question in questions:
        reply_id = uuid5(_NAMESPACE, f"reply\x00{question.id}\x00{persona.key}")
        if session.get(FeedEvent, reply_id) is not None:
            continue
        # Keyed by the question's text, not its random ID, so a rehearsed question gets the same decision.
        text = " ".join(question.payload["question"].lower().split())
        if rng(seed, persona.key, "reply", text).random() >= persona.reply_rate:
            continue
        best = retrieve(session, embedder, circle.goal_id, question.payload["question"], limit=1)
        if not best or best[0].score < min_score:
            continue
        inserted = session.scalar(dialect_insert(session, FeedEvent).values(
            id=reply_id, circle_id=circle.id, type=FeedType.REPLY.value, actor_learner_id=learner.id,
            payload={"question_event_id": str(question.id), "chunk_id": str(best[0].id)}, created_at=now,
        ).on_conflict_do_nothing(index_elements=["id"]).returning(FeedEvent.id))
        posted += inserted is not None
    return posted

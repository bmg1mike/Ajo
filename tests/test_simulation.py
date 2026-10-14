"""Milestone 5: demo seeding, persona behavior, admin commands, virtual days and reset."""
import dataclasses
import random
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select

from app.core import account, circles, qa
from app.core.clock import virtual_day
from app.core.conversation import ADMIN_COMMANDS, Conversation, Inbound
from app.core.i18n import t
from app.core.lessons import LessonDraft, LessonService, QuestionDraft
from app.core.qa import QAService
from app.models import (
    Answer, Channel, Chunk, Circle, CircleMember, ClockState, DailyRun, FeedEvent, Goal, Language, Learner, Lesson,
    Nudge, OutboundDelivery, PregeneratedLesson, QALog, Question, RunTrigger, Simulation,
)
from app.sim import engine as demo_engine
from app.sim import personas
from app.sim.engine import DemoEngine, RESET_NO, RESET_YES
from tests.fakes import FakeEmbedder
from tests.test_lessons import Library, library  # noqa: F401 - the shared two-topic library fixture
from tests.test_onboarding import SALT, User

EMBEDDER = FakeEmbedder()
TOPIC_TITLES = ("Photosynthesis", "Excretion")


def pregenerated_draft(chunk_ids: list[UUID], topic: str) -> dict:
    def item(stem: str, correct: str, follow_up: QuestionDraft | None = None) -> QuestionDraft:
        return QuestionDraft(stem=stem, options={"A": "Glucose", "B": "Chlorophyll", "C": "Urea", "D": "Salt"},
                             correct_option=correct, explanation="The cited chunk says so.",
                             cited_chunk_ids=[chunk_ids[0]], follow_up=follow_up)

    questions = [item(f"{topic} question {n}?", correct, item(f"Easier {topic} question {n}?", "A"))
                 for n, correct in ((1, "B"), (2, "C"))]
    return LessonDraft(lesson=f"A short lesson on {topic.lower()}.", example="A worked example.",
                       cited_chunk_ids=chunk_ids[:2], questions=questions).model_dump(mode="json")


@dataclass
class Demo:
    sessions: object
    clock: object
    engine: DemoEngine
    conversation: Conversation
    library: Library
    admin: User

    def user(self, name: str = "Bola", language: Language = Language.EN) -> User:
        user = User(self.conversation)
        user.onboard(self.library.goal_id, name=name, language=language)
        return user

    @property
    def circle_id(self) -> UUID:
        with self.sessions() as session:
            return circles.demo_circle_id(session.get(Goal, self.library.goal_id))


def make_demo(sessions, clock, make_client, library, *, qa_min_score: float = 0.3, onboard_admin: bool = True,
              goal_code: str = "jamb_bio") -> Demo:
    with sessions.begin() as session:
        for topic_id, chunks, title in zip(library.topics, (library.leaf_chunks, library.kidney_chunks), TOPIC_TITLES):
            session.add(PregeneratedLesson(topic_id=topic_id, content=pregenerated_draft(chunks, title),
                                           cited_chunk_ids=chunks[:2], created_at=clock.now(session)))
    client = make_client(enabled=False)  # every lesson is the validated pre-generated one; no LLM calls
    lessons = LessonService(EMBEDDER, client, clock)
    engine = DemoEngine(sessions, clock, lessons, EMBEDDER, salt=SALT, min_score=0.3, goal_code=goal_code)
    admin_id = str(random.randrange(10**9, 10**10))
    conversation = Conversation(sessions, clock, QAService(EMBEDDER, client, clock, min_score=qa_min_score), lessons,
                                salt=SALT, admin=engine, admin_ids=frozenset({int(admin_id)}))
    admin = User(conversation)
    admin.user_id = admin.chat_id = admin_id
    if onboard_admin:
        admin.onboard(library.goal_id, name="Amaka")
    return Demo(sessions, clock, engine, conversation, library, admin)


@pytest.fixture
def demo(sessions, clock, make_client, library):
    return make_demo(sessions, clock, make_client, library)


def offset(sessions) -> int:
    with sessions() as session:
        return session.get(ClockState, 1).offset_seconds


def persona_answers(sessions, circle_id: UUID) -> list[tuple]:
    """(day, persona, question position, follow-up?, chosen option, correct?) for every persona answer."""
    with sessions() as session:
        rows = session.execute(
            select(Lesson.day_index, Learner.first_name, Question.position, Question.parent_question_id.is_not(None),
                   Answer.chosen_option, Answer.is_correct)
            .join(Question, Question.id == Answer.question_id).join(Lesson, Lesson.id == Question.lesson_id)
            .join(Learner, Learner.id == Answer.learner_id)
            .where(Lesson.circle_id == circle_id, Learner.is_simulated.is_(True))
        ).all()
    return sorted(tuple(row) for row in rows)


def deliveries(sessions, learner_hash: str) -> list[OutboundDelivery]:
    with sessions() as session:
        return list(session.scalars(select(OutboundDelivery).join(Learner, Learner.id == OutboundDelivery.learner_id)
                                    .where(Learner.channel_user_hash == learner_hash)
                                    .order_by(OutboundDelivery.created_at)))


def texts(delivery: OutboundDelivery) -> list[str]:
    return [message["text"] for message in delivery.payload["messages"]]


# Access control


@pytest.mark.parametrize("command", sorted(ADMIN_COMMANDS))
def test_admin_commands_are_refused_for_everyone_else(sessions, demo, command):
    before = offset(sessions)
    learner = demo.user()
    stranger = User(demo.conversation)  # not even onboarded
    for user in (learner, stranger):
        assert [reply.text for reply in user.send(f"/{command} 7")] == [t("admin.forbidden")]
    # The admin's numeric ID on another channel is not the admin.
    web = demo.conversation.handle(Inbound(Channel.WEB, demo.admin.user_id, demo.admin.chat_id, text=f"/{command}"))
    assert [reply.text for reply in web] == [t("admin.forbidden")]
    assert offset(sessions) == before
    with sessions() as session:
        assert session.get(Simulation, demo.circle_id) is None


def test_admin_buttons_are_refused_for_everyone_else(sessions, demo):
    demo.admin.send("/seed_demo")
    learner = demo.user()
    assert [reply.text for reply in learner.tap(RESET_YES)] == [t("admin.forbidden")]
    with sessions() as session:
        assert session.get(Simulation, demo.circle_id) is not None


def test_without_an_admin_handler_every_admin_command_is_refused(sessions, clock, make_client, library):
    demo = make_demo(sessions, clock, make_client, library)
    demo.conversation.admin = None
    assert [reply.text for reply in demo.admin.send("/demo_status")] == [t("admin.forbidden")]


# Seeding


def test_seed_demo_creates_personas_without_chat_ids_and_adds_the_admin(sessions, demo):
    (reply,) = demo.admin.send("/seed_demo")
    assert reply.text.startswith("🌱 Demo ready: Ajo Demo Circle with Amaka, Chidi, Halima, Tunde. Seed 42.")
    demo.admin.send("/seed_demo")  # repeating it changes nothing
    with sessions() as session:
        members = session.execute(select(Learner).join(CircleMember, CircleMember.learner_id == Learner.id)
                                  .where(CircleMember.circle_id == demo.circle_id)).scalars().all()
        by_name = {learner.first_name: learner for learner in members}
        assert sorted(by_name) == ["Amaka", "Chidi", "Halima", "Tunde"]
        for name, language in (("Chidi", Language.EN), ("Halima", Language.PCM), ("Tunde", Language.YO)):
            persona = by_name[name]
            assert persona.is_simulated and persona.channel_chat_id is None and persona.language == language
            assert persona.consented_at is None and persona.difficulty == 2 and not persona.share_scores
        assert not by_name["Amaka"].is_simulated
        assert session.get(Simulation, demo.circle_id).seed == demo_engine.DEFAULT_SEED

    (reply,) = demo.admin.send("/seed_demo 7")
    assert "Seed 7." in reply.text
    assert [r.text for r in demo.admin.send("/seed_demo lots")] == [t("admin.seed_usage")]
    with sessions() as session:
        assert session.get(Simulation, demo.circle_id).seed == 7
        assert session.scalar(select(func.count()).select_from(CircleMember)
                              .where(CircleMember.circle_id == demo.circle_id)) == 4


def test_seed_demo_before_onboarding_seeds_the_configured_goal_and_asks_the_admin_to_join(sessions, clock,
                                                                                            make_client, library):
    with sessions() as session:
        code = session.get(Goal, library.goal_id).code
    demo = make_demo(sessions, clock, make_client, library, onboard_admin=False, goal_code=code)
    seeded, join = demo.admin.send("/seed_demo")
    assert "with Chidi, Halima, Tunde." in seeded.text
    assert join.text == t("admin.join_first")
    with sessions() as session:
        # No learner row without consent: the admin joins by onboarding like anyone else.
        assert session.scalar(select(Learner).where(Learner.channel_user_hash == demo.admin.hash)) is None
    demo.admin.onboard(library.goal_id, name="Amaka")
    with sessions() as session:
        assert circles.is_member(session, demo.circle_id, session.scalar(
            select(Learner.id).where(Learner.channel_user_hash == demo.admin.hash)))


def test_seed_demo_without_an_ingested_goal_says_so(sessions, clock, make_client, library):
    demo = make_demo(sessions, clock, make_client, library, onboard_admin=False, goal_code="no_such_goal")
    assert [reply.text for reply in demo.admin.send("/seed_demo")] == [t("admin.no_goal")]


def test_simulating_needs_a_seeded_demo(sessions, demo):
    before = offset(sessions)
    assert [reply.text for reply in demo.admin.send("/simulate_day")] == [t("admin.not_seeded")]
    assert offset(sessions) == before


# Simulated days


def test_simulate_day_advances_the_clock_and_runs_the_day_in_order(sessions, demo):
    demo.admin.send("/seed_demo")
    before = offset(sessions)
    (reply,) = demo.admin.send("/simulate_day")
    assert offset(sessions) == before + 86400
    now = demo.clock.now()
    assert reply.text.startswith(f"⏩ {virtual_day(now).strftime('%a %d %b %Y')}: Day 1, Photosynthesis\n"
                                 "Checked in: Chidi, Halima, Tunde\nQueued: 1 lesson post(s), 1 circle update(s)")
    with sessions() as session:
        run = session.get(DailyRun, (demo.circle_id, virtual_day(now)))
        assert run.trigger == RunTrigger.SIMULATION
        lesson = session.get(Lesson, run.lesson_id)
        assert lesson.day_index == 1 and virtual_day(lesson.posted_at) == virtual_day(now)
        assert lesson.fallback_reason == "llm_unavailable"
        # Every business timestamp is virtual: persona answers land on the simulated day.
        answered = session.scalars(select(Answer.answered_at).join(Learner, Learner.id == Answer.learner_id)
                                   .join(Question, Question.id == Answer.question_id)
                                   .where(Learner.is_simulated.is_(True), Question.lesson_id == lesson.id)).all()
        assert answered and {virtual_day(moment) for moment in answered} == {virtual_day(now)}
        # Personas never receive anything; only the real member's lesson and circle update are queued.
        assert session.scalar(select(func.count()).select_from(OutboundDelivery)
                              .join(Learner, Learner.id == OutboundDelivery.learner_id)
                              .where(Learner.is_simulated.is_(True))) == 0
    lesson_post, update = deliveries(sessions, demo.admin.hash)
    assert texts(lesson_post)[-1].startswith("📘 Day 1: Photosynthesis")
    assert texts(update) == ["🔵 Circle update\nChidi ✅ answered today's questions\n"
                             "Halima ✅ answered today's questions\nTunde ✅ answered today's questions"]


def test_tunde_goes_quiet_after_day_two_and_returns_the_day_after_a_nudge(sessions, demo):
    demo.admin.send("/seed_demo")
    for _ in range(4):
        demo.admin.send("/simulate_day")
    days = {(day, name) for day, name, *_ in persona_answers(sessions, demo.circle_id)}
    assert {day for day, name in days if name == "Tunde"} == {1, 2}
    assert {day for day, name in days if name == "Chidi"} == {day for day, name in days if name == "Halima"} == {
        1, 2, 3, 4}
    day_two = virtual_day(demo.clock.now() - timedelta(days=2))
    update = texts(deliveries(sessions, demo.admin.hash)[-1])[0]
    assert f"Tunde ⏳ no check-in since {day_two.strftime('%A')}" in update

    # Someone nudges Tunde on day 4 (milestone 6 sends it); he checks in on day 5, then goes quiet again.
    with sessions.begin() as session:
        learner_ids = dict(session.execute(select(Learner.first_name, Learner.id)
                                           .join(CircleMember, CircleMember.learner_id == Learner.id)
                                           .where(CircleMember.circle_id == demo.circle_id)).all())
        now = demo.clock.now(session)
        session.add(Nudge(circle_id=demo.circle_id, from_learner_id=learner_ids["Amaka"],
                          to_learner_id=learner_ids["Tunde"], virtual_day=virtual_day(now), created_at=now))
    (reply,) = demo.admin.send("/simulate_day")
    assert "Checked in: Chidi, Halima, Tunde" in reply.text
    (reply,) = demo.admin.send("/simulate_day")
    assert "Checked in: Chidi, Halima\n" in reply.text
    tunde_days = {day for day, name, *_ in persona_answers(sessions, demo.circle_id) if name == "Tunde"}
    assert tunde_days == {1, 2, 5}


def test_a_learners_own_lesson_request_does_not_shift_the_persona_schedule(sessions, demo):
    demo.admin.send("/seed_demo")
    demo.admin.send("/lesson")  # creates lesson day 1 outside any daily run
    for _ in range(2):
        (reply,) = demo.admin.send("/simulate_day")
        assert "Checked in: Chidi, Halima, Tunde" in reply.text  # lesson days 2 and 3 are Tunde's days 1 and 2
    (reply,) = demo.admin.send("/simulate_day")
    assert "Checked in: Chidi, Halima\n" in reply.text


def test_personas_answer_through_grading_including_follow_ups(sessions, demo):
    demo.admin.send("/seed_demo")
    demo.admin.send("/simulate_week")
    answers = persona_answers(sessions, demo.circle_id)
    for day, name, position, follow_up, chosen, correct in answers:
        if follow_up:  # a follow-up is answered only after a wrong answer to its question
            assert (day, name, position, False, False) in {(d, n, p, f, c) for d, n, p, f, _, c in answers}
    mains = [row for row in answers if not row[3]]
    assert {(day, name) for day, name, *_ in mains} >= {(day, "Chidi") for day in range(1, 8)}
    assert all(sum(1 for row in mains if row[:2] == key) == 2 for key in {row[:2] for row in mains})


@pytest.mark.parametrize("persona, expected", [(personas.CHIDI, 0.9), (personas.HALIMA, 0.5)])
def test_persona_accuracy_follows_the_spec(persona, expected):
    question = Question(position=1, parent_question_id=None, correct_option="B",
                        options={"A": "a", "B": "b", "C": "c", "D": "d"})
    choices = [personas.choose_option(42, persona, day, question) for day in range(1, 2001)]
    assert abs(choices.count("B") / len(choices) - expected) < 0.04
    assert set(choices) <= {"A", "B", "C", "D"}
    assert choices == [personas.choose_option(42, persona, day, question) for day in range(1, 2001)]


def test_a_seeded_simulation_repeats_exactly_after_a_reset(sessions, demo):
    def run(seed: int) -> list[tuple]:
        demo.admin.send(f"/seed_demo {seed}")
        for _ in range(4):
            demo.admin.send("/simulate_day")
        result = persona_answers(sessions, demo.circle_id)
        demo.admin.send("/reset_demo")
        demo.admin.tap(RESET_YES)
        return result

    first = run(42)
    assert len(first) >= 4 * 2 * 2 + 2 * 2  # Chidi and Halima daily, Tunde twice, two questions each at least
    assert run(42) == first
    assert run(1234) != first


def test_simulate_week_runs_seven_days_with_one_update_per_day(sessions, demo):
    demo.admin.send("/seed_demo")
    before = offset(sessions)
    (reply,) = demo.admin.send("/simulate_week")
    assert offset(sessions) == before + 7 * 86400
    assert reply.text.count("⏩") == 7 and reply.text.endswith(t("admin.day_payout", week=1, count=1))
    with sessions() as session:
        lessons = session.execute(select(Lesson.day_index, Lesson.topic_id).where(Lesson.circle_id == demo.circle_id)
                                  .order_by(Lesson.day_index)).all()
        assert [day for day, _ in lessons] == list(range(1, 8))
        # The ten-topic sequence wraps; this library has two topics.
        assert [topic for _, topic in lessons] == [demo.library.topics[(day - 1) % 2] for day in range(1, 8)]
        assert session.scalar(select(func.count()).select_from(DailyRun)
                              .where(DailyRun.circle_id == demo.circle_id)) == 7
    rows = deliveries(sessions, demo.admin.hash)
    feed_keys = [row.deduplication_key for row in rows if row.deduplication_key.startswith("feed:")]
    assert len(feed_keys) == len(set(feed_keys)) == 7


def test_a_day_is_processed_once_by_either_trigger(sessions, demo):
    demo.admin.send("/seed_demo")
    demo.admin.send("/simulate_day")
    assert demo.engine.run_day(demo.circle_id, RunTrigger.SCHEDULER) is None
    reports, failed = demo.engine.run_scheduled()
    assert demo.circle_id not in {report.circle_id for report in reports} and not failed
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(Lesson).where(Lesson.circle_id == demo.circle_id)) == 1
    assert len(deliveries(sessions, demo.admin.hash)) == 2


def test_a_day_without_any_lesson_still_runs_the_feed(sessions, demo):
    with sessions.begin() as session:
        for topic_id in demo.library.topics:
            session.delete(session.get(PregeneratedLesson, topic_id))
    demo.admin.send("/seed_demo")
    (reply,) = demo.admin.send("/simulate_day")
    assert t("admin.no_lesson") in reply.text and "Checked in: nobody" in reply.text
    (update,) = deliveries(sessions, demo.admin.hash)
    assert "Chidi ⏳ no check-in yet" in texts(update)[0]


# Circle questions and Chidi's replies


def test_chidi_replies_to_circle_questions_with_a_citation_only_when_the_library_matches(
        sessions, clock, make_client, library, monkeypatch):
    monkeypatch.setattr(personas, "PERSONAS", (dataclasses.replace(personas.CHIDI, reply_rate=1.0),
                                               personas.HALIMA, personas.TUNDE))
    # QA falls back on everything, so both questions can be posted to the circle.
    demo = make_demo(sessions, clock, make_client, library, qa_min_score=0.999)
    demo.admin.send("/seed_demo")
    asker = demo.user("Bola")
    for question in ("How do leaves use chlorophyll to capture light?", "Who won the Super Eagles match?"):
        (fallback,) = asker.send(question)
        asker.tap(next(b.payload for b in fallback.buttons if b.payload.startswith(qa.ASK_CIRCLE)))
    demo.admin.send("/simulate_day")
    with sessions() as session:
        replies = session.scalars(select(FeedEvent).where(FeedEvent.circle_id == demo.circle_id,
                                                          FeedEvent.type == "reply")).all()
        assert len(replies) == 1  # the off-topic question gets no Biology source
        assert UUID(replies[0].payload["chunk_id"]) in library.leaf_chunks
        assert set(replies[0].payload) == {"question_event_id", "chunk_id"}
    update = deliveries(sessions, demo.admin.hash)[-1]
    text = texts(update)[0]
    assert "❓ Bola asked the circle: How do leaves use chlorophyll to capture light?" in text
    assert "❓ Bola asked the circle: Who won the Super Eagles match?" in text
    assert ("💬 Chidi replied to a circle question: I think this part of the book explains it.\n"
            "Source: Synthetic Book, 1.") in text
    assert [button["payload"] for button in update.payload["messages"][0]["buttons"]] == [
        f"{qa.SHOW_SOURCE}{replies[0].payload['chunk_id']}"]
    # The reply is posted once, however many days pass.
    demo.admin.send("/simulate_day")
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(FeedEvent)
                              .where(FeedEvent.circle_id == demo.circle_id, FeedEvent.type == "reply")) == 1

    # If the asker deletes their data, the question goes and the orphaned reply is no longer shown.
    asker.send("/delete_me")
    asker.tap(account.DELETE_YES)
    with sessions.begin() as session:
        member = session.scalar(select(CircleMember).join(Learner, Learner.id == CircleMember.learner_id)
                                .where(Learner.channel_user_hash == demo.admin.hash))
        member.feed_delivered_at = None
    demo.admin.send("/simulate_day")
    assert "Chidi replied" not in texts(deliveries(sessions, demo.admin.hash)[-1])[0]


# Status and reset


def test_demo_status_shows_the_virtual_date_members_and_check_ins(sessions, demo):
    demo.admin.send("/seed_demo 9")
    demo.admin.send("/simulate_day")
    demo.admin.send("/simulate_day")
    (reply,) = demo.admin.send("/demo_status")
    now = demo.clock.now()
    assert reply.text.startswith(f"🧪 Demo status\nVirtual time: {now.strftime('%a %d %b %Y')}")
    assert "Circle: Ajo Demo Circle, seed 9\nDays run: 2\nToday's lesson: Day 2, Excretion" in reply.text
    today = virtual_day(now).strftime("%a %d %b")
    assert "• Amaka: last check-in never, streak 0" in reply.text
    assert f"• Chidi (persona): last check-in {today}, streak 2" in reply.text
    assert f"• Tunde (persona): last check-in {today}, streak 2" in reply.text


def test_reset_demo_wipes_circle_state_and_clock_but_keeps_learners_and_the_library(sessions, demo):
    demo.admin.send("/seed_demo")
    (fallback,) = demo.admin.send("Who won the Super Eagles match?")
    demo.admin.send("/simulate_day")
    demo.admin.send("/simulate_day")
    (confirm,) = demo.admin.send("/reset_demo")
    assert [button.payload for button in confirm.buttons] == [RESET_YES, RESET_NO]
    assert [reply.text for reply in demo.admin.tap(RESET_NO)] == [t("admin.reset_cancelled")]
    assert offset(sessions) > 0

    assert [reply.text for reply in demo.admin.tap(RESET_YES)] == [t("admin.reset_done")]
    assert offset(sessions) == 0
    with sessions() as session:
        circle_id = demo.circle_id
        for model in (Lesson, FeedEvent, Nudge, DailyRun, Simulation):
            assert session.scalar(select(func.count()).select_from(model).where(model.circle_id == circle_id)) == 0
        names = session.scalars(select(Learner.first_name).join(CircleMember, CircleMember.learner_id == Learner.id)
                                .where(CircleMember.circle_id == circle_id)).all()
        assert names == ["Amaka"]  # personas are gone; the real learner stays a member
        member = session.scalar(select(CircleMember).where(CircleMember.circle_id == circle_id))
        assert (member.last_checkin_at, member.streak_days, member.feed_delivered_at) == (None, 0, None)
        assert session.scalar(select(func.count()).select_from(Learner).where(
            Learner.is_simulated.is_(True), Learner.goal_id == demo.library.goal_id)) == 0
        assert session.scalar(select(func.count()).select_from(OutboundDelivery)
                              .where(OutboundDelivery.learner_id == member.learner_id)) == 0
        # The corpus, its pre-generated lessons and the learner's own QA history are kept.
        assert session.scalar(select(func.count()).select_from(Chunk).where(
            Chunk.id.in_(demo.library.leaf_chunks))) == len(demo.library.leaf_chunks)
        assert session.get(PregeneratedLesson, demo.library.topics[0]) is not None
        assert session.scalar(select(func.count()).select_from(QALog)
                              .where(QALog.learner_id == member.learner_id)) == 1
        assert session.get(Circle, circle_id) is not None

    # A fresh run starts again at day 1.
    demo.admin.send("/seed_demo")
    (reply,) = demo.admin.send("/simulate_day")
    assert "Day 1, Photosynthesis" in reply.text

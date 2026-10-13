import json
import random
import threading
import unicodedata
from pathlib import Path
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, or_, select

from app.core import account, circles, onboarding, qa
from app.core.conversation import Conversation, Inbound, parse_command
from app.core.i18n import LANGUAGE_NAMES, t
from app.core.privacy import channel_hash
from app.core.lessons import LessonService
from app.core.qa import QAService
from app.db import build_engine, session_factory
from app.models import (
    Channel, Circle, CircleMember, FeedEvent, FeedType, Goal, Language, Learner, Onboarding, OnboardingStep, QALog,
)
from tests.fakes import FakeEmbedder, FakeLLM, add_goal_with_chunks, chat

SALT = "synthetic-test-salt-with-32-characters"
LEAF = "Leaves use chlorophyll to capture light. Leaf cells turn carbon dioxide and water into sugar and glucose."
KIDNEY = "The kidney filters blood in each nephron. Urea and salt leave the body in urine stored in the bladder."
LEAF_QUESTION = "How do leaf cells use chlorophyll and light to make sugar?"
OFF_TOPIC = "Who won the Super Eagles match?"


def answer_json(*cited: str) -> str:
    return json.dumps({"answerable": True, "answer": "Leaves capture light with chlorophyll and make sugar.",
                       "cited_chunk_ids": list(cited)})


class User:
    """One synthetic Telegram-like user talking to the core conversation."""

    def __init__(self, conversation: Conversation) -> None:
        self.conversation = conversation
        self.user_id = str(random.randrange(10**9, 10**10))  # shaped like a real Telegram user ID
        self.chat_id = self.user_id  # private chats share the user's ID

    @property
    def hash(self) -> str:
        return channel_hash(Channel.TELEGRAM, self.user_id, SALT)

    def send(self, text: str | None):
        return self.conversation.handle(Inbound(Channel.TELEGRAM, self.user_id, self.chat_id, text=text))

    def tap(self, payload: str):
        return self.conversation.handle(Inbound(Channel.TELEGRAM, self.user_id, self.chat_id, payload=payload))

    def onboard(self, goal_id: UUID, *, name: str = "Amaka", language: Language = Language.EN):
        self.send("/start")
        self.tap(onboarding.AGREE)
        self.tap(f"{onboarding.CHOOSE_LANGUAGE}{language.value}")
        self.send(name)
        return self.tap(f"{onboarding.CHOOSE_GOAL}{goal_id}")


@pytest.fixture
def goal_id(sessions):
    with sessions.begin() as session:
        goal, _ = add_goal_with_chunks(session, [LEAF, KIDNEY])
        return goal.id


@pytest.fixture
def make_conversation(sessions, clock, make_client):
    def make(*responses, fake: FakeLLM | None = None) -> Conversation:
        client = make_client(fake or FakeLLM(*responses))
        service = QAService(FakeEmbedder(), client, clock, min_score=0.3)
        return Conversation(sessions, clock, service, LessonService(FakeEmbedder(), client, clock), salt=SALT)
    return make


def learner_for(sessions, user: User) -> Learner | None:
    with sessions() as session:
        return session.scalar(select(Learner).where(Learner.channel_user_hash == user.hash))


def onboarding_for(sessions, user: User) -> Onboarding | None:
    with sessions() as session:
        return session.get(Onboarding, user.hash)


def payloads(reply) -> list[str]:
    return [button.payload for button in reply.buttons]


# Onboarding


def test_onboarding_flow_creates_a_level_two_learner_in_the_demo_circle(sessions, goal_id, make_conversation):
    user = User(make_conversation())
    intro, consent = user.send("/start")
    assert intro.text == t("onboarding.intro") and len(intro.text.splitlines()) == 3
    assert payloads(consent) == [onboarding.AGREE]
    for topic in ("store", "circle sees only your first name", "/delete_me"):
        assert topic in consent.text
    assert onboarding_for(sessions, user) is None and learner_for(sessions, user) is None  # nothing before consent

    (language_menu,) = user.tap(onboarding.AGREE)
    assert [button.text for button in language_menu.buttons] == ["English", "Yorùbá", "Naijá (Pidgin)"]
    assert payloads(language_menu) == ["ob:lang:en", "ob:lang:yo", "ob:lang:pcm"]
    state = onboarding_for(sessions, user)
    assert state.step == OnboardingStep.LANGUAGE and state.consented_at is not None

    note, ask_name = user.tap("ob:lang:yo")
    assert "native speaker" in note.text and "Yorùbá" in note.text  # Yoruba UI awaits review
    assert ask_name.text == t("onboarding.ask_name")
    (goal_menu,) = user.send("  Ṣadé  ")
    assert f"{onboarding.CHOOSE_GOAL}{goal_id}" in payloads(goal_menu)
    assert all(len(payload.encode()) <= 64 for payload in payloads(goal_menu))

    joined, welcome, help_text = user.tap(f"{onboarding.CHOOSE_GOAL}{goal_id}")
    assert "Ṣadé" in joined.text and "no test to pass" in joined.text
    assert welcome.text.startswith(f"🔵 Welcome to {circles.DEMO_CIRCLE_NAME}")
    assert "/delete_me" in help_text.text

    learner = learner_for(sessions, user)
    assert learner.first_name == "Ṣadé" and learner.language == Language.YO and learner.goal_id == goal_id
    assert learner.difficulty == onboarding.STARTING_DIFFICULTY == 2  # no placement test
    assert learner.share_scores is False and learner.is_simulated is False
    assert learner.channel_chat_id == user.chat_id and learner.consented_at == state.consented_at
    assert learner.channel_user_hash == user.hash and user.user_id not in learner.channel_user_hash
    assert onboarding_for(sessions, user) is None  # progress is removed once the learner exists
    with sessions() as session:
        goal = session.get(Goal, goal_id)
        member = session.get(CircleMember, (circles.demo_circle_id(goal), learner.id))
        assert member is not None and member.last_checkin_at is None


def test_second_learner_joins_the_same_circle_and_sees_first_names_only(sessions, goal_id, make_conversation):
    conversation = make_conversation()
    first, second = User(conversation), User(conversation)
    _, alone, _ = first.onboard(goal_id, name="Amaka")
    assert alone.text == t("circle.welcome_alone", circle=circles.DEMO_CIRCLE_NAME)
    _, welcome, _ = second.onboard(goal_id, name="Bola", language=Language.PCM)
    assert welcome.text == f"🔵 Welcome to {circles.DEMO_CIRCLE_NAME}\nMembers: Amaka, Bola"
    with sessions() as session:
        goal = session.get(Goal, goal_id)
        assert session.scalar(select(func.count()).select_from(CircleMember)
                              .where(CircleMember.circle_id == circles.demo_circle_id(goal))) == 2


@pytest.mark.parametrize("name", ["", "   ", "A", "Ẹ", "Ẹ́", "08031234567", "Ada1", "My name is Amaka", "a" * 31, "😀", "-Ada",
                                  "ada@example.com", "https://x.io", "Ada_Obi"])
def test_names_that_are_not_first_names_are_rejected(sessions, goal_id, make_conversation, name):
    user = User(make_conversation())
    user.send("/start"), user.tap(onboarding.AGREE), user.tap("ob:lang:en")
    (reply,) = user.send(name)
    assert reply.text == t("onboarding.invalid_name", min=onboarding.MIN_NAME_CHARS, max=onboarding.MAX_NAME_CHARS)
    state = onboarding_for(sessions, user)
    assert state.step == OnboardingStep.NAME and state.first_name is None


@pytest.mark.parametrize("raw,expected", [("Amaka", "Amaka"), ("Mary-Jane", "Mary-Jane"), ("O’Neil", "O’Neil"),
                                          ("  Mary   Jane ", "Mary Jane"), ("Chidinma.", "Chidinma.")])
def test_first_names_are_normalized(raw, expected):
    assert onboarding.clean_first_name(raw) == expected


def test_yoruba_names_with_combining_tone_marks_are_accepted():
    decomposed = unicodedata.normalize("NFD", "Ọlágúnjú")
    assert onboarding.clean_first_name(decomposed) == unicodedata.normalize("NFC", "Ọlágúnjú")
    assert onboarding.clean_first_name("Ẹ́mì") is not None  # ẹ́ has no precomposed form


def test_out_of_order_and_repeated_taps_keep_the_current_step(sessions, goal_id, make_conversation):
    user = User(make_conversation())
    (reply,) = user.send("Hello")
    assert reply.text == t("onboarding.start_first")
    user.send("/start")
    consent_required, consent = user.tap("ob:lang:en")  # skipping consent stores nothing
    assert consent_required.text == t("onboarding.consent_required") and payloads(consent) == [onboarding.AGREE]
    assert onboarding_for(sessions, user) is None

    user.tap(onboarding.AGREE)
    (again,) = user.tap(onboarding.AGREE)
    assert payloads(again)[0].startswith(onboarding.CHOOSE_LANGUAGE)
    (menu,) = user.send("What is osmosis?")  # free text during language choice re-asks, never reaches QA
    assert payloads(menu)[0].startswith(onboarding.CHOOSE_LANGUAGE)
    expired, menu = user.tap("ob:lang:fr")
    assert expired.text == t("button.expired") and payloads(menu)[0].startswith(onboarding.CHOOSE_LANGUAGE)
    user.tap("ob:lang:en")
    (ask_name,) = user.tap(f"{onboarding.CHOOSE_GOAL}{goal_id}")  # goal before name
    assert ask_name.text == t("onboarding.ask_name")
    (ask_name,) = user.send("/ask What is osmosis?")  # commands are never taken as names
    assert ask_name.text == t("onboarding.ask_name") and onboarding_for(sessions, user).first_name is None
    user.send("Amaka")
    _, switched = user.tap("ob:lang:pcm")  # language can still change before the end (with the review note)
    assert payloads(switched)[0].startswith(onboarding.CHOOSE_GOAL)
    expired, menu = user.tap(f"{onboarding.CHOOSE_GOAL}not-a-uuid")
    assert expired.text == t("button.expired") and payloads(menu)
    user.tap(f"{onboarding.CHOOSE_GOAL}{goal_id}")
    assert learner_for(sessions, user).language == Language.PCM

    (done,) = user.tap(f"{onboarding.CHOOSE_GOAL}{goal_id}")  # a stale second tap creates nothing
    assert done.text == t("onboarding.already_done", name="Amaka")
    welcome_back, _ = user.send("/start")
    assert welcome_back.text == t("onboarding.already_done", name="Amaka")
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(Learner)
                              .where(Learner.channel_user_hash == user.hash)) == 1
        assert session.get(Onboarding, user.hash) is None


def test_concurrent_final_taps_create_one_learner(sessions, goal_id, make_conversation, engine):
    if engine.dialect.name != "postgresql":
        pytest.skip("SQLite serializes writers, so there is no race to observe")
    user = User(make_conversation())
    user.send("/start"), user.tap(onboarding.AGREE), user.tap("ob:lang:en"), user.send("Amaka")
    replies = []
    with sessions.begin() as holder:  # hold the progress row so both taps start before either finishes
        holder.get(Onboarding, user.hash, with_for_update=True)
        threads = [threading.Thread(target=lambda: replies.append(user.tap(f"{onboarding.CHOOSE_GOAL}{goal_id}")))
                   for _ in range(2)]
        for thread in threads:
            thread.start()
        threads[0].join(0.5)
        assert all(thread.is_alive() for thread in threads)
    for thread in threads:
        thread.join(10)
    assert sorted(len(reply) for reply in replies) == [1, 3]  # one joined; the other is told it already has
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(Learner)
                              .where(Learner.channel_user_hash == user.hash)) == 1


def test_start_restarts_unfinished_onboarding(sessions, goal_id, make_conversation):
    user = User(make_conversation())
    user.send("/start"), user.tap(onboarding.AGREE), user.tap("ob:lang:yo"), user.send("Amaka")
    assert onboarding_for(sessions, user).step == OnboardingStep.GOAL
    user.send("/start")
    assert onboarding_for(sessions, user) is None


def test_goal_menu_says_when_the_library_is_not_ready(tmp_path):
    engine = build_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    try:
        with session_factory(engine)() as session:
            state = Onboarding(channel_user_hash="0" * 64, channel=Channel.TELEGRAM, step=OnboardingStep.GOAL,
                               language=Language.EN)
            (reply,) = onboarding.prompt(session, state)
        assert reply.text == t("onboarding.no_goals") and reply.buttons == []
    finally:
        engine.dispose()


def test_demo_circle_is_created_once(sessions, clock, goal_id):
    now = clock.now()
    with sessions.begin() as session:
        goal = session.get(Goal, goal_id)
        first = circles.demo_circle(session, goal, now)
        assert first.name == circles.DEMO_CIRCLE_NAME and first.goal_id == goal_id
        assert circles.demo_circle(session, goal, now) is first


def test_concurrent_first_joins_create_one_demo_circle(sessions, clock, goal_id, engine):
    if engine.dialect.name != "postgresql":
        pytest.skip("SQLite serializes writers, so there is no race to observe")
    now = clock.now()
    winner = sessions()
    winner.begin()
    created = circles.demo_circle(winner, winner.get(Goal, goal_id), now)  # inserted, not yet committed
    result = {}

    def racer():
        with sessions.begin() as session:
            result["id"] = circles.demo_circle(session, session.get(Goal, goal_id), now).id

    thread = threading.Thread(target=racer)
    thread.start()
    thread.join(0.5)
    assert thread.is_alive()  # blocked on the conflicting insert
    winner.commit()
    winner.close()
    thread.join(10)
    assert result["id"] == created.id
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(Circle).where(Circle.goal_id == goal_id)) == 1


# Learner commands


def test_commands_for_onboarded_learners(sessions, goal_id, make_conversation):
    user = User(make_conversation())
    user.onboard(goal_id)
    (menu,) = user.send("/language")
    assert payloads(menu) == ["acct:lang:en", "acct:lang:yo", "acct:lang:pcm"]
    changed, note = user.tap("acct:lang:pcm")
    assert changed.text == t("language.changed", language_name=LANGUAGE_NAMES[Language.PCM]) and "Pidgin" in note.text
    assert learner_for(sessions, user).language == Language.PCM
    (changed,) = user.tap("acct:lang:en")
    assert learner_for(sessions, user).language == Language.EN and changed.text.endswith("English.")

    (scores,) = user.send("/share_scores")
    assert scores.text == t("scores.status_private")
    assert payloads(scores) == [account.SHARE_SCORES, account.KEEP_SCORES_PRIVATE]
    assert user.tap(account.SHARE_SCORES)[0].text == t("scores.status_shared")
    assert learner_for(sessions, user).share_scores is True
    assert user.send("/share_scores")[0].text == t("scores.status_shared")
    user.tap(account.KEEP_SCORES_PRIVATE)
    assert learner_for(sessions, user).share_scores is False

    assert user.send("/help")[0].text == t("help")
    assert user.send("/progress")[0].text == t("command.coming_soon")
    assert [reply.text for reply in user.send("/dance")] == [t("command.unknown"), t("help")]
    assert user.send("/ask")[0].text == t("ask.usage")
    assert user.send(None)[0].text == t("message.text_only")
    assert user.tap("mystery")[0].text == t("button.expired")
    assert user.tap(f"{qa.SHOW_SOURCE}not-a-uuid")[0].text == t("button.expired")


def test_chat_id_follows_the_learner(sessions, goal_id, make_conversation):
    user = User(make_conversation())
    user.onboard(goal_id)
    user.chat_id = "555000111"
    user.send("/help")
    assert learner_for(sessions, user).channel_chat_id == "555000111"


@pytest.mark.parametrize("text,expected", [
    ("/ask What is osmosis?", ("ask", "What is osmosis?")), ("/ASK@AjoBot  Why?", ("ask", "Why?")),
    ("/start", ("start", "")), ("/delete_me", ("delete_me", "")), ("/ask\nmulti\nline", ("ask", "multi\nline")),
    ("What is osmosis?", (None, "What is osmosis?")), ("  / not a command", (None, "/ not a command")),
])
def test_parse_command(text, expected):
    assert parse_command(text) == expected


def test_inbound_repr_hides_identifiers():
    inbound = Inbound(Channel.TELEGRAM, "7012345678", "7012345678", text="Hi")
    assert "7012345678" not in repr(inbound)


# Questions


def test_free_text_and_ask_reach_cited_qa(sessions, goal_id, make_conversation):
    user = User(make_conversation(chat(answer_json("C1")), chat("yes"), chat(answer_json("C1")), chat("yes")))
    user.onboard(goal_id)
    (answer,) = user.send(LEAF_QUESTION)
    assert answer.text.startswith("Leaves capture light") and "Source: Synthetic Book, 1.1 Section, p. 11" in answer.text
    (source,) = user.tap(answer.buttons[0].payload)
    assert source.text.startswith("Synthetic Book, 1.1 Section, p. 11\n\nLeaves use chlorophyll")
    (answer,) = user.send(f"/ask {LEAF_QUESTION}")
    assert "Source:" in answer.text
    learner = learner_for(sessions, user)
    with sessions() as session:
        logs = session.scalars(select(QALog).where(QALog.learner_id == learner.id)).all()
        assert len(logs) == 2 and all(log.answered for log in logs)


def test_onboarded_name_never_reaches_the_llm_in_any_unicode_form(sessions, goal_id, make_conversation):
    fake = FakeLLM(chat(answer_json("C1")), chat("yes"))
    user = User(make_conversation(fake=fake))
    decomposed = unicodedata.normalize("NFD", "Ṣadé")
    assert decomposed != "Ṣadé"
    user.onboard(goal_id, name=decomposed)
    assert learner_for(sessions, user).first_name == "Ṣadé"  # stored precomposed
    user.send(f"I am {decomposed}, also written ṢADÉ. {LEAF_QUESTION}")
    payload = json.dumps(fake.requests, ensure_ascii=False)
    for form in ("Ṣadé", decomposed, "ṢADÉ", unicodedata.normalize("NFD", "ṢADÉ")):
        assert form not in payload
    assert "I am [redacted], also written [redacted]." in fake.requests[0]["messages"][1]["content"]


def test_fallback_offers_to_ask_the_circle(sessions, goal_id, make_conversation):
    user = User(make_conversation())  # off-topic text falls below the threshold, so no LLM call is made
    user.onboard(goal_id)
    (fallback,) = user.send(OFF_TOPIC)
    assert fallback.text == t("qa.fallback")
    ask_payload, dismiss_payload = payloads(fallback)
    assert dismiss_payload == qa.DISMISS and user.tap(qa.DISMISS)[0].text == t("qa.dismissed")
    assert user.tap(ask_payload)[0].text == t("qa.posted_to_circle")
    assert user.tap(ask_payload)[0].text == t("qa.already_posted")
    learner = learner_for(sessions, user)
    with sessions() as session:
        event = session.scalar(select(FeedEvent).where(FeedEvent.actor_learner_id == learner.id))
        assert event.type == FeedType.QUESTION and event.payload["question"] == OFF_TOPIC


# Deletion


def test_delete_me_needs_confirmation_then_removes_the_learner(sessions, goal_id, make_conversation):
    conversation = make_conversation()
    user, peer = User(conversation), User(conversation)
    user.onboard(goal_id, name="Amaka")
    peer.onboard(goal_id, name="Bola")
    (fallback,) = user.send(OFF_TOPIC)
    user.tap(fallback.buttons[0].payload)  # a question posted to the circle
    learner = learner_for(sessions, user)

    (confirm,) = user.send("/delete_me")
    assert payloads(confirm) == [account.DELETE_YES, account.DELETE_NO]
    assert "can't be undone" in confirm.text
    assert user.tap(account.DELETE_NO)[0].text == t("delete.cancelled")
    assert learner_for(sessions, user) is not None

    assert user.tap(account.DELETE_YES)[0].text == t("delete.done")
    assert learner_for(sessions, user) is None
    with sessions() as session:
        goal = session.get(Goal, goal_id)
        circle_id = circles.demo_circle_id(goal)
        for model, column in ((CircleMember, CircleMember.learner_id), (QALog, QALog.learner_id),
                              (FeedEvent, FeedEvent.actor_learner_id)):
            assert session.scalar(select(func.count()).select_from(model).where(column == learner.id)) == 0
        left = session.scalars(select(FeedEvent).where(FeedEvent.circle_id == circle_id,
                                                       FeedEvent.type == FeedType.MEMBER_LEFT)).all()
        assert [(event.actor_learner_id, event.payload) for event in left] == [(None, {})]
        assert t("circle.member_left") == "A member left the circle."
        assert circles.member_first_names(session, circle_id) == ["Bola"]
        assert session.scalar(select(func.count()).select_from(Learner).where(
            or_(Learner.channel_chat_id == user.chat_id, Learner.channel_user_hash == user.hash))) == 0
    assert user.send("/delete_me")[0].text == t("delete.nothing_stored")
    assert user.tap(account.DELETE_YES)[0].text == t("delete.nothing_stored")  # stale confirmation is harmless
    assert len(user.send("/start")) == 2  # the user can come back as a new learner


def test_delete_me_mid_onboarding_removes_progress(sessions, goal_id, make_conversation):
    user = User(make_conversation())
    assert user.send("/delete_me")[0].text == t("delete.nothing_stored")
    user.send("/start"), user.tap(onboarding.AGREE), user.tap("ob:lang:en"), user.send("Amaka")
    (confirm,) = user.send("/delete_me")
    assert payloads(confirm) == [account.DELETE_YES, account.DELETE_NO]
    assert onboarding_for(sessions, user) is not None
    assert user.tap(account.DELETE_YES)[0].text == t("delete.done")
    assert onboarding_for(sessions, user) is None and learner_for(sessions, user) is None

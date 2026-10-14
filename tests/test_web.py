"""Milestone 9: the web simulator. Three panes drive one circle over HTTP alone, with no Telegram."""
import asyncio
import base64
import json
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select

from app.adapters.base import AdapterRegistry, Button
from app.adapters.web import WebAdapter, message_json
from app.config import Settings
from app.core import account, lessons, onboarding, outbox, qa
from app.core.clock import virtual_day
from app.core.i18n import t
from app.core.qa import QAService
from app.core.replies import Attachment, Reply
from app.db import build_engine
from app.main import create_app
from app.models import (
    Answer, Channel, ClockState, Goal, Language, Learner, OutboundDelivery, Question, WebMessage,
)
from app.sim import personas
from tests.conftest import migrate, throwaway_database
from tests.fakes import FakeEmbedder, FakeLLM, chat
from tests.test_lessons import library  # noqa: F401 - the shared two-topic library fixture
from tests.test_onboarding import LEAF_QUESTION, OFF_TOPIC, SALT, answer_json
from tests.test_simulation import Demo, make_demo
from tests.test_telegram import _null_context

WEB_TOKEN = "synthetic-web-token-for-tests"
AUTH = {"Authorization": f"Bearer {WEB_TOKEN}"}


def web_settings(database_url: str, **overrides) -> Settings:
    return Settings(_env_file=None, database_url=database_url, hash_salt=SALT, web_enabled=True, web_token=WEB_TOKEN,
                    **overrides)


@dataclass
class Sim:
    client: TestClient
    demo: Demo
    llm: FakeLLM
    goal: Goal

    def get(self, path: str, **params) -> dict:
        response = self.client.get(path, params=params, headers=AUTH)
        assert response.status_code == 200, response.text
        return response.json()

    def post(self, path: str, body: dict, status: int = 200) -> dict:
        response = self.client.post(path, json=body, headers=AUTH)
        assert response.status_code == status, response.text
        return response.json()

    def admin(self, command: str, argument: str = "") -> str:
        return "\n".join(reply["text"] for reply in self.post("/web/admin", {"command": command, "argument": argument})["replies"])

    def control(self, learner_id: UUID, control: bool = True) -> dict:
        return self.post("/web/control", {"learner_id": str(learner_id), "control": control})

    def deliver(self) -> int:
        """One pass of the scheduler's delivery job, through the app's own adapter registry."""
        state = self.client.app.state
        return self.client.portal.call(outbox.deliver_pending, state.sessions, state.adapters, state.clock)

    def persona_id(self, persona: personas.Persona) -> UUID:
        with self.demo.sessions() as session:
            return session.scalar(select(Learner.id).where(
                Learner.channel_user_hash == personas.persona_hash(self.goal, persona, SALT)))


@dataclass(eq=False)
class Pane:
    """One phone-style pane: a new learner (guest key) until onboarded, then a learner ID."""
    sim: Sim
    learner_id: str | None = None
    guest: str | None = field(default_factory=lambda: uuid4().hex)
    since: int = 0

    @classmethod
    def speaking_for(cls, sim: Sim, learner_id: UUID) -> "Pane":
        return cls(sim, str(learner_id), None)

    def _post(self, status: int = 200, **body) -> list[dict]:
        who = {"learner_id": self.learner_id} if self.learner_id else {"guest": self.guest}
        data = self.sim.post("/web/message", {**who, **body}, status)
        if status != 200:
            return data
        self.learner_id = data["learner_id"]
        return data["replies"]

    def send(self, text: str, status: int = 200) -> list[dict]:
        return self._post(status, text=text)

    def tap(self, payload: str) -> list[dict]:
        return self._post(payload=payload)

    def inbox(self) -> list[dict]:
        """Deliver whatever the outbox holds, then poll as the page does every two seconds."""
        self.sim.deliver()
        data = self.sim.get("/web/poll", learner_id=self.learner_id, since=self.since)
        self.since = data["since"]
        return data["messages"]

    def onboard(self, name: str, goal_id: UUID) -> list[dict]:
        self.send("/start")
        self.tap(onboarding.AGREE)
        self.tap(f"{onboarding.CHOOSE_LANGUAGE}en")
        self.send(name)
        return self.tap(f"{onboarding.CHOOSE_GOAL}{goal_id}")


def payload(message: dict, prefix: str) -> str:
    return next(button["payload"] for button in message["buttons"] if button["payload"].startswith(prefix))


def lesson_post(messages: list[dict]) -> dict:
    return next(message for message in messages if any(button["payload"].startswith(lessons.START_QUESTIONS)
                                                        for button in message["buttons"]))


def answer_lesson(pane: Pane, lesson_message: dict, picks: dict[int, str] | None = None) -> list[str]:
    """Answer every question offered (main questions by position, follow-ups with "A"); returns the reply texts."""
    picks = picks or {1: "B", 2: "C"}  # the pre-generated lesson's correct options
    texts, replies = [], pane.tap(payload(lesson_message, lessons.START_QUESTIONS))
    while (question := next((reply for reply in replies
                             if any(b["payload"].startswith(lessons.ANSWER) for b in reply["buttons"])), None)):
        question_id = UUID(question["buttons"][0]["payload"].removeprefix(lessons.ANSWER).rsplit(":", 1)[0])
        with pane.sim.demo.sessions() as session:
            row = session.get(Question, question_id)
            option = "A" if row.parent_question_id is not None else picks[row.position]
        replies = pane.tap(next(b["payload"] for b in question["buttons"] if b["payload"].endswith(f":{option}")))
        texts.extend(reply["text"] for reply in replies)
    return texts


def answer_today(pane: Pane, picks: dict[int, str] | None = None) -> list[str]:
    return answer_lesson(pane, lesson_post(pane.send("/lesson")), picks)


def checkin_days(sessions, circle_id: UUID, learner_id: UUID) -> set:
    with sessions() as session:
        return {virtual_day(moment) for moment in session.scalars(
            select(Answer.answered_at).join(Question, Question.id == Answer.question_id)
            .where(Answer.learner_id == learner_id))}


@pytest.fixture
def sim(database_url, engine, sessions, clock, make_client, library):  # noqa: F811
    with sessions() as session:
        goal = session.get(Goal, library.goal_id)
        session.expunge(goal)
    demo = make_demo(sessions, clock, make_client, library, onboard_admin=False, goal_code=goal.code)
    llm = FakeLLM()  # QA answers are scripted per test; lessons are the pre-generated ones
    demo.conversation.qa = QAService(FakeEmbedder(), make_client(llm), clock, min_score=0.3)

    def wire(application):  # the synthetic library's demo engine and conversation, instead of the real corpus
        application.state.demo = demo.engine
        application.state.conversation = demo.conversation
        return _null_context()

    with TestClient(create_app(web_settings(database_url), services=[wire])) as client:
        yield Sim(client, demo, llm, goal)


# Access


def test_the_simulator_is_off_unless_enabled(database_url, engine):
    with TestClient(create_app(Settings(_env_file=None, database_url=database_url, hash_salt=SALT))) as client:
        assert client.get("/sim").status_code == 404
        assert client.get("/web/learners", headers=AUTH).status_code == 404
        assert client.app.state.adapters.channels == frozenset()
    for token in ("", "too-short"):
        with pytest.raises(ValidationError, match="WEB_TOKEN"):
            Settings(_env_file=None, hash_salt=SALT, web_enabled=True, web_token=token)


def test_every_web_request_needs_the_token_but_the_page_does_not(sim):
    page = sim.client.get("/sim")
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert "const PANES = 3;" in page.text and "const POLL_MS = 2000;" in page.text
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert WEB_TOKEN not in page.text
    with sim.demo.sessions() as session:
        offset = session.get(ClockState, 1).offset_seconds
    learner = str(uuid4())
    requests = [("GET", "/web/learners", None), ("GET", f"/web/poll?learner_id={learner}", None),
                ("POST", "/web/message", {"guest": uuid4().hex, "text": "/start"}),
                ("POST", "/web/control", {"learner_id": learner, "control": True}),
                ("POST", "/web/admin", {"command": "simulate_day"})]
    for headers in ({}, {"Authorization": "Bearer wrong-token-of-some-length"}, {"Authorization": WEB_TOKEN}):
        for method, path, body in requests:
            assert sim.client.request(method, path, json=body, headers=headers).status_code == 401
    with sim.demo.sessions() as session:  # nothing ran: the clock did not move
        assert session.get(ClockState, 1).offset_seconds == offset


def test_requests_are_validated(sim):
    for body in ({"text": "hi"}, {"guest": uuid4().hex}, {"guest": uuid4().hex, "text": "a", "payload": "b"},
                 {"guest": "not-hex", "text": "hi"}, {"guest": uuid4().hex, "learner_id": str(uuid4()), "text": "hi"},
                 {"guest": uuid4().hex, "text": "x" * 4097}, {"guest": uuid4().hex, "payload": "p" * 65}):
        assert sim.client.post("/web/message", json=body, headers=AUTH).status_code == 422
    for body in ({"command": "delete_everything"}, {"payload": "acct:del:yes"}, {}):
        assert sim.client.post("/web/admin", json=body, headers=AUTH).status_code == 422
    assert sim.client.get("/web/poll", params={"learner_id": str(uuid4()), "since": -1}, headers=AUTH).status_code == 422


# The completion check


def test_three_panes_drive_the_same_circle_through_the_engagement_demo(sim, library):  # noqa: F811
    sessions = sim.demo.sessions
    assert sim.client.app.state.adapters.channels == {Channel.WEB}  # no Telegram anywhere
    seeded = sim.admin("seed_demo")
    assert "with Chidi, Halima, Tunde." in seeded and t("admin.join_first") not in seeded
    halima_id, tunde_id, chidi_id = (sim.persona_id(p) for p in (personas.HALIMA, personas.TUNDE, personas.CHIDI))

    # Pane 1 onboards a new learner exactly as on Telegram; panes 2 and 3 take control of Halima and Tunde.
    amaka = Pane(sim)
    assert [reply["text"] for reply in amaka.send("/start")] == [t("onboarding.intro"), t("onboarding.consent")]
    joined = amaka.onboard("Amaka", library.goal_id)
    assert joined[0]["text"].startswith("You're in, Amaka!") and "Chidi, Halima, Tunde, Amaka" in joined[1]["text"]
    assert amaka.learner_id is not None and amaka.guest is not None
    # Admin commands belong to the console: typed in a pane they are refused, as for any Telegram learner.
    assert [reply["text"] for reply in amaka.send("/simulate_day")] == [t("admin.forbidden")]
    halima, tunde = Pane.speaking_for(sim, halima_id), Pane.speaking_for(sim, tunde_id)
    assert "Take control" in halima.send("/lesson", status=409)["detail"]
    assert sim.control(halima_id) == {"id": str(halima_id), "first_name": "Halima", "language": "pcm",
                                      "persona": True, "controlled": True}
    sim.control(tunde_id)
    listed = {item["id"]: item for item in sim.get("/web/learners")["learners"]}
    assert listed[str(halima_id)]["controlled"] and listed[str(tunde_id)]["controlled"]
    assert not listed[str(chidi_id)]["controlled"] and listed[amaka.learner_id]["persona"] is False

    # Day 1: only Chidi acts on his own. Every pane gets the lesson post and its circle update.
    assert "Checked in: Chidi\n" in sim.admin("simulate_day")
    for pane in (amaka, halima, tunde):
        messages = pane.inbox()
        assert lesson_post(messages)["text"].count("Day 1: Photosynthesis") == 1
        assert messages[-1]["text"].startswith("🔵 Circle update") and "Chidi ✅" in messages[-1]["text"]
    assert halima.inbox() == []  # polling again returns nothing new

    # Each pane answers for its learner. A wrong answer gets the private follow-up.
    wrong = answer_today(halima, {1: "A", 2: "C"})
    assert wrong[0].startswith(t("question.wrong", option="B", answer="Chlorophyll").split("\n")[0])
    answer_today(tunde)
    answer_today(amaka)
    # A cited answer and an off-topic question that goes to the circle; the LLM never sees a name.
    sim.llm.responses.extend([chat(answer_json("C1")), chat("yes")])
    (cited,) = amaka.send(LEAF_QUESTION)
    assert "Source: Synthetic Book" in cited["text"]
    (fallback,) = amaka.send(OFF_TOPIC)
    assert fallback["text"] == t("qa.fallback")
    assert amaka.tap(payload(fallback, qa.ASK_CIRCLE))[0]["text"] == t("qa.posted_to_circle")
    assert all("Amaka" not in json.dumps(request) for request in sim.llm.requests)

    # Day 2: the circle sees who answered, and Halima's pane receives Amaka's circle question.
    sim.admin("simulate_day")
    update = halima.inbox()[-1]["text"]
    assert f"❓ Amaka asked the circle: {OFF_TOPIC}" in update
    assert f"❓ Amaka asked the circle" not in amaka.inbox()[-1]["text"]  # never your own event
    for pane in (amaka, halima, tunde):
        answer_today(pane)
    # Days 3 and 4: Tunde's pane stays silent. Day 5 detects him, exactly three days after his last check-in.
    for _ in range(2):
        sim.admin("simulate_day")
        for pane in (amaka, halima):
            answer_today(pane)
    report = sim.admin("simulate_day")
    assert "Gone quiet: Tunde" in report
    prompts = {pane: pane.inbox()[-1] for pane in (amaka, halima, tunde)}
    button = t("nudge.button", name="Tunde")
    assert [b["text"] for b in prompts[amaka]["buttons"]].count(button) == 1
    assert button in [b["text"] for b in prompts[halima]["buttons"]]
    assert t("nudge.prompt", name="Tunde") not in prompts[tunde]["text"]  # never prompted about yourself

    # Amaka nudges first; Halima's tap is refused. Tunde's pane gets exactly one nudge, at once.
    nudge = next(b["payload"] for b in prompts[amaka]["buttons"] if b["text"] == button)
    assert amaka.tap(nudge)[0]["text"] == t("nudge.sent", name="Tunde")
    assert halima.tap(next(b["payload"] for b in prompts[halima]["buttons"] if b["text"] == button))[0]["text"] == \
        t("nudge.already_sent", name="Tunde")
    (received,) = tunde.inbox()
    assert received["text"] == t("nudge.message", Language.YO, name="Amaka")
    # Tunde comes back: "Yes, send it" sends today's lesson, and answering it is his check-in.
    lesson_reply = lesson_post(tunde.tap(payload(received, lessons.TODAY)))
    answer_lesson(tunde, lesson_reply)
    today = virtual_day(sim.demo.clock.now())
    assert today in checkin_days(sessions, sim.demo.circle_id, tunde_id)
    # While controlled, the nudge does not make the simulation check Tunde in on its own the next day.
    report = sim.admin("simulate_day")
    assert "Tunde" not in report.split("Checked in: ")[1].split("\n")[0]
    assert virtual_day(sim.demo.clock.now()) not in checkin_days(sessions, sim.demo.circle_id, tunde_id)

    # Day 7 pays out the week to every pane: progress, circle summary, opportunity and the pack.
    report = sim.admin("simulate_week")
    assert report.endswith(t("admin.day_payout", week=1, count=3))
    for pane in (amaka, halima, tunde):
        messages = pane.inbox()
        assert any(message["text"].startswith(t("payout.intro", week=1)) for message in messages)
        (pack,) = [message["file"] for message in messages if message["file"]]
        assert pack["filename"].endswith(".txt") and len(base64.b64decode(pack["data"])) < 500_000

    with sessions() as session:
        for learner_id in (halima_id, tunde_id, chidi_id):
            persona = session.get(Learner, learner_id)
            assert persona.is_simulated and persona.channel_chat_id is None
        # Chidi was never controlled, so he was never queued anything.
        assert session.scalar(select(func.count()).select_from(OutboundDelivery)
                              .where(OutboundDelivery.learner_id == chidi_id)) == 0


# Persona control


def test_controlling_a_persona_pauses_it_and_releasing_hands_it_back(sim):
    sessions, circle_id = sim.demo.sessions, sim.demo.circle_id
    sim.admin("seed_demo")
    chidi_id = sim.persona_id(personas.CHIDI)
    sim.admin("simulate_day")
    assert len(checkin_days(sessions, circle_id, chidi_id)) == 1
    sim.control(chidi_id)
    sim.control(chidi_id)  # taking control twice changes nothing
    report = sim.admin("simulate_day")
    assert "Checked in: Halima, Tunde\n" in report
    assert len(checkin_days(sessions, circle_id, chidi_id)) == 1
    chidi = Pane.speaking_for(sim, chidi_id)
    assert lesson_post(chidi.inbox())["text"].startswith(t("lesson.header", day=2, topic="Excretion"))

    assert sim.control(chidi_id, False)["controlled"] is False
    assert chidi.send("/lesson", status=409)
    assert "Checked in: Chidi, Halima" in sim.admin("simulate_day")
    assert len(checkin_days(sessions, circle_id, chidi_id)) == 2
    with sessions() as session:  # nothing is queued for a persona once released
        before = session.scalar(select(func.count()).select_from(OutboundDelivery)
                                .where(OutboundDelivery.learner_id == chidi_id))
    sim.admin("simulate_day")
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(OutboundDelivery)
                              .where(OutboundDelivery.learner_id == chidi_id)) == before


def test_a_controlled_persona_starts_from_todays_feed_not_its_whole_history(sim, library):  # noqa: F811
    sim.admin("seed_demo")
    asker = Pane(sim)
    asker.onboard("Bola", library.goal_id)
    (fallback,) = asker.send(OFF_TOPIC)
    asker.tap(payload(fallback, qa.ASK_CIRCLE))
    sim.admin("simulate_day")
    halima_id = sim.persona_id(personas.HALIMA)
    sim.control(halima_id)
    sim.admin("simulate_day")
    update = Pane.speaking_for(sim, halima_id).inbox()[-1]["text"]
    assert update.startswith("🔵 Circle update") and "Bola asked the circle" not in update


def test_panes_can_drive_only_web_learners(sim, library):  # noqa: F811
    sim.admin("seed_demo")
    telegram = sim.demo.user("Kemi")  # a Telegram learner in the same circle
    with sim.demo.sessions() as session:
        telegram_id = session.scalar(select(Learner.id).where(Learner.channel_user_hash == telegram.hash))
        evaluator = Learner(channel=Channel.WEB, channel_user_hash=uuid4().hex, first_name="Evaluator",
                            goal_id=library.goal_id, is_simulated=True, created_at=sim.demo.clock.now(session))
        session.add(evaluator)
        session.commit()
        evaluator_id = evaluator.id
    for learner_id in (telegram_id, uuid4()):
        assert Pane.speaking_for(sim, learner_id).send("/lesson", status=404)
        assert sim.client.get("/web/poll", params={"learner_id": str(learner_id)}, headers=AUTH).status_code == 404
        assert sim.client.post("/web/control", json={"learner_id": str(learner_id), "control": True},
                               headers=AUTH).status_code == 404
    # A simulated learner that is not a demo persona can't be controlled, and isn't listed.
    assert sim.client.post("/web/control", json={"learner_id": str(evaluator_id), "control": True},
                           headers=AUTH).status_code == 404
    listed = {item["id"] for item in sim.get("/web/learners")["learners"]}
    assert str(telegram_id) not in listed and str(evaluator_id) not in listed
    assert str(sim.persona_id(personas.TUNDE)) in listed


def test_starting_without_the_simulator_hands_every_persona_back(sim, database_url):
    sim.admin("seed_demo")
    tunde_id = sim.persona_id(personas.TUNDE)
    sim.control(tunde_id)
    with TestClient(create_app(Settings(_env_file=None, database_url=database_url, hash_salt=SALT))):
        pass
    with sim.demo.sessions() as session:
        assert session.get(Learner, tunde_id).controlled_at is None
    assert "Tunde" in sim.admin("simulate_day").split("\n")[1]  # he checks in on his own again


def test_the_check_constraint_keeps_control_for_personas(sim):
    with sim.demo.sessions() as session:
        session.add(Learner(channel=Channel.WEB, channel_user_hash=uuid4().hex, channel_chat_id="sim:x",
                            first_name="Real", is_simulated=False, controlled_at=sim.demo.clock.now(session),
                            created_at=sim.demo.clock.now(session)))
        with pytest.raises(sa.exc.IntegrityError):
            session.commit()
    with sim.demo.sessions() as session:
        with pytest.raises(ValueError):
            personas.take_control(session, Learner(is_simulated=False), sim.demo.clock.now(session))


# Delivery, privacy and reset


def test_a_web_learner_is_reachable_again_from_the_list_and_deletion_removes_their_inbox(sim, library):  # noqa: F811
    sim.admin("seed_demo")
    bola, carol = Pane(sim), Pane(sim)
    bola.onboard("Bola", library.goal_id)
    carol.onboard("Carol", library.goal_id)
    sim.admin("simulate_day")
    assert bola.inbox() and carol.inbox()
    # Another page picks Bola from the list and carries on as her.
    again = Pane.speaking_for(sim, UUID(bola.learner_id))
    assert again.send("/start")[0]["text"] == t("onboarding.already_done", name="Bola")
    (fallback,) = again.send(OFF_TOPIC)
    again.tap(payload(fallback, qa.ASK_CIRCLE))
    sim.admin("simulate_day")
    assert "Bola asked the circle" in carol.inbox()[-1]["text"]

    bola_id = UUID(bola.learner_id)
    bola.send("/delete_me")
    assert bola.tap(account.DELETE_YES)[0]["text"] == t("delete.done")
    assert bola.learner_id is None
    with sim.demo.sessions() as session:
        assert session.scalar(select(func.count()).select_from(WebMessage).where(WebMessage.learner_id == bola_id)) == 0
        # Carol's delivered updates that named Bola are gone too; the rest of her inbox stays.
        remaining = [row.payload["text"] for row in session.scalars(
            select(WebMessage).where(WebMessage.learner_id == UUID(carol.learner_id)))]
    assert remaining and not any("Bola" in text for text in remaining)
    assert sim.client.get("/web/poll", params={"learner_id": str(bola_id)}, headers=AUTH).status_code == 404


def test_reset_demo_from_the_console_clears_the_panes_inboxes_and_personas(sim, library):  # noqa: F811
    sim.admin("seed_demo")
    amaka = Pane(sim)
    amaka.onboard("Amaka", library.goal_id)
    tunde_id = sim.persona_id(personas.TUNDE)
    sim.control(tunde_id)
    sim.admin("simulate_day")
    assert amaka.inbox() and Pane.speaking_for(sim, tunde_id).inbox()
    (confirm,) = sim.post("/web/admin", {"command": "reset_demo"})["replies"]
    reset = next(button["payload"] for button in confirm["buttons"] if button["text"] == t("admin.reset_yes"))
    assert sim.post("/web/admin", {"payload": reset})["replies"][0]["text"] == t("admin.reset_done")
    with sim.demo.sessions() as session:
        assert session.get(Learner, tunde_id) is None
        assert session.scalar(select(func.count()).select_from(WebMessage)
                              .where(WebMessage.learner_id == UUID(amaka.learner_id))) == 0
    assert str(tunde_id) not in {item["id"] for item in sim.get("/web/learners")["learners"]}
    assert sim.admin("demo_status").startswith("🧪 Demo status")


def test_the_web_adapter_stores_text_buttons_files_and_voice_in_order(sessions, clock):
    with sessions.begin() as session:
        learner = Learner(channel=Channel.WEB, channel_user_hash=uuid4().hex, channel_chat_id=f"sim:{uuid4().hex}",
                          first_name="Ada", created_at=clock.now(session))
        session.add(learner)
    registry = AdapterRegistry()
    registry.register(WebAdapter(sessions, clock))
    asyncio.run(registry.send_text(learner, "Hello", [Button("Go", "ls:today")]))
    asyncio.run(registry.send_file(learner, "pack.txt", "Ọ̀rọ̀".encode()))
    asyncio.run(registry.send_voice(learner, b"OggS"))
    with sessions() as session:
        rows = session.scalars(select(WebMessage).where(WebMessage.learner_id == learner.id)
                               .order_by(WebMessage.id)).all()
    assert [row.payload for row in rows] == [
        {"text": "Hello", "buttons": [{"text": "Go", "payload": "ls:today"}], "voice": None, "file": None},
        {"text": "", "buttons": [], "voice": None,
         "file": {"filename": "pack.txt", "data": base64.b64encode("Ọ̀rọ̀".encode()).decode()}},
        {"text": "", "buttons": [], "voice": {"filename": "voice.ogg", "data": base64.b64encode(b"OggS").decode()},
         "file": None},
    ]
    assert rows[0].id < rows[1].id < rows[2].id
    assert message_json(Reply("x", file=Attachment("a.txt", b"1")))["file"] == {"filename": "a.txt", "data": "MQ=="}


def test_poll_returns_only_messages_after_since(sim, library):  # noqa: F811
    sim.admin("seed_demo")
    pane = Pane(sim)
    pane.onboard("Bola", library.goal_id)
    sim.admin("simulate_day")
    sim.deliver()
    first = sim.get("/web/poll", learner_id=pane.learner_id)
    assert len(first["messages"]) == 2 and first["since"] == first["messages"][-1]["id"]
    assert sim.get("/web/poll", learner_id=pane.learner_id, since=first["messages"][0]["id"])["messages"] == \
        first["messages"][1:]
    empty = sim.get("/web/poll", learner_id=pane.learner_id, since=first["since"])
    assert empty == {"messages": [], "since": first["since"]}


def test_a_failure_gets_the_generic_notice(sim, caplog):
    sim.client.app.state.conversation.handle = lambda inbound: 1 / 0
    (reply,) = Pane(sim).send("/start")
    assert reply["text"] == t("error.generic") and "ZeroDivisionError" in caplog.text


@pytest.mark.parametrize("dialect", ["sqlite", "postgres"])
def test_upgrading_leaves_every_persona_automated(dialect, tmp_path):
    with throwaway_database(dialect, tmp_path) as url:
        engine = build_engine(url)
        try:
            migrate(engine, "0008")
            with engine.begin() as connection:
                connection.execute(sa.text(
                    "INSERT INTO learners (id, channel, channel_user_hash, first_name, language, is_simulated, "
                    "share_scores, difficulty, created_at) VALUES (:id, 'web', :hash, 'Tunde', 'yo', true, false, 2, "
                    ":now)"), {"id": uuid4().hex if dialect == "sqlite" else str(uuid4()), "hash": uuid4().hex,
                               "now": "2026-10-14 08:00:00+00:00"})
            migrate(engine)
            with engine.connect() as connection:
                assert connection.execute(sa.text("SELECT controlled_at FROM learners")).scalar_one() is None
                assert connection.execute(sa.text("SELECT count(*) FROM web_messages")).scalar_one() == 0
        finally:
            engine.dispose()

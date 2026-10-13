"""The Telegram adapter, end to end through python-telegram-bot's real polling loop and a fake Bot API."""
import asyncio
import json
import queue
import threading
import time
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from telegram import Update
from telegram.request import BaseRequest

from app.adapters.base import AdapterRegistry, Button
from app.adapters.telegram_bot import TelegramAdapter, inbound_from_update, keyboard, telegram_polling
from app.config import Settings
from app.core import account, onboarding
from app.core.i18n import t
from app.core.privacy import channel_hash
from app.core.qa import QAService
from app.main import create_app
from app.models import Channel, Learner, QALog
from tests.fakes import FakeEmbedder, FakeLLM, add_goal_with_chunks, chat

SALT = "synthetic-test-salt-with-32-characters"
TOKEN = "123456:synthetic-test-token"
LEAF = "Leaves use chlorophyll to capture light. Leaf cells turn carbon dioxide and water into sugar and glucose."
USER = {"id": 7012345678, "is_bot": False, "first_name": "Synthetic"}
PRIVATE = {"id": 7012345678, "type": "private"}


def message_update(update_id: int, text: str | None, *, chat: dict = PRIVATE, sender: dict = USER) -> dict:
    message = {"message_id": update_id, "date": 0, "chat": chat, "from": sender}
    if text is not None:
        message["text"] = text
    else:
        message["sticker"] = {"file_id": "s", "file_unique_id": "s", "width": 1, "height": 1, "is_animated": False,
                              "is_video": False, "type": "regular"}
    return {"update_id": update_id, "message": message}


def callback_update(update_id: int, data: str, *, chat: dict = PRIVATE) -> dict:
    return {"update_id": update_id, "callback_query": {
        "id": f"cb{update_id}", "from": USER, "chat_instance": "ci", "data": data,
        "message": {"message_id": 1, "date": 0, "chat": chat, "text": "menu"}}}


# Translation between Telegram and the core


def test_keyboard_puts_each_button_on_its_own_row():
    assert keyboard(None) is None and keyboard([]) is None
    markup = keyboard([Button("English", "ob:lang:en"), Button("Yorùbá", "ob:lang:yo")])
    assert [[(button.text, button.callback_data) for button in row] for row in markup.inline_keyboard] == [
        [("English", "ob:lang:en")], [("Yorùbá", "ob:lang:yo")]]


def test_private_messages_and_taps_become_inbound():
    inbound = inbound_from_update(Update.de_json(message_update(1, "/start")))
    assert (inbound.channel, inbound.user_id, inbound.chat_id, inbound.text, inbound.payload) == (
        Channel.TELEGRAM, "7012345678", "7012345678", "/start", None)
    inbound = inbound_from_update(Update.de_json(callback_update(2, "ob:agree")))
    assert inbound.payload == "ob:agree" and inbound.text is None
    assert inbound_from_update(Update.de_json(message_update(3, None))).text is None  # non-text: core says so


@pytest.mark.parametrize("update", [
    message_update(1, "/start", chat={"id": -100123, "type": "supergroup", "title": "Group"}),
    message_update(2, "hi", sender={"id": 99, "is_bot": True, "first_name": "Other bot"}),
    callback_update(3, "ob:agree", chat={"id": -100123, "type": "group", "title": "Group"}),
])
def test_group_chats_and_bots_are_ignored(update):
    assert inbound_from_update(Update.de_json(update)) is None


def test_adapter_delivers_through_the_registry():
    bot = AsyncMock()
    adapter = TelegramAdapter(bot)
    registry = AdapterRegistry()
    registry.register(adapter)
    learner = Learner(channel=Channel.TELEGRAM, channel_chat_id="7012345678", is_simulated=False)
    asyncio.run(registry.send_text(learner, "Hello", [Button("Yes", "y")]))
    asyncio.run(registry.send_voice(learner, b"ogg"))
    asyncio.run(registry.send_file(learner, "pack.txt", b"data"))
    sent = bot.send_message.await_args.kwargs
    assert sent["chat_id"] == "7012345678" and sent["text"] == "Hello"
    assert sent["reply_markup"].inline_keyboard[0][0].callback_data == "y"
    bot.send_voice.assert_awaited_once_with(chat_id="7012345678", voice=b"ogg")
    bot.send_document.assert_awaited_once_with(chat_id="7012345678", document=b"data", filename="pack.txt")
    asyncio.run(registry.send_text(Learner(channel=Channel.TELEGRAM, is_simulated=True), "Skipped"))
    assert bot.send_message.await_count == 1
    with pytest.raises(LookupError):
        asyncio.run(adapter.send_text(Learner(channel=Channel.TELEGRAM, is_simulated=False), "No chat"))


# End to end


class FakeBotAPI:
    """Just enough of the Bot API for python-telegram-bot to poll, reply and answer button taps."""

    def __init__(self) -> None:
        self.updates: queue.Queue[dict] = queue.Queue()
        self.calls: list[tuple[str, dict]] = []
        self.sent: list[dict] = []
        self.failing: set[str] = set()  # methods answered with a Bot API error
        self._condition = threading.Condition()

    def push(self, update: dict) -> None:
        self.updates.put(update)

    def wait_for_messages(self, count: int, timeout: float = 15) -> list[dict]:
        deadline = time.monotonic() + timeout
        with self._condition:
            while len(self.sent) < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(f"expected {count} messages, got {[m['text'] for m in self.sent]}")
                self._condition.wait(remaining)
            return list(self.sent)

    async def respond(self, method: str, parameters: dict) -> object:
        self.calls.append((method, parameters))
        if method == "getMe":
            return {"id": 4242, "is_bot": True, "first_name": "Ajo", "username": "ajo_test_bot"}
        if method == "getUpdates":
            batch = []
            while not self.updates.empty():
                batch.append(self.updates.get_nowait())
            if not batch:
                await asyncio.sleep(0.01)
            return batch
        if method == "sendMessage":
            with self._condition:
                self.sent.append(parameters)
                self._condition.notify_all()
            return {"message_id": len(self.sent), "date": 0, "chat": {"id": int(parameters["chat_id"]),
                                                                      "type": "private"}, "text": parameters["text"]}
        return True  # deleteWebhook, answerCallbackQuery, sendChatAction


class FakeRequest(BaseRequest):
    def __init__(self, api: FakeBotAPI) -> None:
        self.api = api

    @property
    def read_timeout(self) -> float | None:
        return None

    async def initialize(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    async def do_request(self, url, method, request_data=None, read_timeout=None, write_timeout=None,
                         connect_timeout=None, pool_timeout=None) -> tuple[int, bytes]:
        parameters = json.loads(json.dumps(request_data.parameters if request_data else {}, default=str))
        method = url.rsplit("/", 1)[-1]
        if method in self.api.failing:
            self.api.calls.append((method, parameters))
            return 400, json.dumps({"ok": False, "error_code": 400,
                                    "description": "Bad Request: query is too old"}).encode()
        result = await self.api.respond(method, parameters)
        return 200, json.dumps({"ok": True, "result": result}).encode()


def test_telegram_account_onboards_asks_and_deletes(database_url, engine, sessions, clock, make_client):
    with sessions.begin() as session:
        goal, _ = add_goal_with_chunks(session, [LEAF])
        goal_id = goal.id
    answer = json.dumps({"answerable": True, "answer": "Leaves capture light with chlorophyll and make sugar.",
                         "cited_chunk_ids": ["C1"]})
    llm = make_client(FakeLLM(chat(answer), chat("yes")))
    api = FakeBotAPI()

    def fake_qa(application):  # runs before Telegram starts: no model download, scripted LLM
        application.state.conversation.qa = QAService(FakeEmbedder(), llm, application.state.clock, min_score=0.3)
        return _null_context()

    settings = Settings(_env_file=None, database_url=database_url, hash_salt=SALT, telegram_enabled=True,
                        telegram_bot_token=TOKEN)
    app = create_app(settings, services=[fake_qa], telegram=lambda application: telegram_polling(
        application, request=FakeRequest(api), get_updates_request=FakeRequest(api)))
    steps = [
        (message_update(1, "/start"), 2),
        (callback_update(2, onboarding.AGREE), 1),
        (callback_update(3, "ob:lang:en"), 1),
        (message_update(4, "Amaka"), 1),
        (callback_update(5, f"{onboarding.CHOOSE_GOAL}{goal_id}"), 3),
        (message_update(6, "How do leaf cells use chlorophyll and light to make sugar?"), 1),
        (message_update(7, "Who won the Super Eagles match?"), 1),
        (message_update(8, None), 1),
        (message_update(9, "/delete_me"), 1),
        (callback_update(10, account.DELETE_YES), 1),
    ]
    with TestClient(app) as client:
        assert client.app.state.adapters.get(Channel.TELEGRAM) is not None
        expected = 0
        for update, replies in steps:
            api.push(update)
            expected += replies
            sent = api.wait_for_messages(expected)
            if update["update_id"] == 6:
                with sessions() as session:  # onboarded before asking
                    learner = session.scalar(select(Learner).where(
                        Learner.channel_user_hash == channel_hash(Channel.TELEGRAM, str(USER["id"]), SALT)))
                    assert learner.first_name == "Amaka" and learner.difficulty == 2
                    learner_id = learner.id
    texts = [message["text"] for message in sent]
    assert texts[0] == t("onboarding.intro")
    assert json.loads(json.dumps(sent[1]["reply_markup"]))["inline_keyboard"][0][0]["callback_data"] == "ob:agree"
    assert texts[5].startswith("You're in, Amaka!") and texts[6].startswith("🔵 Welcome to")
    assert "Source: Synthetic Book" in texts[8]  # cited answer
    assert texts[9] == t("qa.fallback")  # off-topic fallback
    assert texts[10] == t("message.text_only")
    assert texts[12] == t("delete.done")
    assert all(message["chat_id"] in (USER["id"], str(USER["id"])) for message in sent)
    methods = [method for method, _ in api.calls]
    assert methods.count("answerCallbackQuery") == 4 and "sendChatAction" in methods
    with sessions() as session:
        assert session.scalar(select(Learner).where(
            Learner.channel_user_hash == channel_hash(Channel.TELEGRAM, str(USER["id"]), SALT))) is None
        assert session.get(Learner, learner_id) is None
        assert session.scalar(select(QALog).where(QALog.learner_id == learner_id)) is None


def test_failures_reply_with_a_notice_and_log_no_identifiers(database_url, engine, caplog):
    api = FakeBotAPI()
    api.failing.add("answerCallbackQuery")
    settings = Settings(_env_file=None, database_url=database_url, hash_salt=SALT, telegram_enabled=True,
                        telegram_bot_token=TOKEN)
    app = create_app(settings, telegram=lambda application: telegram_polling(
        application, request=FakeRequest(api), get_updates_request=FakeRequest(api)))
    with TestClient(app) as client:
        api.push(callback_update(1, onboarding.AGREE))  # too old to acknowledge, still handled
        assert api.wait_for_messages(1)[0]["reply_markup"]
        client.app.state.conversation.handle = lambda inbound: 1 / 0
        api.push(message_update(2, "/start"))
        assert api.wait_for_messages(2)[1]["text"] == t("error.generic")
    assert "ZeroDivisionError" in caplog.text
    assert str(USER["id"]) not in caplog.text


def test_telegram_stays_off_unless_enabled(database_url, engine):
    started = []
    settings = Settings(_env_file=None, database_url=database_url, hash_salt=SALT)
    with TestClient(create_app(settings, telegram=lambda application: started.append(1))) as client:
        with pytest.raises(LookupError):
            client.app.state.adapters.get(Channel.TELEGRAM)
    assert started == []


class _null_context:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False

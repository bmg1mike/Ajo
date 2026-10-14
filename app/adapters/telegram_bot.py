"""Telegram adapter: long polling in, Bot API messages out (spec section 10.2).

Thin by design. Updates from private chats become `Inbound` messages for the core
conversation; its replies go back as text with inline buttons, voice notes and documents. Core
code reaches learners through `AdapterRegistry`, which routes to `TelegramAdapter`.

A voice note arrives as a `VoiceNote` whose `fetch` downloads the OGG/Opus file on the event
loop when the core decides to transcribe it, so nothing is downloaded for a sender who has not
onboarded or for a note that is too long.
"""
import asyncio
import logging
import math
import warnings
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update, Voice
from telegram.constants import ChatAction, ChatType
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from telegram.request import BaseRequest
from telegram.warnings import PTBDeprecationWarning

from app.adapters.base import Button
from app.core.conversation import Conversation, Inbound
from app.core.i18n import t
from app.core.replies import Reply
from app.core.voice import VoiceNote
from app.models import Channel, Learner

logger = logging.getLogger(__name__)

ALLOWED_UPDATES = ["message", "callback_query"]
MAX_MESSAGE_CHARS = 4096  # Bot API limit for one text message
DOWNLOAD_TIMEOUT_SECONDS = 60


def keyboard(buttons: list[Button] | None) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton(button.text, callback_data=button.payload)]
                                 for button in buttons])


def voice_note(voice: Voice, loop: asyncio.AbstractEventLoop) -> VoiceNote:
    """A voice note the conversation can download from its worker thread, on the bot's event loop."""

    async def download() -> bytes:
        file = await voice.get_file()
        return bytes(await file.download_as_bytearray())

    def fetch() -> bytes:
        return asyncio.run_coroutine_threadsafe(download(), loop).result(timeout=DOWNLOAD_TIMEOUT_SECONDS)

    with warnings.catch_warnings():  # an int today, a timedelta in later python-telegram-bot releases: both work
        warnings.simplefilter("ignore", PTBDeprecationWarning)
        duration = voice.duration
    seconds = math.ceil(duration.total_seconds() if isinstance(duration, timedelta) else duration)
    return VoiceNote(duration_seconds=seconds, fetch=fetch, size_bytes=voice.file_size)


def inbound_from_update(update: Update) -> Inbound | None:
    """Private-chat messages and button taps only: group chats would expose replies to others.

    Call it on the event loop: a voice note's download is scheduled there.
    """
    user, chat = update.effective_user, update.effective_chat
    if user is None or chat is None or user.is_bot or chat.type != ChatType.PRIVATE:
        return None
    base = dict(channel=Channel.TELEGRAM, user_id=str(user.id), chat_id=str(chat.id))
    if update.callback_query is not None:
        return Inbound(**base, payload=update.callback_query.data or "")
    if update.message is not None:
        if update.message.voice is not None:
            return Inbound(**base, voice=voice_note(update.message.voice, asyncio.get_running_loop()))
        return Inbound(**base, text=update.message.text)
    return None


class TelegramAdapter:
    channel = Channel.TELEGRAM

    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    async def reply(self, chat_id: str, reply: Reply) -> None:
        """The text with its buttons, then any voice note, then any file as a document."""
        if reply.text or reply.buttons:
            await self.bot.send_message(chat_id=chat_id, text=reply.text[:MAX_MESSAGE_CHARS],
                                        reply_markup=keyboard(reply.buttons))
        if reply.voice is not None:
            await self.bot.send_voice(chat_id=chat_id, voice=reply.voice)
        if reply.file is not None:
            await self.bot.send_document(chat_id=chat_id, document=reply.file.data, filename=reply.file.filename)

    async def send_text(self, learner: Learner, text: str, buttons: list[Button] | None = None) -> None:
        await self.reply(_chat_id(learner), Reply(text, buttons or []))

    async def send_voice(self, learner: Learner, ogg_bytes: bytes) -> None:
        await self.bot.send_voice(chat_id=_chat_id(learner), voice=ogg_bytes)

    async def send_file(self, learner: Learner, filename: str, data: bytes) -> None:
        await self.bot.send_document(chat_id=_chat_id(learner), document=data, filename=filename)


def _chat_id(learner: Learner) -> str:
    if not learner.channel_chat_id:
        raise LookupError("Learner has no Telegram chat ID")
    return learner.channel_chat_id


def build_application(token: str, conversation: Conversation, *, request: BaseRequest | None = None,
                      get_updates_request: BaseRequest | None = None) -> Application:
    builder = Application.builder().token(token)
    if request is not None:
        builder = builder.request(request)
    if get_updates_request is not None:
        builder = builder.get_updates_request(get_updates_request)
    bot_app = builder.build()
    adapter = TelegramAdapter(bot_app.bot)

    async def on_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        inbound = inbound_from_update(update)
        if inbound is None:
            return
        try:  # cosmetic: stop the button spinner, or show "typing" while QA runs
            if update.callback_query is not None:
                await update.callback_query.answer()
            elif inbound.text is not None or inbound.voice is not None:
                await context.bot.send_chat_action(chat_id=inbound.chat_id, action=ChatAction.TYPING)
        except TelegramError as error:  # e.g. a callback query too old to answer
            logger.info("Ignoring Telegram acknowledgement failure: %s", type(error).__name__)
        # Database and LLM work is synchronous; keep it off the event loop.
        replies = await asyncio.to_thread(conversation.handle, inbound)
        for reply in replies:
            await adapter.reply(inbound.chat_id, reply)

    async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        # Log the failure without the update, which holds raw user and chat IDs.
        logger.error("Telegram update failed: %r", context.error, exc_info=context.error)
        if isinstance(update, Update):
            inbound = inbound_from_update(update)
            if inbound is not None:
                try:
                    await adapter.reply(inbound.chat_id, Reply(t("error.generic")))
                except Exception:  # nothing more can be done for this update
                    logger.warning("Could not send the error notice")

    private = filters.ChatType.PRIVATE
    bot_app.add_handler(MessageHandler(private & filters.UpdateType.MESSAGE, on_update))
    bot_app.add_handler(CallbackQueryHandler(on_update))
    bot_app.add_error_handler(on_error)
    bot_app.bot_data["adapter"] = adapter
    return bot_app


def telegram_polling(application: FastAPI, *, request: BaseRequest | None = None,
                     get_updates_request: BaseRequest | None = None):
    """Background service: register the adapter, poll Telegram, and stop cleanly on shutdown."""

    @asynccontextmanager
    async def run() -> AsyncIterator[None]:
        settings = application.state.settings
        # httpx logs each request URL at INFO, and Bot API URLs contain the token.
        logging.getLogger("httpx").setLevel(max(logging.getLogger("httpx").level, logging.WARNING))
        bot_app = build_application(settings.telegram_bot_token.get_secret_value(), application.state.conversation,
                                    request=request, get_updates_request=get_updates_request)
        await bot_app.initialize()
        try:
            application.state.adapters.register(bot_app.bot_data["adapter"])
            try:
                await bot_app.start()
                try:
                    await bot_app.updater.start_polling(allowed_updates=ALLOWED_UPDATES)
                    try:
                        yield
                    finally:
                        await bot_app.updater.stop()
                finally:
                    await bot_app.stop()
            finally:
                # Queued deliveries wait for the next start instead of failing against a stopped bot.
                application.state.adapters.unregister(Channel.TELEGRAM)
        finally:
            await bot_app.shutdown()

    return run()

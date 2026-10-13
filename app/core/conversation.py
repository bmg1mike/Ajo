"""Channel-agnostic message routing. Adapters turn platform updates into `Inbound` and send back the replies.

Commands: /start, /lesson, /ask, /language, /share_scores, /delete_me and /help. Any other text from
an onboarded learner is a question for QA (spec section 7.4). Raw channel user IDs are hashed
on arrival and never stored.
"""
import re
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core import account, grading, lessons, onboarding, qa
from app.core.clock import Clock
from app.core.i18n import t
from app.core.privacy import channel_hash
from app.core.replies import Reply
from app.models import Channel, Language, Learner, Onboarding

# Spec section 10.2 commands from later milestones; until they exist they say so rather than act as questions.
COMING_SOON = frozenset({"progress", "pack"})

_COMMAND = re.compile(r"/([A-Za-z0-9_]{1,32})(?:@\w+)?(?:\s+(.*))?", re.DOTALL)


@dataclass(frozen=True)
class Inbound:
    channel: Channel
    user_id: str  # the platform's user ID, hashed immediately
    chat_id: str  # where replies go; stored on the learner for later deliveries
    text: str | None = None
    payload: str | None = None  # button callback data

    def __repr__(self) -> str:  # keep raw identifiers out of logs and tracebacks
        return f"Inbound(channel={self.channel.value!r}, text={self.text is not None}, payload={self.payload!r})"


def parse_command(text: str) -> tuple[str | None, str]:
    """("ask", "What is osmosis?") for "/ask What is osmosis?"; (None, text) for anything that isn't a command."""
    text = text.strip()
    match = _COMMAND.fullmatch(text)
    if match is None:
        return None, text
    return match.group(1).lower(), (match.group(2) or "").strip()


class Conversation:
    def __init__(self, sessions: sessionmaker[Session], clock: Clock, qa_service: qa.QAService,
                 lesson_service: lessons.LessonService, *, salt: str) -> None:
        self.sessions, self.clock, self.qa, self.lessons, self.salt = sessions, clock, qa_service, lesson_service, salt

    def handle(self, inbound: Inbound) -> list[Reply]:
        """Process one message or button tap in a single transaction. Blocks on LLM calls."""
        with self.sessions.begin() as session:
            return self._route(session, inbound)

    def _route(self, session: Session, inbound: Inbound) -> list[Reply]:
        user_hash = channel_hash(inbound.channel, inbound.user_id, self.salt)
        now = self.clock.now(session)
        # Lock onboarding progress first so concurrent taps on the last step create one learner, not two.
        state = session.get(Onboarding, user_hash, with_for_update=True)
        learner = session.scalar(select(Learner).where(Learner.channel_user_hash == user_hash))
        if learner is not None and learner.channel_chat_id != inbound.chat_id:
            learner.channel_chat_id = inbound.chat_id
        language = learner.language if learner else state.language if state else Language.EN

        if inbound.payload is not None:
            return self._button(session, inbound, learner, state, user_hash, now)
        if inbound.text is None:
            return [Reply(t("message.text_only", language))]
        command, argument = parse_command(inbound.text)
        if command == "start":
            if learner is not None:
                return [Reply(t("onboarding.already_done", language, name=learner.first_name)),
                        Reply(t("help", language))]
            return onboarding.start(session, user_hash)
        if command == "delete_me":
            return [account.confirm_delete(learner, state)]
        if learner is None:
            return onboarding.resume(session, state, inbound.text if command is None else None, now)
        if command is None:
            return [self.qa.ask(session, learner, argument).reply]
        if command == "ask":
            return [self.qa.ask(session, learner, argument).reply] if argument else [Reply(t("ask.usage", language))]
        if command == "lesson":
            return self.lessons.today(session, learner, now)
        if command == "language":
            return [account.language_menu(learner)]
        if command == "share_scores":
            return [account.scores_menu(learner)]
        if command == "help":
            return [Reply(t("help", language))]
        if command in COMING_SOON:
            return [Reply(t("command.coming_soon", language))]
        return [Reply(t("command.unknown", language)), Reply(t("help", language))]

    def _button(self, session: Session, inbound: Inbound, learner: Learner | None, state: Onboarding | None,
                user_hash: str, now: datetime) -> list[Reply]:
        payload = inbound.payload or ""
        # Deletion works mid-onboarding too, and only ever for the user who tapped.
        if payload == account.DELETE_YES:
            return [account.delete_me(session, learner, user_hash, now)]
        if payload == account.DELETE_NO:
            return [account.cancel_delete(learner, state)]
        if payload.startswith(onboarding.PREFIX):
            if learner is not None:
                return [Reply(t("onboarding.already_done", learner.language, name=learner.first_name))]
            return onboarding.handle_button(session, state, payload, channel=inbound.channel, user_hash=user_hash,
                                            chat_id=inbound.chat_id, now=now)
        if learner is None:
            return onboarding.resume(session, state, None, now)
        if payload.startswith(account.SET_LANGUAGE):
            return account.set_language(learner, payload.removeprefix(account.SET_LANGUAGE))
        if payload in (account.SHARE_SCORES, account.KEEP_SCORES_PRIVATE):
            return [account.set_share_scores(learner, payload == account.SHARE_SCORES)]
        if payload.startswith(lessons.PREFIX):
            return self._lesson_button(session, learner, payload, now)
        if payload == qa.DISMISS:
            return [qa.dismiss(learner)]
        for prefix in (qa.SHOW_SOURCE, qa.ASK_CIRCLE):
            if payload.startswith(prefix):
                try:
                    target = UUID(payload.removeprefix(prefix))
                except ValueError:
                    break
                if prefix == qa.SHOW_SOURCE:
                    return [qa.show_source(session, learner, target)]
                return [qa.ask_circle(session, learner, target, now)]
        return [Reply(t("button.expired", learner.language))]

    def _lesson_button(self, session: Session, learner: Learner, payload: str, now: datetime) -> list[Reply]:
        try:
            if payload.startswith(lessons.START_QUESTIONS):
                return grading.start(session, self.lessons, learner, UUID(payload.removeprefix(lessons.START_QUESTIONS)))
            if payload.startswith(lessons.ANSWER):
                question_id, option = payload.removeprefix(lessons.ANSWER).rsplit(":", 1)
                return grading.answer(session, self.lessons, learner, UUID(question_id), option, now)
        except ValueError:
            pass
        return [Reply(t("button.expired", learner.language))]

"""Web simulator adapter (spec section 10.3): up to three learner panes in one page, without Telegram.

`GET /sim` serves `web/simulator.html`. Each pane speaks for one web learner, either a new learner
onboarding exactly as on Telegram or a persona the pane has taken control of, which pauses that
persona's automated behavior (app/sim/personas.py). Telegram learners can never be driven from here.

- `POST /web/message` hands a pane's text or button tap to `Conversation.handle` and returns its
  replies, as the Telegram adapter sends replies straight back.
- Messages a learner did not ask for (the lesson post, circle updates, nudges and payouts) are
  queued in the outbox by the core. The outbox's delivery job sends them through `WebAdapter`,
  which stores them in `web_messages`, and the pane collects them with `GET /web/poll` every two
  seconds, passing the last message ID it has seen as `since`.
- `GET /web/learners` lists the learners a pane can pick, and `POST /web/control` takes or releases
  control of a persona.
- `POST /web/admin` runs the demo admin commands (`/seed_demo`, `/simulate_day` and the others)
  for the page's console, so the whole engagement demo can run here.

Access: the simulator answers only with WEB_ENABLED set, and every `/web` request needs WEB_TOKEN
as a bearer token, because it can speak for any web learner and move the virtual clock (the USSD
tunnel of milestone 10 makes the app reachable from outside).

Identity: a pane onboarding a new learner makes up a random `guest` key; the channel user ID is
`sim:<guest>`, hashed on arrival like any channel ID. It is also the learner's chat ID, which the
web channel otherwise never needs, so a pane can later pick that learner from the list. Nothing is
stored before "I agree": a pane's own messages and the replies it gets are not kept, only what the
outbox delivered. A persona's user ID is its namespaced pseudo-ID (`personas.persona_user_id`).
"""
import asyncio
import base64
import logging
import secrets
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.base import Button
from app.core.clock import Clock
from app.core.conversation import ADMIN_COMMANDS, ADMIN_PREFIX, Inbound
from app.core.i18n import t
from app.core.privacy import channel_hash
from app.core.replies import Attachment, Reply
from app.models import Channel, Learner, WebMessage
from app.sim import personas

logger = logging.getLogger(__name__)

SIMULATOR_PAGE = Path(__file__).resolve().parents[2] / "web" / "simulator.html"
GUEST_PREFIX = "sim:"
CONSOLE_USER = "console"  # the page's admin console: never a learner, so commands use the demo goal and English
MAX_TEXT_CHARS = 4096  # as on Telegram
MAX_PAYLOAD_CHARS = 64  # Telegram's callback data limit; every payload the core makes fits
POLL_LIMIT = 100
VOICE_FILENAME = "voice.ogg"
# The page is self-contained: inline script and style, same-origin API calls, media from data/blob URLs.
PAGE_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                               "connect-src 'self'; media-src blob:; img-src data:; frame-ancestors 'none'; "
                               "base-uri 'none'; form-action 'none'",
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


# The channel


def _attachment(filename: str, data: bytes) -> dict:
    return {"filename": filename, "data": base64.b64encode(data).decode("ascii")}


def message_json(reply: Reply) -> dict:
    """A reply as the page renders it: text, one button per row, then any voice note and file (base64)."""
    return {"text": reply.text,
            "buttons": [{"text": button.text, "payload": button.payload} for button in reply.buttons],
            "voice": _attachment(VOICE_FILENAME, reply.voice) if reply.voice is not None else None,
            "file": _attachment(reply.file.filename, reply.file.data) if reply.file is not None else None}


class WebAdapter:
    """Delivers to a web learner's inbox, which their pane polls. The outbox calls it, one message at a time."""
    channel = Channel.WEB

    def __init__(self, sessions: sessionmaker[Session], clock: Clock) -> None:
        self.sessions, self.clock = sessions, clock

    async def send_text(self, learner: Learner, text: str, buttons: list[Button] | None = None) -> None:
        await self._store(learner, Reply(text, buttons or []))

    async def send_voice(self, learner: Learner, ogg_bytes: bytes) -> None:
        await self._store(learner, Reply("", voice=ogg_bytes))

    async def send_file(self, learner: Learner, filename: str, data: bytes) -> None:
        await self._store(learner, Reply("", file=Attachment(filename, data)))

    async def _store(self, learner: Learner, reply: Reply) -> None:
        await asyncio.to_thread(self._insert, learner.id, message_json(reply))

    def _insert(self, learner_id: UUID, payload: dict) -> None:
        with self.sessions.begin() as session:
            session.add(WebMessage(learner_id=learner_id, payload=payload, created_at=self.clock.now(session)))


def register(application: FastAPI) -> None:
    """Add the routes. They answer only once the app's settings enable the simulator."""
    application.include_router(router)


# Requests


def require_token(request: Request, authorization: Annotated[str | None, Header()] = None) -> None:
    settings = request.app.state.settings
    if not settings.web_enabled:
        raise HTTPException(404)
    scheme, _, token = (authorization or "").partition(" ")
    expected = settings.web_token.get_secret_value().strip()
    if scheme.lower() != "bearer" or not secrets.compare_digest(token.strip().encode(), expected.encode()):
        raise HTTPException(401, "A valid WEB_TOKEN is required", headers={"WWW-Authenticate": "Bearer"})


class MessageIn(BaseModel):
    """One pane message: who speaks (`learner_id`, or `guest` for a learner still onboarding) and what."""
    model_config = ConfigDict(extra="forbid")
    learner_id: UUID | None = None
    guest: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    text: str | None = Field(default=None, min_length=1, max_length=MAX_TEXT_CHARS)
    payload: str | None = Field(default=None, min_length=1, max_length=MAX_PAYLOAD_CHARS)

    @model_validator(mode="after")
    def one_of_each(self) -> "MessageIn":
        if (self.learner_id is None) == (self.guest is None):
            raise ValueError("Give exactly one of learner_id and guest")
        if (self.text is None) == (self.payload is None):
            raise ValueError("Give exactly one of text and payload")
        return self


class ControlIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    learner_id: UUID
    control: bool


class AdminIn(BaseModel):
    """An admin command with its argument (such as a seed), or an admin button such as the reset confirmation."""
    model_config = ConfigDict(extra="forbid")
    command: str | None = None
    argument: str = Field(default="", max_length=32)
    payload: str | None = Field(default=None, max_length=MAX_PAYLOAD_CHARS)

    @model_validator(mode="after")
    def known(self) -> "AdminIn":
        if (self.command is None) == (self.payload is None):
            raise ValueError("Give exactly one of command and payload")
        if self.command is not None and self.command not in ADMIN_COMMANDS:
            raise ValueError(f"command must be one of {sorted(ADMIN_COMMANDS)}")
        if self.payload is not None and not self.payload.startswith(ADMIN_PREFIX):
            raise ValueError("payload must be an admin button")
        return self


def _salt(request: Request) -> str:
    return request.app.state.settings.hash_salt.get_secret_value()


def _web_learner(session: Session, learner_id: UUID, *, lock: bool = False) -> Learner:
    """A web learner or persona by ID. Telegram and USSD learners look the same as missing ones."""
    learner = session.get(Learner, learner_id, with_for_update=lock)
    if learner is None or learner.channel != Channel.WEB:
        raise HTTPException(404, "No such web learner")
    return learner


def _speaker(request: Request, body: MessageIn) -> str:
    """The channel user ID the pane speaks with, checked against the learner it names."""
    if body.guest is not None:
        return f"{GUEST_PREFIX}{body.guest}"
    salt = _salt(request)
    with request.app.state.sessions() as session:
        learner = _web_learner(session, body.learner_id)
        if learner.is_simulated:
            if learner.controlled_at is None:
                raise HTTPException(409, "Take control of this persona before speaking for it")
            user_id = personas.user_id_of(session, learner, salt)
        else:
            user_id = learner.channel_chat_id
        if not user_id or channel_hash(Channel.WEB, user_id, salt) != learner.channel_user_hash:
            raise HTTPException(404, "No such web learner")
        return user_id


def _learner_json(session: Session, learner: Learner, salt: str) -> dict | None:
    persona = learner.is_simulated
    if persona and personas.user_id_of(session, learner, salt) is None:
        return None  # a simulated learner that is not a demo persona, such as the QA evaluator's
    return {"id": str(learner.id), "first_name": learner.first_name, "language": learner.language.value,
            "persona": persona, "controlled": learner.controlled_at is not None}


router = APIRouter()


@router.get("/sim", response_class=HTMLResponse, include_in_schema=False)
def simulator_page(request: Request) -> HTMLResponse:
    """The page itself holds no data, so it needs no token; it asks for one before calling the API."""
    if not request.app.state.settings.web_enabled:
        raise HTTPException(404)
    return HTMLResponse(SIMULATOR_PAGE.read_text(encoding="utf-8"), headers=PAGE_HEADERS)


web = APIRouter(prefix="/web", dependencies=[Depends(require_token)])


@web.get("/learners")
def list_learners(request: Request) -> dict:
    """The learners a pane can pick: the demo personas, then learners who onboarded here, and the virtual time."""
    salt = _salt(request)
    with request.app.state.sessions() as session:
        rows = session.scalars(select(Learner).where(Learner.channel == Channel.WEB)
                               .order_by(Learner.is_simulated.desc(), Learner.created_at, Learner.first_name,
                                         Learner.id)).all()
        learners = [shown for learner in rows if (shown := _learner_json(session, learner, salt)) is not None]
        now = request.app.state.clock.now(session)
    return {"virtual_time": now.isoformat(), "learners": learners}


@web.post("/control")
def control(body: ControlIn, request: Request) -> dict:
    """Take control of a persona, pausing its automation, or hand it back to the simulation."""
    salt = _salt(request)
    with request.app.state.sessions.begin() as session:
        learner = _web_learner(session, body.learner_id, lock=True)
        if personas.user_id_of(session, learner, salt) is None:
            raise HTTPException(404, "No such persona")
        if body.control:
            personas.take_control(session, learner, request.app.state.clock.now(session))
        else:
            personas.release_control(learner)
        return _learner_json(session, learner, salt)


@web.post("/message")
def post_message(body: MessageIn, request: Request) -> dict:
    """The replies to one pane message, and the learner it now belongs to (none before onboarding or after deletion)."""
    user_id = _speaker(request, body)
    state = request.app.state
    try:
        replies = state.conversation.handle(Inbound(Channel.WEB, user_id, user_id, text=body.text,
                                                    payload=body.payload))
    except Exception as error:  # noqa: BLE001 - as on Telegram: a notice for the learner, the details in the log
        logger.error("Web message failed: %r", error, exc_info=error)
        replies = [Reply(t("error.generic"))]
    with state.sessions() as session:
        learner_id = session.scalar(select(Learner.id).where(
            Learner.channel_user_hash == channel_hash(Channel.WEB, user_id, _salt(request))))
    return {"learner_id": str(learner_id) if learner_id is not None else None,
            "replies": [message_json(reply) for reply in replies]}


@web.get("/poll")
def poll(request: Request, learner_id: UUID, since: Annotated[int, Query(ge=0)] = 0) -> dict:
    """Delivered messages after `since`, oldest first; `since` in the response is the cursor for the next poll."""
    with request.app.state.sessions() as session:
        _web_learner(session, learner_id)
        rows = session.scalars(select(WebMessage).where(WebMessage.learner_id == learner_id, WebMessage.id > since)
                               .order_by(WebMessage.id).limit(POLL_LIMIT)).all()
    return {"messages": [{"id": row.id, **row.payload} for row in rows], "since": rows[-1].id if rows else since}


@web.post("/admin")
def admin(body: AdminIn, request: Request) -> dict:
    """The demo admin commands for the page's console, as Telegram admins send them (spec section 9.3)."""
    state = request.app.state
    console = channel_hash(Channel.WEB, CONSOLE_USER, _salt(request))
    if body.payload is not None:
        replies = state.demo.button(body.payload, console)
    else:
        replies = state.demo.command(body.command, body.argument.strip(), console)
    # The console is never a learner, so /seed_demo's "send /start to join" is not for it; panes join by onboarding.
    replies = [reply for reply in replies if reply.text != t("admin.join_first")]
    return {"replies": [message_json(reply) for reply in replies],
            "virtual_time": state.clock.now().isoformat()}


router.include_router(web)

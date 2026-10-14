from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from typing import AsyncContextManager

from fastapi import FastAPI
from sqlalchemy import text

from app.adapters.base import AdapterRegistry
from app.adapters.telegram_bot import telegram_polling
from app.config import Settings
from app.core import opportunities
from app.core.clock import Clock, initialize
from app.core.conversation import Conversation
from app.core.embeddings import E5Embedder, LazyEmbedder
from app.core.lessons import LessonService
from app.core.qa import QAService
from app.core.voice import VoiceService
from app.db import build_engine, session_factory
from app.llm.client import LLMClient
from app.scheduler import scheduler_service
from app.sim.engine import DemoEngine
from app.speech import stt, tts

# Background services (the scheduler) supply async context managers so their
# tasks are stopped before the database pool is disposed, even on startup failure.
BackgroundService = Callable[[FastAPI], AsyncContextManager[None]]


def create_app(settings: Settings | None = None, *, services: Sequence[BackgroundService] = (),
               telegram: BackgroundService = telegram_polling) -> FastAPI:
    """`telegram` runs only when TELEGRAM_ENABLED is set; tests swap in a fake Bot API through it."""
    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        configured = settings or Settings()
        engine = build_engine(configured.database_url)
        llm = voice = None
        try:
            application.state.settings = configured
            application.state.sessions = session_factory(engine)
            application.state.clock = Clock(application.state.sessions)
            application.state.adapters = AdapterRegistry()
            llm = LLMClient.from_settings(configured, application.state.sessions, application.state.clock)
            embedder = LazyEmbedder(lambda: E5Embedder(configured.embed_model))  # shared: one model in memory
            application.state.qa = QAService(embedder, llm, application.state.clock,
                                             min_score=configured.retrieval_min_score)
            # STT and TTS models load on first use; a missing or Opus-less ffmpeg stops startup here.
            speaker = tts.from_settings(configured, application.state.sessions, application.state.clock)
            voice = VoiceService(application.state.sessions, transcriber=stt.from_settings(configured),
                                 speaker=speaker, max_seconds=configured.stt_max_seconds)
            application.state.voice = voice
            application.state.lessons = LessonService(embedder, llm, application.state.clock,
                                                      audio=voice.speaker is not None)
            salt = configured.hash_salt.get_secret_value()
            application.state.demo = DemoEngine(application.state.sessions, application.state.clock,
                                                application.state.lessons, embedder, salt=salt,
                                                min_score=configured.retrieval_min_score,
                                                goal_code=configured.demo_goal)
            application.state.conversation = Conversation(application.state.sessions, application.state.clock,
                                                          application.state.qa, application.state.lessons,
                                                          salt=salt, admin=application.state.demo,
                                                          admin_ids=configured.admin_user_ids, voice=voice)
            with application.state.sessions() as session:
                initialize(session)
            # A malformed file stops startup rather than leaving payouts with stale or unchecked entries.
            catalog = opportunities.load_catalog(configured.opportunities_file)
            with application.state.sessions.begin() as session:
                opportunities.sync(session, catalog)
            async with AsyncExitStack() as stack:
                for service in services:
                    await stack.enter_async_context(service(application))
                if configured.telegram_enabled:
                    await stack.enter_async_context(telegram(application))
                yield
        finally:
            if llm is not None:
                llm.close()
            if voice is not None:
                voice.close()
            engine.dispose()

    application = FastAPI(title="Ajo", lifespan=lifespan)

    @application.get("/")
    def root() -> dict[str, str]:
        return {"message": "Hello World"}

    @application.get("/health")
    def health() -> dict[str, str]:
        with application.state.sessions() as session:
            session.execute(text("SELECT 1"))
            virtual_time = application.state.clock.now(session)
        return {"status": "ok", "virtual_time": virtual_time.isoformat()}

    return application


# The served app delivers queued messages and, with SCHEDULER_ENABLED, runs daily jobs. Tests opt in.
app = create_app(services=[scheduler_service])

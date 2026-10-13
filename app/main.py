from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from typing import AsyncContextManager

from fastapi import FastAPI
from sqlalchemy import text

from app.adapters.base import AdapterRegistry
from app.config import Settings
from app.core.clock import Clock, initialize
from app.db import build_engine, session_factory

# Future polling/scheduler services supply async context managers so their
# tasks are stopped before the database pool is disposed, even on startup failure.
BackgroundService = Callable[[FastAPI], AsyncContextManager[None]]


def create_app(settings: Settings | None = None, *, services: Sequence[BackgroundService] = ()) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        configured = settings or Settings()
        engine = build_engine(configured.database_url)
        try:
            application.state.settings = configured
            application.state.sessions = session_factory(engine)
            application.state.clock = Clock(application.state.sessions)
            application.state.adapters = AdapterRegistry()
            with application.state.sessions() as session:
                initialize(session)
            async with AsyncExitStack() as stack:
                for service in services:
                    await stack.enter_async_context(service(application))
                yield
        finally:
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


app = create_app()

from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import StatementError

from app.config import Settings
from app.core.clock import Clock
from app.db import build_engine, session_factory
from app.main import create_app
from app.models import Base, Chunk, ClockState, Source


def test_fresh_schema_contains_all_entities(engine):
    assert set(Base.metadata.tables) <= set(inspect(engine).get_table_names())
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT offset_seconds FROM clock_state WHERE id = 1")) is not None
        if engine.dialect.name == "postgresql":
            assert connection.scalar(text("SELECT extname FROM pg_extension WHERE extname = 'vector'")) == "vector"
            assert connection.scalar(text("SELECT format_type(atttypid, atttypmod) FROM pg_attribute WHERE attrelid = 'chunks'::regclass AND attname = 'embedding'")) == "vector(384)"


def test_clock_persists_across_engines_and_is_timezone_aware(database_url, clock):
    before = clock.now()
    advanced = clock.advance(timedelta(days=1))
    assert 86400 <= (advanced - before).total_seconds() < 86405
    restarted_engine = build_engine(database_url)
    try:
        restarted = Clock(session_factory(restarted_engine)).now()
        assert restarted.tzinfo is not None
        assert 0 <= (restarted - advanced).total_seconds() < 5
    finally:
        restarted_engine.dispose()


@pytest.mark.parametrize("delta", [timedelta(0), timedelta(days=-1), timedelta(microseconds=1)])
def test_clock_rejects_invalid_advances(clock, delta):
    with pytest.raises(ValueError):
        clock.advance(delta)


def test_clock_advances_accumulate(sessions, clock):
    with sessions() as session:
        before = session.scalar(select(ClockState.offset_seconds))
    clock.advance(timedelta(seconds=3))
    clock.advance(timedelta(seconds=7))
    with sessions() as session:
        assert session.scalar(select(ClockState.offset_seconds)) == before + 10


def test_startup_health_and_service_lifecycle(database_url, engine):
    events = []

    @asynccontextmanager
    async def service(app):
        events.append("start")
        assert app.state.clock.now().tzinfo is not None
        try:
            yield
        finally:
            events.append("stop")

    settings = Settings(_env_file=None, database_url=database_url, hash_salt="synthetic-test-salt-with-32-characters")
    app = create_app(settings, services=[service])
    with TestClient(app) as client:
        assert client.get("/").json() == {"message": "Hello World"}
        assert client.get("/health").json()["status"] == "ok"
        assert events == ["start"]
    assert events == ["start", "stop"]


def test_service_startup_failure_stops_started_services(database_url, engine):
    events = []

    @asynccontextmanager
    async def first(app):
        try:
            yield
        finally:
            events.append("stopped")

    @asynccontextmanager
    async def fails(app):
        raise RuntimeError("synthetic startup failure")
        yield

    settings = Settings(_env_file=None, database_url=database_url, hash_salt="synthetic-test-salt-with-32-characters")
    with pytest.raises(RuntimeError, match="synthetic startup failure"):
        with TestClient(create_app(settings, services=[first, fails])):
            pass
    assert events == ["stopped"]


def test_startup_requires_migrations(tmp_path):
    settings = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / 'empty.db'}",
                        hash_salt="synthetic-test-salt-with-32-characters")
    with pytest.raises(Exception, match="clock_state"):
        with TestClient(create_app(settings)):
            pass


def test_embedding_round_trip(sessions, engine):
    with sessions.begin() as session:
        source = Source(title="Synthetic", publisher="Demo", license="CC0", url="https://example.org", file_path="demo.pdf")
        session.add(source)
        session.flush()
        chunk = Chunk(source_id=source.id, section="Cells", page=1, text="Synthetic corpus", embedding=[0.1] * 384)
        session.add(chunk)
        session.flush()
        identifier = chunk.id
    with sessions() as session:
        assert len(session.get(Chunk, identifier).embedding) == 384
    if engine.dialect.name == "postgresql":
        with pytest.raises(StatementError, match="384"):
            with sessions.begin() as session:
                session.add(Chunk(source_id=source.id, section="Cells", page=1, text="Bad", embedding=[0.1] * 3))


def test_migration_matches_models(engine):
    from alembic import command
    from alembic.config import Config

    config = Config("alembic.ini")
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.check(config)


def test_concurrent_clock_advances_do_not_lose_updates(sessions, clock):
    from concurrent.futures import ThreadPoolExecutor

    with sessions() as session:
        before = session.scalar(select(ClockState.offset_seconds))
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: clock.advance(timedelta(seconds=1)), range(8)))
    with sessions() as session:
        assert session.scalar(select(ClockState.offset_seconds)) == before + 8
    assert all(result.tzinfo is not None for result in results)


def test_cli_discovers_the_package_application():
    from pathlib import Path
    from fastapi_cli.discover import get_import_data
    from app.main import app
    from main import app as compatibility_app

    discovered = get_import_data(path=Path("app/main.py"))
    assert discovered.import_string == "app.main:app"
    assert compatibility_app is app

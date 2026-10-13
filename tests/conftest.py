import os
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.core.clock import Clock
from app.db import build_engine, session_factory
from app.llm.client import LLMClient
from tests.fakes import FakeLLM


@pytest.fixture(scope="session", params=["sqlite", "postgres"])
def database_url(request, tmp_path_factory):
    if request.param == "sqlite":
        yield f"sqlite:///{tmp_path_factory.mktemp('db') / 'foundation.db'}"
        return
    configured = os.getenv("TEST_DATABASE_URL")
    if not configured:
        pytest.skip("Set TEST_DATABASE_URL to run isolated PostgreSQL integration tests")
    url = make_url(configured)
    database_name = f"ajo_test_{uuid4().hex}"
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    # Generated database name contains only a fixed prefix and UUID hex.
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{database_name}"'))
    try:
        yield url.set(database=database_name).render_as_string(hide_password=False)
    finally:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{database_name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture(scope="session")
def engine(database_url):
    engine = build_engine(database_url)
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
        command.upgrade(config, "head")  # Idempotent repeated deployment.
    yield engine
    engine.dispose()


@pytest.fixture
def sessions(engine):
    return session_factory(engine)


@pytest.fixture
def clock(sessions):
    return Clock(sessions)


@pytest.fixture
def make_client(sessions, clock):
    clients = []

    def make(fake: FakeLLM | None = None, **options) -> LLMClient:
        defaults = dict(sessions=sessions, clock=clock, base_url="https://llm.example/v1/", api_key="test-key",
                        model="test-model", enabled=True, cache=False, transport=httpx.MockTransport(fake or FakeLLM()),
                        sleep=options.pop("sleep", lambda seconds: None))
        client = LLMClient(**(defaults | options))
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.close()

from collections.abc import Iterator

from fastapi import Request
from sqlalchemy import create_engine, event
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


def build_engine(database_url: str) -> Engine:
    options = {"pool_pre_ping": True, "hide_parameters": True}
    if database_url.startswith("sqlite"):
        options["connect_args"] = {"check_same_thread": False}
    engine = create_engine(database_url, **options)
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def enable_foreign_keys(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")
    return engine


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


def get_session(request: Request) -> Iterator[Session]:
    with request.app.state.sessions() as session:
        with session.begin():
            yield session


def dialect_insert(session: Session, model):
    """An INSERT that supports ON CONFLICT on both PostgreSQL and SQLite."""
    return (postgresql if session.get_bind().dialect.name == "postgresql" else sqlite).insert(model)

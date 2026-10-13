from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app.models import ClockState


def initialize(session: Session) -> None:
    # The migration creates the singleton. Startup verifies it exists.
    if session.get(ClockState, 1) is None:
        raise RuntimeError("Clock state is missing; run alembic upgrade head")


class Clock:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def now(self, session: Session | None = None) -> datetime:
        if session is None:
            with self.sessions() as owned:
                return self.now(owned)
        offset = session.scalar(select(ClockState.offset_seconds).where(ClockState.id == 1))
        if offset is None:
            raise RuntimeError("Clock state is missing; run alembic upgrade head")
        return datetime.now(UTC) + timedelta(seconds=offset)

    def advance(self, delta: timedelta) -> datetime:
        seconds = delta.total_seconds()
        if seconds <= 0 or not seconds.is_integer():
            raise ValueError("Clock advancement must be a positive whole number of seconds")
        # SQL arithmetic serializes concurrent updates without losing advances.
        with self.sessions.begin() as session:
            result = session.execute(
                update(ClockState).where(ClockState.id == 1)
                .values(offset_seconds=ClockState.offset_seconds + int(seconds))
            )
            if result.rowcount != 1:
                raise RuntimeError("Clock state is missing; run alembic upgrade head")
            return self.now(session)

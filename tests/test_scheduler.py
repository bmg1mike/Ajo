"""Milestone 5: APScheduler drives daily jobs on the virtual clock, without processing a day twice."""
import time as real_time
from datetime import UTC, datetime, time, timedelta
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from app import scheduler
from app.config import Settings
from app.core import outbox
from app.core.clock import virtual_day
from app.core.replies import Reply
from app.main import create_app
from app.models import Channel, DailyRun, Learner, Lesson, OutboundDelivery, RunTrigger
from app.scheduler import DailyTicker, lesson_due, scheduler_service
from tests.test_feed import FakeAdapter
from tests.test_lessons import library  # noqa: F401 - the shared two-topic library fixture
from tests.test_onboarding import SALT
from tests.test_simulation import demo  # noqa: F401 - shared fixture

SEVEN = time(7, 0)


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)


def test_a_lesson_is_due_when_the_virtual_lesson_time_passes_between_ticks():
    assert lesson_due(at(14, 6, 59), at(14, 7, 0), SEVEN)
    assert lesson_due(at(14, 6, 59), at(14, 7, 1), SEVEN)
    assert not lesson_due(at(14, 7, 0), at(14, 7, 1), SEVEN)  # already past at the previous tick
    assert not lesson_due(at(14, 6, 0), at(14, 6, 59), SEVEN)
    # /simulate_day jumps 24 hours: the new day's lesson time is crossed (and already claimed by the simulation).
    assert lesson_due(at(14, 9), at(15, 9), SEVEN)
    # /reset_demo moves the clock back: no catch-up post.
    assert not lesson_due(at(20, 9), at(14, 9), SEVEN)


class FakeClock:
    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def now(self, session=None) -> datetime:
        return self.moment


class FakeEngine:
    def __init__(self) -> None:
        self.runs, self.fail = 0, False

    def run_scheduled(self):
        self.runs += 1
        return [], self.fail


def test_the_ticker_runs_once_per_lesson_time_and_retries_a_failure():
    clock, engine = FakeClock(at(14, 6, 58)), FakeEngine()
    ticker = DailyTicker(engine, clock, SEVEN)
    for moment, runs in ((at(14, 6, 59), 0), (at(14, 7, 0), 1), (at(14, 7, 1), 1), (at(14, 23), 1),
                         (at(15, 7, 30), 2), (at(15, 8), 2)):
        clock.moment = moment
        ticker.tick()
        assert engine.runs == runs, moment
    engine.fail = True
    clock.moment = at(16, 7, 0)
    ticker.tick()
    clock.moment = at(16, 7, 1)
    ticker.tick()  # the failed run is retried at the next tick
    assert engine.runs == 4
    engine.fail = False
    ticker.tick()
    clock.moment = at(16, 7, 2)
    ticker.tick()
    assert engine.runs == 5


def test_scheduled_and_simulated_runs_never_process_a_day_twice(sessions, demo):
    reports, _ = demo.engine.run_scheduled()  # other tests' circles in the shared database may fail; this one not
    (report,) = [report for report in reports if report.circle_id == demo.circle_id]
    assert report.lesson_day == 1 and report.lessons_queued == 1
    assert demo.circle_id not in {r.circle_id for r in demo.engine.run_scheduled()[0]}

    demo.admin.send("/seed_demo")
    (reply,) = demo.admin.send("/simulate_day")
    assert "Day 2, Excretion" in reply.text
    assert demo.circle_id not in {r.circle_id for r in demo.engine.run_scheduled()[0]}
    with sessions() as session:
        runs = session.execute(select(DailyRun.virtual_day, DailyRun.trigger)
                               .where(DailyRun.circle_id == demo.circle_id).order_by(DailyRun.virtual_day)).all()
        today = virtual_day(demo.clock.now(session))
        assert runs == [(today - timedelta(days=1), RunTrigger.SCHEDULER), (today, RunTrigger.SIMULATION)]
        assert session.scalar(select(func.count()).select_from(Lesson)
                              .where(Lesson.circle_id == demo.circle_id)) == 2


def test_the_service_delivers_queued_messages_and_stops_cleanly(database_url, engine, sessions, clock, monkeypatch):
    monkeypatch.setattr(scheduler, "DELIVERY_SECONDS", 0.1)
    with sessions.begin() as session:
        now = clock.now(session)
        session.execute(update(OutboundDelivery).where(OutboundDelivery.delivered_at.is_(None))
                        .values(delivered_at=now))  # earlier tests share the database
        learner = Learner(channel=Channel.TELEGRAM, channel_user_hash=uuid4().hex, channel_chat_id="5550001",
                          first_name="Amaka", created_at=now)
        session.add(learner)
        session.flush()
        outbox.enqueue(session, learner, [Reply("🔵 Circle update")], key=f"feed:{uuid4()}", now=now)
    settings = Settings(_env_file=None, database_url=database_url, hash_salt=SALT, scheduler_enabled=True)
    adapter = FakeAdapter()
    with TestClient(create_app(settings, services=[scheduler_service])) as client:
        application = client.app
        assert {job.id for job in application.state.scheduler.get_jobs()} == {"deliver", "daily"}
        application.state.adapters.register(adapter)
        deadline = real_time.monotonic() + 10
        while not adapter.sent and real_time.monotonic() < deadline:
            real_time.sleep(0.05)
    assert adapter.sent == [("5550001", "🔵 Circle update", [])]
    assert not application.state.scheduler.running
    with sessions() as session:
        assert session.scalar(select(OutboundDelivery.delivered_at)
                              .where(OutboundDelivery.learner_id == learner.id)) is not None


def test_without_the_scheduler_flag_only_delivery_runs(database_url, engine):
    settings = Settings(_env_file=None, database_url=database_url, hash_salt=SALT)
    with TestClient(create_app(settings, services=[scheduler_service])) as client:
        assert {job.id for job in client.app.state.scheduler.get_jobs()} == {"deliver"}

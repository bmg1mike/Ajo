"""The in-process APScheduler service (spec sections 3 and 9).

APScheduler fires on real time; every decision it triggers is made on the virtual clock:

- `deliver`, every DELIVERY_SECONDS: send queued outbound messages through the adapter registry.
- `daily`, every TICK_SECONDS when SCHEDULER_ENABLED: read the virtual clock and, if today's virtual
  LESSON_TIME passed since the previous tick, run every circle's daily jobs.

A day is processed once whichever way it is reached: `DemoEngine.run_day` claims the circle and
virtual day in `daily_runs`, so a tick that crosses a lesson time /simulate_day already processed
does nothing. The scheduler does not catch up: after downtime, or when /reset_demo moves the clock
back, it waits for the next lesson time rather than posting a lesson late.
"""
import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, time

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI

from app.core import outbox
from app.core.clock import Clock
from app.sim.engine import DemoEngine

logger = logging.getLogger(__name__)

DELIVERY_SECONDS = 2
TICK_SECONDS = 60
SHUTDOWN_TIMEOUT_SECONDS = 30


def lesson_due(previous: datetime, current: datetime, lesson_time: time) -> bool:
    """Whether the current virtual day's lesson time lies in (previous, current]."""
    moment = datetime.combine(current.astimezone(UTC).date(), lesson_time, tzinfo=UTC)
    return previous < moment <= current


class DailyTicker:
    """Remembers the virtual time of the last tick, so each lesson time triggers one run."""

    def __init__(self, engine: DemoEngine, clock: Clock, lesson_time: time) -> None:
        self.engine, self.clock, self.lesson_time = engine, clock, lesson_time
        self.previous = clock.now()

    def tick(self) -> None:
        current = self.clock.now()
        if lesson_due(self.previous, current, self.lesson_time):
            reports, failed = self.engine.run_scheduled()
            logger.info("Scheduled daily jobs ran for %d circle(s)", len(reports))
            if failed:
                return  # keep `previous`, so the next tick retries; finished circles are already claimed
        self.previous = current


def scheduler_service(application: FastAPI):
    """Background service for `create_app(services=...)`: start the jobs, and on shutdown wait for running ones."""

    @asynccontextmanager
    async def run() -> AsyncIterator[None]:
        state = application.state
        running: set[asyncio.Future] = set()

        def tracked(job: Callable[[], Awaitable[None]]) -> Callable[[], Awaitable[None]]:
            # APScheduler cancels its tasks on shutdown, but a cancelled task cannot stop a database call
            # running in a thread. The work runs in a shielded task that shutdown waits for instead.
            async def wrapper() -> None:
                task = asyncio.ensure_future(job())
                running.add(task)
                task.add_done_callback(running.discard)
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - logged; the next run tries again
                    logger.exception("Scheduled job failed")
            return wrapper

        async def deliver() -> None:
            await outbox.deliver_pending(state.sessions, state.adapters, state.clock)

        scheduler = AsyncIOScheduler(timezone=UTC)
        scheduler.add_job(tracked(deliver), "interval", seconds=DELIVERY_SECONDS, id="deliver", max_instances=1,
                          coalesce=True)
        if state.settings.scheduler_enabled:
            ticker = await asyncio.to_thread(DailyTicker, state.demo, state.clock, state.settings.lesson_time)

            async def daily() -> None:
                await asyncio.to_thread(ticker.tick)

            scheduler.add_job(tracked(daily), "interval", seconds=TICK_SECONDS, id="daily", max_instances=1,
                              coalesce=True)
        scheduler.start()
        state.scheduler = scheduler
        try:
            yield
        finally:
            scheduler.shutdown(wait=False)
            if running:
                await asyncio.wait(set(running), timeout=SHUTDOWN_TIMEOUT_SECONDS)

    return run()

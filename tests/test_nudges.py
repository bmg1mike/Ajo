"""Milestone 6: inactivity detection and peer nudges (spec sections 7.6 and 16)."""
import asyncio
import threading
from datetime import UTC, date, datetime, time, timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy import delete, func, select, update

from app.adapters.base import AdapterRegistry, Button
from app.core import account, lessons, nudges, outbox
from app.core.clock import Clock, virtual_day
from app.db import build_engine, session_factory
from app.core.i18n import t
from app.models import CircleMember, FeedEvent, FeedType, Learner, Nudge, OutboundDelivery
from tests.conftest import migrate, throwaway_database
from tests.test_feed import FakeAdapter, answer_todays_questions
from tests.test_lessons import library  # noqa: F401 - the shared two-topic library fixture
from tests.test_simulation import deliveries, demo, persona_answers, texts  # noqa: F401 - shared fixtures

TUESDAY = date(2026, 10, 13)


def at(day: date, hour: int) -> datetime:
    return datetime.combine(day, time(hour), tzinfo=UTC)


def feed_updates(sessions, user) -> list[OutboundDelivery]:
    return [row for row in deliveries(sessions, user.hash) if row.deduplication_key.startswith("feed:")]


def nudge_payloads(delivery: OutboundDelivery) -> dict[str, str]:
    """{button text: payload} for the nudge buttons of a queued circle update."""
    return {button["text"]: button["payload"] for button in delivery.payload["messages"][0]["buttons"]
            if button["payload"].startswith(nudges.SEND)}


def learner_id(sessions, name: str, circle_id) -> object:
    with sessions() as session:
        return session.scalar(select(Learner.id).join(CircleMember, CircleMember.learner_id == Learner.id)
                              .where(CircleMember.circle_id == circle_id, Learner.first_name == name))


def nudges_to(sessions, recipient_id) -> list[Nudge]:
    with sessions() as session:
        return list(session.scalars(select(Nudge).where(Nudge.to_learner_id == recipient_id)
                                    .order_by(Nudge.virtual_day)))


def nudge_deliveries(sessions, user) -> list[OutboundDelivery]:
    return [row for row in deliveries(sessions, user.hash) if row.deduplication_key.startswith("nudge:")]


# Detection


def test_a_member_is_quiet_from_the_third_day_after_their_last_check_in():
    """Spec section 7.6 says two days without a check-in; section 16 says detection on day 3. Both hold:
    a Tuesday check-in leaves Wednesday and Thursday empty, and Friday's morning run detects it."""
    checked_in = CircleMember(joined_at=at(TUESDAY - timedelta(days=30), 9), last_checkin_at=at(TUESDAY, 23))
    assert [nudges.is_quiet(checked_in, TUESDAY + timedelta(days=n)) for n in range(6)] == [
        False, False, False, True, True, True]
    # Never checked in: counted from the day they joined.
    joined = CircleMember(joined_at=at(TUESDAY, 8), last_checkin_at=None)
    assert [nudges.is_quiet(joined, TUESDAY + timedelta(days=n)) for n in range(5)] == [
        False, False, False, True, True]
    assert nudges.quiet_since(checked_in) == nudges.quiet_since(joined) == TUESDAY


def test_tunde_is_detected_exactly_on_day_three_after_his_last_check_in_and_only_once(sessions, demo):
    demo.admin.send("/seed_demo")
    reports = [demo.admin.send("/simulate_day")[0].text for _ in range(7)]
    quiet = [next((line for line in text.splitlines() if line.startswith("Gone quiet")), None) for text in reports]
    # Tunde last checks in on day 2, so day 5 detects him. Amaka, a real learner who never checks in
    # here, counts from the day she joined (day 0).
    assert quiet == [None, None, "Gone quiet: Amaka (the circle was invited to nudge)", None,
                     "Gone quiet: Tunde (the circle was invited to nudge)", None, None]
    tunde = learner_id(sessions, "Tunde", demo.circle_id)
    with sessions() as session:
        events = session.scalars(select(FeedEvent).where(FeedEvent.type == FeedType.MISSED,
                                                         FeedEvent.actor_learner_id == tunde)).all()
        last_checkin = session.get(CircleMember, (demo.circle_id, tunde)).last_checkin_at
    assert len(events) == 1 and events[0].payload == {"since": virtual_day(last_checkin).isoformat()}
    assert (virtual_day(events[0].created_at) - virtual_day(last_checkin)).days == 3

    updates = [texts(row)[0] for row in feed_updates(sessions, demo.admin)]
    prompt = t("nudge.prompt", name="Tunde")
    assert [prompt in update for update in updates] == [False, False, False, False, True, False, False]
    assert updates[4].endswith(f"Tunde ⏳ no check-in since {virtual_day(last_checkin).strftime('%A')}\n\n{prompt}")
    # Nobody is prompted about themselves, and personas are never sent anything.
    assert not any(t("nudge.prompt", name="Amaka") in update for update in updates)
    assert nudge_payloads(feed_updates(sessions, demo.admin)[4]) == {
        t("nudge.button", name="Tunde"): f"{nudges.SEND}{events[0].id}"}
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(OutboundDelivery)
                              .join(Learner, Learner.id == OutboundDelivery.learner_id)
                              .where(Learner.is_simulated.is_(True))) == 0


def test_a_nudge_brings_tunde_back_the_next_simulated_day(sessions, demo):
    demo.admin.send("/seed_demo")
    for _ in range(5):
        demo.admin.send("/simulate_day")
    payload = nudge_payloads(feed_updates(sessions, demo.admin)[-1])[t("nudge.button", name="Tunde")]
    assert [reply.text for reply in demo.admin.tap(payload)] == [t("nudge.sent", name="Tunde")]
    assert [reply.text for reply in demo.admin.tap(payload)] == [t("nudge.already_sent", name="Tunde")]
    (nudge,) = nudges_to(sessions, learner_id(sessions, "Tunde", demo.circle_id))
    assert nudge.from_learner_id == learner_id(sessions, "Amaka", demo.circle_id)
    assert nudge.virtual_day == virtual_day(demo.clock.now())

    (reply,) = demo.admin.send("/simulate_day")
    assert "Checked in: Chidi, Halima, Tunde\n" in reply.text
    assert [reply.text for reply in demo.admin.tap(payload)] == [t("nudge.not_quiet", name="Tunde")]
    (reply,) = demo.admin.send("/simulate_day")
    assert "Checked in: Chidi, Halima\n" in reply.text  # one nudge, one return
    tunde_days = {day for day, name, *_ in persona_answers(sessions, demo.circle_id) if name == "Tunde"}
    assert tunde_days == {1, 2, 6}
    assert len(nudges_to(sessions, nudge.to_learner_id)) == 1


# Real members


def quiet_bola(demo):
    """Amaka and Carol answer every day; Bola never does, and is detected on day 3 after joining."""
    demo.admin.send("/seed_demo")
    bola, carol = demo.user("Bola"), demo.user("Carol")
    for _ in range(3):
        demo.admin.send("/simulate_day")
        answer_todays_questions(demo.admin)
        answer_todays_questions(carol)
    return bola, carol


def bola_payload(sessions, user) -> str:
    return nudge_payloads(feed_updates(sessions, user)[-1])[t("nudge.button", name="Bola")]


def test_the_prompt_reaches_the_other_members_and_the_nudge_goes_out_at_once(sessions, demo, clock):
    bola, carol = quiet_bola(demo)
    for user in (demo.admin, carol):
        assert t("nudge.prompt", name="Bola") in texts(feed_updates(sessions, user)[-1])[0]
    # The quiet member is never prompted about themselves, and gets nothing the circle didn't send.
    assert all(t("nudge.prompt", name="Bola") not in text for row in deliveries(sessions, bola.hash)
               for text in texts(row))
    assert nudge_payloads(feed_updates(sessions, bola)[-1]) == {}
    assert {row.deduplication_key.split(":")[0] for row in deliveries(sessions, bola.hash)} == {"lesson", "feed"}

    assert [reply.text for reply in carol.tap(bola_payload(sessions, carol))] == [t("nudge.sent", name="Bola")]
    assert [reply.text for reply in demo.admin.tap(bola_payload(sessions, demo.admin))] == [
        t("nudge.already_sent", name="Bola")]
    (delivery,) = nudge_deliveries(sessions, bola)
    (nudge,) = nudges_to(sessions, delivery.learner_id)
    assert delivery.deduplication_key == f"nudge:{nudge.id}"
    assert delivery.payload["messages"] == [{
        "text": "👋 Carol from your circle is checking on you.\nToday's lesson is short. Want it now?",
        "buttons": [{"text": "Yes, send it", "payload": lessons.TODAY}, {"text": "Later", "payload": nudges.LATER}]}]

    # Delivered on the outbox's next pass, not held for tomorrow's circle update.
    with sessions.begin() as session:
        session.execute(update(OutboundDelivery).where(OutboundDelivery.learner_id != delivery.learner_id,
                                                       OutboundDelivery.delivered_at.is_(None))
                        .values(delivered_at=clock.now(session)))
    registry, adapter = AdapterRegistry(), FakeAdapter()
    registry.register(adapter)
    asyncio.run(outbox.deliver_pending(sessions, registry, clock))
    assert adapter.sent[-1] == (bola.chat_id, t("nudge.message", name="Carol"),
                                [Button("Yes, send it", lessons.TODAY), Button("Later", nudges.LATER)])

    # "Yes, send it" is today's lesson, as /lesson would send it; "Later" just acknowledges.
    replies = bola.tap(lessons.TODAY)
    assert replies[-1].text.startswith("📘 Day 3: Photosynthesis")
    assert [reply.text for reply in bola.tap(nudges.LATER)] == [t("nudge.later_reply")]


def test_one_nudge_per_quiet_member_per_day_whoever_taps(sessions, demo):
    bola, carol = quiet_bola(demo)
    payload = bola_payload(sessions, carol)
    assert [reply.text for reply in carol.tap(payload)] == [t("nudge.sent", name="Bola")]
    assert [reply.text for reply in carol.tap(payload)] == [t("nudge.already_sent", name="Bola")]
    assert [reply.text for reply in demo.admin.tap(payload)] == [t("nudge.already_sent", name="Bola")]
    assert len(nudge_deliveries(sessions, bola)) == 1

    # Still quiet the next day: the same prompt works once more, for whoever taps first.
    demo.admin.send("/simulate_day")
    assert [reply.text for reply in demo.admin.tap(payload)] == [t("nudge.sent", name="Bola")]
    assert [reply.text for reply in carol.tap(payload)] == [t("nudge.already_sent", name="Bola")]
    sent = nudges_to(sessions, nudge_deliveries(sessions, bola)[0].learner_id)
    assert [nudge.virtual_day for nudge in sent] == [sent[0].virtual_day, sent[0].virtual_day + timedelta(days=1)]
    assert texts(nudge_deliveries(sessions, bola)[1]) == [t("nudge.message", name="Amaka")]
    # Only the first detection of a spell prompts the circle.
    assert t("nudge.prompt", name="Bola") not in texts(feed_updates(sessions, carol)[-1])[0]

    # Once Bola checks in, the prompt has done its job.
    answer_todays_questions(bola)
    demo.admin.send("/simulate_day")
    assert [reply.text for reply in carol.tap(payload)] == [t("nudge.not_quiet", name="Bola")]
    assert len(nudge_deliveries(sessions, bola)) == 2


def deliver_only(sessions, clock, user) -> FakeAdapter:
    """Run one outbox pass with every other learner's pending rows out of the way; returns the adapter."""
    with sessions.begin() as session:
        own = session.scalar(select(Learner.id).where(Learner.channel_user_hash == user.hash))
        session.execute(update(OutboundDelivery).where(OutboundDelivery.learner_id != own,
                                                       OutboundDelivery.delivered_at.is_(None))
                        .values(delivered_at=clock.now(session)))
    registry, adapter = AdapterRegistry(), FakeAdapter()
    registry.register(adapter)
    asyncio.run(outbox.deliver_pending(sessions, registry, clock))
    return adapter


def test_a_nudge_not_delivered_on_its_day_is_never_delivered(sessions, demo, clock):
    """The daily limit holds for delivery, not just queuing: yesterday's undelivered nudge expires."""
    bola, carol = quiet_bola(demo)
    payload = bola_payload(sessions, carol)
    carol.tap(payload)  # queued on day 3, but no adapter delivers it that day
    (late,) = nudge_deliveries(sessions, bola)
    assert virtual_day(late.expires_at) == virtual_day(demo.clock.now()) + timedelta(days=1)
    demo.admin.send("/simulate_day")
    demo.admin.tap(payload)  # day 4's nudge
    adapter = deliver_only(sessions, clock, bola)
    nudge_texts = [text for _, text, _ in adapter.sent if text.startswith("👋")]
    assert nudge_texts == [t("nudge.message", name="Amaka")]  # one nudge on day 4, from day 4's sender
    late_row, sent_row = nudge_deliveries(sessions, bola)
    assert late_row.delivered_at is None and late_row.failed_at is None and sent_row.delivered_at is not None
    # The expired row stays unsent and does not hold back anything queued after it.
    assert all(row.delivered_at is not None for row in deliveries(sessions, bola.hash) if row.id != late_row.id)
    assert deliver_only(sessions, clock, bola).sent == []


def test_concurrent_taps_deliver_one_nudge(sessions, demo, engine):
    if engine.dialect.name != "postgresql":
        pytest.skip("SQLite serializes writers, so there is no race to observe")
    bola, carol = quiet_bola(demo)
    payload = bola_payload(sessions, carol)
    bola_id = learner_id(sessions, "Bola", demo.circle_id)
    replies = []
    with sessions.begin() as holder:  # hold Bola's circle row so both taps start before either finishes
        holder.get(CircleMember, (demo.circle_id, bola_id), with_for_update=True)
        threads = [threading.Thread(target=lambda user=user: replies.extend(user.tap(payload)))
                   for user in (demo.admin, carol)]
        for thread in threads:
            thread.start()
        threads[0].join(0.5)
        assert all(thread.is_alive() for thread in threads)
    for thread in threads:
        thread.join(10)
    assert sorted(reply.text for reply in replies) == sorted([t("nudge.sent", name="Bola"),
                                                              t("nudge.already_sent", name="Bola")])
    assert len(nudges_to(sessions, bola_id)) == 1
    assert len(nudge_deliveries(sessions, bola)) == 1


def test_the_database_allows_one_nudge_per_recipient_and_day(sessions, demo):
    bola, carol = quiet_bola(demo)
    carol.tap(bola_payload(sessions, carol))
    (nudge,) = nudges_to(sessions, learner_id(sessions, "Bola", demo.circle_id))
    with pytest.raises(Exception, match="(?i)unique"):
        with sessions.begin() as session:
            session.add(Nudge(circle_id=nudge.circle_id, from_learner_id=learner_id(sessions, "Amaka", demo.circle_id),
                              to_learner_id=nudge.to_learner_id, virtual_day=nudge.virtual_day,
                              created_at=demo.clock.now(session)))


def test_nudge_buttons_that_do_not_apply_are_refused(sessions, demo):
    bola, carol = quiet_bola(demo)
    payload = bola_payload(sessions, carol)
    expired = [t("button.expired")]
    assert [reply.text for reply in carol.tap(f"{nudges.SEND}not-a-uuid")] == expired
    assert [reply.text for reply in bola.tap(payload)] == expired  # nobody nudges themselves
    with sessions() as session:
        answered = session.scalar(select(FeedEvent.id).where(FeedEvent.circle_id == demo.circle_id,
                                                             FeedEvent.type == FeedType.ANSWERED).limit(1))
    assert [reply.text for reply in carol.tap(f"{nudges.SEND}{answered}")] == expired  # not a quiet spell
    stranger = demo.user("Dayo")
    with sessions.begin() as session:  # someone outside the circle
        stranger_id = session.scalar(select(Learner.id).where(Learner.channel_user_hash == stranger.hash))
        session.execute(delete(CircleMember).where(CircleMember.learner_id == stranger_id))
    assert [reply.text for reply in stranger.tap(payload)] == expired
    # A quiet member who deletes their data takes the prompt with them.
    bola.send("/delete_me")
    bola.tap(account.DELETE_YES)
    assert [reply.text for reply in carol.tap(payload)] == expired
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(Nudge)
                              .where(Nudge.circle_id == demo.circle_id)) == 0


# Upgrading a database that already holds queued nudges


def _table(name: str, *columns: tuple[str, sa.types.TypeEngine]) -> sa.TableClause:
    """A table as revision 0005 has it, independent of today's models."""
    return sa.table(name, *(sa.column(column, kind) for column, kind in columns))


GOALS = _table("goals", ("id", sa.Uuid()), ("code", sa.Text()), ("title", sa.Text()), ("syllabus_ref", sa.Text()))
CIRCLES = _table("circles", ("id", sa.Uuid()), ("goal_id", sa.Uuid()), ("name", sa.Text()), ("start_date", sa.Date()),
                 ("created_at", sa.DateTime(timezone=True)))
LEARNERS = _table("learners", ("id", sa.Uuid()), ("channel", sa.Text()), ("channel_user_hash", sa.Text()),
                  ("channel_chat_id", sa.Text()), ("first_name", sa.Text()), ("goal_id", sa.Uuid()),
                  ("is_simulated", sa.Boolean()), ("created_at", sa.DateTime(timezone=True)))
NUDGES = _table("nudges", ("id", sa.Uuid()), ("circle_id", sa.Uuid()), ("from_learner_id", sa.Uuid()),
                ("to_learner_id", sa.Uuid()), ("virtual_day", sa.Date()), ("created_at", sa.DateTime(timezone=True)))
DELIVERIES = _table("outbound_deliveries", ("id", sa.Uuid()), ("learner_id", sa.Uuid()), ("channel", sa.Text()),
                    ("channel_chat_id", sa.Text()), ("payload", sa.JSON()), ("deduplication_key", sa.Text()),
                    ("created_at", sa.DateTime(timezone=True)))


@pytest.mark.parametrize("dialect", ["sqlite", "postgres"])
def test_upgrading_expires_nudges_queued_before_expiry_existed(dialect, tmp_path):
    """Nudges left in the outbox by revision 0005 get their day's expiry, so they can't double up today."""
    with throwaway_database(dialect, tmp_path) as url:
        engine = build_engine(url)
        try:
            migrate(engine, "0005")
            now = datetime.now(UTC)  # a fresh database's clock has no offset
            yesterday = now - timedelta(days=1)
            goal_id, circle_id, amaka, bola, kept, orphan, update_id = (uuid4() for _ in range(7))

            def message(text: str) -> dict:
                return {"messages": [{"text": text, "buttons": []}], "scores": []}

            with engine.begin() as connection:
                connection.execute(sa.insert(GOALS).values(id=goal_id, code=f"test_{goal_id.hex}", title="Biology",
                                                           syllabus_ref="synthetic.json"))
                connection.execute(sa.insert(CIRCLES).values(id=circle_id, goal_id=goal_id, name="Circle",
                                                             start_date=yesterday.date(), created_at=yesterday))
                connection.execute(sa.insert(LEARNERS), [
                    dict(id=learner, channel="telegram", channel_user_hash=learner.hex, channel_chat_id=chat,
                         first_name=name, goal_id=goal_id, is_simulated=False, created_at=yesterday)
                    for learner, name, chat in ((amaka, "Amaka", "1000000001"), (bola, "Bola", "1000000002"))])
                connection.execute(sa.insert(NUDGES).values(id=kept, circle_id=circle_id, from_learner_id=amaka,
                                                            to_learner_id=bola, virtual_day=yesterday.date(),
                                                            created_at=yesterday))
                connection.execute(sa.insert(DELIVERIES), [
                    # Yesterday's nudge, still waiting; and one whose nudge row went with its sender's data.
                    dict(id=uuid4(), learner_id=bola, channel="telegram", channel_chat_id="1000000002",
                         payload=message("yesterday's nudge"), deduplication_key=f"nudge:{kept}",
                         created_at=yesterday),
                    dict(id=uuid4(), learner_id=bola, channel="telegram", channel_chat_id="1000000002",
                         payload=message("an orphaned nudge"), deduplication_key=f"nudge:{orphan}",
                         created_at=yesterday + timedelta(seconds=1)),
                    # Other messages never expire.
                    dict(id=update_id, learner_id=bola, channel="telegram", channel_chat_id="1000000002",
                         payload=message("circle update"), deduplication_key=f"feed:{uuid4()}",
                         created_at=yesterday + timedelta(seconds=2)),
                ])

            migrate(engine)
            migrate(engine)  # repeating the upgrade changes nothing
            sessions = session_factory(engine)
            expected = nudges.end_of_day(yesterday.date())
            with sessions() as session:
                expiries = dict(session.execute(select(OutboundDelivery.deduplication_key,
                                                       OutboundDelivery.expires_at)).all())
            assert {key.split(":")[0] if key.startswith("feed:") else key: moment and moment.replace(tzinfo=UTC)
                    for key, moment in expiries.items()} == {
                f"nudge:{kept}": expected, f"nudge:{orphan}": expected, "feed": None}

            # Today's nudge is the only one delivered today.
            clock = Clock(sessions)
            with sessions.begin() as session:
                today = virtual_day(clock.now(session))
                outbox.enqueue(session, session.get(Learner, bola), [nudges.message(session.get(Learner, amaka),
                                                                                   session.get(Learner, bola))],
                               key=f"nudge:{uuid4()}", now=clock.now(session), expires_at=nudges.end_of_day(today))
            registry, adapter = AdapterRegistry(), FakeAdapter()
            registry.register(adapter)
            assert asyncio.run(outbox.deliver_pending(sessions, registry, clock)) == 2
            assert [text for _, text, _ in adapter.sent] == ["circle update", t("nudge.message", name="Amaka")]
        finally:
            engine.dispose()

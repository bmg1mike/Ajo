"""Milestone 5: the batched circle feed and the outbox that delivers it."""
import asyncio
from datetime import date, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update

from app.adapters.base import AdapterRegistry, Button
from app.core import account, feed, lessons, outbox
from app.core.clock import virtual_day
from app.core.feed import MemberStatus
from app.core.privacy import delete_learner
from app.core.replies import Reply
from app.models import Channel, Circle, CircleMember, FeedEvent, Language, Learner, OutboundDelivery
from tests.test_lessons import library  # noqa: F401 - the shared two-topic library fixture
from tests.test_simulation import deliveries, demo, texts  # noqa: F401 - shared fixtures

MONDAY = date(2026, 10, 12)


def status(**overrides) -> MemberStatus:
    values = dict(learner_id=uuid4(), first_name="Tunde", share_scores=False, answered=False, correct=0, total=2,
                  last_checkin=None)
    return MemberStatus(**(values | overrides))


def test_status_lines_show_check_ins_and_scores_only_after_opt_in():
    today = MONDAY + timedelta(days=2)
    assert feed.status_line(status(answered=True, correct=1, last_checkin=today), today, Language.EN) == \
        "Tunde ✅ answered today's questions"
    assert feed.status_line(status(answered=True, correct=1, share_scores=True, last_checkin=today), today,
                            Language.EN) == "Tunde ✅ answered today's questions (1/2 correct)"
    assert feed.status_line(status(last_checkin=today), today, Language.EN) == "Tunde ✅ checked in today"
    assert feed.status_line(status(last_checkin=today - timedelta(days=1)), today, Language.EN) == \
        "Tunde ☑️ checked in yesterday"
    assert feed.status_line(status(last_checkin=MONDAY), today, Language.EN) == "Tunde ⏳ no check-in since Monday"
    assert feed.status_line(status(last_checkin=today - timedelta(days=7)), today, Language.EN) == \
        "Tunde ⏳ no check-in for over a week"
    assert feed.status_line(status(), today, Language.EN) == "Tunde ⏳ no check-in yet"
    # Unreviewed languages show English UI until a native speaker signs off (app/core/i18n.py).
    assert feed.status_line(status(last_checkin=MONDAY), today, Language.YO) == "Tunde ⏳ no check-in since Monday"


def answer_todays_questions(user) -> None:
    """Tap through today's lesson, always choosing option A (wrong for both main questions here)."""
    replies = user.send("/lesson")
    replies = user.tap(replies[-1].buttons[-1].payload)
    while options := [b.payload for b in replies[-1].buttons if b.payload.startswith(lessons.ANSWER)]:
        replies = user.tap(options[0])


def queue_updates(sessions, demo) -> None:
    with sessions.begin() as session:
        feed.queue_digests(session, demo.clock, session.get(Circle, demo.circle_id), demo.clock.now(session))


def updates_of(sessions, user) -> list[OutboundDelivery]:
    return [row for row in deliveries(sessions, user.hash) if row.deduplication_key.startswith("feed:")]


def sent_text(sessions, delivery: OutboundDelivery) -> str:
    """The update as it would be sent now, with each score applied only if its owner still shares it."""
    with sessions() as session:
        return outbox.render(session, delivery, 0).text


def latest_update(sessions, user) -> str:
    return sent_text(sessions, updates_of(sessions, user)[-1])


def test_the_feed_never_shows_a_score_unless_that_learner_opted_in(sessions, demo):
    demo.admin.send("/seed_demo")
    with sessions.begin() as session:
        halima = session.scalar(select(Learner).where(Learner.first_name == "Halima", Learner.is_simulated.is_(True),
                                                      Learner.goal_id == demo.library.goal_id))
        halima.share_scores = True
    demo.admin.send("/simulate_day")
    bola = demo.user("Bola")
    answer_todays_questions(bola)
    carol = demo.user("Carol")
    queue_updates(sessions, demo)  # Bola's and Carol's first updates of the day
    lines = latest_update(sessions, carol).splitlines()
    assert lines[0] == "🔵 Circle update"
    assert any(line.startswith("Halima ✅ answered today's questions (") and line.endswith("/2 correct)")
               for line in lines)
    assert {"Amaka ⏳ no check-in yet", "Chidi ✅ answered today's questions", "Bola ✅ answered today's questions",
            "Tunde ✅ answered today's questions"} <= set(lines)
    assert sum("correct)" in line for line in lines) == 1  # only Halima opted in
    assert not any(line.startswith("Carol") for line in lines)  # nobody is told their own status

    bola.tap(account.SHARE_SCORES)
    dayo = demo.user("Dayo")
    queue_updates(sessions, demo)
    assert "Bola ✅ answered today's questions (0/2 correct)" in latest_update(sessions, dayo).splitlines()

    # The next morning Bola has not answered the new lesson yet, so there is no score to show.
    demo.admin.send("/simulate_day")
    update = latest_update(sessions, demo.admin)
    assert "Bola ☑️ checked in yesterday" in update and "Bola ✅" not in update


def test_circle_updates_are_batched_to_one_message_per_learner_per_day(sessions, demo):
    demo.admin.send("/seed_demo")
    demo.admin.send("/simulate_day")
    bola = demo.user("Bola")
    with sessions.begin() as session:
        circle = session.get(Circle, demo.circle_id)
        now = demo.clock.now(session)
        # Bola's question is posted after today's update went out: it waits for tomorrow's.
        session.add(FeedEvent(circle_id=circle.id, type="question", actor_learner_id=session.scalar(
            select(Learner.id).where(Learner.channel_user_hash == bola.hash)), payload={"question": "Why?"},
            created_at=now))
        session.flush()
        assert feed.queue_digests(session, demo.clock, circle, now) == 1  # only Bola, who had none today
    updates = [row for row in deliveries(sessions, demo.admin.hash) if row.deduplication_key.startswith("feed:")]
    assert len(updates) == 1 and "Why?" not in texts(updates[0])[0]

    demo.admin.send("/simulate_day")
    updates = [row for row in deliveries(sessions, demo.admin.hash) if row.deduplication_key.startswith("feed:")]
    assert len(updates) == 2 and "❓ Bola asked the circle: Why?" in texts(updates[1])[0]
    demo.admin.send("/simulate_day")
    third = [row for row in deliveries(sessions, demo.admin.hash) if row.deduplication_key.startswith("feed:")][-1]
    assert "Why?" not in texts(third)[0]  # each event is relayed once
    # Bola's own question is never relayed back to Bola.
    assert all("Why?" not in text for row in deliveries(sessions, bola.hash) for text in texts(row))


def test_a_deleted_member_leaves_anonymously(sessions, demo):
    demo.admin.send("/seed_demo")
    demo.admin.send("/simulate_day")
    leaver = demo.user("Zainab")
    leaver.send("/delete_me")
    leaver.tap(account.DELETE_YES)
    demo.admin.send("/simulate_day")
    update = texts(deliveries(sessions, demo.admin.hash)[-1])[0]
    assert "A member left the circle." in update and "Zainab" not in update


def test_simulated_learners_are_never_queued(sessions, clock):
    persona = Learner(id=uuid4(), channel=Channel.WEB, channel_user_hash=uuid4().hex, channel_chat_id=None,
                      first_name="Chidi", is_simulated=True)
    with sessions.begin() as session:
        assert not outbox.enqueue(session, persona, [Reply("hi")], key=f"test:{uuid4()}", now=clock.now(session))


# Delivery


class FakeAdapter:
    channel = Channel.TELEGRAM

    def __init__(self, fail: Exception | None = None) -> None:
        self.sent: list[tuple[str, str, list[Button]]] = []
        self.fail = fail

    async def send_text(self, learner, text, buttons=None):
        if self.fail is not None:
            raise self.fail
        self.sent.append((learner.channel_chat_id, text, buttons or []))

    async def send_voice(self, learner, ogg_bytes): ...
    async def send_file(self, learner, filename, data): ...


@pytest.fixture
def real_learner(sessions, clock):
    with sessions.begin() as session:
        learner = Learner(channel=Channel.TELEGRAM, channel_user_hash=uuid4().hex, channel_chat_id=str(uuid4().int)[:10],
                          first_name="Amaka", created_at=clock.now(session))
        session.add(learner)
    return learner


def clear_other_deliveries(sessions, clock, learner):
    """Earlier tests share the database; only this learner's rows should be pending."""
    with sessions.begin() as session:
        session.execute(update(OutboundDelivery).where(OutboundDelivery.learner_id != learner.id,
                                                       OutboundDelivery.delivered_at.is_(None))
                        .values(delivered_at=clock.now(session)))


def test_queued_batches_are_sent_in_order_once(sessions, clock, real_learner):
    with sessions.begin() as session:
        assert outbox.enqueue(session, real_learner, [Reply("note"), Reply("lesson", [Button("Go", "ls:go:1")])],
                              key=f"lesson:{uuid4()}", now=clock.now(session))
        key = f"feed:{uuid4()}"
        assert outbox.enqueue(session, real_learner, [Reply("update")], key=key, now=clock.now(session))
        assert not outbox.enqueue(session, real_learner, [Reply("again")], key=key, now=clock.now(session))
    clear_other_deliveries(sessions, clock, real_learner)
    registry, adapter = AdapterRegistry(), FakeAdapter()
    assert asyncio.run(outbox.deliver_pending(sessions, registry, clock)) == 0  # no adapter yet: rows wait
    registry.register(adapter)
    assert asyncio.run(outbox.deliver_pending(sessions, registry, clock)) == 2
    assert [(chat, text, buttons) for chat, text, buttons in adapter.sent] == [
        (real_learner.channel_chat_id, "note", []), (real_learner.channel_chat_id, "lesson", [Button("Go", "ls:go:1")]),
        (real_learner.channel_chat_id, "update", [])]
    assert asyncio.run(outbox.deliver_pending(sessions, registry, clock)) == 0
    with sessions() as session:
        rows = session.scalars(select(OutboundDelivery).where(OutboundDelivery.learner_id == real_learner.id)).all()
        assert len(rows) == 2 and all(row.delivered_at is not None and row.attempts == 0 for row in rows)


def test_failed_deliveries_are_retried_then_given_up(sessions, clock, real_learner, caplog):
    with sessions.begin() as session:
        outbox.enqueue(session, real_learner, [Reply("update")], key=f"feed:{uuid4()}", now=clock.now(session))
    clear_other_deliveries(sessions, clock, real_learner)
    registry = AdapterRegistry()
    registry.register(FakeAdapter(fail=RuntimeError(f"chat {real_learner.channel_chat_id} blocked the bot")))
    for _ in range(outbox.MAX_ATTEMPTS + 2):
        assert asyncio.run(outbox.deliver_pending(sessions, registry, clock)) == 0
    with sessions() as session:
        row = session.scalar(select(OutboundDelivery).where(OutboundDelivery.learner_id == real_learner.id))
        assert row.attempts == outbox.MAX_ATTEMPTS and row.failed_at is not None and row.delivered_at is None
    assert real_learner.channel_chat_id not in caplog.text  # only the error type is logged


def test_deleting_a_learner_removes_their_queued_messages(sessions, clock, real_learner):
    with sessions.begin() as session:
        outbox.enqueue(session, real_learner, [Reply("update")], key=f"feed:{uuid4()}", now=clock.now(session))
    with sessions.begin() as session:
        assert delete_learner(session, real_learner.id, confirmed=True)
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(OutboundDelivery)
                              .where(OutboundDelivery.learner_id == real_learner.id)) == 0


class FlakyAdapter(FakeAdapter):
    """Fails the first message it is given, then works."""

    async def send_text(self, learner, text, buttons=None):
        if self.fail is not None:
            self.fail, error = None, self.fail
            raise error
        await super().send_text(learner, text, buttons)


def test_an_expired_batch_is_never_sent_even_after_failures(sessions, clock, real_learner):
    with sessions.begin() as session:
        now = clock.now(session)
        outbox.enqueue(session, real_learner, [Reply("nudge")], key=f"nudge:{uuid4()}", now=now,
                       expires_at=now + timedelta(hours=1))
        outbox.enqueue(session, real_learner, [Reply("update")], key=f"feed:{uuid4()}",
                       now=now + timedelta(seconds=1))
    clear_other_deliveries(sessions, clock, real_learner)
    registry, failing = AdapterRegistry(), FakeAdapter(fail=RuntimeError("down"))
    registry.register(failing)
    assert asyncio.run(outbox.deliver_pending(sessions, registry, clock)) == 0
    clock.advance(timedelta(hours=1))
    registry.unregister(Channel.TELEGRAM)
    registry.register(adapter := FakeAdapter())
    assert asyncio.run(outbox.deliver_pending(sessions, registry, clock)) == 1
    assert [text for _, text, _ in adapter.sent] == ["update"]  # the expired nudge is skipped, not blocking
    with sessions() as session:
        expired = session.scalar(select(OutboundDelivery).where(OutboundDelivery.learner_id == real_learner.id,
                                                                OutboundDelivery.deduplication_key.like("nudge:%")))
        assert (expired.delivered_at, expired.failed_at, expired.attempts) == (None, None, 1)


def test_a_failed_batch_holds_back_that_learners_later_batches(sessions, clock, real_learner):
    with sessions.begin() as session:
        for text in ("lesson 1", "update 1", "lesson 2"):
            outbox.enqueue(session, real_learner, [Reply(text)], key=f"test:{uuid4()}", now=clock.now(session))
    clear_other_deliveries(sessions, clock, real_learner)
    registry, adapter = AdapterRegistry(), FlakyAdapter(fail=RuntimeError("timeout"))
    registry.register(adapter)
    assert asyncio.run(outbox.deliver_pending(sessions, registry, clock)) == 0
    assert adapter.sent == []
    assert asyncio.run(outbox.deliver_pending(sessions, registry, clock)) == 3
    assert [text for _, text, _ in adapter.sent] == ["lesson 1", "update 1", "lesson 2"]


# Review fixes: cancelled deliveries, late opt-outs and oversized updates


def test_a_learner_deleted_during_delivery_gets_nothing_more(sessions, clock):
    with sessions.begin() as session:
        first, second = (Learner(channel=Channel.TELEGRAM, channel_user_hash=uuid4().hex, first_name=name,
                                 channel_chat_id=str(uuid4().int)[:10], created_at=clock.now(session))
                         for name in ("Amaka", "Bola"))
        session.add_all([first, second])
        session.flush()
        session.execute(update(OutboundDelivery).where(OutboundDelivery.delivered_at.is_(None))
                        .values(delivered_at=clock.now(session)))  # earlier tests share the database
        outbox.enqueue(session, first, [Reply("lesson"), Reply("note")], key=f"t:{uuid4()}", now=clock.now(session))
        outbox.enqueue(session, second, [Reply("update")], key=f"t:{uuid4()}", now=clock.now(session))

    class DeletingAdapter(FakeAdapter):
        """Both learners delete their data while the first message is being sent."""

        async def send_text(self, learner, text, buttons=None):
            await super().send_text(learner, text, buttons)
            if len(self.sent) == 1:
                for learner_id in (first.id, second.id):
                    with sessions.begin() as session:
                        delete_learner(session, learner_id, confirmed=True)

    registry, adapter = AdapterRegistry(), DeletingAdapter()
    registry.register(adapter)
    assert asyncio.run(outbox.deliver_pending(sessions, registry, clock)) == 0
    # The batches were loaded before the deletion, yet nothing after the first message went out.
    assert [text for _, text, _ in adapter.sent] == ["lesson"]


def test_a_score_opt_out_applies_to_updates_already_queued(sessions, demo):
    demo.admin.send("/seed_demo")
    with sessions.begin() as session:
        halima = session.scalar(select(Learner).where(Learner.first_name == "Halima", Learner.is_simulated.is_(True),
                                                      Learner.goal_id == demo.library.goal_id))
        halima.share_scores = True
    demo.admin.send("/simulate_day")
    (queued,) = updates_of(sessions, demo.admin)
    # Stored privately; the score is a substitution decided when the update is sent.
    assert "correct)" not in texts(queued)[0]
    assert [entry["learner_id"] for entry in queued.payload["scores"]] == [str(halima.id)]
    assert "correct)" in sent_text(sessions, queued)
    with sessions.begin() as session:
        session.get(Learner, halima.id).share_scores = False  # Halima opts out before delivery
    with sessions() as session:
        admin = session.scalar(select(Learner).where(Learner.channel_user_hash == demo.admin.hash))
    with sessions.begin() as session:
        session.execute(update(OutboundDelivery).where(OutboundDelivery.delivered_at.is_(None),
                                                       OutboundDelivery.learner_id != admin.id)
                        .values(delivered_at=demo.clock.now(session)))
    registry, adapter = AdapterRegistry(), FakeAdapter()
    registry.register(adapter)
    asyncio.run(outbox.deliver_pending(sessions, registry, demo.clock))
    update_text = adapter.sent[-1][1]
    assert update_text.startswith("🔵 Circle update") and "Halima ✅ answered today's questions" in update_text
    assert "correct)" not in update_text


def test_long_circle_posts_are_bounded_and_carried_into_the_next_update(sessions, demo):
    demo.admin.send("/seed_demo")
    bola = demo.user("Bola")
    with sessions.begin() as session:
        bola_id = session.scalar(select(Learner.id).where(Learner.channel_user_hash == bola.hash))
        now = demo.clock.now(session)
        # Twelve long questions, half of them sharing one timestamp, so the cursor must break ties by ID.
        for number in range(12):
            session.add(FeedEvent(circle_id=demo.circle_id, type="question", actor_learner_id=bola_id,
                                  payload={"question": f"Q{number:02d} " + "why " * 225},
                                  created_at=now if number % 2 else now + timedelta(microseconds=number)))
    seen = []
    for _ in range(3):
        demo.admin.send("/simulate_day")
        text = latest_update(sessions, demo.admin)
        assert len(text) <= feed.FEED_MAX_CHARS
        seen += [line.split("circle: ")[1][:3] for line in text.splitlines() if line.startswith("❓ Bola")]
        if "more circle post(s)" not in text:
            break
    assert sorted(seen) == [f"Q{number:02d}" for number in range(12)]  # every question exactly once
    assert len(updates_of(sessions, demo.admin)) == len({row.deduplication_key
                                                           for row in updates_of(sessions, demo.admin)})
    first = sent_text(sessions, updates_of(sessions, demo.admin)[0])
    assert "…and" in first and "more circle post(s) in your next update." in first

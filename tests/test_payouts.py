"""Milestone 7: weekly payouts, /progress, /pack and the opportunity list."""
import asyncio
import base64
import json
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select
from unittest.mock import AsyncMock

from app.adapters.base import AdapterRegistry
from app.adapters.telegram_bot import TelegramAdapter
from app.config import Settings
from app.core import account, lessons, opportunities, outbox, pack, payouts
from app.core.clock import virtual_day
from app.core.i18n import t
from app.core.privacy import delete_learner
from app.core.replies import Attachment, Reply
from app.db import build_engine, session_factory
from app.main import create_app
from app.models import (
    Channel, Circle, DailyRun, Goal, Learner, Lesson, Opportunity, OpportunityKind, OutboundDelivery, RunTrigger,
    WeeklyPayout,
)
from tests.conftest import migrate, throwaway_database
from tests.test_feed import FakeAdapter, clear_other_deliveries, real_learner  # noqa: F401 - shared fixture
from tests.test_lessons import library  # noqa: F401 - the shared two-topic library fixture
from tests.test_onboarding import SALT
from tests.test_simulation import deliveries, demo, texts  # noqa: F401 - shared fixtures

PERSONA_NAMES = ("Chidi", "Halima", "Tunde")


def payout_rows(sessions, user) -> list[OutboundDelivery]:
    return [row for row in deliveries(sessions, user.hash) if row.deduplication_key.startswith("payout:")]


def rendered(sessions, row: OutboundDelivery) -> list[Reply]:
    with sessions() as session:
        return outbox.replies_of(session, row)


def payout_of(sessions, demo, week: int) -> WeeklyPayout:
    with sessions() as session:
        return session.get(WeeklyPayout, (demo.circle_id, week))


def goal_of(sessions, demo) -> Goal:
    with sessions() as session:
        return session.get(Goal, demo.library.goal_id)


def add_opportunity(sessions, goal: Goal, title: str, *, deadline: date | None, verified_on: date,
                    country: str = "NG", goal_codes: list[str] | None = None) -> None:
    with sessions.begin() as session:
        session.add(Opportunity(title=title, provider="Synthetic Trust", url=f"https://example.org/{uuid4().hex}",
                                country=country, goal_codes=goal_codes or [goal.code], deadline=deadline,
                                verified_on=verified_on, eligibility="Synthetic eligibility."))


def answer(user, picks: list[str]) -> None:
    """Tap through today's lesson, choosing the given options for each question in turn."""
    replies = user.send("/lesson")
    replies = user.tap(replies[-1].buttons[-1].payload)
    for pick in picks:
        options = {b.text: b.payload for b in replies[-1].buttons if b.payload.startswith(lessons.ANSWER)}
        replies = user.tap(options[pick])
    assert not any(b.payload.startswith(lessons.ANSWER) for b in replies[-1].buttons)


# The weekly payout


def test_simulate_week_pays_out_once_with_all_four_components(sessions, demo):
    goal = goal_of(sessions, demo)
    start = virtual_day(demo.clock.now())
    payout_day = start + timedelta(days=7)
    add_opportunity(sessions, goal, "Expired by payout day", deadline=start + timedelta(days=3), verified_on=start)
    add_opportunity(sessions, goal, "Later deadline", deadline=start + timedelta(days=40), verified_on=start)
    add_opportunity(sessions, goal, "Nearest open deadline", deadline=start + timedelta(days=20), verified_on=start)
    add_opportunity(sessions, goal, "No stated deadline", deadline=None, verified_on=start)
    add_opportunity(sessions, goal, "Not re-checked lately", deadline=start + timedelta(days=10),
                    verified_on=start - timedelta(days=opportunities.VERIFICATION_MAX_AGE_DAYS))
    add_opportunity(sessions, goal, "Another country", deadline=start + timedelta(days=9), verified_on=start,
                    country="GH")
    add_opportunity(sessions, goal, "Another goal", deadline=start + timedelta(days=9), verified_on=start,
                    goal_codes=["waec_chem"])

    demo.admin.send("/seed_demo")
    (reply,) = demo.admin.send("/simulate_week")
    assert reply.text.count("⏩") == 7
    assert reply.text.endswith(t("admin.day_payout", week=1, count=1))  # on day 7, for the one real member

    payout = payout_of(sessions, demo, 1)
    assert payout.period_start is None and virtual_day(payout.period_end) == payout_day
    # Chidi and Halima every day, Tunde on days 1 and 2; Amaka has not answered.
    assert payout.summary == {"checkins": 16, "lesson_days": 7, "members": 4, "active_members": 3}

    (row,) = payout_rows(sessions, demo.admin)
    assert row.deduplication_key == f"payout:{demo.circle_id}:1:{row.learner_id}"
    progress, summary, opportunity, study_pack = rendered(sessions, row)
    assert progress.text == (f"{t('payout.intro', week=1)}\n\n📊 Your progress, week 1\n🔒 Only you can see this.\n"
                             "Days checked in: 0 of 7\nQuestions answered: 0 of 14\nAccuracy: no answers yet\n"
                             "Topics covered: none yet")
    assert summary.text == ("🔵 Circle summary, week 1\nTogether, the circle checked in 16 time(s) over 7 lesson "
                            "day(s).\n3 of 4 members checked in at least once.")
    assert opportunity.text.startswith("🎓 Opportunity: Nearest open deadline\nFrom: Synthetic Trust\n"
                                       "Who can apply: Synthetic eligibility.\n"
                                       f"Deadline: {(start + timedelta(days=20)).isoformat()}\nhttps://example.org/")
    assert opportunity.text.endswith(f"Last checked by the Ajo team: {start.isoformat()}")
    assert study_pack.file.filename == "ajo-week-1-study-pack.txt"
    assert study_pack.text == t("pack.caption", week=1, size=-(-len(study_pack.file.data) // 1000))
    text = study_pack.file.data.decode("utf-8")
    assert len(study_pack.file.data) < pack.PACK_MAX_BYTES
    assert text.startswith(f"Ajo study pack: Synthetic Biology, week 1\nLessons from {(start + timedelta(days=1))} "
                           f"to {payout_day}\n")
    for day in range(1, 8):
        assert f"📘 Day {day}: {('Photosynthesis', 'Excretion')[(day - 1) % 2]}" in text
    assert text.count("Answer: B. Chlorophyll") == 7 and text.count("Answer: C. Urea") == 7
    assert text.count(t("pack.not_answered")) == 14 and "Your answer" not in text
    assert text.count(t("question.easier")) == 14  # follow-ups are study material too
    assert "Source: Synthetic Book, 1.1 Section, p. 101" in text
    assert text.endswith("Sources and licences\n• Synthetic Book, Test Press. CC0. https://example.org\n")
    # Shared and private messages alike name no other member and carry nobody else's score.
    for message in (progress.text, summary.text, opportunity.text, text):
        assert not any(name in message for name in PERSONA_NAMES)

    with sessions() as session:
        persona_rows = session.scalar(select(func.count()).select_from(OutboundDelivery)
                                      .join(Learner, Learner.id == OutboundDelivery.learner_id)
                                      .where(Learner.is_simulated.is_(True)))
        assert persona_rows == 0
        assert session.scalar(select(func.count()).select_from(WeeklyPayout)
                              .where(WeeklyPayout.circle_id == demo.circle_id)) == 1


def test_simulate_week_runs_to_the_weekly_boundary_and_the_next_week_starts_after_it(sessions, demo):
    demo.admin.send("/seed_demo")
    for _ in range(5):
        (reply,) = demo.admin.send("/simulate_day")
        assert "💰" not in reply.text
    # The demo script's "/simulate_week to day 7": two days, then the payout.
    (reply,) = demo.admin.send("/simulate_week")
    assert reply.text.count("⏩") == 2 and reply.text.endswith(t("admin.day_payout", week=1, count=1))
    (reply,) = demo.admin.send("/simulate_week")
    assert reply.text.count("⏩") == 7 and reply.text.endswith(t("admin.day_payout", week=2, count=1))

    first, second = payout_of(sessions, demo, 1), payout_of(sessions, demo, 2)
    assert second.period_start == first.period_end
    assert virtual_day(second.period_end) - virtual_day(first.period_end) == timedelta(days=7)
    assert second.summary == {"checkins": 14, "lesson_days": 7, "members": 4, "active_members": 2}  # Tunde is quiet
    week_two = payout_rows(sessions, demo.admin)[-1]
    text = rendered(sessions, week_two)[-1].file.data.decode("utf-8")
    assert "📘 Day 7:" not in text and "📘 Day 8:" in text and "📘 Day 14:" in text


def test_a_week_is_paid_out_once_whichever_way_its_last_day_is_reached(sessions, demo):
    demo.admin.send("/seed_demo")
    demo.admin.send("/simulate_week")
    assert demo.engine.run_day(demo.circle_id, RunTrigger.SCHEDULER) is None  # the day is already claimed
    reports, failed = demo.engine.run_scheduled()
    assert demo.circle_id not in {report.circle_id for report in reports} and not failed
    with sessions.begin() as session:
        circle, now = session.get(Circle, demo.circle_id), demo.clock.now(session)
        assert payouts.queue_weekly(session, demo.clock, circle, now, 7) is None  # the week's row is the guard
        assert payouts.queue_weekly(session, demo.clock, circle, now, 6) is None  # not a weekly boundary
        assert payouts.queue_weekly(session, demo.clock, circle, now, 0) is None
    assert len(payout_rows(sessions, demo.admin)) == 1


def test_progress_is_private_and_the_circle_summary_holds_counts_only(sessions, demo):
    demo.admin.send("/seed_demo")
    bola = demo.user("Bola")
    bola.send("/share_scores")
    bola.tap(account.SHARE_SCORES)  # sharing scores in the daily feed does not put them in the circle summary
    demo.admin.send("/simulate_day")
    answer(demo.admin, ["A", "A", "C"])  # question 1 wrong, its easier follow-up right, question 2 right
    answer(bola, ["B", "C"])
    demo.admin.send("/simulate_week")

    (amaka_row,), (bola_row,) = payout_rows(sessions, demo.admin), payout_rows(sessions, bola)
    amaka, bola_messages = rendered(sessions, amaka_row), rendered(sessions, bola_row)
    assert amaka[0].text.endswith("Days checked in: 1 of 7\nQuestions answered: 2 of 14\n"
                                  "Accuracy: 50% (1 of 2 correct)\nTopics covered: Photosynthesis\n"
                                  "Weakest topic: Photosynthesis (1 of 2 correct)")
    assert bola_messages[0].text.endswith("Accuracy: 100% (2 of 2 correct)\nTopics covered: Photosynthesis\n"
                                          "Weakest topic: none, every answer was correct 🎉")
    # Everyone gets the same circle summary: counts, no names, no scores.
    assert amaka[1].text == bola_messages[1].text == (
        "🔵 Circle summary, week 1\nTogether, the circle checked in 18 time(s) over 7 lesson day(s).\n"
        "5 of 5 members checked in at least once.")
    assert payout_of(sessions, demo, 1).summary == {"checkins": 18, "lesson_days": 7, "members": 5,
                                                    "active_members": 5}
    for messages, other in ((amaka, "Bola"), (bola_messages, "Amaka")):
        assert not any(other in reply.text or (reply.file and other.encode() in reply.file.data)
                       for reply in messages)
    amaka_pack = amaka[-1].file.data.decode("utf-8")
    assert "Your answer: A ❌" in amaka_pack and "Your answer: C ✅" in amaka_pack
    assert amaka_pack.count("Your answer: A ✅") == 1  # the easier follow-up
    assert "Your answer: B ✅" in bola_messages[-1].file.data.decode("utf-8")


def test_a_payout_reaches_members_after_their_circle_update_and_their_file_is_sent(sessions, demo):
    demo.admin.send("/seed_demo")
    demo.admin.send("/simulate_week")
    learner = demo.admin
    with sessions() as session:
        row = session.scalar(select(Learner).where(Learner.channel_user_hash == learner.hash))
    clear_other_deliveries(sessions, demo.clock, row)
    registry, adapter = AdapterRegistry(), RecordingAdapter()
    registry.register(adapter)
    assert asyncio.run(outbox.deliver_pending(sessions, registry, demo.clock)) == 7 * 2 + 1
    kinds = [kind for kind, *_ in adapter.sent]
    assert kinds[-5:] == ["text", "text", "text", "text", "file"]
    assert adapter.sent[-6][2].startswith("🔵 Circle update")  # day 7's update comes first
    _, chat_id, filename, data = adapter.sent[-1]
    assert chat_id == learner.chat_id and filename == "ajo-week-1-study-pack.txt" and len(data) < pack.PACK_MAX_BYTES


class RecordingAdapter(FakeAdapter):
    async def send_text(self, learner, text, buttons=None):
        self.sent.append(("text", learner.channel_chat_id, text))

    async def send_file(self, learner, filename, data):
        self.sent.append(("file", learner.channel_chat_id, filename, data))


def test_reset_clears_payouts_and_deletion_leaves_only_the_anonymous_aggregate(sessions, demo):
    demo.admin.send("/seed_demo")
    demo.admin.send("/simulate_week")
    bola = demo.user("Bola")
    bola.send("/delete_me")
    bola.tap(account.DELETE_YES)
    demo.admin.send("/delete_me")
    demo.admin.tap(account.DELETE_YES)
    payout = payout_of(sessions, demo, 1)
    assert set(payout.summary) == {"checkins", "lesson_days", "members", "active_members"}
    assert all(isinstance(value, int) for value in payout.summary.values())
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(OutboundDelivery)
                              .where(OutboundDelivery.deduplication_key.like(f"payout:{demo.circle_id}:%"))) == 0

    demo.admin.onboard(demo.library.goal_id, name="Amaka")  # the admin rejoins, then resets the demo
    demo.admin.send("/reset_demo")
    demo.admin.tap("adm:reset:yes")
    assert payout_of(sessions, demo, 1) is None
    demo.admin.send("/seed_demo")
    (reply,) = demo.admin.send("/simulate_week")
    assert reply.text.count("⏩") == 7 and reply.text.endswith(t("admin.day_payout", week=1, count=1))


# /progress and /pack


def test_progress_and_pack_describe_the_current_week_or_the_one_just_paid_out(sessions, demo):
    assert [reply.text for reply in demo.admin.send("/progress")] == [t("progress.no_lessons")]
    assert [reply.text for reply in demo.admin.send("/pack")] == [t("pack.empty")]
    demo.admin.send("/seed_demo")
    for _ in range(3):
        demo.admin.send("/simulate_day")
    (progress,) = demo.admin.send("/progress")
    assert progress.text.startswith("📊 Your progress, week 1 so far\n🔒 Only you can see this.\n"
                                    "Days checked in: 0 of 3\nQuestions answered: 0 of 6")
    (study_pack,) = demo.admin.send("/pack")
    assert study_pack.file.filename == "ajo-week-1-so-far-study-pack.txt"
    assert study_pack.text.startswith("📦 Your study pack for week 1 so far (")
    assert "📘 Day 3:" in study_pack.file.data.decode("utf-8")

    demo.admin.send("/simulate_week")  # days 4 to 7, then the payout
    (progress,) = demo.admin.send("/progress")
    assert progress.text.startswith("📊 Your progress, week 1\n") and "Days checked in: 0 of 7" in progress.text
    answer(demo.admin, ["B", "C"])  # day 7's questions, after the morning payout, still count for week 1
    (progress,) = demo.admin.send("/progress")
    assert "Days checked in: 1 of 7\nQuestions answered: 2 of 14\nAccuracy: 100% (2 of 2 correct)" in progress.text
    assert demo.admin.send("/pack")[0].file.filename == "ajo-week-1-study-pack.txt"

    demo.admin.send("/simulate_day")  # day 8's lesson starts week 2
    (progress,) = demo.admin.send("/progress")
    assert progress.text.startswith("📊 Your progress, week 2 so far\n") and "Days checked in: 0 of 1" in progress.text
    assert "📘 Day 7:" not in demo.admin.send("/pack")[0].file.data.decode("utf-8")


def test_commands_need_a_circle(sessions, clock):
    with sessions.begin() as session:
        learner = Learner(channel=Channel.TELEGRAM, channel_user_hash=uuid4().hex, channel_chat_id="1",
                          first_name="Ada", created_at=clock.now(session))
        session.add(learner)
        session.flush()
        now = clock.now(session)
        assert payouts.progress_command(session, learner, now).text == t("lesson.not_in_circle")
        assert payouts.pack_command(session, learner, now).text == t("lesson.not_in_circle")


def test_a_pack_that_would_reach_the_limit_drops_lessons_from_the_end(sessions, demo, monkeypatch):
    demo.admin.send("/seed_demo")
    demo.admin.send("/simulate_week")
    with sessions() as session:
        learner = session.scalar(select(Learner).where(Learner.channel_user_hash == demo.admin.hash))
        week_lessons = session.scalars(select(Lesson).where(Lesson.circle_id == demo.circle_id)
                                       .order_by(Lesson.day_index)).all()
        full = pack.build(session, learner, week_lessons, goal_title="Biology", week=1)
        assert not full.trimmed and full.lessons == 7

        monkeypatch.setattr(pack, "PACK_MAX_BYTES", len(full.data))  # the limit is strict: equal is too big
        trimmed = pack.build(session, learner, week_lessons, goal_title="Biology", week=1)
        assert trimmed.trimmed and trimmed.lessons == 6 and len(trimmed.data) < len(full.data)
        text = trimmed.data.decode("utf-8")
        assert t("pack.trimmed") in text and "📘 Day 6:" in text and "📘 Day 7:" not in text

        monkeypatch.setattr(pack, "PACK_MAX_BYTES", 40)
        tiny = pack.build(session, learner, week_lessons, goal_title="Biology", week=1)
        assert len(tiny.data) < 40 and tiny.lessons == 1
        tiny.data.decode("utf-8")  # still valid text
        with pytest.raises(ValueError):
            pack.build(session, learner, [], goal_title="Biology", week=1)


# Delivery of files


def test_queued_files_are_stored_as_base64_and_sent_after_their_text(sessions, clock, real_learner):  # noqa: F811
    data = "Ọ̀sẹ̀ 1 ✅\n".encode() * 100
    with sessions.begin() as session:
        assert outbox.enqueue(session, real_learner, [Reply("Your pack", file=Attachment("pack.txt", data))],
                              key=f"payout:{uuid4()}", now=clock.now(session))
        row = session.scalar(select(OutboundDelivery).where(OutboundDelivery.learner_id == real_learner.id))
        assert row.payload["messages"][0]["file"] == {"filename": "pack.txt",
                                                      "data": base64.b64encode(data).decode("ascii")}
    clear_other_deliveries(sessions, clock, real_learner)
    registry, adapter = AdapterRegistry(), RecordingAdapter()
    registry.register(adapter)
    assert asyncio.run(outbox.deliver_pending(sessions, registry, clock)) == 1
    assert adapter.sent == [("text", real_learner.channel_chat_id, "Your pack"),
                            ("file", real_learner.channel_chat_id, "pack.txt", data)]


def test_telegram_replies_send_a_file_as_a_document():
    bot = AsyncMock()
    asyncio.run(TelegramAdapter(bot).reply("7012345678", Reply("📦 Your pack", file=Attachment("p.txt", b"x"))))
    assert bot.send_message.await_args.kwargs["text"] == "📦 Your pack"
    bot.send_document.assert_awaited_once_with(chat_id="7012345678", document=b"x", filename="p.txt")


# The opportunity list


def entry(**overrides) -> dict:
    return {"title": "Synthetic Scholarship", "provider": "Synthetic Trust", "url": "https://example.org/apply",
            "country": "NG", "goal_codes": ["jamb_bio"], "deadline": "2026-12-01", "verified_on": "2026-10-01",
            "eligibility": "Synthetic eligibility.", "kind": "scholarship", "priority": 5} | overrides


@pytest.mark.parametrize("bad", [
    {"url": "http://example.org/apply"}, {"url": "example.org"}, {"country": "Nigeria"}, {"goal_codes": []},
    {"eligibility": ""}, {"deadline": "soon"}, {"surprise": "field"}, {"kind": "prize"}, {"priority": 0},
])
def test_the_catalog_rejects_unchecked_or_malformed_entries(tmp_path, bad):
    path = tmp_path / "opportunities.json"
    path.write_text(json.dumps({"opportunities": [entry(**bad)]}))
    with pytest.raises(ValidationError):
        opportunities.load_catalog(path)


def test_the_catalog_rejects_duplicate_urls(tmp_path):
    path = tmp_path / "opportunities.json"
    path.write_text(json.dumps({"opportunities": [entry(), entry(title="Copy")]}))
    with pytest.raises(ValidationError):
        opportunities.load_catalog(path)


def test_the_checked_in_catalog_is_valid_and_offers_the_demo_goal_something():
    entries = opportunities.load_catalog(opportunities.Path("data/opportunities.json"))
    assert entries and all(entry.country == "NG" and "jamb_bio" in entry.goal_codes for entry in entries)
    assert all(entry.deadline is None or entry.deadline > entry.verified_on for entry in entries)
    latest = max(entry.verified_on for entry in entries)
    assert opportunities.main(["--today", latest.isoformat()]) == 0
    # The demo ends on "a real scholarship link" (spec section 17): a scholarship is ranked first.
    first = min(entries, key=lambda entry: entry.priority)
    assert first.kind == OpportunityKind.SCHOLARSHIP and first.url.startswith("https://")
    assert len({entry.priority for entry in entries}) == len(entries)  # an explicit order, never a tie


def test_sync_makes_the_table_match_the_file(sessions):
    first, second = (opportunities.Entry(**entry(url=f"https://example.org/{name}", title=name))
                     for name in ("one", "two"))
    with sessions.begin() as session:
        assert opportunities.sync(session, [first, second]) == 2
    renamed = first.model_copy(update={"title": "One, renamed", "deadline": None})
    with sessions.begin() as session:
        opportunities.sync(session, [renamed])
    with sessions() as session:
        (row,) = session.scalars(select(Opportunity)).all()
        assert (row.id, row.title, row.deadline) == (opportunities.opportunity_id(first), "One, renamed", None)
        assert row.eligibility == "Synthetic eligibility."
    with sessions.begin() as session:
        opportunities.sync(session, [])
        assert session.scalar(select(func.count()).select_from(Opportunity)) == 0


def test_choose_shows_open_recently_checked_entries_nearest_deadline_first(sessions):
    today = date(2026, 10, 14)
    with sessions.begin() as session:
        goal = Goal(code=f"test_{uuid4().hex}", title="Biology", syllabus_ref="x", country="NG")
        session.add(goal)
        session.flush()
        assert opportunities.choose(session, goal, today) is None
        assert opportunities.opportunity_reply(None, goal_language := "en").text == t("opportunity.none")

        def add(title, deadline, verified_on=today - timedelta(days=1), **extra):
            session.add(Opportunity(title=title, provider="P", url=f"https://example.org/{uuid4().hex}",
                                    country=extra.get("country", "NG"), goal_codes=[goal.code], deadline=deadline,
                                    verified_on=verified_on, eligibility="E"))
            session.flush()

        add("Rolling", None)
        assert opportunities.choose(session, goal, today).title == "Rolling"
        add("Closes today", today)
        add("Closed yesterday", today - timedelta(days=1))
        add("Checked tomorrow", today + timedelta(days=3), verified_on=today + timedelta(days=1))
        add("Stale", today + timedelta(days=2), verified_on=today - timedelta(days=91))
        add("Ghana", today + timedelta(days=1), country="GH")
        chosen = opportunities.choose(session, goal, today)
        assert chosen.title == "Closes today"
        assert f"Deadline: {today.isoformat()}" in opportunities.opportunity_reply(chosen, goal_language).text
        add("Just within the check window", today + timedelta(days=1), verified_on=today - timedelta(days=90))
        assert opportunities.choose(session, goal, today).title == "Closes today"
        # A day later that check is too old, and the entry checked "tomorrow" has become valid.
        assert opportunities.choose(session, goal, today + timedelta(days=1)).title == "Checked tomorrow"
        rolling = opportunities.choose(session, goal, today + timedelta(days=4))
        assert rolling.title == "Rolling" and t("opportunity.no_deadline") in opportunities.opportunity_reply(
            rolling, goal_language).text
        # The curator's priority comes before any deadline: a ranked scholarship beats a closing notice.
        session.add(Opportunity(title="Ranked scholarship", provider="P", url=f"https://example.org/{uuid4().hex}",
                                country="NG", goal_codes=[goal.code], deadline=None, verified_on=today,
                                eligibility="E", kind=OpportunityKind.SCHOLARSHIP, priority=1))
        session.flush()
        assert opportunities.choose(session, goal, today).title == "Ranked scholarship"


def test_the_command_line_check_reports_what_a_payout_would_show(tmp_path, capsys):
    path = tmp_path / "opportunities.json"
    path.write_text(json.dumps({"opportunities": [entry(), entry(url="https://example.org/old", title="Old",
                                                                 deadline="2026-10-01", verified_on="2026-09-01")]}))
    assert opportunities.main(["--file", str(path), "--today", "2026-10-14"]) == 0
    out = capsys.readouterr().out
    assert ("- [5] Synthetic Scholarship (Synthetic Trust, scholarship): deadline 2026-12-01, verified 2026-10-01; "
            "shown") in out
    assert ("- [5] Old (Synthetic Trust, scholarship): deadline 2026-10-01, verified 2026-09-01; hidden: deadline "
            "2026-10-01 has passed") in out
    assert "1 of 2 can be shown." in out
    assert opportunities.main(["--file", str(path), "--today", "2027-06-01"]) == 1  # nothing open by then
    path.write_text("{not json")
    assert opportunities.main(["--file", str(path)]) == 2


def test_startup_syncs_the_opportunity_file_and_refuses_a_malformed_one(database_url, engine, tmp_path):
    path = tmp_path / "opportunities.json"
    path.write_text(json.dumps({"about": "test", "opportunities": [entry(url="https://example.org/startup")]}))
    settings = Settings(_env_file=None, database_url=database_url, hash_salt=SALT, opportunities_file=path)
    with TestClient(create_app(settings)):
        pass
    with session_factory(engine)() as session:
        assert session.scalars(select(Opportunity.url)).all() == ["https://example.org/startup"]
    path.write_text(json.dumps({"opportunities": [entry(url="http://insecure.example.org")]}))
    with pytest.raises(ValidationError), TestClient(create_app(settings)):
        pass


@pytest.mark.parametrize("dialect", ["sqlite", "postgres"])
def test_upgrading_keeps_existing_opportunities_without_eligibility(dialect, tmp_path):
    opportunities_table = sa.table("opportunities", sa.column("id", sa.Uuid()), sa.column("title", sa.Text()),
                                   sa.column("provider", sa.Text()), sa.column("url", sa.Text()),
                                   sa.column("country", sa.String()), sa.column("goal_codes", sa.JSON()),
                                   sa.column("deadline", sa.Date()), sa.column("verified_on", sa.Date()))
    with throwaway_database(dialect, tmp_path) as url:
        engine = build_engine(url)
        try:
            migrate(engine, "0006")
            row_id = uuid4()
            with engine.begin() as connection:
                connection.execute(sa.insert(opportunities_table).values(
                    id=row_id, title="Old", provider="P", url="https://example.org/old", country="NG",
                    goal_codes=["jamb_bio"], deadline=None, verified_on=datetime.now(UTC).date()))
            migrate(engine)
            migrate(engine)
            with session_factory(engine)() as session:
                row = session.get(Opportunity, row_id)
                assert row.title == "Old" and row.eligibility is None
                assert (row.kind, row.priority) == (OpportunityKind.OTHER, 100)
                assert session.scalar(select(func.count()).select_from(WeeklyPayout)) == 0
        finally:
            engine.dispose()


def test_deleting_a_learner_keeps_other_learners_queued_files(sessions, clock):
    with sessions.begin() as session:
        now = clock.now(session)
        ada, bola = (Learner(channel=Channel.TELEGRAM, channel_user_hash=uuid4().hex,
                             channel_chat_id=str(uuid4().int)[:10], first_name=name, created_at=now)
                     for name in ("Ada", "Bola"))
        session.add_all([ada, bola])
        session.flush()
        data = base64.b64decode("+Ada/" * 4 + "AAAA")  # its base64 holds "Ada" as a whole word
        assert "+Ada/" in base64.b64encode(data).decode()
        key = f"payout:{uuid4()}"
        assert outbox.enqueue(session, bola, [Reply("Your pack", file=Attachment("pack.txt", data))], key=key, now=now)
        ada_id = ada.id
    with sessions.begin() as session:
        assert delete_learner(session, ada_id, confirmed=True)
    with sessions() as session:
        assert session.scalar(select(OutboundDelivery).where(OutboundDelivery.deduplication_key == key)) is not None


def test_a_deletion_while_the_caption_is_sent_stops_the_private_file(sessions, clock, real_learner):  # noqa: F811
    """The learner deletes their data after the caption is handed over and before the pack would follow."""
    with sessions.begin() as session:
        assert outbox.enqueue(session, real_learner, [Reply("Your pack", file=Attachment("pack.txt", b"private"))],
                              key=f"payout:{uuid4()}", now=clock.now(session))
    clear_other_deliveries(sessions, clock, real_learner)

    class DeletingAdapter(RecordingAdapter):
        async def send_text(self, learner, text, buttons=None):
            await super().send_text(learner, text, buttons)
            with sessions.begin() as session:
                assert delete_learner(session, learner.id, confirmed=True)

    registry, adapter = AdapterRegistry(), DeletingAdapter()
    registry.register(adapter)
    assert asyncio.run(outbox.deliver_pending(sessions, registry, clock)) == 0
    assert adapter.sent == [("text", real_learner.channel_chat_id, "Your pack")]  # never the file


@pytest.fixture
def history_without_payouts(sessions, demo, monkeypatch):
    """Seven days processed before payouts existed (an upgraded database): runs and lessons, no payout row."""
    demo.admin.send("/seed_demo")
    with monkeypatch.context() as patch:
        patch.setattr(payouts, "queue_weekly", lambda *args: None)
        for _ in range(7):
            demo.admin.send("/simulate_day")
    assert payout_of(sessions, demo, 1) is None
    with sessions() as session:
        return session.scalars(select(DailyRun.created_at).where(DailyRun.circle_id == demo.circle_id)
                               .order_by(DailyRun.virtual_day)).all()


def test_weeks_processed_before_payouts_existed_still_bound_the_next_week(sessions, demo, history_without_payouts):
    runs = history_without_payouts
    (progress,) = demo.admin.send("/progress")  # the unpaid week 1 has ended at its seventh run
    assert progress.text.startswith("📊 Your progress, week 1\n") and "Days checked in: 0 of 7" in progress.text
    demo.admin.send("/simulate_day")
    (progress,) = demo.admin.send("/progress")
    assert progress.text.startswith("📊 Your progress, week 2 so far\n") and "Days checked in: 0 of 1" in progress.text
    pack_text = demo.admin.send("/pack")[0].file.data.decode("utf-8")
    assert "📘 Day 7:" not in pack_text and "📘 Day 8:" in pack_text

    (reply,) = demo.admin.send("/simulate_week")  # days 9 to 14, then week 2's payout: seven lesson days, not 14
    assert reply.text.count("⏩") == 6 and reply.text.endswith(t("admin.day_payout", week=2, count=1))
    week_two = payout_of(sessions, demo, 2)
    assert week_two.period_start == runs[6] and week_two.summary["lesson_days"] == 7
    pack_text = rendered(sessions, payout_rows(sessions, demo.admin)[-1])[-1].file.data.decode("utf-8")
    assert [day for day in range(1, 15) if f"📘 Day {day}:" in pack_text] == list(range(8, 15))
    (progress,) = demo.admin.send("/progress")
    assert progress.text.startswith("📊 Your progress, week 2\n") and "of 7" in progress.text

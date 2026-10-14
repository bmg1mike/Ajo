"""Rehearse the minimum Telegram demo (spec section 17, without voice, web or USSD) and count LLM cache misses.

A synthetic admin goes through the script through the same `Conversation` the Telegram adapter
uses: onboarding, /seed_demo, day 1 with a wrong answer and its private follow-up, a cited
question and an off-topic one, days 2 to 5 until Tunde goes quiet, a nudge, day 6 when he
returns, /simulate_week to the day 7 payout (which must offer the top-ranked open scholarship with
its link), then /progress and /pack.

By default the LLM is switched off, so every response must come from the cache: a miss is counted
and the step falls back as it would in a demo with no network. With --live, the configured LLM
answers misses and the cache is warmed for the next rehearsal. Run it against a copy of the
database, because it seeds and simulates the demo circle and leaves the synthetic learner behind:

    docker compose exec db createdb -U ajo -T ajo ajo_rehearsal   # or pg_dump | psql if ajo has other clients
    uv run python -m app.sim.rehearse --database-url postgresql+psycopg://ajo:ajo@localhost:5432/ajo_rehearsal
"""
import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

from app.config import Settings
from app.core import lessons, opportunities, outbox, qa
from app.core.clock import Clock, virtual_day
from app.core.conversation import Conversation, Inbound
from app.core.embeddings import E5Embedder
from app.core.pack import PACK_MAX_BYTES
from app.core.lessons import LessonService
from app.core.privacy import channel_hash
from app.core.qa import QAService
from app.db import build_engine, session_factory
from app.llm.client import LLMClient, LLMUnavailable
from app.models import Channel, Goal, Language, Learner, OpportunityKind, OutboundDelivery, Question
from app.sim.engine import RESET_YES, DemoEngine

ADMIN_ID = "7000000001"  # synthetic, shaped like a Telegram user ID
OFF_TOPIC = "Who won the Super Eagles match?"


class CountingLLM(LLMClient):
    """Counts requests and the ones the cache could not answer while the LLM is off."""
    requests = misses = 0

    def complete(self, messages, *, json_output: bool = False) -> str:
        CountingLLM.requests += 1
        try:
            return super().complete(messages, json_output=json_output)
        except LLMUnavailable:
            CountingLLM.misses += 1
            raise


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rehearse the minimum demo and report LLM cache misses.")
    parser.add_argument("--database-url", required=True, help="a copy of the demo database; it will be changed")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--language", choices=[item.value for item in Language], default=Language.EN.value)
    parser.add_argument("--question", help="the cited question to ask (default: the first answerable QA eval question)")
    parser.add_argument("--live", action="store_true", help="let the configured LLM answer misses and warm the cache")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    settings = Settings(database_url=args.database_url)
    question = args.question or next(item["question"] for item in json.loads(
        Path("data/qa_eval_jamb_bio.json").read_text(encoding="utf-8"))["questions"] if item["answerable"])
    engine = build_engine(settings.database_url)
    sessions = session_factory(engine)
    clock = Clock(sessions)
    # Same model and URL as the demo, so the cache keys match; off unless --live.
    llm = CountingLLM.from_settings(settings, sessions, clock, enabled=args.live and settings.llm_enabled, cache=True)
    embedder = E5Embedder(settings.embed_model)
    salt = settings.hash_salt.get_secret_value()
    lesson_service = LessonService(embedder, llm, clock)
    demo = DemoEngine(sessions, clock, lesson_service, embedder, salt=salt, min_score=settings.retrieval_min_score,
                      goal_code=settings.demo_goal)
    conversation = Conversation(sessions, clock, QAService(embedder, llm, clock, min_score=settings.retrieval_min_score),
                                lesson_service, salt=salt, admin=demo, admin_ids=frozenset({int(ADMIN_ID)}))
    problems: list[str] = []

    def say(label: str, *, text: str | None = None, payload: str | None = None):
        replies = conversation.handle(Inbound(Channel.TELEGRAM, ADMIN_ID, ADMIN_ID, text=text, payload=payload))
        print(f"\n> {label}")
        for reply in replies:
            shown = reply.text if len(reply.text) <= 400 else reply.text[:400] + " …"
            print("  " + shown.replace("\n", "\n  ") + (f"\n  [file {reply.file.filename}, {len(reply.file.data)} "
                                                       "bytes]" if reply.file else ""))
        return replies

    def button(replies, prefix: str, index: int = 0) -> str:
        found = [b.payload for reply in replies for b in reply.buttons if b.payload.startswith(prefix)]
        if not found:
            raise SystemExit(f"Rehearsal stopped: no {prefix!r} button in the last replies")
        return found[index]

    try:
        with sessions.begin() as session:
            opportunities.sync(session, opportunities.load_catalog(settings.opportunities_file))
            goal = session.scalar(select(Goal).where(Goal.code == settings.demo_goal))
            if goal is None:
                print(f"Goal {settings.demo_goal!r} is not ingested in this database", file=sys.stderr)
                return 2
            goal_id = goal.id
        # Onboarding (step 2), unless an earlier rehearsal left the learner in place.
        replies = say("/start", text="/start")
        if any(b.payload.startswith("ob:") for r in replies for b in r.buttons):
            say("I agree", payload="ob:agree")
            say(f"language {args.language}", payload=f"ob:lang:{args.language}")
            say("first name", text="Amaka")
            say("goal", payload=f"ob:goal:{goal_id}")
        say("/reset_demo", text="/reset_demo")
        say("confirm reset", payload=RESET_YES)
        say(f"/seed_demo {args.seed}", text=f"/seed_demo {args.seed}")
        # Day 1 (step 3): the lesson, a wrong answer and the private follow-up.
        say("/simulate_day", text="/simulate_day")
        replies = say("/lesson", text="/lesson")
        replies = say("Answer questions", payload=button(replies, lessons.START_QUESTIONS))
        options = [b.payload for b in replies[-1].buttons if b.payload.startswith(lessons.ANSWER)]
        with sessions() as session:
            first = session.get(Question, UUID(options[0].removeprefix(lessons.ANSWER).rsplit(":", 1)[0]))
            wrong = next(p for p in options if not p.endswith(f":{first.correct_option}"))
        replies = say("a wrong answer", payload=wrong)
        if not any("❌" in reply.text for reply in replies):
            problems.append("the wrong answer got no private follow-up")
        # Ask (step 4): a cited answer, then the "ask my circle" fallback.
        replies = say(question, text=question)
        if not any(b.payload.startswith(qa.SHOW_SOURCE) for r in replies for b in r.buttons):
            problems.append("the library question was not answered with a citation")
        replies = say(OFF_TOPIC, text=OFF_TOPIC)
        if not any(b.payload.startswith(qa.ASK_CIRCLE) for r in replies for b in r.buttons):
            problems.append("the off-topic question did not fall back")
        # Days 2 to 5 (step 5): Tunde goes quiet; the day 5 update invites a nudge.
        for _ in range(4):
            say("/simulate_day", text="/simulate_day")
        with sessions() as session:
            learner = session.scalar(select(Learner).where(
                Learner.channel_user_hash == channel_hash(Channel.TELEGRAM, ADMIN_ID, salt)))
            update = session.scalars(select(OutboundDelivery).where(
                OutboundDelivery.learner_id == learner.id, OutboundDelivery.deduplication_key.like("feed:%"))
                .order_by(OutboundDelivery.created_at.desc())).first()
            nudge = [b["payload"] for b in update.payload["messages"][0]["buttons"] if b["payload"].startswith("nd:s:")]
        if not nudge:
            problems.append("no nudge prompt on day 5")
        else:
            say("Send Tunde a nudge", payload=nudge[0])
        replies = say("/simulate_day", text="/simulate_day")
        if "Tunde" not in replies[0].text.split("\n")[1]:
            problems.append("Tunde did not return the day after the nudge")
        # Payout (step 7).
        replies = say("/simulate_week", text="/simulate_week")
        if "💰" not in replies[0].text:
            problems.append("/simulate_week did not pay out")
        with sessions() as session:
            row = session.scalars(select(OutboundDelivery).where(
                OutboundDelivery.learner_id == learner.id, OutboundDelivery.deduplication_key.like("payout:%"))
                .order_by(OutboundDelivery.created_at.desc())).first()
            payout = outbox.replies_of(session, row) if row is not None else []
            # Spec section 17 ends on "a real scholarship link": the payout must offer the top-ranked open entry.
            expected = opportunities.choose(session, session.get(Goal, goal_id), virtual_day(row.created_at)) \
                if row is not None else None
        print("\n> the queued weekly payout")
        for reply in payout:
            print("  " + reply.text.replace("\n", "\n  ") +
                  (f"\n  [file {reply.file.filename}, {len(reply.file.data)} bytes]" if reply.file else ""))
        if len(payout) != 4 or payout[-1].file is None:
            problems.append("the payout does not have all four parts")
        elif len(payout[-1].file.data) >= PACK_MAX_BYTES:
            problems.append("the study pack is not under 500 KB")
        if expected is None or expected.kind != OpportunityKind.SCHOLARSHIP:
            problems.append("no open scholarship to offer; check data/opportunities.json")
        elif len(payout) > 2 and not (payout[2].text.startswith(f"🎓 Opportunity: {expected.title}\n")
                                      and expected.url in payout[2].text):
            problems.append(f"the payout did not offer the scholarship {expected.title!r} with its link")
        say("/progress", text="/progress")
        say("/pack", text="/pack")
    finally:
        llm.close()
        engine.dispose()
    print(f"\nLLM requests: {CountingLLM.requests}, cache misses: {CountingLLM.misses}"
          + (" (answered live)" if args.live else ""))
    if CountingLLM.misses and not args.live:
        problems.append(f"{CountingLLM.misses} LLM request(s) were not cached; rehearse once with --live to warm them")
    print("\n".join(["PASS"] if not problems else [f"PROBLEM: {problem}" for problem in problems]))
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())

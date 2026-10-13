# Ajo

**Learning circles for exam prep, built on messaging apps people already use.**

Across West Africa, an *ajo* (also called *esusu*, *susu*, *chama* or *tontine*) is a rotating savings group. A few people commit to paying in a little every week, and the group keeps each member on track. Ajo applies the same idea to studying. Learners join a small circle, get a short lesson every day, keep each other accountable, and at the end of each week receive a "payout" of progress and opportunities.

This repository holds a zero-cost proof of concept. It targets one goal (JAMB Biology), one country (Nigeria) and English plus one local language (Yoruba or Nigerian Pidgin).

## How it works

- **Daily micro-lessons.** An AI facilitator posts a lesson of at most 150 words with a worked example and two multiple-choice questions. Lessons come from a curated library of openly licensed Biology material, such as OpenStax and the official JAMB and WAEC syllabuses.
- **Answers with sources.** Learners can ask questions in plain language. Ajo answers only from the library and cites the source, section and page. If it can't find a reliable answer, it says so and offers to post the question to the circle. The rule is *no citation, no answer*.
- **Private follow-ups.** A wrong answer gets a private explanation, the source, and an easier related question.
- **Peer nudges, not bot spam.** When a member goes quiet, the rest of the circle is invited to send a nudge, and the nudge comes from a person. Ajo never sends automated reminders of its own.
- **Weekly payout.** Every seven days, each learner gets a private progress summary, the circle gets a shared check-in total, and everyone gets a study pack under 500 KB and one real opportunity, such as a scholarship, that matches their goal.

## Design principles

1. **Every answer is traceable.** Generated content is accepted only if its citations come from the retrieved material and pass a claim-support check.
2. **Private by default.** Circle members see each other's first name and check-in status only. Scores stay private unless a learner opts in.
3. **Nothing is gated.** Difficulty adapts, but every lesson and circle is open to everyone.
4. **Channel-agnostic core.** All logic lives in one core service. Telegram, a web simulator and USSD (for feature phones) are thin adapters.
5. **Low bandwidth first.** Text comes first, audio is available on request, and packs stay small.

## Demo setup

The demo runs without extra phone numbers, paid APIs or hosting. One real learner joins a circle of three simulated personas with deterministic behavior:

- **Chidi** checks in every day and is usually right.
- **Halima** checks in every day and gets about half the questions right.
- **Tunde** goes quiet after day 2 and returns once someone nudges him.

A database-backed virtual clock lets admin commands such as `/simulate_day` and `/simulate_week` play out a week in a few minutes. The full five-minute script is in section 17 of [spec.md](spec.md).

## Tech stack

Python 3.12, FastAPI, SQLAlchemy and Alembic, PostgreSQL 16 with pgvector, and local `multilingual-e5-small` embeddings. Answers come from any OpenAI-compatible LLM; the POC is configured for OpenAI GPT (Gemini and Groq also work). Learners use the bot through python-telegram-bot. The plan also uses faster-whisper or Groq Whisper for speech-to-text, Meta MMS-TTS for speech output, and the Africa's Talking USSD sandbox.

## Status

The **foundation** is complete: configuration, the database schema and migrations, the virtual clock, privacy helpers (salted identity hashing and learner deletion), and the adapter registry. **Corpus ingestion** is complete too: about 200 pages of OpenStax Biology, chunked, embedded locally and mapped to 10 JAMB topics. **Cited question answering** is complete: retrieval, a citation-enforced answer pipeline with a claim-support check, the library fallback with an "ask my circle" option, and a 20-question evaluation set. **Telegram onboarding** works: consent, language, first name, goal, and a place in the demo circle. After that, learners can ask questions, change their language, opt in to sharing scores and delete their data. Yoruba and Pidgin menu text is drafted but shows only after native-speaker review. The next steps are lessons, simulation, nudges and payouts. See [plan.md](plan.md) for the milestone checklist.

## Getting started

Requirements: [uv](https://docs.astral.sh/uv/) and Docker.

```bash
uv sync                        # install dependencies
cp .env.example .env           # then set HASH_SALT to a random string of 32+ characters
docker compose up -d db        # start PostgreSQL + pgvector
uv run alembic upgrade head    # create the schema (required before the app will start)
uv run fastapi dev             # http://localhost:8000
```

`GET /health` reports database connectivity and the current virtual time.

To build the corpus, run the command below. The first run downloads about 590 MB of OpenStax PDFs into `data/corpus/` and the embedding model. It prints a coverage report and writes nothing if any topic has fewer than five chunks.

```bash
uv run python -m ingest.ingest --sources ingest/sources.yaml
```

To answer questions, set `LLM_ENABLED=true`, `LLM_BASE_URL=https://api.openai.com/v1`, `LLM_API_KEY` and `LLM_MODEL` (for example `gpt-5.4-mini`) in `.env`. Responses are cached in the database by prompt hash, so a rehearsed demo makes no live LLM calls. Then run the evaluation set:

```bash
uv run python -m app.core.qa_eval                   # 20 questions; exits non-zero if acceptance fails
uv run python -m app.core.qa_eval --retrieval-only  # tune RETRIEVAL_MIN_SCORE without calling the LLM
```

To run the Telegram bot, create a bot with [@BotFather](https://t.me/BotFather) and set `TELEGRAM_ENABLED=true` and `TELEGRAM_BOT_TOKEN` in `.env`. Then run the corpus ingestion above and start the server. The bot long-polls, so it needs no public URL. Send `/start` to the bot in a private chat.

Yoruba and Pidgin menu text waits for a native speaker. Print a review sheet with `uv run python -m app.core.i18n --review yo` (or `pcm`). Once the sheet is signed off, add the language to `REVIEWED` in `app/core/i18n.py`.

Integrations (Telegram, LLM, speech-to-text) are off by default and are switched on with flags in `.env`. Each one asks for its credentials only when it is enabled. Never commit `.env`.

### Tests

```bash
uv run pytest                  # runs against SQLite
TEST_DATABASE_URL=postgresql+psycopg://ajo:ajo@localhost:5432/ajo uv run pytest   # also runs PostgreSQL tests
```

The PostgreSQL tests create and drop a temporary database, so that user needs the `CREATEDB` privilege.

## Privacy

The proof of concept uses **synthetic learner data only**, because free LLM tiers may train on submitted inputs. Channel user IDs and phone numbers are stored only as salted hashes. LLM requests never include names or identifiers. `/delete_me` removes a learner and every record that references them.

## Documentation

- [spec.md](spec.md): the full specification, including the data model, flows and acceptance criteria
- [plan.md](plan.md): the implementation plan and milestone progress
- [AGENTS.md](AGENTS.md): contributor guidelines

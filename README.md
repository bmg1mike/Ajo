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

The **foundation** is complete: configuration, the database schema and migrations, the virtual clock, privacy helpers (salted identity hashing and learner deletion), and the adapter registry. **Corpus ingestion** is complete too: about 200 pages of OpenStax Biology, chunked, embedded locally and mapped to 10 JAMB topics. **Cited question answering** is complete: retrieval, a citation-enforced answer pipeline with a claim-support check, the library fallback with an "ask my circle" option, and a 20-question evaluation set. **Telegram onboarding** works: consent, language, first name, goal, and a place in the demo circle. After that, learners can ask questions, change their language, opt in to sharing scores and delete their data. Yoruba and Pidgin menu text is drafted but shows only after native-speaker review. **Daily lessons** work: `/lesson` posts a cited lesson with a worked example and two questions, translated into the learner's language. A wrong answer gets a private explanation, the source and an easier question. Every generated lesson is validated, and a pre-generated lesson is the fallback. **Simulation** works too: admins can seed the demo circle with Chidi, Halima and Tunde, then play out virtual days with `/simulate_day` or `/simulate_week`. Each day posts the lesson, runs the personas (repeatable for a given seed) and sends every member one batched circle update. An optional scheduler posts lessons at the virtual lesson time. **Peer nudges** work: a member with no check-in for two whole days is detected on the third day, the rest of the circle gets a "Send Tunde a nudge" button in that day's update, and the first tap each day sends the quiet member a nudge from that person with a button to get today's lesson. The **weekly payout** works too. Every seventh day, each learner privately gets a progress summary (days checked in, accuracy, topics covered, weakest topic). Everyone gets the same circle summary, which holds counts only. Each learner also gets one hand-checked opportunity and a study pack under 500 KB. `/progress` and `/pack` show the current week on request. Milestones 1–7 make up the minimum Telegram demo. **Voice** works too: a voice note is transcribed (local faster-whisper or Groq Whisper) and answered like a typed question, and a lesson's 🔊 Listen button plays it as a voice note made with Meta's MMS-TTS. Learners hear their own language only once a native speaker has approved that voice; until then they get English audio with a note. The **web simulator** works too: one browser page with three phone-style panes drives the same circle, so the whole engagement demo runs without Telegram. A pane can onboard a new learner or take control of a persona, which pauses that persona's automated behavior while the pane answers, asks and nudges for it. USSD comes next. See [plan.md](plan.md) for the milestone checklist.

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

Before a demo, pre-generate and validate a fallback lesson (with Yoruba and Pidgin translations) for every topic. It exits non-zero if any topic or translation failed, and re-runs are served from the cache:

```bash
uv run python -m app.core.lessons --pregenerate
```

To run the Telegram bot, create a bot with [@BotFather](https://t.me/BotFather) and set `TELEGRAM_ENABLED=true` and `TELEGRAM_BOT_TOKEN` in `.env`. Then run the corpus ingestion above and start the server. The bot long-polls, so it needs no public URL. Send `/start` to the bot in a private chat.

To run the demo, put your numeric Telegram user ID in `ADMIN_IDS`, onboard with `/start`, then:

```text
/seed_demo 42     # add Chidi, Halima and Tunde to the demo circle (the seed makes their behavior repeatable)
/simulate_day     # advance the virtual clock 24 hours: lesson, persona check-ins, one circle update per member
/simulate_week    # simulate up to the next weekly payout (days 1 to 7 from a fresh demo)
/demo_status      # virtual date, members and last check-ins
/reset_demo       # wipe circle state and the clock offset (asks to confirm); learners and the library stay
```

Learners can send `/progress` for their private progress this week and `/pack` for the week's study pack.

Opportunities come from `data/opportunities.json`, a hand-checked list that is synced into the database at startup. Re-check each official page and update its `verified_on` before a demo. Each entry has a `priority` (1 is offered first; a scholarship is ranked first for the demo). Entries whose deadline has passed, or that nobody has checked for 90 days, are not shown. To see what a payout would offer:

```bash
uv run python -m app.core.opportunities
```

To rehearse the minimum demo with no live LLM calls, run the rehearsal against a copy of the database (if another client such as DBeaver is connected to `ajo`, copy it with `pg_dump ajo | psql ajo_rehearsal` instead). It reports every LLM request the cache could not answer. Add `--live` once to warm the cache, and `--language yo` to rehearse in Yoruba.

```bash
docker compose exec db createdb -U ajo -T ajo ajo_rehearsal
uv run python -m app.sim.rehearse --database-url postgresql+psycopg://ajo:ajo@localhost:5432/ajo_rehearsal
```

Set `SCHEDULER_ENABLED=true` to post each day's lesson automatically when the virtual clock passes `LESSON_TIME` (UTC). A day is processed once whether the scheduler or `/simulate_day` reaches it first.

Yoruba and Pidgin menu text waits for a native speaker. Print a review sheet with `uv run python -m app.core.i18n --review yo` (or `pcm`). Once the sheet is signed off, add the language to `REVIEWED` in `app/core/i18n.py`.

Voice needs `STT_ENABLED=true` for voice notes (`STT_BACKEND=local` downloads the faster-whisper `STT_LOCAL_MODEL` on the first note; `groq` needs `GROQ_API_KEY`) and `TTS_ENABLED=true` for the Listen button. TTS needs an `ffmpeg` with libopus: `brew install ffmpeg`, or point `FFMPEG_PATH` at a standalone build. The app refuses to start with TTS on and no usable ffmpeg. The MMS voices are licensed CC BY-NC 4.0, which suits this non-commercial proof of concept only. Add `--voice` to the rehearsal to tap Listen and ask a question by voice (spoken by the English MMS voice, so no microphone is needed). Yoruba lesson audio waits for a native speaker. Write the audio files and a listening sheet with `uv run python -m app.speech.tts --review yo --out review-yo`, and once it is signed off, add the language to `AUDIO_REVIEWED` in `app/speech/tts.py`.

To run the demo in the browser instead of Telegram (or beside it), set `WEB_ENABLED=true` and a `WEB_TOKEN` of 16 or more characters, start the server and open <http://localhost:8000/sim>. Enter the token once per tab. The console runs the same admin commands as Telegram (seed, simulate a day or a week, status, reset). Each of the three panes can onboard a new learner with `/start` or speak for Chidi, Halima or Tunde. Choosing a persona pauses its automated behavior and sends it what a real member gets, until the pane switches away or the page closes. Panes poll for lesson posts, circle updates, nudges and payouts every two seconds. The token matters: the simulator can speak for any web learner and move the virtual clock, and the USSD tunnel makes the app reachable from outside.

Integrations (Telegram, LLM, speech-to-text, text-to-speech, web simulator) are off by default and are switched on with flags in `.env`. Each one asks for its credentials only when it is enabled. Never commit `.env`.

### Tests

```bash
uv run pytest                  # runs against SQLite
AJO_SPEECH_TESTS=1 uv run pytest -m speech   # real MMS-TTS, ffmpeg and faster-whisper (downloads ~600 MB)
TEST_DATABASE_URL=postgresql+psycopg://ajo:ajo@localhost:5432/ajo uv run pytest   # also runs PostgreSQL tests
```

The PostgreSQL tests create and drop a temporary database, so that user needs the `CREATEDB` privilege.

## Privacy

The proof of concept uses **synthetic learner data only**, because free LLM tiers may train on submitted inputs. Channel user IDs and phone numbers are stored only as salted hashes. LLM requests never include names or identifiers. Voice notes are downloaded only for onboarded learners, never stored, and sent to Groq only when `STT_BACKEND=groq`; the transcript is redacted like typed text before QA. `/delete_me` removes a learner and every record that references them.

## Documentation

- [spec.md](spec.md): the full specification, including the data model, flows and acceptance criteria
- [plan.md](plan.md): the implementation plan and milestone progress
- [AGENTS.md](AGENTS.md): contributor guidelines

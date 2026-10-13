# Ajo Implementation Plan

## Objective and Scope

Implement the learning-circle proof of concept defined in `spec.md`: a messaging-first JAMB Biology companion for Nigeria with cited answers, daily lessons, peer accountability, and weekly study payouts. Use synthetic learners and free or local services.

The repository currently contains a FastAPI starter in `main.py`; the architecture in the specification remains to be built. Retain the project's Python 3.12+ requirement. Milestones 1–7 below deliver the minimum demo; voice, the web simulator, and USSD are additive milestones 8–10.

Exclude WhatsApp, production authentication, automated opportunity scraping, additional subjects, and large-scale circle matching.

## Implementation Rules

- Keep business logic in `app/core/`; Telegram, web, and USSD adapters call the same services through an adapter registry.
- Use the database-backed virtual clock for all business timestamps and scheduling decisions.
- Accept generated answers only when their citations and supporting claims pass validation.
- Share first names and check-in status by default; expose scores only after explicit opt-in.
- Keep secrets in an ignored `.env`, document configuration in `.env.example`, and exclude learner identifiers from LLM inputs.
- Add focused pytest tests alongside each milestone, using deterministic fixtures and mocked external providers.

## Foundation: Application, Storage, and Configuration

Complete before milestone 1.

- [x] Create the package layout from specification section 4: `app/core/`, `app/llm/`, `app/adapters/`, `app/sim/`, `app/speech/`, `ingest/`, `data/`, `web/`, and `tests/`.
- [x] Move the FastAPI application into `app/main.py`; update startup instructions and avoid duplicate application instances.
- [x] Add dependencies incrementally with uv and update `uv.lock`. Configure pytest and document its command.
- [x] Add environment-driven settings for the variables in specification section 13 and validate required settings when their integrations are enabled.
- [x] Provide Docker Compose for PostgreSQL 16 with pgvector, SQLAlchemy sessions, and versioned schema migrations.
- [x] Implement the entities in section 5, including 384-dimensional chunk embeddings. Add supporting storage for clock offset, LLM cache, and outbound delivery as needed.
- [x] Implement `clock.now()`, persisted offset advancement, the adapter protocol, and application lifecycle hooks for background services.
- [x] Add privacy helpers for salted channel hashes and confirmed learner deletion, including dependent records and delivery identifiers.

**Completion check:** A fresh database initializes successfully; the API starts; clock state persists; privacy and configuration tests pass.

**Validation:** `TEST_DATABASE_URL=postgresql+psycopg://ajo:ajo@localhost:5432/ajo uv run pytest` passes across SQLite and PostgreSQL; `uv run alembic upgrade head` and `uv run alembic check` succeed. Integration tests create a fresh disposable database and verify clock persistence, configuration, privacy, and application lifecycle.

## Milestone 1: Corpus Ingestion

Depends on the foundation.

- [x] Curate licensed or official public sources in `ingest/sources.yaml`, recording publisher, URL, and license. Download approximately 200 pages of Biology content.
- [x] Define 10 ordered JAMB Biology topics in `data/syllabus_jamb_bio.json`.
- [x] Extract text with page metadata; create 300–500-token chunks with 50-token overlap and respect section boundaries where detectable.
- [x] Map chunks to syllabus topics with embedding similarity and manual spot checks.
- [x] Embed locally with `intfloat/multilingual-e5-small`, using `passage: ` prefixes, and store vectors and source metadata.
- [x] Provide `uv run python -m ingest.ingest --sources ingest/sources.yaml` and a source/topic coverage report.

**Completion check:** Approximately 200 pages ingested; every topic has at least five chunks; citations retain source, section, and page information.

**Validation:** `uv run python -m ingest.ingest --sources ingest/sources.yaml` ingests 213 pages (35 sections of OpenStax *Concepts of Biology* and *Biology 2e*) into 300 chunks (only section-final chunks fall below 300 tokens); every topic has at least 16 chunks, and a second run updates rows in place. `AJO_CORPUS_TESTS=1 uv run pytest -m corpus` checks these criteria against the real PDFs and model; `tests/test_ingest.py` covers chunking, section and page extraction, topic mapping, idempotent persistence, and the citation guard and single-goal scoping on SQLite and PostgreSQL. Five sections are pinned to topics in `sources.yaml` after spot checks of the report. Note: OpenStax now licenses both books CC BY-NC-SA 4.0, not CC BY 4.0 as `spec.md` states, and the syllabus objectives in `data/syllabus_jamb_bio.json` still need review against the official JAMB syllabus.

## Milestone 2: Cited Question Answering

Depends on milestone 1.

- [x] Build the configurable OpenAI-compatible LLM client with retries, backoff, and prompt-hash caching; centralize prompt templates.
- [x] Embed questions with `query: ` prefixes and retrieve the top eight chunks filtered to the learner's goal.
- [x] Start `RETRIEVAL_MIN_SCORE` at 0.80 and tune it against the evaluation set.
- [x] Validate output schema, answerability, nonempty citations, membership of citation IDs in the retrieved set, and a second claim-support check.
- [x] Render source title, section, and page; limit source excerpts to 600 characters. Log all outcomes in `qa_log`.
- [x] Implement the library fallback and the learner-controlled option to ask the circle.
- [x] Create the 20-question evaluation set: 15 answerable Biology questions and five out-of-scope questions.

**Completion check:** At least 13/15 answerable questions receive valid cited answers; all 5/5 out-of-scope questions fall back; no accepted citation comes from outside the retrieved set.

**Validation:** `uv run python -m app.core.qa_eval` with OpenAI `gpt-5.4-mini` (`LLM_REASONING_EFFORT=low`) answers 14/15 answerable questions with valid citations, falls back on 5/5 out-of-scope questions (three below the retrieval threshold, two returned unanswerable by the model), and accepts zero citations outside the retrieved set. The one miss (osmosis) is a strict claim-check rejection, which fails safe. `AJO_QA_EVAL=1 uv run pytest -m qa_eval` reruns the check; with a warm cache it makes no live LLM calls. Retrieval ranks an expected section first for all 15 answerable questions. Their top scores range from 0.815 to 0.899, and out-of-scope top scores range from 0.738 to 0.822. No threshold separates the two sets, so `RETRIEVAL_MIN_SCORE` stays at 0.80: it keeps every answerable question and leaves the two highest-scoring out-of-scope questions to the model and the claim check. `tests/test_qa.py` covers client retries, cache and offline behaviour, goal-scoped retrieval on SQLite and PostgreSQL, every rejection path, payload privacy, source excerpts, and asking the circle. Implementation notes: the model cites short labels (`C1`–`C8`) that are mapped back to chunk IDs, and any unknown label rejects the answer. The claim check sees only the cited chunks, so an answer relying on an uncited chunk is rejected. Its verdict must be exactly yes or no; qualified replies fall back. Phone numbers, numeric IDs and emails are redacted by shape from LLM prompts, `qa_log` and circle posts. Posting to the circle records a `question` feed event that milestone 5 delivers with the batched feed. Fixed UI strings are English only until the milestone 3 language review.

## Milestone 3: Telegram Onboarding

Depends on the foundation and milestone 2 for free-text QA.

- [x] Implement async Telegram long polling and adapter delivery methods.
- [x] Add `/start`, consent, language selection, first-name collection, JAMB Biology selection, and demo-circle enrollment.
- [ ] Implement fixed UI strings in `app/core/i18n.py` and arrange native-speaker review. *(Strings are implemented; the Yoruba and Pidgin drafts await review.)*
- [ ] Support English plus a reviewed local language for the POC; prepare `en`, `yo`, and `pcm` language handling as specified. *(Handling for all three is prepared; no local language has been reviewed yet.)*
- [x] Route `/ask` and ordinary free text through QA. Add `/language`, `/share_scores`, and confirmed `/delete_me`.
- [x] Ensure learners start at difficulty level 2 without a placement test or access restrictions.

**Completion check:** A Telegram test account completes consent and onboarding, joins the circle, receives cited QA or fallback, and can delete its data.

**Validation:** `tests/test_telegram.py` runs python-telegram-bot's real polling loop against a fake Bot API, on SQLite and PostgreSQL. In that test a synthetic account consents, chooses a language, gives a first name, picks the goal, joins the demo circle, gets a cited answer and a fallback, and deletes its data. `tests/test_onboarding.py` covers the core flow. Nothing is stored before consent, and stale, repeated or out-of-order taps keep the current step. It also checks name validation (including decomposed Yoruba tone marks), level 2 with no placement test, the account commands, QA routing, and deletion during and after onboarding. Two Postgres-only tests race the final onboarding tap and the first demo-circle insert and check that each produces one row. **Still open:** the manual check with a real Telegram account and bot token, and native-speaker review of the Yoruba and Pidgin drafts (`uv run python -m app.core.i18n --review yo` prints the review sheet). Until a language is added to `i18n.REVIEWED`, its learners see English menus while their QA answers are generated in their language, and the bot says so when they pick it. Implementation notes: onboarding progress is kept in an `onboarding` table keyed by the salted channel hash. It is created on "I agree" and deleted when the learner row is created, so circle code only ever sees fully onboarded learners. The demo circle has an ID derived from the goal code (`circles.demo_circle`), and milestone 5's `/seed_demo` should seed the personas into it. `/delete_me` records an anonymous `member_left` feed event, with no actor and an empty payload, for milestone 5's feed to render. First names need at least two letters (counted as letters, so a letter plus a combining tone mark is still one). QA redaction NFC-normalizes the question and the learner's identifiers and redacts identifiers of any length, so a name typed with combining marks, in another case, or one letter long never reaches the LLM. Tests inspect the recorded LLM payloads. The deletion scan now matches identifiers as whole words, so a learner named "Ada" no longer deletes other members' rows that mention "adaptation". Updates are processed one at a time (the PTB default), so one slow LLM call delays other users; this is acceptable for the demo. The project uses python-telegram-bot 22.8; the spec names v21, and v22 keeps the same async API.

## Milestone 4: Daily Lessons and Grading

Depends on milestones 1–3.

- [ ] Retrieve six chunks for the next ordered topic and generate a lesson of at most 150 words, one worked example, and two multiple-choice questions.
- [ ] Validate structured content and citations; retry once on failure, then use a validated pre-generated lesson.
- [ ] Translate lesson bodies and answers while preserving original citations and source excerpts; retain English Biology terms in brackets where appropriate.
- [ ] Deliver questions individually, record answers and check-ins, and provide private explanations plus an easier related question after incorrect answers.
- [ ] Implement `/lesson` and emit score-safe answer feed events.
- [ ] Add `uv run python -m app.core.lessons --pregenerate` for all 10 topics.

**Completion check:** A learner receives a cited lesson, answers both questions, and receives a private follow-up for an incorrect answer; invalid generation uses the fallback.

## Milestone 5: Virtual Days, Personas, and Feed

Depends on milestones 3–4.

- [ ] Seed the goal, circle, admin learner, and Chidi, Halima, and Tunde with repeatable random seeds.
- [ ] Implement their specified check-in and answer behavior; simulated members have no chat IDs and receive no external messages.
- [ ] Restrict `/seed_demo`, `/simulate_day`, `/simulate_week`, `/reset_demo`, and `/demo_status` to `ADMIN_IDS`.
- [ ] Advance the clock by 24 hours per simulated day and run persona actions, lessons, inactivity checks, and feed delivery in a defined sequence.
- [ ] Connect APScheduler to virtual-time-aware job decisions and prevent duplicate daily processing.
- [ ] Batch ordinary circle updates to at most one message per learner per day. Reset circle state and clock without deleting the corpus.

**Completion check:** Repeating a seeded simulation produces the same behavior; timestamps use the virtual clock; unauthorized admin commands fail; feeds obey privacy and batching rules.

## Milestone 6: Inactivity and Peer Nudges

Depends on milestone 5.

- [ ] Detect quiet members and send the first nudge prompt to other circle members only.
- [ ] Deliver nudges immediately, attributed to the initiating member, with lesson-request buttons.
- [ ] Enforce one delivered nudge per recipient per virtual day across all senders, including concurrent requests.
- [ ] Make Tunde return on the next simulated day after a nudge; avoid unsolicited automated reminders.

**Completion check:** Detection occurs on day 3 after the last check-in, only one daily nudge reaches the recipient, and Tunde returns as specified. Pin the exact boundary in a test because section 7.6 describes two days of inactivity while section 16 states day-3 detection.

## Milestone 7: Weekly Payout and Minimum Demo

Depends on milestones 4–6.

- [ ] Produce private progress summaries, including accuracy and weakest topic, and a circle summary containing aggregate check-ins only.
- [ ] Generate a text or compressed PDF study pack with lessons, questions, answers, and citations, strictly under 500 KB.
- [ ] Curate `data/opportunities.json`; filter by goal, country, and deadline, and display the verification date.
- [ ] Implement `/progress` and `/pack`, with payouts every seven virtual days.
- [ ] Ensure `/simulate_week` produces one payout at the weekly boundary without duplicating scheduler payouts.
- [ ] Cache the rehearsed QA and lesson responses and verify the minimum demo with no live LLM calls.

**Completion check:** A simulated week delivers all four payout components, protects individual scores, and meets the pack-size limit. Milestones 1–7 now support the minimum Telegram demo.

## Milestone 8: Voice Input and Output

Depends on milestones 2–4; optional after the minimum demo.

- [ ] Add configurable local faster-whisper or Groq Whisper transcription for Telegram OGG/Opus voice notes.
- [ ] Add MMS-TTS generation, convert WAV to OGG/Opus with ffmpeg, and implement the lesson Listen action.
- [ ] Review local-language output with a native speaker; offer English audio with a note when local output is unavailable or unapproved.

**Completion check:** An English voice question receives cited QA, and a lesson plays reviewed local-language audio or the documented English fallback.

## Milestone 9: Web Simulator

Depends on milestone 5; optional, but required for the full demo's channel fallback.

- [ ] Serve `web/simulator.html` at `GET /sim` with up to three learner chat panes.
- [ ] Implement `POST /web/message` and `GET /web/poll?learner_id=...&since=...`, polling every two seconds.
- [ ] Support learner selection and pause automated persona behavior while a pane controls that persona.
- [ ] Route all interactions through the same core services and provide equivalent lesson, nudge, QA, and payout delivery.

**Completion check:** Three panes drive the same circle; manual persona control suppresses automation; the engagement demo works without Telegram.

## Milestone 10: USSD Sandbox

Depends on milestones 3–4 and demo seeding; optional.

- [ ] Implement `POST /ussd` for Africa's Talking callback fields and `CON`/`END` responses.
- [ ] Hash incoming phone numbers immediately and link the sandbox identity to a seeded learner.
- [ ] Provide check-in, today's question, and private progress menus; record answers and return result plus source title.
- [ ] Document sandbox setup and the tunnel needed for callbacks.

**Completion check:** The sandbox completes check-in and a cited question flow; every response is strictly under 160 characters.

## Validation and Demo Release Checklist

- [ ] Run `uv run pytest` against the implemented unit and integration suite, including a fresh database and deterministic multi-day simulation.
- [ ] Verify citation acceptance metrics, inactivity boundaries, concurrent nudge limits, score opt-in, pack size, and USSD length where implemented.
- [ ] Confirm deletion leaves no learner-referencing rows except anonymized aggregates, including logs and supporting delivery storage.
- [ ] Confirm outbound LLM payloads contain no learner names, channel IDs, or phone numbers.
- [ ] Review UI translations and audio, verify opportunity links and dates, and check provider quotas at implementation/demo time.
- [ ] Rehearse section 17 of `spec.md`: onboarding, lesson, wrong-answer follow-up, cited and off-topic QA, inactivity, nudge, return, and weekly payout. Include voice, web, and USSD once available.
- [ ] Update `README.md` and `AGENTS.md` with actual setup, migration, seeding, testing, reset, and demo commands.

## Decisions to Resolve During Implementation

- Select the local language for the minimum demo; the full demo script favors Yoruba while personas also use Pidgin.
- Select the free LLM provider and supported model, verify current API compatibility and quotas, and retain configuration-based switching. Milestone 2 uses OpenAI GPT (`gpt-5.4-mini`); the client remains provider-agnostic. Unlike the free tiers in spec section 3.1, this is a paid API.
- Define virtual-day scheduling and weekly-boundary behavior before implementing the scheduler so real and simulated runs agree.
- Define syllabus behavior after the ten-topic sequence and expired-opportunity handling without inventing unverified content.
- Keep WhatsApp, production auth, real matching, additional exams, mentor roles, and offline Android work outside this implementation.

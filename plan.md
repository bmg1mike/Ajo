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

- [x] Retrieve six chunks for the next ordered topic and generate a lesson of at most 150 words, one worked example, and two multiple-choice questions.
- [x] Validate structured content and citations; retry once on failure, then use a validated pre-generated lesson.
- [x] Translate lesson bodies and answers while preserving original citations and source excerpts; retain English Biology terms in brackets where appropriate.
- [x] Deliver questions individually, record answers and check-ins, and provide private explanations plus an easier related question after incorrect answers.
- [x] Implement `/lesson` and emit score-safe answer feed events.
- [x] Add `uv run python -m app.core.lessons --pregenerate` for all 10 topics.

**Completion check:** A learner receives a cited lesson, answers both questions, and receives a private follow-up for an incorrect answer; invalid generation uses the fallback.

**Validation:** `uv run python -m app.core.lessons --pregenerate` with OpenAI `gpt-5.4-mini` (`LLM_REASONING_EFFORT=low`) generates and validates lessons for all 10 topics (71–97 words, 3–6 cited chunks each). The per-item claim check rejected the first lesson for topics 1, 4 and 9, and each passed on its retry. 17 of 20 translations pass. The three that fail are Yoruba (topics 1, 7 and 10). The reviewer's notes name real meaning changes: "kingdom" became "family", "plant" became "seed", and the heart option was mistranslated. Those lessons show English with a note until a translation passes, so the command exits non-zero. Against the ingested database with `LLM_ENABLED=false`, a synthetic Pidgin learner then ran `/lesson`, answered question 1 wrong, got the explanation, source and easier question, answered the rest, and produced one `answered` feed event with no score. All of this was served from the cache, with no live calls. `tests/test_lessons.py` covers topic-scoped retrieval, every validation rejection and its retry with feedback, the claim check, fallback to the pre-generated lesson (including with the LLM disabled), the unavailable message when no fallback exists, translation validation and the English fallback with a note, lazy translation for a learner in a new language, one-at-a-time questions, follow-up ordering, idempotent and forged taps, membership checks, check-in streaks, deletion, posting to a circle, pre-generation, and (Postgres only) concurrent `/lesson` requests creating one lesson. Implementation notes: the six chunks are the topic's chunks most similar to the topic title and objectives. Every item (the lesson with its example, each question, each follow-up) must cite chunks from those six. Each item is then claim-checked separately against only the chunks it cites, so a question cannot borrow support from a chunk only the lesson cites. A question is checked with its options, marked answer and explanation, and the check rejects a marked answer that is wrong or a second option that is also correct. The retry adds the rejected reply and a fixed reason to the conversation, so the request differs from the cached one; a provider error is retried unchanged. The easier follow-up for each question (difficulty 1; main questions are difficulty 2) is generated and validated with the lesson, so grading never calls the LLM. Order: question 1, its follow-up if wrong, question 2, its follow-up if wrong. Translations cover every string a learner reads, never answer keys or citations. The translator gets a flat JSON object of named parts ("Lesson", "Question 1, option B", …) and must return exactly the same keys. In the nested form, the model left the questions in English. A lesson, example, question or explanation returned unchanged is rejected in code, with the parts named. Options are exempt, since one may be just a Biology term. A fidelity check then sees the target language and every English part beside its translation, and returns JSON listing problems, which go into the retry. A failed translation leaves the English lesson with a note. Lessons are per circle per virtual (UTC) day: the first `/lesson` of a day creates the next day's lesson under a circle row lock, and `LessonService.post` prepares it for every member for milestone 5's daily job. After topic 10 the sequence starts again at topic 1 as revision. The `answered` event carries only the lesson ID and day, so milestone 5's feed must compute scores from `answers` and show them only for learners with `share_scores`. Each learner answers each question once (`one_answer_per_question`). Ingestion deletes pre-generated lessons whose chunks it prunes. The Listen button waits for milestone 8.

## Milestone 5: Virtual Days, Personas, and Feed

Depends on milestones 3–4.

- [x] Seed the goal, circle, admin learner, and Chidi, Halima, and Tunde with repeatable random seeds.
- [x] Implement their specified check-in and answer behavior; simulated members have no chat IDs and receive no external messages.
- [x] Restrict `/seed_demo`, `/simulate_day`, `/simulate_week`, `/reset_demo`, and `/demo_status` to `ADMIN_IDS`.
- [x] Advance the clock by 24 hours per simulated day and run persona actions, lessons, inactivity checks, and feed delivery in a defined sequence. *(The inactivity step is a placeholder until milestone 6.)*
- [x] Connect APScheduler to virtual-time-aware job decisions and prevent duplicate daily processing.
- [x] Batch ordinary circle updates to at most one message per learner per day. Reset circle state and clock without deleting the corpus.

**Completion check:** Repeating a seeded simulation produces the same behavior; timestamps use the virtual clock; unauthorized admin commands fail; feeds obey privacy and batching rules.

**Validation:** `tests/test_simulation.py`, `tests/test_feed.py` and `tests/test_scheduler.py` run on SQLite and PostgreSQL, with every lesson served from a pre-generated fallback and the LLM disabled. They cover the following:
- Seeding is idempotent, and personas have no chat IDs.
- Every admin command and button is refused for non-admins, for users who haven't onboarded, and for the admin's numeric ID arriving on another channel.
- `/simulate_day` adds exactly 86,400 seconds, and persona answers carry the virtual date.
- Personas never get outbox rows. Tunde answers on days 1 and 2 only, then on the day after a nudge.
- Chidi's accuracy is close to 0.9 and Halima's to 0.5 over 2,000 draws.
- A seed-42 run repeats exactly after `/reset_demo`, and a different seed differs.
- `/simulate_week` produces seven lessons, seven `daily_runs` rows and seven circle updates with distinct keys.
- Scheduled and simulated runs never process a day twice.
- Chidi replies only to on-topic circle questions.
- Scores appear only for learners who opted in. Updates are batched one per day, with later events carried into the next update, and deleted members appear anonymously.
- Outbox delivery is ordered, retries on failure and then gives up, and logs no chat IDs.
- The real APScheduler service starts, delivers and stops cleanly.

Against a copy of the ingested database with `LLM_ENABLED=false`, a synthetic admin onboarded, ran `/seed_demo 42` and simulated four days. Each day queued one cited lesson and one circle update; nothing reached the personas, and no live LLM calls were made. On day 4 the update read "Tunde ⏳ no check-in since Thursday". `/demo_status` and `/reset_demo` behaved as specified.

Implementation notes:
- **Daily run.** `DemoEngine.run_day` (`app/sim/engine.py`) is the single daily job for both the scheduler and `/simulate_day`. Its order is: prepare today's lesson (every LLM call happens before the transaction writes), claim the circle and virtual day in `daily_runs` with ON CONFLICT, queue the lesson, run the personas, run the inactivity check (milestone 6), then queue the circle updates. The spec lists persona actions before the lesson post; the lesson comes first here because personas answer it, and nothing is delivered before commit.
- **Outbox.** Messages a learner didn't ask for go into `outbound_deliveries`, in the same transaction as the work that produced them. A two-second APScheduler job sends them through the adapter registry. Delivery is retried up to five times, and a failure part-way through a batch resends the whole batch. Rows for a channel with no adapter wait without blocking others. A failed batch holds back that learner's later batches until the next pass, so messages never arrive out of order. The Telegram adapter is unregistered when polling stops. Each row and its learner are re-read just before every message, including between messages in one batch, so a deletion committed during delivery cancels whatever has not yet been handed to the adapter.
- **Feed.** The feed key `feed:<circle>:<learner>:<virtual day>` is unique, so the one-update-per-day limit is enforced by the database. Each update lists the other members' check-in status ("answered today's questions", "checked in today", "checked in yesterday", "no check-in since <weekday>" after two or more quiet days, "no check-in for over a week"). It then lists new circle questions, persona replies and anonymous departures since that member's previous update (`circle_members.feed_delivered_at`). Scores come from `answers`, and only for members with `share_scores`. A score line is queued in its private form, with a substitution record only for members who had opted in when the update was queued. The substitution is applied at send time only if the owner still shares scores, so an opt-out also covers updates already queued. An update stays under 3,500 characters (question previews are capped at 280). Events that don't fit stay after the member's `(feed_delivered_at, feed_cursor_id)` cursor for the next day's update, and a "…and N more" line says so. The ID part of the cursor keeps events that share a timestamp from being skipped. The "checked in yesterday" line exists because real learners answer after the morning update.
- **Scheduler.** With `SCHEDULER_ENABLED` (default off), a 60-second tick runs every circle that has a real member once today's virtual `LESSON_TIME` (default 07:00 UTC) falls between two ticks. It does not catch up after downtime or after `/reset_demo` moves the clock back, and it retries on the next tick if a circle fails.
- **Personas.** Each persona decision uses `random.Random("seed|persona|...|day|question")`, so results don't depend on the order of actions. "Day" counts the circle's processed days, so a learner's own `/lesson` can't shift Tunde's schedule. Chidi's reply decision is keyed by the question's normalized text, so a rehearsed question gets the same outcome. Personas answer through `grading.record_answer`, including the easier follow-up after a wrong answer. Tunde's accuracy is not specified; it is 0.7.
- **Chidi's replies.** Chidi replies to half of circle questions, chosen by seed. Each reply is a fixed i18n string plus the library's best-matching citation, and is posted only if retrieval clears `RETRIEVAL_MIN_SCORE`, so it needs no LLM call.
- **Lesson translation.** `LessonService.post` now translates only into real members' languages, since personas read nothing.
- **Admin learner.** An admin becomes a learner only by onboarding, so consent is recorded. `/seed_demo` uses the admin's goal, or `DEMO_GOAL` if they haven't onboarded, and in that case asks them to `/start`. It refuses when no goal is ingested.
- **Reset.** `/reset_demo` asks for confirmation. It then deletes the circle's lessons (with questions and answers), feed events, nudges, daily runs, simulation row, personas and members' outbox rows, clears check-in state and sets the clock offset to 0. Learners, their QA history, the corpus, pre-generated lessons and the LLM cache are kept.
- **Payouts.** `/simulate_week` reports that payouts are not available yet (milestone 7).

Known limitations:
- `/simulate_week` runs synchronously and can take a while with a cold LLM cache; Telegram handles one update at a time.
- The virtual clock is global, so `/reset_demo` moves it back for every circle, but only the demo circle's state is wiped. The POC has one circle per goal and one goal.
- On PostgreSQL, a circle question whose transaction commits after that day's update has been built, but carries an earlier timestamp, is missed by the feed cursor. That window is milliseconds wide.
- On SQLite, which has no row locks, a scheduler tick racing `/simulate_day` is resolved by the lesson's unique key and reported as "already ran".

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
- Define weekly-boundary behavior for payouts so scheduled and simulated runs agree. (Virtual-day scheduling is settled in milestone 5: one claimed `daily_runs` row per circle and virtual day, triggered at `LESSON_TIME` or by `/simulate_day`.)
- Define expired-opportunity handling without inventing unverified content. (After the ten-topic sequence, lessons restart at topic 1 as revision; see milestone 4.)
- Keep WhatsApp, production auth, real matching, additional exams, mentor roles, and offline Android work outside this implementation.

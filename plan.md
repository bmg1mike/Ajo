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

**Validation:** `uv run python -m app.core.lessons --pregenerate` with OpenAI `gpt-5.4-mini` (`LLM_REASONING_EFFORT=low`) generates and validates lessons for all 10 topics (71–97 words, 3–6 cited chunks each). The per-item claim check rejected the first lesson for topics 1, 4 and 9, and each passed on its retry. 17 of 20 translations pass. The three that fail are Yoruba (topics 1, 7 and 10). The reviewer's notes name real meaning changes: "kingdom" became "family", "plant" became "seed", and the heart option was mistranslated. Those lessons show English with a note until a translation passes, so the command exits non-zero. Against the ingested database with `LLM_ENABLED=false`, a synthetic Pidgin learner then ran `/lesson`, answered question 1 wrong, got the explanation, source and easier question, answered the rest, and produced one `answered` feed event with no score. All of this was served from the cache, with no live calls. `tests/test_lessons.py` covers topic-scoped retrieval, every validation rejection and its retry with feedback, the claim check, fallback to the pre-generated lesson (including with the LLM disabled), the unavailable message when no fallback exists, translation validation and the English fallback with a note, lazy translation for a learner in a new language, one-at-a-time questions, follow-up ordering, idempotent and forged taps, membership checks, check-in streaks, deletion, posting to a circle, pre-generation, and (Postgres only) concurrent `/lesson` requests creating one lesson. Implementation notes: the six chunks are the topic's chunks most similar to the topic title and objectives. Every item (the lesson with its example, each question, each follow-up) must cite chunks from those six. Each item is then claim-checked separately against only the chunks it cites, so a question cannot borrow support from a chunk only the lesson cites. A question is checked with its options, marked answer and explanation, and the check rejects a marked answer that is wrong or a second option that is also correct. The retry adds the rejected reply and a fixed reason to the conversation, so the request differs from the cached one; a provider error is retried unchanged. The easier follow-up for each question (difficulty 1; main questions are difficulty 2) is generated and validated with the lesson, so grading never calls the LLM. Order: question 1, its follow-up if wrong, question 2, its follow-up if wrong. Translations cover every string a learner reads, never answer keys or citations. The translator gets a flat JSON object of named parts ("Lesson", "Question 1, option B", …) and must return exactly the same keys. In the nested form, the model left the questions in English. A lesson, example, question or explanation returned unchanged is rejected in code, with the parts named. Options are exempt, since one may be just a Biology term. A fidelity check then sees the target language and every English part beside its translation, and returns JSON listing problems, which go into the retry. A failed translation leaves the English lesson with a note. Lessons are per circle per virtual (UTC) day: the first `/lesson` of a day creates the next day's lesson under a circle row lock, and `LessonService.post` prepares it for every member for milestone 5's daily job. After topic 10 the sequence starts again at topic 1 as revision. The `answered` event carries only the lesson ID and day, so milestone 5's feed must compute scores from `answers` and show them only for learners with `share_scores`. Each learner answers each question once (`one_answer_per_question`). Ingestion deletes pre-generated lessons whose chunks it prunes. The Listen button arrived with milestone 8.

## Milestone 5: Virtual Days, Personas, and Feed

Depends on milestones 3–4.

- [x] Seed the goal, circle, admin learner, and Chidi, Halima, and Tunde with repeatable random seeds.
- [x] Implement their specified check-in and answer behavior; simulated members have no chat IDs and receive no external messages.
- [x] Restrict `/seed_demo`, `/simulate_day`, `/simulate_week`, `/reset_demo`, and `/demo_status` to `ADMIN_IDS`.
- [x] Advance the clock by 24 hours per simulated day and run persona actions, lessons, inactivity checks, and feed delivery in a defined sequence. *(The inactivity step arrived with milestone 6.)*
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
- **Payouts.** `/simulate_week` reported that payouts were not available yet; milestone 7 replaced this.

Known limitations:
- `/simulate_week` runs synchronously and can take a while with a cold LLM cache; Telegram handles one update at a time.
- The virtual clock is global, so `/reset_demo` moves it back for every circle, but only the demo circle's state is wiped. The POC has one circle per goal and one goal.
- On PostgreSQL, a circle question whose transaction commits after that day's update has been built, but carries an earlier timestamp, is missed by the feed cursor. That window is milliseconds wide.
- On SQLite, which has no row locks, a scheduler tick racing `/simulate_day` is resolved by the lesson's unique key and reported as "already ran".

## Milestone 6: Inactivity and Peer Nudges

Depends on milestone 5.

- [x] Detect quiet members and send the first nudge prompt to other circle members only.
- [x] Deliver nudges immediately, attributed to the initiating member, with lesson-request buttons.
- [x] Enforce one delivered nudge per recipient per virtual day across all senders, including concurrent requests.
- [x] Make Tunde return on the next simulated day after a nudge; avoid unsolicited automated reminders.

**Completion check:** Detection occurs on day 3 after the last check-in, only one daily nudge reaches the recipient, and Tunde returns as specified. Pin the exact boundary in a test because section 7.6 describes two days of inactivity while section 16 states day-3 detection.

**Validation:** `tests/test_nudges.py` runs on SQLite and PostgreSQL with the LLM disabled. It covers the following:
- The boundary: after a Tuesday check-in, a member is not quiet on Tuesday, Wednesday or Thursday, and is quiet from Friday. A member who never checked in counts from the day they joined.
- In a seeded run, Tunde (last check-in on day 2) is detected on day 5 and on no other day. Exactly one `missed` event is recorded for the spell. Only day 5's circle update carries the prompt and its button, and nobody is prompted about themselves.
- Tapping the button records a nudge from the tapper. Tunde checks in again on day 6 and not on day 7, and the old button then answers that he has checked in recently.
- A real quiet member gets nothing but the lesson posts and circle updates until someone taps. The nudge is queued under its own `nudge:<id>` key, outside the daily batch, and goes out on the outbox's next pass. It reads "👋 Carol from your circle is checking on you." with **Yes, send it** (today's lesson, as `/lesson`) and **Later** buttons.
- Several senders, or one sender tapping repeatedly, produce one nudge per recipient per virtual day. The same prompt works again the next day while the member is still quiet, and a check-in ends it.
- Two concurrent taps on PostgreSQL deliver one nudge, and the database rejects a second nudge row for the same recipient and day.
- Upgrading a database that already holds queued nudges (revision 0005 → head, on both dialects) gives each one the expiry of its own nudge day, falling back to its queued time if its nudge row is gone, and leaves other messages without an expiry. Afterwards, yesterday's waiting nudge is not delivered beside today's.
- The limit holds for delivery, not only queuing. A nudge left unsent on its day (no adapter, here) expires, so the next day only that day's nudge arrives. An expired row stays unsent, even after failed attempts, and holds back nothing queued after it.
- Malformed payloads, a non-`missed` event, a member's own prompt, a non-member, and a deleted quiet member are all refused.

Implementation notes:
- **Boundary.** A member is quiet when `today − (last check-in day, or join day) > 2`, i.e. two whole virtual days have ended without a check-in. The daily run checks in the morning, at `LESSON_TIME`, before real learners act, so the day in progress never counts, and day-3 detection (section 16) and "after 2 days without check-in" (section 7.6) agree. Personas act earlier in the same run, so a persona who checks in that day is not detected. With Tunde's check-ins on days 1 and 2, he is detected on day 5, so the rehearsal of section 17 step 5 must simulate days 2 to 5 rather than 2 to 4.
- **First detection.** `nudges.detect` (step 5 of `DemoEngine.run_day`) records a `missed` feed event whose ID is a uuid5 of circle, member and the day of the last check-in. ON CONFLICT DO NOTHING makes it the once-per-spell guard: a still-quiet member is not detected again, and a new check-in starts a new spell. The `missed` feed type and the `nudges` table with `one_nudge_per_recipient_day` came with the foundation.
- **Prompt.** The prompt rides in that day's batched circle update, after the status lines ("💤 Tunde has gone quiet. A nudge from you could help.", button "👋 Send Tunde a nudge"), so it adds no message beyond the one-update-per-day limit. Every real member except the quiet one gets it. The button payload is `nd:s:<missed event id>`, so it names no learner, and it dies with the quiet member's data.
- **Sending.** A tap locks the quiet member's `circle_members` row, re-checks that they are still quiet, and inserts the nudge with ON CONFLICT DO NOTHING on `(to_learner_id, virtual_day)`. Only the winner queues a message, under `nudge:<nudge id>`. The others are told someone already nudged today. Core code never calls an adapter, so "immediately" means the next two-second outbox pass rather than the next daily update. The queued nudge has `outbound_deliveries.expires_at` (migration 0006, which backfills nudges already queued from their nudge's `virtual_day`) set to the end of its virtual day, and the outbox never sends a row once its expiry has passed, checking on load and again just before sending. A nudge delayed by failures, a missing adapter or `/simulate_day` is therefore dropped rather than delivered next to the following day's, so at most one nudge reaches a recipient per virtual day. A nudge to a persona is recorded but never queued; the persona reads the `nudges` row the next simulated day.
- **No automated reminders.** Ajo sends a quiet member nothing because they are quiet. They still get the daily lesson and circle update every member gets, and a nudge only when a person taps. Personas never send nudges.

Known limitations:
- A dropped nudge is not re-sent. Its sender is not told, and the quiet member can be nudged again the next day.
- Expiry is checked just before the adapter is called, so a send that starts in the last instant of a day can land just after midnight. That window is milliseconds wide.
- Personas cannot receive the nudge message, so the "Tunde's pane receives it" step of the demo script needs the milestone 9 web simulator to show a persona's nudges from the `nudges` table.
- The prompt appears only in the update on the day of first detection. Its button keeps working while the spell lasts, but a member who joins the circle later, or whose update was never delivered, gets no prompt for that spell.

## Milestone 7: Weekly Payout and Minimum Demo

Depends on milestones 4–6.

- [x] Produce private progress summaries, including accuracy and weakest topic, and a circle summary containing aggregate check-ins only.
- [x] Generate a text or compressed PDF study pack with lessons, questions, answers, and citations, strictly under 500 KB.
- [x] Curate `data/opportunities.json`; filter by goal, country, and deadline, and display the verification date.
- [x] Implement `/progress` and `/pack`, with payouts every seven virtual days.
- [x] Ensure `/simulate_week` produces one payout at the weekly boundary without duplicating scheduler payouts.
- [x] Cache the rehearsed QA and lesson responses and verify the minimum demo with no live LLM calls. *(English rehearsal: done. Yoruba: one QA answer still needs a live call; see the validation below.)*

**Completion check:** A simulated week delivers all four payout components, protects individual scores, and meets the pack-size limit. Milestones 1–7 now support the minimum Telegram demo.

**Validation:** `tests/test_payouts.py` runs on SQLite and PostgreSQL with the LLM disabled. It covers the following:
- From a fresh demo, `/simulate_week` runs seven days and pays out once, on day 7. The real member's batch has all four parts: the private progress, the circle summary, the opportunity and the study pack. Personas get nothing.
- The circle summary holds counts only, for example "checked in 16 time(s) over 7 lesson day(s)" and "3 of 4 members". It is the same for every member, even when a member shares scores in the daily feed. No member's payout names another member or carries their numbers.
- The progress summary gets accuracy, topics covered and the weakest topic right (a wrong answer, then a right easier follow-up, then a right answer gives 50%). The pack shows the learner's own answers, the marked answers, explanations, follow-ups and citations, and ends with each source's licence.
- From day 5, `/simulate_week` runs days 6 and 7 and pays out. The next `/simulate_week` runs days 8–14, and week 2 starts exactly where week 1 ended.
- A day that has already run, a scheduler pass and a second `queue_weekly` for the same week queue nothing more.
- `/progress` and `/pack` show "week N so far" mid-week. After the payout they show the week just paid out, including answers given later on day 7, until the next week's first lesson.
- The pack stays strictly under the limit. At an exact-size limit it drops the last lesson and says so, and with a tiny limit it is still valid UTF-8.
- Files in the outbox are stored base64-encoded and sent after their text. Telegram sends them as documents. If the learner deletes their data while the text is being sent, the file is not sent.
- A database with seven days processed before payouts existed (no payout row) still bounds its weeks: `/progress` and `/pack` treat week 1 as ended at its seventh run, and week 2's payout covers days 8–14, not 1–14.
- Opportunities: malformed or non-https entries and duplicate URLs are rejected. Sync makes the table match the file. Selection respects goal, country, deadline (inclusive), and checks that are too old or dated in the future. It orders by the curator's priority, then the nearest deadline. The checked-in file is valid, ranks a scholarship first and has no tied priorities. The command-line check reports each entry. Startup syncs the file and refuses a malformed one.
- Reset deletes payouts. `/delete_me` leaves only the circle's anonymous counts. Deleting one learner never removes another learner's queued pack, even when the pack's base64 happens to contain their name.
- Upgrading from revision 0006 keeps existing opportunities.

Rehearsal: `uv run python -m app.sim.rehearse` ran the minimum demo script against a migrated copy of the ingested database with the LLM switched off. It covered onboarding, `/seed_demo 42`, day 1 with a wrong answer and the private follow-up, a cited answer, the off-topic fallback, days 2–5, the nudge, Tunde's return on day 6, `/simulate_week` to the day-7 payout, `/progress` and `/pack`.
- In English: 46 LLM requests, all served from the cache (0 misses), and PASS. The payout offered the MTN Foundation scholarship with its link, which the rehearsal now checks (spec section 17 ends on "a real scholarship link"). All seven lessons were generated from the cache, not the fallback. The pack was 22 KB.
- In Yoruba: one miss. The cited answer was cached only in English (milestone 2's evaluation learner), so it fell back. Lessons 1 and 7 show English with a note, because their Yoruba translations failed review in milestone 4.

Before a Yoruba demo, run the rehearsal once with `--live` to cache that answer.

Implementation notes:
- **Weekly boundary.** A circle's week is seven processed days, counted from `daily_runs`, the same count the personas use. The payout is step 7 of `DemoEngine.run_day`, after the circle update. The scheduler and `/simulate_day` therefore share one boundary, and the day's claim stops a week being paid twice. The `weekly_payouts` primary key `(circle_id, week_index)` is a second guard, and the row records the period and the circle's counts.
- **What a week covers.** A week covers the lessons posted after the end of the previous week up to and including the paying run. The previous week ended at its payout, or, if it was never paid out (days processed before revision 0007), at the circle's 7(N − 1)th daily run. `/progress` and `/pack` use the same boundaries, so each lesson belongs to one week. Answers count towards the week of the lesson they answer, whenever they were given. The payout runs at the morning lesson time of day 7, so answers given later that day are not in it, but `/progress` and `/pack` include them.
- **`/simulate_week`.** It simulates days until a payout, seven at most. Spec section 9.3 says "seven days, then payout", and section 17 says "/simulate_week to day 7". From a fresh demo the two agree. From mid-week, stopping at the boundary is what the demo script needs.
- **Progress.** "Days checked in" counts the week's lesson days with at least one answer. Questions and accuracy count main questions only, since easier follow-ups appear only after a wrong answer. The weakest topic is the one with the lowest accuracy below 100%; ties go to the topic with more answers, then the earlier one. With every answer correct, the summary says there is none.
- **Study pack.** The pack is a UTF-8 `.txt` file (spec 7.7 allows plain text), with no new dependency. It uses stored translations only and never calls the LLM. Lessons without a translation are shown in English with a note. Citations are never translated, and the pack ends with each cited source's publisher, licence and URL, which CC BY requires. `PACK_MAX_BYTES` is 500,000, so "strictly under 500 KB" holds whether a KB is 1,000 or 1,024 bytes. Lessons are dropped from the end to fit, with a note. A week is about 22 KB.
- **Delivery.** Each member gets one outbox batch, keyed `payout:<circle>:<week>:<learner>`, queued after the day's circle update. A message can now carry an `Attachment`. The outbox stores it base64-encoded and sends it with `send_file` after the text, re-reading the row and learner in between, so a deletion during the text stops the private file. The deletion scan checks only an attachment's filename, because packs name nobody.
- **Opportunities.** Each entry in `data/opportunities.json` names its official page, `verified_on`, eligibility, a `kind` (scholarship, loan, exam, resource or other) and an explicit `priority` (1 is offered first). The order is MTN scholarship, NELFUND loan, UTME registration, syllabus. A deadline is set only when the page states one. The four entries (JAMB 2027 UTME registration, the official Biology syllabus, MTN Foundation scholarships and NELFUND loans) were checked against their official pages on 2026-10-14. Oil-company scholarships, the Federal Scholarship Board and olympiads were left out, because no current official page could be loaded. JAMB's 31 October admissions deadline was also left out, because it binds universities, not 2027 candidates. Startup syncs the file into `opportunities`, keyed by URL, and refuses to start if the file is malformed.
- **Expired opportunities.** A payout skips any entry whose deadline has passed on the virtual clock, or whose check is over 90 days old (`VERIFICATION_MAX_AGE_DAYS`). It then shows the best priority, then the nearest deadline, then an entry with no deadline. If nothing qualifies, the payout says no checked opportunity is open, rather than showing an unverified one. `uv run python -m app.core.opportunities` shows what a payout would offer today, or on `--today`.

Known limitations:
- Opportunity titles and eligibility text are shown in English, as the provider wrote them, like citations.
- The circle summary counts current members only. A member who deleted their data takes their answers with them, so earlier check-ins drop out of later summaries (the stored `weekly_payouts` counts are unchanged).
- A week with lessons skipped (no topics, or no fallback) still pays out after seven processed days. Its pack covers the lessons that exist.
- `/reset_demo` restarts the week count with the day count, because the virtual clock is global.

## Milestone 8: Voice Input and Output

Depends on milestones 2–4; optional after the minimum demo.

- [x] Add configurable local faster-whisper or Groq Whisper transcription for Telegram OGG/Opus voice notes.
- [x] Add MMS-TTS generation, convert WAV to OGG/Opus with ffmpeg, and implement the lesson Listen action.
- [ ] Review local-language output with a native speaker; offer English audio with a note when local output is unavailable or unapproved. *(The English fallback with a note is done and tested. The native-speaker listening check is the one open item, and only a person can close it. To close it: run `uv run python -m app.speech.tts --review yo --out review-yo`, have a Yoruba speaker fill in `review-yo/review.md`, then add `Language.YO` to `AUDIO_REVIEWED` in `app/speech/tts.py` with their name and the date, and update the `AUDIO_REVIEWED` assertion in `tests/test_voice.py`. Until then, Yoruba learners hear English audio with the note, which the completion check accepts as "the documented English fallback".)*

**Completion check:** An English voice question receives cited QA, and a lesson plays reviewed local-language audio or the documented English fallback.

**Validation:** `tests/test_voice.py` runs on SQLite and PostgreSQL with fake STT and TTS engines and the LLM scripted. It covers the following:
- An English voice note gets the "I heard" echo, then a cited answer, and is logged to `qa_log`. A transcript is never parsed as a command (a spoken "/delete_me" is a question). Spoken names and phone numbers are redacted before the LLM.
- Nothing is downloaded or transcribed before a learner exists, even after "I agree". Notes over `STT_MAX_SECONDS`, or over the size limit (reported or actual), are refused, and the first two cases are refused before download. Download failures, transcription errors and empty transcripts each get a plain message, and the logs carry no IDs. With STT off, voice notes are refused without download, and other media say what is supported.
- Groq requests carry only a fixed file name, the model, a language hint (none for Yoruba) and a fixed prompt; they retry on 429 honouring `Retry-After`. Local Whisper loads once and wraps decoder errors. A failed first model load or download (for example `OSError` offline) becomes a `TranscriptionError`, so the learner gets the "couldn't understand" reply, and the load is retried on the next note. Any other httpx request error from Groq is wrapped the same way. Settings validate STT and TTS options, and startup refuses a missing ffmpeg or one without libopus.
- Text preparation: English and Pidgin spell numbers out and drop accents, Yoruba keeps tone marks in NFC, and text is grouped by sentence under 300 characters. The MMS synthesizer is seeded (same text, same bytes), adds pauses, writes 16-bit mono WAV and turns model failures into `SpeechError`. The `speech_cache` is keyed by model, language and text.
- Listen appears only when TTS is on, before "Answer questions". English learners hear the lesson and example (not the citations), served from the cache on a repeat tap. Yoruba and Pidgin learners get English audio with the note until their voice is in `AUDIO_REVIEWED`. A reviewed language plays its translation, and falls back to English with the note when the translation, the model or the synthesis is missing. Non-members and forged payloads get "button expired", and nothing is synthesized. Voice replies are never queued.
- Telegram: a voice update becomes a `VoiceNote` that calls `getFile` and downloads only when fetched. A polling end-to-end test onboards, asks by voice, gets the cited answer, then taps Listen and receives `sendVoice`.
- With `AJO_SPEECH_TESTS=1` (`-m speech`), the real models run: MMS English speaks a question, ffmpeg encodes it, faster-whisper `small` hears "photosynthesis", and Yoruba synthesis produces OGG.

Rehearsal: `uv run python -m app.sim.rehearse --voice` ran against a migrated copy of the ingested database, with ffmpeg 7.1 (libopus) and faster-whisper `small` on CPU. The question "What gas do plants release during photosynthesis?" was spoken by the English MMS voice and heard exactly. It was answered "Plants release oxygen during photosynthesis" with a citation to *Concepts of Biology* 5.1, p. 120. Listen sent a 226 KB voice note. Each language was run once with `--live` and then again with the LLM off:
- In English: 48 LLM requests, 0 misses, PASS.
- In Yoruba: 84 LLM requests, 0 misses, PASS. The same English voice question (language detected) was answered in Yoruba with the citation. Listen played English audio with the "isn't ready yet" note, which is the documented fallback.

`uv run python -m app.speech.tts --review yo` wrote Yoruba audio and a listening sheet for 7 of 10 topics (about 6 minutes cold, 4 seconds from the cache). Topics 1, 7 and 10 have no Yoruba translation (see milestone 4), and the command exits non-zero until they do.

Implementation notes:
- **Voice in.** The adapter attaches a `VoiceNote` whose `fetch` downloads lazily. The Telegram version runs the download on the bot's event loop from the conversation's worker thread. The conversation checks for a learner, the length and the size, downloads, transcribes and only then opens its transaction for QA, so a slow model never holds a database transaction. The transcript is echoed so the learner can see a mishearing. QA redacts it like typed text, and audio is never stored.
- **STT backends.** `STT_BACKEND=local` uses faster-whisper (`STT_LOCAL_MODEL`, default `small`, int8 on CPU, VAD on, loaded on the first note). `base` was faster but turned "photosynthesis" into "cotasandesis". `groq` posts to Groq's OpenAI-compatible endpoint (`STT_GROQ_MODEL`). English and Pidgin learners get an `en` hint, since Whisper has no Pidgin. Yoruba learners get language detection, because spec section 18 expects English voice in with Yoruba out, and a forced `yo` hint would garble English. faster-whisper 1.2.1 fails with PyAV 19 (`metadata_errors`), so PyAV is pinned below 16.
- **Voice out.** The Listen button (`ls:listen:<lesson>`) is handled before the conversation's transaction. It reads the lesson and worked example, and leaves the citations on screen. `speakable` prepares the text for each voice. The MMS Yoruba tokenizer keeps dotted letters and tone marks only in NFC (NFD loses ẹ, ọ and ṣ), and it has no digits or c/q/v/x/z. The English one has only the digits 0–6, so numbers are spelled out for English and Pidgin. Sentences are synthesized in groups of up to 300 characters with 0.3 s pauses, under a fixed seed. ffmpeg encodes 32 kbps mono Opus in OGG. Startup checks that `FFMPEG_PATH` exists and has libopus. Audio is cached in `speech_cache` (migration 0008), keyed by a hash of model, language, text, seed and format. Lessons name nobody, so the cache needs no deletion scan, and it survives `/reset_demo` like the LLM cache.
- **Review gate.** `tts.AUDIO_REVIEWED` is a human sign-off, kept in code like i18n's `REVIEWED`, and starts as English only. A learner hears their own language only when it is listed, has a model and the lesson has that translation. Otherwise, or if that synthesis fails, they get English audio with the `listen.english_only` note. `python -m app.speech.tts --review <lang>` writes each pre-generated lesson's audio and a Markdown sheet for the listener.

Known limitations:
- MMS's English voice is synthetic and sometimes unclear on short phrases. Whisper heard its bare "What is photosynthesis?" as "What is thought to say this is?". The rehearsal uses a full sentence, and a real human voice note has not yet been tested over Telegram.
- On CPU, a warm faster-whisper `small` takes about 10–13 s for a short note, and the first note downloads the model. Groq is the faster option for a live demo. The first Listen per lesson synthesizes (several seconds); after that it is served from the cache.
- Yoruba audio drops digits and the letters c, q, v, x and z, which appear in English Biology terms the translation keeps. The listening check must judge whether that is acceptable.
- The MMS models are CC BY-NC 4.0, fine for this proof of concept but not for commercial use.
- Homebrew's ffmpeg could not be installed on the development machine (the Xcode licence needed sudo), so validation used the standalone ffmpeg 7.1 from `imageio-ffmpeg` through `FFMPEG_PATH`.

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
- ~~Define weekly-boundary behavior for payouts so scheduled and simulated runs agree.~~ Settled in milestone 7: the payout is step 7 of every seventh processed day's `DemoEngine.run_day`.
- ~~Define expired-opportunity handling without inventing unverified content.~~ Settled in milestone 7: expired or stale entries are hidden, and the payout says when none is open. (After the ten-topic sequence, lessons restart at topic 1 as revision; see milestone 4.)
- Keep WhatsApp, production auth, real matching, additional exams, mentor roles, and offline Android work outside this implementation.

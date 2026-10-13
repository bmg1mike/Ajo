# Ajo: Learning Circles POC Specification

## 1. Overview

Ajo is a messaging-first study companion that places learners in small accountability circles, modeled on rotating savings groups (ajo, esusu, susu, chama, tontine). An AI facilitator delivers daily micro-lessons from a curated open library, answers questions with citations, detects when learners drop off, and links progress to real opportunities.

This document specifies a zero-cost proof of concept that one developer can build and demo without extra phone numbers, paid APIs, or hosting.

### 1.1 Tracks

- **Track 2, Engagement (core):** circles, peer nudges, weekly payouts.
- **Track 1, Access & Discovery:** plain-language questions answered from a vetted corpus.
- **Track 3, Guidance & Opportunity:** goal-based pathways and a filtered opportunity feed.

### 1.2 POC scope

| In scope | Out of scope |
| --- | --- |
| One goal: JAMB Biology | Other subjects and exams |
| One country: Nigeria | Multi-country configuration UI |
| Two languages: English plus one local language (Yoruba or Nigerian Pidgin) | Full multilingual coverage |
| Telegram bot (real channel) | WhatsApp integration |
| Web simulator (multi-learner view) | Production auth |
| USSD check-in via Africa's Talking sandbox | Real SMS delivery |
| Simulated circle members | Real circle matching at scale |
| Hand-curated opportunity list | Automated opportunity scraping |

## 2. Design principles

1. **Every answer is traceable.** The AI answers only from retrieved corpus chunks and must cite them. No citation, no answer.
2. **Private by default.** Circle members see first name and check-in status only. Scores stay private unless shared.
3. **Nothing is gated.** Difficulty adapts, but every learner can access every lesson and join any circle.
4. **Channel-agnostic core.** All logic lives in the core service. Telegram, web, and USSD are thin adapters.
5. **Low bandwidth first.** Text before audio, audio on request, weekly packs under 500 KB.

## 3. Architecture

```
            +----------------+   +----------------+   +------------------+
            | Telegram       |   | Web simulator  |   | Africa's Talking |
            | adapter        |   | adapter        |   | USSD adapter     |
            +-------+--------+   +-------+--------+   +--------+---------+
                    |                    |                     |
                    +--------------------+---------------------+
                                         |
                                 +-------v--------+
                                 |  Core service  |
                                 |  (FastAPI)     |
                                 +-------+--------+
          +------------+------------+----+-------+------------+-------------+
          |            |            |            |            |             |
     Circle engine  Lesson     RAG/QA       Speech       Scheduler    Simulation
                    generator  pipeline     (STT/TTS)    (APScheduler) engine
          |            |            |            |            |             |
          +------------+------------+------+-----+------------+-------------+
                                           |
                                 +---------v----------+
                                 | Postgres + pgvector|
                                 +--------------------+
```

### 3.1 Stack (all free)

| Concern | Choice | Notes |
| --- | --- | --- |
| Language | Python 3.11+ | Best library coverage for speech and embeddings |
| API | FastAPI | Webhooks for web simulator and USSD |
| Telegram | python-telegram-bot v21 (async) | Long polling in dev, no public URL needed |
| LLM | Gemini Flash (free tier) or Groq (free tier) | Accessed through an OpenAI-compatible client, base URL and model set in config |
| Embeddings | `intfloat/multilingual-e5-small` via sentence-transformers | Runs locally on CPU, 384 dimensions. Prefix inputs with `query: ` and `passage: ` |
| Vector store | Postgres 16 + pgvector in Docker | Supabase free tier as hosted alternative |
| STT | faster-whisper (local) or Groq Whisper (free tier) | Telegram voice notes are OGG/Opus |
| TTS | Meta MMS-TTS via Hugging Face transformers (local) | e.g. `facebook/mms-tts-yor`. Convert WAV to OGG/Opus with ffmpeg before sending |
| Scheduler | APScheduler (in-process) | Driven by a virtual clock, see Section 9 |
| USSD | Africa's Talking sandbox + simulator | Free, no real SMS sent |
| Tunnel (only for USSD) | Cloudflare Tunnel or ngrok free plan | |
| Web simulator | Single HTML page, vanilla JS | Served by FastAPI |

### 3.2 Privacy note on free tiers

Free LLM tiers may use submitted data to improve the provider's models. The POC must use synthetic learner data only. Production would use a paid tier with no training on inputs, or a self-hosted model.

## 4. Repository structure

```
ajo/
  app/
    main.py                 # FastAPI app, mounts adapters, starts scheduler
    config.py               # env-driven settings
    db.py                   # SQLAlchemy engine/session
    models.py               # ORM models (Section 5)
    core/
      clock.py              # virtual clock
      circles.py            # circle engine, feed events, nudges
      lessons.py            # daily lesson + question generation
      qa.py                 # RAG pipeline with citation enforcement
      grading.py            # answer checking, private follow-ups
      payouts.py            # weekly summary, pack, opportunity
      privacy.py            # hashing, delete-me
      i18n.py               # language handling, fixed UI strings
    llm/
      client.py             # OpenAI-compatible wrapper, retries, cache
      prompts.py            # all prompt templates
    speech/
      stt.py
      tts.py
    adapters/
      base.py               # Adapter interface
      telegram_bot.py
      web.py                # REST + static simulator
      ussd.py               # Africa's Talking callback
    sim/
      personas.py           # Chidi, Halima, Tunde behavior
      engine.py             # /simulate_day logic
  ingest/
    ingest.py               # PDF -> chunks -> embeddings
    sources.yaml            # list of source documents with license info
  data/
    corpus/                 # downloaded open-licensed PDFs
    opportunities.json      # hand-curated opportunities
    syllabus_jamb_bio.json  # topic list with ordering
  web/
    simulator.html
  tests/
  docker-compose.yml        # postgres + pgvector
  .env.example
  README.md
```

## 5. Data model

All timestamps use the virtual clock (Section 9).

**learners**
| Column | Type | Notes |
| --- | --- | --- |
| id | uuid PK | |
| channel | enum(telegram, web, ussd) | |
| channel_user_hash | text unique | SHA-256 of channel user ID or phone number plus server salt |
| channel_chat_id | text | Needed to send messages. Deleted on delete-me |
| first_name | text | Display name in circle |
| language | enum(en, yo, pcm) | |
| goal_id | fk goals | |
| is_simulated | bool | True for personas |
| share_scores | bool default false | |
| created_at | timestamptz | |

**goals**: id, code (`jamb_bio`), title, exam_date, country, syllabus_ref.

**circles**: id, goal_id, name, start_date, created_at.

**circle_members**: circle_id, learner_id, joined_at, last_checkin_at, streak_days.

**sources**: id, title, publisher, license, url, file_path.

**chunks**: id, source_id, section, page, text, embedding vector(384), syllabus_topic_id.

**syllabus_topics**: id, goal_id, order_index, title, objectives (text).

**lessons**: id, circle_id, day_index, topic_id, body (per language, jsonb), cited_chunk_ids (uuid[]), posted_at.

**questions**: id, lesson_id, stem, options (jsonb), correct_option, explanation, cited_chunk_ids, difficulty (1 to 3).

**answers**: id, question_id, learner_id, chosen_option, is_correct, answered_at.

**feed_events**: id, circle_id, type (checkin, answered, missed, nudge_sent, payout), actor_learner_id, payload jsonb, created_at.

**nudges**: id, circle_id, from_learner_id, to_learner_id, created_at.

**opportunities**: loaded from `opportunities.json`: id, title, provider, url, country, goal_codes, deadline, verified_on.

**qa_log**: id, learner_id, question_text, retrieved_chunk_ids, answered (bool), fallback_reason, created_at. Used for evaluation only.

## 6. Content and ingestion

### 6.1 Sources

Only openly licensed or official public material. Record license in `sources.yaml`.

- OpenStax Biology 2e (CC BY 4.0)
- Siyavula Life Sciences textbooks (CC BY)
- JAMB UTME Biology syllabus (official public document)
- WAEC Biology syllabus (official public document)

Target about 200 pages of Biology text mapped to 10 syllabus topics.

### 6.2 Ingestion pipeline

1. Extract text per page with pypdf or pdfplumber, keep page numbers.
2. Split into chunks of about 300 to 500 tokens with 50-token overlap, never crossing a section heading where detectable.
3. Tag each chunk with the closest syllabus topic (embedding similarity against topic titles and objectives, manually spot-checked).
4. Embed with `passage: ` prefix and store.
5. Print a report: chunks per source, chunks per topic, topics with fewer than 5 chunks.

Command: `python -m ingest.ingest --sources ingest/sources.yaml`

## 7. Core flows

### 7.1 Onboarding

1. Learner sends `/start`.
2. Bot explains Ajo in three short lines and shows a consent message: what is stored, who sees what, how to delete. Learner taps **I agree**.
3. Language choice: English, Yoruba, Pidgin (buttons).
4. First name.
5. Goal: JAMB Biology (only option in POC, shown as a button).
6. Learner is placed in the demo circle with the seeded personas.
7. Bot sends a welcome circle update listing members' first names.

No placement test. Difficulty starts at level 2 for everyone.

### 7.2 Daily lesson

Triggered by the scheduler at the circle's lesson time, or by `/simulate_day`.

1. Pick the next syllabus topic by `order_index`.
2. Retrieve top 6 chunks for that topic.
3. LLM generates, from those chunks only: a lesson of at most 150 words, one worked example, and two multiple-choice questions with explanations. Output must be JSON with `cited_chunk_ids`.
4. Validate (Section 8.3). On failure, retry once, then fall back to a pre-generated lesson cached for that topic.
5. Translate lesson body into each member's language. Source citation stays in its original language.
6. Post to every member's chat:

```
📘 Day 3: Cell Division
<lesson text>

Example: <worked example>

Source: OpenStax Biology 2e, Ch. 10, p. 287
[🔊 Listen]  [Answer questions]
```

### 7.3 Answering questions

1. Questions arrive one at a time with option buttons (A to D). USSD shows them as numbered options.
2. Record answer and update `last_checkin_at`.
3. Correct: short confirmation.
4. Wrong: private follow-up with the explanation, the cited source, and one easier related question (difficulty minus 1, generated from the same chunks).
5. Emit a `answered` feed event. The circle sees "Halima answered today's questions", never the score unless `share_scores` is true.

### 7.4 Asking a question (`/ask` or any free text that is not a command)

1. If input is a voice note, transcribe with STT.
2. Run the QA pipeline (Section 8).
3. Answer in the learner's language with citation, or trigger the fallback:

```
I couldn't find a reliable answer to that in the Ajo library.
Do you want to post it to your circle? [Ask my circle] [No thanks]
```

4. If posted to circle, other members receive it as a feed item. Simulated members may reply via persona logic.

### 7.5 Circle feed

Feed events are relayed into each member's private chat, batched to at most one update message per learner per day, plus immediate delivery of nudges.

```
🔵 Circle update
Chidi ✅ answered today's questions
Halima ✅ answered today's questions
Tunde ⏳ no check-in since Monday
[Send Tunde a nudge]
```

### 7.6 Inactivity detection and nudges

- Runs after each simulated or real day.
- A member is **quiet** after 2 days without check-in.
- On first detection, the circle (excluding the quiet member) receives a prompt with a **Send nudge** button.
- When tapped, the quiet member receives:

```
👋 Amaka from your circle is checking on you.
Today's lesson is short. Want it now? [Yes, send it] [Later]
```

- Rate limit: one nudge per quiet member per day, regardless of how many members tap.
- The bot never sends automated reminders on its own beyond this. The nudge comes from a person.

### 7.7 Weekly payout

Every 7 virtual days:

1. **Private progress summary** per learner: days checked in, questions answered, accuracy, topics covered, weakest topic.
2. **Circle summary** (shared): total check-ins for the circle, no individual scores.
3. **Study pack**: plain text or compressed PDF under 500 KB containing the week's lessons, questions, answers, and citations. Generated with reportlab or as `.txt`.
4. **One opportunity** from `opportunities.json` matching goal and country, with deadline and the date it was last verified.

### 7.8 Delete me

`/delete_me` asks for confirmation, then removes the learner row, answers, feed events where they are the actor, and chat ID. Circle shows "A member left the circle."

## 8. QA pipeline and citation enforcement

### 8.1 Retrieval

1. Embed the question with `query: ` prefix.
2. Retrieve top 8 chunks by cosine similarity, filtered to the learner's goal.
3. If the best similarity is below `RETRIEVAL_MIN_SCORE` (start at 0.80 for e5, tune on test set), go straight to fallback.

### 8.2 Generation

The LLM receives the question, the learner's language, and the retrieved chunks each labelled with its chunk ID. It must return:

```json
{
  "answerable": true,
  "answer": "string in learner's language, max 120 words",
  "cited_chunk_ids": ["uuid", "uuid"]
}
```

System prompt rules:

- Use only the provided chunks.
- If the chunks do not contain the answer, return `answerable: false`.
- Do not add facts that are not in the chunks.
- Write at a secondary-school reading level in short sentences.

### 8.3 Validation

An answer is accepted only if all are true:

1. JSON parses and matches schema.
2. `answerable` is true.
3. `cited_chunk_ids` is non-empty and every ID was in the retrieved set.
4. A second short LLM check ("Is every claim in this answer supported by these chunks? yes/no") returns yes.

Otherwise, fallback. Every outcome is written to `qa_log`.

### 8.4 Citation rendering

Format: `Source: <title>, <section>, p. <page>`. When the learner taps **Show source**, send the original chunk text (truncated to 600 characters).

## 9. Virtual clock and simulation

### 9.1 Virtual clock

`core/clock.py` exposes `now()` which returns real time plus a per-deployment offset stored in the database. `/simulate_day` advances the offset by 24 hours and runs the daily jobs immediately. All business logic calls `clock.now()`, never `datetime.now()`.

### 9.2 Personas

Seeded with `is_simulated = true`. They have no chat ID and receive no messages. Their behavior runs inside the simulation engine.

| Persona | Language | Behavior |
| --- | --- | --- |
| Chidi | en | Checks in daily. Answers correctly 90% of the time. Occasionally replies to circle questions using a canned helpful message plus a citation. |
| Halima | pcm | Checks in daily. Answers correctly 50% of the time. |
| Tunde | yo | Checks in on days 1 and 2, then goes quiet. If nudged, checks in the next simulated day. |

Persona behavior is deterministic given a seed, so the demo is repeatable.

### 9.3 Admin commands

Restricted to Telegram user IDs listed in `ADMIN_IDS`.

| Command | Effect |
| --- | --- |
| `/seed_demo` | Create goal, circle, personas, and add the admin as a member |
| `/simulate_day` | Advance clock 1 day, run persona actions, post lesson, run inactivity check, deliver feed |
| `/simulate_week` | Run `/simulate_day` 7 times, then payout |
| `/reset_demo` | Wipe circle state and clock offset, keep corpus |
| `/demo_status` | Show virtual date, members, last check-ins |

## 10. Adapters

### 10.1 Adapter interface

```python
class Adapter(Protocol):
    channel: str
    async def send_text(self, learner, text: str, buttons: list[Button] | None = None): ...
    async def send_voice(self, learner, ogg_bytes: bytes): ...
    async def send_file(self, learner, filename: str, data: bytes): ...
```

The core never imports a specific adapter. A registry maps `learner.channel` to the adapter instance.

### 10.2 Telegram

- Long polling in development.
- Commands: `/start`, `/lesson`, `/ask`, `/progress`, `/pack`, `/language`, `/share_scores`, `/delete_me`, plus admin commands.
- Voice notes in: download OGG, pass to STT.
- Voice out: TTS WAV, ffmpeg to OGG/Opus, `send_voice`.

### 10.3 Web simulator

- `GET /sim` serves `simulator.html`.
- Page shows up to three phone-style chat panes side by side. Each pane picks a learner (including personas, so you can "be" Tunde manually).
- `POST /web/message` with learner ID and text or button payload.
- `GET /web/poll?learner_id=...&since=...` returns new outbound messages (simple polling every 2 seconds; no websockets needed).
- When a pane takes control of a persona, that persona's automated behavior is paused.

### 10.4 USSD (Africa's Talking sandbox)

- `POST /ussd` receives `sessionId`, `serviceCode`, `phoneNumber`, `text`.
- Respond with `CON <menu>` to continue or `END <message>` to finish.
- Menu:

```
CON Ajo - JAMB Biology
1. Check in
2. Today's question
3. My progress
```

- Option 2 shows one question with numbered options, records the answer, and ends with the result and the source title.
- Each response must stay under the USSD length limit (keep under 160 characters).
- Phone number is hashed immediately. The demo USSD learner is pre-linked to a seeded learner record.

## 11. Language handling

- Fixed UI strings (buttons, menus, consent) live in `i18n.py` and are written and checked by a native speaker, not generated at runtime.
- Lesson bodies and answers are generated in English and translated by the LLM into the learner's language. Biology terms stay in English in brackets after the local term where one exists.
- Citations and source excerpts are never translated.
- TTS is offered for the learner's language if an MMS model is available and has passed a native-speaker listening check; otherwise English audio is offered with a note.

## 12. Privacy and security

- Channel IDs and phone numbers stored only as salted SHA-256 hashes, except `channel_chat_id` which is required for delivery and removed on deletion.
- No real learner data in the POC. Personas and test accounts only.
- Circle members see first name and check-in status. Scores require `share_scores`.
- `.env` holds API keys, salt, admin IDs. Never committed.
- LLM requests contain no names or IDs. Learner is referenced only by language and level.

## 13. Configuration

`.env.example`

```
DATABASE_URL=postgresql+psycopg://ajo:ajo@localhost:5432/ajo
TELEGRAM_BOT_TOKEN=
ADMIN_IDS=
LLM_BASE_URL=
LLM_API_KEY=
LLM_MODEL=
STT_BACKEND=local            # local | groq
GROQ_API_KEY=
TTS_MODEL_YO=facebook/mms-tts-yor
EMBED_MODEL=intfloat/multilingual-e5-small
RETRIEVAL_MIN_SCORE=0.80
HASH_SALT=
LLM_CACHE=true
```

## 14. Reliability for the demo

- **LLM cache:** responses keyed by prompt hash stored in a local table. With `LLM_CACHE=true`, a rehearsed demo makes zero live LLM calls.
- **Pre-generated lessons:** run `python -m app.core.lessons --pregenerate` to create and validate lessons for all 10 topics before the demo.
- **Rate limits:** the LLM client retries with backoff and falls back to cache.
- **Channel fallback:** if Telegram or the tunnel fails, run the entire demo in the web simulator.

## 15. Milestones

| # | Milestone | Done when |
| --- | --- | --- |
| 1 | Ingestion | 200 pages ingested, every topic has at least 5 chunks |
| 2 | QA pipeline | 20-question test set passes acceptance criteria in Section 16 |
| 3 | Telegram onboarding | Real account completes onboarding and joins demo circle |
| 4 | Lessons and answers | Daily lesson with citation, questions, private follow-up on wrong answer |
| 5 | Simulation | `/seed_demo` and `/simulate_day` produce feed updates from personas |
| 6 | Nudges | Tunde goes quiet, nudge prompt appears, nudge delivered, Tunde returns |
| 7 | Payout | Weekly summary, pack under 500 KB, one opportunity |
| 8 | Voice | Voice question transcribed and answered; lesson audio in one local language |
| 9 | Web simulator | Three panes driving the same circle |
| 10 | USSD | Check-in and one question through the sandbox simulator |

Milestones 1 to 7 are the minimum demo. 8 to 10 are additive.

## 16. Acceptance criteria

**QA test set:** 20 questions: 15 answerable from the corpus, 5 out of scope (e.g. "Who won the Super Eagles match?", "What is quantum entanglement?").

- At least 13 of 15 answerable questions answered with a valid citation.
- 5 of 5 out-of-scope questions trigger the fallback.
- Zero answers accepted with a citation outside the retrieved set.

**Engagement flow:**

- Quiet member detected exactly on day 3 after last check-in.
- Only one nudge reaches the quiet member per day.
- Circle feed never reveals an individual score unless that learner opted in.

**Constraints:**

- Weekly pack under 500 KB.
- Every USSD response under 160 characters.
- `/delete_me` leaves no rows referencing the learner except anonymised aggregates.

## 17. Demo script (5 minutes)

1. **Setup (30s):** Telegram on phone (Amaka, real), web simulator on laptop (Halima and Tunde), USSD simulator in a third pane.
2. **Onboarding (45s):** `/start`, consent, choose Yoruba, join circle.
3. **Day 1 (60s):** `/simulate_day`. Lesson arrives with citation. Tap **Listen** for Yoruba audio. Answer a question wrong and show the private follow-up.
4. **Ask (45s):** Send a voice note asking about photosynthesis. Cited answer comes back. Ask an off-topic question and show the "I don't know, ask my circle" fallback.
5. **Days 2 to 4 (60s):** Simulate three days. Tunde goes quiet. Circle update appears with **Send nudge**. Tap it. Show Tunde's pane receive it, and Tunde checks in next day.
6. **USSD (30s):** Check in from the feature-phone simulator and answer one question.
7. **Payout (30s):** `/simulate_week` to day 7. Show progress summary, pack size, and a real scholarship link.

## 18. Known limitations and risks

- Local-language STT is weaker than English. For the demo, voice input in English and voice output in Yoruba is the safer combination.
- MMS-TTS quality varies by language and needs native-speaker review.
- Free-tier quotas change. Check current Gemini and Groq limits at build time.
- Opportunity data goes stale quickly and needs human verification before every demo.
- WhatsApp, the production channel, has per-conversation costs. Scale would require sponsors such as schools, NGOs, or edtech partners.
- Circle matching is trivial in the POC (one seeded circle). Real matching by goal, exam date, and language is future work.

## 19. Future work

- WhatsApp adapter.
- Matching algorithm for circles.
- Additional exams (WASSCE, KCSE, BAC) and French and Arabic support.
- Teacher or mentor role inside circles.
- Offline-first Android client that syncs packs over Wi-Fi.
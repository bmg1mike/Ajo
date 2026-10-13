# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Ajo is a proof of concept for a messaging-first JAMB Biology study companion: small accountability "circles" with cited RAG answers, daily lessons, peer nudges, and weekly payouts. `spec.md` is the source of truth for behavior and acceptance criteria (section 16); `plan.md` orders the work into a foundation phase plus milestones 1–10. Only the foundation exists so far (config, DB models, migrations, virtual clock, privacy helpers, adapter registry). `app/llm/`, `app/sim/`, `app/speech/`, `ingest/`, `data/`, and `web/` are empty placeholders. Note that `AGENTS.md` still describes these directories as unimplemented and is partly stale.

## Commands

```bash
uv sync                                   # install deps (incl. dev group: pytest, httpx)
cp .env.example .env                      # then set HASH_SALT to >= 32 chars (required)
docker compose up -d db                   # Postgres 16 + pgvector on 127.0.0.1:5432
uv run alembic upgrade head               # app refuses to start until migrations have run
uv run fastapi dev                        # dev server; entrypoint is app.main:app (pyproject [tool.fastapi])

uv run pytest                                             # full suite (SQLite only by default)
uv run pytest tests/test_foundation.py::test_clock_advances_accumulate
uv run pytest -k "clock and sqlite"                       # DB tests are parametrized [sqlite] / [postgres]
TEST_DATABASE_URL=postgresql+psycopg://ajo:ajo@localhost:5432/ajo uv run pytest   # also run Postgres variants
uv run alembic revision --autogenerate -m "..."           # new migration in migrations/versions/
```

With `TEST_DATABASE_URL` set, the Postgres fixture creates and drops a throwaway `ajo_test_<uuid>` database, so the configured user needs CREATEDB. Without it, the Postgres variants are skipped. No linter or formatter is configured.

## Architecture

- **App factory**: `app/main.py:create_app(settings, services=...)` builds the app. The lifespan creates the engine, then attaches `settings`, `sessions` (sessionmaker), `clock`, and `adapters` to `app.state`, and checks that the `clock_state` singleton row exists. Background workers (Telegram polling, scheduler) should be passed as `services`: async context-manager factories taking the app. They are entered in order and torn down before the engine is disposed. Root `main.py` only re-exports `app`.
- **Sessions**: `app/db.py:get_session` is the request dependency (one transaction per request). `build_engine` supports SQLite (enables foreign keys) as well as Postgres.
- **Dual-dialect models**: `app/models.py` must work on both Postgres and SQLite, because tests run against both. Use the existing variant types: `JSON_TYPE` (JSONB→JSON), `UUID_LIST` (ARRAY(Uuid)→JSON), and `Vector(384).with_variant(JSON(), "sqlite")`. Enums are non-native string enums with check constraints (`enum_type`). Schema changes need an Alembic migration. `migrations/env.py` accepts an injected connection via `config.attributes["connection"]`, which the tests use, and migrations must stay idempotent because the tests run `upgrade head` twice.
- **Virtual clock** (`app/core/clock.py`): `now()` = real UTC + `offset_seconds`, stored in the singleton `clock_state` row (seeded by migration 0001). Business logic must use `app.state.clock.now()`, never `datetime.now()`. `advance()` accepts only positive whole seconds and updates atomically in SQL.
- **Adapters** (`app/adapters/base.py`): core code calls `AdapterRegistry.send_*`, which routes by `learner.channel` and silently skips `is_simulated` learners. Core must never import a concrete adapter. Keep adapters thin.
- **Privacy** (`app/core/privacy.py`): `channel_hash` produces salted SHA-256 hashes (channel user IDs and phone numbers are never stored raw). `delete_learner(..., confirmed=True)` relies on FK `ON DELETE CASCADE`, and it also scans `feed_events`, `outbound_deliveries` and `llm_cache` JSON for learner identifiers and deletes matching rows. When you add tables that reference learners, give them a cascading FK or extend this scan.
- **Config** (`app/config.py`): pydantic-settings reads `.env`. Each integration (`TELEGRAM_ENABLED`, `LLM_ENABLED`, `STT_ENABLED`) validates its own credentials only when enabled. Tests build `Settings(_env_file=None, hash_salt=...)` explicitly instead of relying on `.env`.

## Rules from spec/plan that constrain new code

- Citation enforcement: accept an LLM answer only if the JSON matches the schema, `answerable` is true, citations are non-empty and all appear in the retrieved set, and a second claim-support check passes. Otherwise use the fallback. Log every outcome to `qa_log`.
- LLM requests must not contain learner names, IDs, chat IDs, or phone numbers; refer to a learner only by language and level. `llm_cache` is keyed by prompt hash and must never store raw prompts.
- The circle feed shows first names and check-in status only. Scores appear only when `share_scores` is true. Send at most one batched feed message per learner per day, while nudges are delivered immediately. Allow one nudge per recipient per virtual day; this is enforced by the `one_nudge_per_recipient_day` unique constraint.
- Simulation must be deterministic given a seed. Simulated personas have no `channel_chat_id` (DB check constraint).
- Use synthetic learner data only. Fixed UI strings go in `app/core/i18n.py` and are never generated at runtime.
- Tests go in `tests/test_<feature>.py` and use synthetic data and mocked external providers.

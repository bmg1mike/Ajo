# Repository Guidelines

## Project Structure & Module Organization

Ajo is a Python FastAPI scaffold for a learning-circle proof of concept. `app/main.py` defines the application, `GET /`, and `GET /health`; root `main.py` re-exports the same app for compatibility. `pyproject.toml` declares Python 3.12+ and dependencies; `uv.lock` records dependency resolution. `README.md` covers startup and deployment. `spec.md` defines the intended architecture, privacy rules, and acceptance criteria.

The package layout, settings, ORM models, Alembic migrations, virtual clock, adapter registry, and privacy helpers are implemented; feature milestones in `plan.md` remain planned. As features are added, place business logic in `app/core/`, channel integrations in `app/adapters/`, ingestion tools in `ingest/`, corpus and curated data in `data/`, simulator assets in `web/`, and tests in `tests/`. Keep adapters thin and independent of core logic.

## Build, Test, and Development Commands

- `uv sync`: install dependencies from the project lockfile.
- `docker compose up -d --wait`: start PostgreSQL 16 with pgvector.
- `uv run alembic upgrade head`: initialize or upgrade the schema before starting the API.
- `uv run alembic check`: check model/schema agreement.
- `uv run python -m ingest.ingest --sources ingest/sources.yaml`: download (first run, ~590 MB, SHA-256 verified) and ingest the Biology corpus, print a coverage report, and upsert sources, topics, and chunks. Add `--dry-run` to report without writing, `--offline` to forbid downloads.
- `AJO_CORPUS_TESTS=1 uv run pytest -m corpus`: check milestone 1 criteria against the real PDFs and embedding model (~1 minute).
- `uv run pytest`: run unit tests; PostgreSQL tests skip unless `TEST_DATABASE_URL` is set.
- `TEST_DATABASE_URL=postgresql+psycopg://ajo:ajo@localhost:5432/ajo uv run pytest`: run both SQLite and isolated PostgreSQL suites.
- `uv run fastapi dev app/main.py`: start the local development server at `http://localhost:8000` with reload.
- `uv run fastapi run app/main.py`: run the application without development reload.
- `uv run fastapi deploy`: deploy to FastAPI Cloud after account setup, as documented in `README.md`.

No separate build step, formatter, or lint command is configured. Configure an ignored `.env` with `HASH_SALT` before running migrations or the server, as documented in `README.md`.

## Coding Style & Naming Conventions

Use four-space indentation, conventional Python import grouping, and type hints for new service interfaces. Use `snake_case` for modules, functions, and variables; `PascalCase` for classes; and `UPPER_SNAKE_CASE` for constants. Keep route handlers small. No formatter or linter is currently configured.

## Testing Guidelines

The foundation has pytest tests with FastAPI's `TestClient`; there is no coverage threshold. For new functionality, name files `tests/test_<feature>.py`, and name cases `test_<behavior>`. Run `uv run pytest`. PostgreSQL tests create and remove randomly named databases and require a test user with `CREATEDB`; use the Compose user locally. Cover changed behavior and applicable acceptance criteria in `spec.md`, especially citation validation, privacy, deletion, and deterministic simulation.

## Commit & Pull Request Guidelines

This checkout has Git metadata but no established commit conventions. Use short, imperative subjects, such as `Add citation validation`. PRs should explain the behavior change, reference relevant issues or specification sections, and record validation performed. Include screenshots for simulator UI changes.

## Security & Configuration

Use synthetic learner data for the proof of concept. Never commit API keys, bot tokens, or `.env` files. Keep learner names and identifiers out of LLM requests. Document new configuration variable names with empty or safe example values in `.env.example`.

Use `Clock.now()` for every business timestamp; model timestamp fields have no real-time defaults. Keep schema changes in reviewed Alembic revisions. Pass only the authenticated learner ID to the confirmed deletion helper.

set dotenv-load := true

# List available recipes
default:
    @just --list

# Install Python deps into the project venv
sync:
    uv sync

# Run the Python-level test suite
# Use pytest, not `unittest discover`: unittest ignores conftest.py, so the
# shared bootstrap there (DB/embedding stubs, rerank disabled) never runs and
# tests fail spuriously. pytest collects the same unittest TestCases and loads
# conftest. (`uv run pytest` resolves to the wrong binary here — invoke via -m.)
test:
    uv run python -m pytest tests/

# Run tests plus the ephemeral Docker PostgreSQL + pgvector schema trial
test-postgres:
    bash scripts/run-tests.sh --postgres

# Spin up an ephemeral Postgres and apply the schema, without running tests
db-trial:
    bash scripts/trial-run-postgres.sh

# Drive the real thought pipeline (store/extract/backfill) against an ephemeral
# Postgres over asyncpg; only the model calls are stubbed (Plan F live check)
pipeline-trial:
    uv run python scripts/trial-run-thought-pipeline.py

# Lint with ruff
lint:
    uv run ruff check .

# Format with ruff
fmt:
    uv run ruff format .

# Check formatting without modifying files
fmt-check:
    uv run ruff format --check .

# Run all non-destructive checks
check: lint fmt-check test

# Apply the core SQL schema to DATABASE_URL (reads .env)
setup-db:
    bash scripts/setup_db.sh

# Full install: venv, deps, DB, systemd service (needs sudo)
install:
    bash scripts/install.sh

# Run the MCP server locally
serve:
    uv run python -m openbrain.server

# Ingest a PDF/text file or directory into the library
ingest *ARGS:
    uv run python -m openbrain.cli {{ARGS}}

# Run the memory-extraction + embedding-backfill maintenance job
maintenance *ARGS:
    uv run python scripts/openbrain-maintenance.py {{ARGS}}

# Back up the database
backup:
    bash scripts/backup.sh

# Install openbrain hooks + skills into ~/.claude, ~/.codex, ~/.cursor
install-integrations:
    python scripts/install_assistant_integrations.py

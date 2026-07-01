#!/usr/bin/env bash
# Run local tests, with an optional Docker-backed PostgreSQL schema trial run.
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
RUN_POSTGRES=false

case "${1:-}" in
    "")
        ;;
    --postgres|--all)
        RUN_POSTGRES=true
        ;;
    *)
        echo "Usage: bash scripts/run-tests.sh [--postgres|--all]"
        exit 2
        ;;
esac

echo "== openbrain verification =="
echo
echo "[1/2] Running Python-level tests..."
(
    cd "$PROJECT_DIR"
    python -m unittest discover -s tests -v
)
echo "PASS: Python-level tests"

echo
if [ "$RUN_POSTGRES" = true ]; then
    echo "[2/3] Running PostgreSQL + pgvector schema trial run..."
    bash "$SCRIPT_DIR/trial-run-postgres.sh"
    echo "PASS: PostgreSQL + pgvector schema trial run"
    echo
    echo "[3/3] Running thought-pipeline live round-trip (model calls stubbed)..."
    uv run python "$SCRIPT_DIR/trial-run-thought-pipeline.py"
    echo "PASS: thought-pipeline live round-trip"
else
    echo "[2/3] Skipping PostgreSQL trial run."
    echo "[3/3] Skipping thought-pipeline round-trip."
    echo "Run with --postgres to include the ephemeral Docker database checks."
fi

echo
echo "RESULT: PASS"

#!/usr/bin/env bash
# setup_db.sh — Install the clean core schema in order.
# Run from the project root after filling in .env.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Load .env from project root
if [ -f "$PROJECT_DIR/.env" ]; then
    set -o allexport
    source "$PROJECT_DIR/.env"
    set +o allexport
else
    echo "ERROR: .env not found at $PROJECT_DIR/.env"
    echo "Copy .env.example to .env and fill in your credentials."
    exit 1
fi

if [ -z "${DATABASE_URL:-}" ]; then
    echo "ERROR: DATABASE_URL is not set in .env"
    exit 1
fi

echo "Installing core schema in: $DATABASE_URL"

for schema in "$PROJECT_DIR"/schemas/core/*.sql; do
    echo "  → $(basename "$schema")"
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f "$schema"
done

echo "Database setup complete."

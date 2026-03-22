#!/usr/bin/env bash
# setup_db.sh — Apply all migrations in order.
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

echo "Applying migrations to: $DATABASE_URL"

for migration in "$PROJECT_DIR"/migrations/*.sql; do
    echo "  → $(basename "$migration")"
    psql "$DATABASE_URL" -f "$migration"
done

echo "Database setup complete."

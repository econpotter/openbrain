#!/usr/bin/env bash
# backup.sh — Back up the Open Brain database and library files.
#
# Usage:
#   bash scripts/backup.sh
#
# Configured via .env:
#   BACKUP_DIR        — where to write backups (default: ~/backups/openbrain)
#   BACKUP_KEEP_DAYS  — how many days of backups to keep (default: 30)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Load .env
if [ -f "$PROJECT_DIR/.env" ]; then
    set -o allexport
    source "$PROJECT_DIR/.env"
    set +o allexport
else
    echo "ERROR: .env not found at $PROJECT_DIR/.env"
    exit 1
fi

BACKUP_DIR="${BACKUP_DIR:-$HOME/backups/openbrain}"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
KEEP_DAYS="${BACKUP_KEEP_DAYS:-30}"

mkdir -p "$BACKUP_DIR"

# ── Database ──────────────────────────────────────────────────────────────────
echo "Backing up database..."
DB_FILE="$BACKUP_DIR/openbrain_${TIMESTAMP}.sql.gz"
pg_dump "$DATABASE_URL" | gzip > "$DB_FILE"
echo "  → $DB_FILE"

# ── Library files ─────────────────────────────────────────────────────────────
if [ -n "${LIBRARY_PATH:-}" ]; then
    LIBRARY_EXPANDED="${LIBRARY_PATH/#\~/$HOME}"
    if [ -d "$LIBRARY_EXPANDED" ]; then
        echo "Backing up library files..."
        LIB_FILE="$BACKUP_DIR/library_${TIMESTAMP}.tar.gz"
        tar -czf "$LIB_FILE" -C "$(dirname "$LIBRARY_EXPANDED")" \
            "$(basename "$LIBRARY_EXPANDED")"
        echo "  → $LIB_FILE"
    else
        echo "  Library path not found, skipping: $LIBRARY_EXPANDED"
    fi
fi

# ── Prune old backups ─────────────────────────────────────────────────────────
PRUNED=$(find "$BACKUP_DIR" -name "*.gz" -mtime "+$KEEP_DAYS" -print -delete | wc -l)
if [ "$PRUNED" -gt 0 ]; then
    echo "Pruned $PRUNED backup(s) older than $KEEP_DAYS days."
fi

echo "Backup complete."

#!/usr/bin/env bash
# backup.sh — Back up the openbrain database and library files.
#
# Usage:
#   bash scripts/backup.sh
#
# Secrets are loaded from .env. Non-secret backup settings are read from
# config.yaml:
#   backup_path
#   backup.keep_days
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

PYTHON="$PROJECT_DIR/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
    PYTHON="python3"
fi

eval "$("$PYTHON" - "$PROJECT_DIR/config.yaml" <<'PY'
import shlex
import sys
from pathlib import Path

import yaml

config_path = Path(sys.argv[1])
with config_path.open() as f:
    config = yaml.safe_load(f) or {}

backup = config.get("backup", {})
backup_path = config.get("backup_path", "~/backups/openbrain")
library_path = config.get("library_path", "")

backup_dir = Path(backup_path).expanduser()

print(f"BACKUP_DIR={shlex.quote(str(backup_dir))}")
print(f"KEEP_DAYS={shlex.quote(str(backup.get('keep_days', 30)))}")
print(f"LIBRARY_PATH={shlex.quote(str(library_path))}")
PY
)"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)

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

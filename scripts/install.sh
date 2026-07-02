#!/usr/bin/env bash
# install.sh — Full install: venv, deps, DB, systemd service.
# Run from the project root as a user with sudo access.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
SERVICE_NAME="openbrain-mcp"
CURRENT_USER="$(whoami)"

echo "=== openbrain install ==="
echo "Project dir: $PROJECT_DIR"
echo "User:        $CURRENT_USER"
echo

# ── 1. Python venv ──────────────────────────────────────────────────────────
echo "[1/6] Creating Python virtual environment..."
cd "$PROJECT_DIR"
python3 -m venv .venv
source .venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -e ".[server]"
echo "      Done."

# ── 2. .env ──────────────────────────────────────────────────────────────────
echo "[2/6] Checking .env..."
if [ ! -f "$PROJECT_DIR/.env" ]; then
    cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
    echo "      Created .env from .env.example."
    echo "      *** Fill in DATABASE_URL, provider API keys, and MCP_ACCESS_KEY if you want HTTP auth. Backup location comes from library_path. ***"
    echo "      Then re-run this script."
    exit 0
else
    echo "      .env already exists, skipping."
fi

# ── 3. Database ───────────────────────────────────────────────────────────────
echo "[3/6] Installing core database schema..."
bash "$SCRIPT_DIR/setup_db.sh"

# ── 4. systemd service ────────────────────────────────────────────────────────
echo "[4/6] Installing systemd service..."
SERVICE_SRC="$SCRIPT_DIR/openbrain-mcp.service"
SERVICE_DST="/etc/systemd/system/$SERVICE_NAME.service"

# Substitute placeholders in the service template
sed \
    -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
    -e "s|__USER__|$CURRENT_USER|g" \
    "$SERVICE_SRC" | sudo tee "$SERVICE_DST" > /dev/null

sudo systemctl daemon-reload
sudo systemctl enable --now "$SERVICE_NAME"
echo "      Service enabled and started."
echo "      Check status: sudo systemctl status $SERVICE_NAME"

# ── 5. Backup timer ───────────────────────────────────────────────────────────
echo "[5/6] Installing backup timer..."
for unit in openbrain-backup.service openbrain-backup.timer; do
    sed \
        -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
        -e "s|__USER__|$CURRENT_USER|g" \
        "$SCRIPT_DIR/$unit" | sudo tee "/etc/systemd/system/$unit" > /dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable --now openbrain-backup.timer
echo "      Backup timer enabled (runs daily)."
echo "      Run now: bash $SCRIPT_DIR/backup.sh"

# ── 6. Thought straggler sweep timer ──────────────────────────────────────────
echo "[6/6] Installing thought straggler sweep timer..."
for unit in openbrain-sweep.service openbrain-sweep.timer; do
    sed \
        -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
        -e "s|__USER__|$CURRENT_USER|g" \
        "$SCRIPT_DIR/$unit" | sudo tee "/etc/systemd/system/$unit" > /dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable --now openbrain-sweep.timer
echo "      Sweep timer enabled (heals thought stragglers every few minutes)."
echo "      Run now: $PROJECT_DIR/.venv/bin/python scripts/openbrain-maintenance.py --sweep"

echo
echo "=== Installation complete ==="
echo "Next: set up HTTPS with Caddy (required for remote clients):"
echo "      bash scripts/setup-caddy.sh <your-hostname>"
echo
echo "Then connect Claude.ai:   Settings → Connectors → Add MCP server"
echo "URL:                      https://<your-hostname>:8443/mcp"
echo "Connect Claude Code:      claude mcp add --transport http --header \"Authorization: Bearer <your-mcp-access-key>\" openbrain https://<your-hostname>:8443/mcp"

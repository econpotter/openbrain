#!/usr/bin/env bash
# Install the openbrain thin-client sweep as a systemd *user* timer.
#
# Prereq: `uv tool install openbrain` (or `uv tool install .` from this repo) so
# the `openbrain` command is on your PATH, and OPENBRAIN_MCP_URL/OPENBRAIN_MCP_KEY
# are set (env, ~/.openbrain/config.yaml, or ~/.openbrain/env).
#
# This is the local, capture-side counterpart to the server-side
# openbrain-maintenance container — that container is untouched.
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

mkdir -p "$UNIT_DIR"
install -m 644 "$SRC_DIR/openbrain-sweep.service" "$UNIT_DIR/openbrain-sweep.service"
install -m 644 "$SRC_DIR/openbrain-sweep.timer" "$UNIT_DIR/openbrain-sweep.timer"

systemctl --user daemon-reload
systemctl --user enable --now openbrain-sweep.timer

echo "Installed openbrain-sweep.timer (user)."
echo "  Status:   systemctl --user status openbrain-sweep.timer"
echo "  Run now:  systemctl --user start openbrain-sweep.service"
echo "  Logs:     journalctl --user -u openbrain-sweep.service"

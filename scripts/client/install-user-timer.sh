#!/usr/bin/env bash
# Install the openbrain thin-client sweep as a systemd *user* timer.
#
# Prereq: `uv tool install openbrain` (or `uv tool install .` from this repo) so
# the `openbrain` command is on your PATH, and OPENBRAIN_MCP_URL/OPENBRAIN_MCP_KEY
# are in ~/.openbrain/env or ~/.openbrain/config.yaml. The timer cannot see
# variables exported by your shell profile.
#
# This is the local, capture-side counterpart to the server-side
# openbrain-maintenance container — that container is untouched.
set -euo pipefail

if [[ ! -s "$HOME/.openbrain/env" && ! -s "$HOME/.openbrain/config.yaml" ]]; then
  echo "error: put OPENBRAIN_MCP_URL and OPENBRAIN_MCP_KEY in ~/.openbrain/env first." >&2
  exit 1
fi

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

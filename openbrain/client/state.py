"""Per-session capture watermark, persisted to ~/.openbrain/capture-state.json.

Keyed by session id; each value records the transcript line count already
captured. The watermark advances only after the server acknowledges a store (or
an explicit trivial-skip) so a failed round trip is retried, never lost.
"""

from __future__ import annotations

import json
from pathlib import Path

from openbrain.client.config import STATE_PATH


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def get_watermark(session_id: str, path: Path = STATE_PATH) -> int:
    entry = _load(path).get(session_id)
    if isinstance(entry, dict):
        return int(entry.get("lines", 0))
    return 0


def set_watermark(session_id: str, lines: int, path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _load(path)
    data[session_id] = {"lines": lines}
    path.write_text(json.dumps(data, indent=2, sort_keys=True))


def all_watermarks(path: Path = STATE_PATH) -> dict:
    return _load(path)

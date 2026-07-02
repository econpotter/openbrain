"""Fail-soft capture spool at ~/.openbrain/spool/.

When the server is unreachable, a capture is serialized to a spool file and the
command still exits 0 — a hook must never block a session or lose a capture. A
`sweep` flushes the spool first-in-first-out; an entry is removed only after the
server acknowledges its store.

Each spool file holds one queued MCP tool call::

    {"tool": "capture_thought", "arguments": {...}, "queued_at": "<iso>"}
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from openbrain.client.config import SPOOL_DIR


def _new_name(index: int) -> str:
    # Monotonic-ish, FIFO-orderable by name: pid + a per-call index. No wall
    # clock needed for ordering; enqueue order within a process is preserved and
    # os.getpid keeps concurrent processes from colliding.
    return f"{os.getpid():07d}-{index:04d}.json"


def enqueue(tool: str, arguments: dict, queued_at: str, spool_dir: Path = SPOOL_DIR) -> Path:
    """Serialize one queued tool call to the spool. Returns the file path."""
    spool_dir.mkdir(parents=True, exist_ok=True)
    index = len(list(spool_dir.glob("*.json")))
    path = spool_dir / _new_name(index)
    # Guard against a same-name collision within one process burst.
    while path.exists():
        index += 1
        path = spool_dir / _new_name(index)
    payload = {"tool": tool, "arguments": arguments, "queued_at": queued_at}
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    return path


def pending(spool_dir: Path = SPOOL_DIR) -> list[Path]:
    """Spool files in FIFO order (by filename)."""
    if not spool_dir.exists():
        return []
    return sorted(spool_dir.glob("*.json"))


def load(path: Path) -> dict:
    return json.loads(path.read_text())


def remove(path: Path) -> None:
    path.unlink(missing_ok=True)


def depth(spool_dir: Path = SPOOL_DIR) -> int:
    return len(pending(spool_dir))

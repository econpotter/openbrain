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
import tempfile
import time
from pathlib import Path

from openbrain.client.config import SPOOL_DIR


def _new_name(index: int) -> str:
    # FIFO-orderable by name across processes: a nanosecond wall-clock stamp
    # sorts entries by *when* they were queued, not by which pid queued them, so
    # an older capture from a higher-pid process is never swept after a newer one
    # from a lower-pid process. Fixed-width fields keep lexicographic sort ==
    # chronological sort. pid + a per-call index follow the stamp only as a
    # tiebreaker: pid keeps concurrent processes collision-free, and index
    # preserves intra-process FIFO when two enqueues land in the same nanosecond.
    return f"{time.time_ns():020d}-{os.getpid():07d}-{index:04d}.json"


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
    data = json.dumps(payload, indent=2, sort_keys=True)
    # Atomic write: fully materialize a temp file in the same dir, fsync it, then
    # os.replace into place. A crash mid-write leaves only the .tmp file (which
    # the *.json glob ignores), never a partial JSON that load() would choke on.
    fd, tmp = tempfile.mkstemp(prefix=".spool-", suffix=".json.tmp", dir=spool_dir)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
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

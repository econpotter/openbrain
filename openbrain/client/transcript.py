"""Parse Claude Code transcript JSONL and extract the conversation delta.

A transcript is one JSON object per line. Only `user` and `assistant` message
lines carry conversation text; everything else (mode/permission/attachment/
tool_use/tool_result/thinking) is control or machine noise and is dropped from
the summarization delta. The watermark is a line count per session, so re-runs
process only lines appended since the last acknowledged capture.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Delta:
    text: str
    total_lines: int  # watermark to advance to once the capture is acknowledged


def _text_from_message(msg: dict) -> str:
    """Extract human-readable text from one user/assistant message."""
    content = msg.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            str(block.get("text", "")).strip()
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "\n".join(p for p in parts if p)
    return ""


def _line_text(line: str) -> str:
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return ""
    if obj.get("type") not in ("user", "assistant"):
        return ""
    msg = obj.get("message")
    if not isinstance(msg, dict):
        return ""
    role = msg.get("role") or obj.get("type")
    body = _text_from_message(msg)
    if not body:
        return ""
    return f"{role}: {body}"


def read_delta(transcript_path: str | Path, watermark: int) -> Delta:
    """Read conversation text appended past `watermark` lines.

    `total_lines` in the returned Delta is the new watermark to persist once the
    server acknowledges the capture. If nothing was appended, text is empty and
    total_lines equals the current line count.
    """
    path = Path(transcript_path).expanduser()
    lines = path.read_text(errors="replace").splitlines()
    total = len(lines)
    delta_lines = lines[watermark:] if watermark < total else []
    texts = [t for t in (_line_text(line) for line in delta_lines) if t]
    return Delta(text="\n\n".join(texts), total_lines=total)

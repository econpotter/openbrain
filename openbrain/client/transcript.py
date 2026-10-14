"""Parse assistant transcript JSONL and extract the conversation delta.

A transcript is one JSON object per line. Only `user` and `assistant` message
lines carry conversation text; everything else (mode/permission/attachment/
tool_use/tool_result/thinking, plus Codex's per-turn `developer` boilerplate) is
control or machine noise and is dropped from the summarization delta. The
watermark is a line count per session, so re-runs process only lines appended
since the last acknowledged capture.

Two providers are recognized and auto-detected per line by shape, so a single
sweep pass can mix both kinds of transcript on disk:

- Claude Code: a flat line, `{"type": "user"|"assistant", "message": {...}}`.
- Codex: `{"type": "response_item", "payload": {"type": "message", "role":
  "user"|"assistant"|"developer", "content": [{"type": "input_text"|
  "output_text", "text": ...}]}}`. The `developer` role carries injected
  AGENTS.md/system boilerplate on every turn and is excluded.
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


def _claude_line_text(obj: dict) -> str:
    """Extract text from a Claude Code line: flat type + sibling message dict."""
    msg = obj.get("message")
    if not isinstance(msg, dict):
        return ""
    role = msg.get("role") or obj.get("type")
    body = _text_from_message(msg)
    if not body:
        return ""
    return f"{role}: {body}"


def _codex_line_text(obj: dict) -> str:
    """Extract text from a Codex `response_item` message line.

    Keeps only user/assistant roles (drops `developer` boilerplate) and joins the
    `input_text`/`output_text` content blocks.
    """
    payload = obj.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "message":
        return ""
    role = payload.get("role")
    if role not in ("user", "assistant"):
        return ""
    content = payload.get("content")
    if not isinstance(content, list):
        return ""
    parts = [
        str(block.get("text", "")).strip()
        for block in content
        if isinstance(block, dict)
        and block.get("type") in ("input_text", "output_text")
    ]
    body = "\n".join(p for p in parts if p)
    if not body:
        return ""
    return f"{role}: {body}"


def _line_text(line: str) -> str:
    """Return one line's conversation text, auto-detecting the provider shape.

    Unparseable or unrecognized lines contribute empty text; never raises, so a
    stray line shape can never abort a capture.
    """
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return ""
    if not isinstance(obj, dict):
        return ""
    # Claude Code: flat user/assistant line with a sibling `message` dict.
    if obj.get("type") in ("user", "assistant") and isinstance(
        obj.get("message"), dict
    ):
        return _claude_line_text(obj)
    # Codex: a `response_item` wrapping a `payload` message.
    if obj.get("type") == "response_item":
        return _codex_line_text(obj)
    return ""


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


def read_last_reply(transcript_path: str | Path) -> str:
    """Return the agent's text from its most recent reply, or "".

    Walks back from the end, skipping any user line already written for the
    current prompt, then joins assistant text lines until the user line before
    them. Never raises on a bad line.
    """
    path = Path(transcript_path).expanduser()
    reply: list[str] = []
    for line in reversed(path.read_text(errors="replace").splitlines()):
        text = _line_text(line)
        if text.startswith("assistant: "):
            reply.append(text.removeprefix("assistant: "))
        elif text.startswith("user: ") and reply:
            break
    return "\n\n".join(reversed(reply))

"""openbrain thin-client CLI.

Talks MCP to the openbrain server so hooks and timers have a deterministic way to
capture and recall. Every capture path fails soft: if the server is unreachable
the payload is spooled locally and the command still exits 0, so a hook never
blocks a session or surfaces a stack trace.

Subcommands:
    capture             direct passthrough to capture_thought
    capture-transcript  summarize a transcript delta past the watermark
    brief               briefing + project-relevant recall (for SessionStart)
    sweep               flush the spool, then capture any lagging session
    status              connectivity + spool depth + watermark summary
"""

from __future__ import annotations

import json
import select
import sys
from datetime import datetime, timezone
from pathlib import Path

import typer
from rich.console import Console

from openbrain.client import spool, state, transcript
from openbrain.client.config import ConfigError, load_config
from openbrain.client.mcp import MCPClient, MCPError, MCPToolError

app = typer.Typer(help="openbrain thin client: capture and recall over MCP.")
console = Console()
# stderr for diagnostics; stdout stays clean for hook additionalContext capture.
err = Console(stderr=True)

DEFAULT_TRANSCRIPT_DIRS = [Path.home() / ".claude" / "projects"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _capture_or_spool(tool: str, arguments: dict) -> tuple[bool, str]:
    """Call a tool online; on any transport failure, spool it. Never raises.

    Returns (online, message). online=True means the server acknowledged the
    call (message is its reply); online=False means it was spooled.
    """
    try:
        cfg = load_config()
    except ConfigError as exc:
        # No config at all: still fail soft by spooling, so a later configured
        # sweep can flush it.
        spool.enqueue(tool, arguments, _now())
        return False, f"not configured ({exc.args[0].splitlines()[0]}); spooled"
    try:
        with MCPClient(cfg) as client:
            reply = client.call_tool(tool, arguments)
        return True, reply
    except (MCPError, MCPToolError) as exc:
        spool.enqueue(tool, arguments, _now())
        return False, f"server unreachable ({exc}); spooled"


@app.command()
def capture(
    text: str = typer.Argument(..., help="Thought text to store"),
    type: str = typer.Option("thought", "--type", help="thought|note|memory|conversation"),
    tags: list[str] = typer.Option(None, "--tags", help="Topic keyword; repeatable"),
    projects: list[str] = typer.Option(None, "--projects", help="Project name; repeatable"),
    people: list[str] = typer.Option(None, "--people", help="Person name; repeatable"),
):
    """Capture a thought directly (passthrough to capture_thought)."""
    arguments: dict = {"content": text, "type": type, "source": "openbrain-cli"}
    if tags:
        arguments["tags"] = tags
    if projects:
        arguments["projects"] = projects
    if people:
        arguments["people"] = people

    online, message = _capture_or_spool("capture_thought", arguments)
    (console if online else err).print(message)
    raise typer.Exit(0)


def _read_hook_stdin() -> dict:
    """Claude Code passes hook JSON on stdin (transcript_path, session_id, cwd).

    Reads only if stdin actually has data pending — a hook pipes JSON then closes
    it, but an interactive or empty-but-open pipe would otherwise block .read()
    forever. select() with a zero-ish timeout distinguishes the two.
    """
    if sys.stdin is None or sys.stdin.isatty():
        return {}
    try:
        ready, _, _ = select.select([sys.stdin], [], [], 0.2)
    except (ValueError, OSError):
        return {}
    if not ready:
        return {}
    raw = sys.stdin.read().strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


@app.command(name="capture-transcript")
def capture_transcript(
    transcript_path: str = typer.Argument(None, help="Path to the transcript JSONL"),
    session: str = typer.Option(None, "--session", help="Session id (watermark key)"),
    trigger: str = typer.Option(None, "--trigger", help="stop|auto|manual"),
):
    """Capture the transcript delta past this session's watermark.

    Reads the appended lines, sends them to the server's capture_transcript tool,
    and advances the watermark once the delta is durably captured (server ack or
    spool). Accepts Claude Code hook JSON on stdin so a hook body needs no
    argument plumbing.
    """
    hook = _read_hook_stdin()
    path = transcript_path or hook.get("transcript_path")
    session_id = session or hook.get("session_id")
    trig = trigger or hook.get("hook_event_name") or "manual"

    if not path or not session_id:
        err.print("capture-transcript: need a transcript path and session id.")
        raise typer.Exit(0)  # fail soft — never break a hook
    if not Path(path).expanduser().exists():
        err.print(f"capture-transcript: transcript not found: {path}")
        raise typer.Exit(0)

    watermark = state.get_watermark(session_id)
    delta = transcript.read_delta(path, watermark)
    if not delta.text.strip():
        err.print("capture-transcript: no new content past watermark.")
        # Nothing to capture; keep the watermark current so we don't rescan.
        state.set_watermark(session_id, delta.total_lines)
        raise typer.Exit(0)

    arguments = {
        "content": delta.text,
        "session_id": session_id,
        "trigger": trig,
        "source": hook.get("source", "claude-code"),
    }
    online, message = _capture_or_spool("capture_transcript", arguments)
    # Advance the watermark once the delta is durably committed — server ack
    # (stored or trivial-skip) or a durable spool entry. Exactly-once, no rescan.
    state.set_watermark(session_id, delta.total_lines)
    (console if online else err).print(message)
    raise typer.Exit(0)


def _infer_project(cwd: str | None) -> str | None:
    if not cwd:
        return None
    name = Path(cwd).name
    return name or None


@app.command()
def brief(
    cwd: str = typer.Option(None, "--cwd", help="Working dir to scope recall"),
    query: str = typer.Option(None, "--query", help="Explicit recall query"),
):
    """Print a briefing plus project-relevant recall (SessionStart context).

    One round trip, short timeout. On any failure prints nothing and exits 0 so a
    SessionStart hook injects empty additionalContext rather than an error.
    """
    hook = _read_hook_stdin()
    scope = cwd or hook.get("cwd")
    recall_query = query or _infer_project(scope)
    try:
        cfg = load_config(timeout=8.0)
        with MCPClient(cfg) as client:
            briefing = client.call_tool("get_briefing", {"days": 1})
            recall = ""
            if recall_query:
                recall = client.call_tool("search_brain", {"query": recall_query})
    except (ConfigError, MCPError, MCPToolError):
        raise typer.Exit(0)  # silent: no context is better than a broken hook

    blocks = [b for b in (briefing.strip(), recall.strip()) if b]
    if blocks:
        console.print("\n\n".join(blocks))
    raise typer.Exit(0)


def _flush_spool(client: MCPClient) -> tuple[int, int]:
    """Flush spooled captures FIFO. Returns (flushed, remaining)."""
    flushed = 0
    for entry_path in spool.pending():
        try:
            entry = spool.load(entry_path)
        except (json.JSONDecodeError, OSError) as exc:
            # A stray malformed spool file (e.g. a crash mid-write on an older
            # client) must not abort the whole flush. Skip it loudly and leave it
            # on disk — never silently discard a capture we cannot parse.
            err.print(f"sweep: skipping malformed spool file {entry_path.name}: {exc}")
            continue
        try:
            client.call_tool(entry["tool"], entry.get("arguments", {}))
        except (MCPError, MCPToolError):
            break  # server down again mid-flush; stop, keep the rest spooled
        spool.remove(entry_path)
        flushed += 1
    return flushed, spool.depth()


def _sweep_transcripts(client: MCPClient, dirs: list[Path]) -> int:
    """Capture any session whose watermark lags its transcript. Returns captures."""
    captured = 0
    for base in dirs:
        base = base.expanduser()
        if not base.exists():
            continue
        for tpath in sorted(base.glob("**/*.jsonl")):
            session_id = tpath.stem
            watermark = state.get_watermark(session_id)
            delta = transcript.read_delta(tpath, watermark)
            if not delta.text.strip():
                if delta.total_lines != watermark:
                    state.set_watermark(session_id, delta.total_lines)
                continue
            try:
                client.call_tool(
                    "capture_transcript",
                    {
                        "content": delta.text,
                        "session_id": session_id,
                        "trigger": "sweep",
                        "source": "claude-code",
                    },
                )
            except (MCPError, MCPToolError) as exc:
                err.print(f"sweep: {tpath.name}: {exc}")
                continue
            state.set_watermark(session_id, delta.total_lines)
            captured += 1
    return captured


@app.command()
def sweep(
    transcript_dirs: list[str] = typer.Option(
        None, "--transcript-dirs", help="Dirs of *.jsonl transcripts to scan"
    ),
):
    """Flush the spool, then capture any lagging session. Idempotent."""
    dirs = [Path(d) for d in transcript_dirs] if transcript_dirs else DEFAULT_TRANSCRIPT_DIRS
    try:
        cfg = load_config()
    except ConfigError as exc:
        err.print(str(exc))
        raise typer.Exit(1)
    try:
        with MCPClient(cfg) as client:
            flushed, remaining = _flush_spool(client)
            captured = _sweep_transcripts(client, dirs)
    except (MCPError, MCPToolError) as exc:
        err.print(f"sweep: server unreachable: {exc}")
        raise typer.Exit(1)
    console.print(
        f"Flushed {flushed} spooled capture(s) ({remaining} remaining); "
        f"captured {captured} lagging session(s)."
    )
    raise typer.Exit(0)


@app.command()
def status():
    """Report connectivity, spool depth, and watermark summary."""
    console.print(f"Spool depth: {spool.depth()}")
    watermarks = state.all_watermarks()
    console.print(f"Tracked sessions: {len(watermarks)}")
    try:
        cfg = load_config(timeout=8.0)
    except ConfigError as exc:
        console.print(f"Config: [red]not configured[/red] — {exc.args[0].splitlines()[0]}")
        raise typer.Exit(0)
    console.print(f"Endpoint: {cfg.url}")
    try:
        with MCPClient(cfg) as client:
            reply = client.call_tool("get_stats", {})
        console.print("Server: [green]reachable[/green]")
        console.print(reply)
    except (MCPError, MCPToolError) as exc:
        console.print(f"Server: [red]unreachable[/red] — {exc}")
    raise typer.Exit(0)


if __name__ == "__main__":
    app()

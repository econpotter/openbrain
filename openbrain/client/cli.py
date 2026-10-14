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
    sync-projects       mirror the workspace PROJECTS.md into the projects table
"""

from __future__ import annotations

import json
import os
import re
import select
import sys
import time
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

# Ordered {source_label: transcript root}. Each swept directory tags its own
# captures with the matching source, so one sweep pass labels Claude Code and
# Codex sessions distinctly. The parser (transcript.py) auto-detects the line
# shape regardless; the label is the naming/analytics side.
DEFAULT_TRANSCRIPT_DIRS: dict[str, Path] = {
    "claude-code": Path.home() / ".claude" / "projects",
    "codex": Path.home() / ".codex" / "sessions",
}


def _source_for_dir(base: Path) -> str:
    """Best-effort source label for an explicitly-passed sweep directory.

    Reverse-matches the known default roots; falls back to `codex` for a
    `.codex` path and `claude-code` otherwise. The parser tells the transcripts
    apart by shape either way, so this only affects the analytics label.
    """
    resolved = base.expanduser().resolve()
    for label, known in DEFAULT_TRANSCRIPT_DIRS.items():
        if resolved == known.expanduser().resolve():
            return label
    return "codex" if ".codex" in resolved.parts else "claude-code"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _capture_or_spool(tool: str, arguments: dict) -> tuple[bool, str]:
    """Call a tool online; on any transport failure, spool it. Never raises.

    Returns (online, message). online=True means the server acknowledged the
    call (message is its reply); online=False means it was spooled.
    """

    def _spool_or_report(reason: str) -> tuple[bool, str]:
        try:
            spool.enqueue(tool, arguments, _now())
        except OSError as exc:
            return False, f"{reason}; spool failed ({exc})"
        return False, f"{reason}; spooled"

    try:
        cfg = load_config()
    except ConfigError as exc:
        # No config at all: still fail soft by spooling, so a later configured
        # sweep can flush it.
        return _spool_or_report(f"not configured ({exc.args[0].splitlines()[0]})")
    try:
        with MCPClient(cfg) as client:
            reply = client.call_tool(tool, arguments)
        return True, reply
    except (MCPError, MCPToolError) as exc:
        return _spool_or_report(f"server unreachable ({exc})")


@app.command()
def capture(
    text: str = typer.Argument(..., help="Thought text to store"),
    type: str = typer.Option(
        "thought", "--type", help="thought|note|memory|conversation"
    ),
    tags: list[str] = typer.Option(None, "--tags", help="Topic keyword; repeatable"),
    projects: list[str] = typer.Option(
        None, "--projects", help="Project name; repeatable"
    ),
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


def _find_codex_transcript(session_id: str | None) -> Path | None:
    """Find the Codex JSONL transcript for a thread id.

    Codex exposes the thread id in CODEX_THREAD_ID in non-interactive hook
    contexts. Session files are date-nested and include that id in the filename.
    """
    if not session_id:
        return None
    base = DEFAULT_TRANSCRIPT_DIRS["codex"].expanduser()
    matches = sorted(
        base.glob(f"**/*{session_id}.jsonl"),
        key=lambda p: p.stat().st_mtime if p.exists() else 0,
        reverse=True,
    )
    return matches[0] if matches else None


def _quiet_print(quiet: bool, message: str, *, stderr: bool = True) -> None:
    if quiet:
        return
    (err if stderr else console).print(message)


def _set_watermark_or_report(quiet: bool, session_id: str, total_lines: int) -> None:
    try:
        state.set_watermark(session_id, total_lines)
    except OSError as exc:
        _quiet_print(quiet, f"capture-transcript: watermark update failed: {exc}")


@app.command(name="capture-transcript")
def capture_transcript(
    transcript_path: str = typer.Argument(None, help="Path to the transcript JSONL"),
    session: str = typer.Option(None, "--session", help="Session id (watermark key)"),
    trigger: str = typer.Option(None, "--trigger", help="stop|auto|manual"),
    source: str = typer.Option(
        None, "--source", help="Provider label: claude-code|codex"
    ),
    quiet: bool = typer.Option(False, "--quiet", help="Suppress all hook output"),
    defer: bool = typer.Option(
        False,
        "--defer",
        help="Queue locally for sweep instead of calling the server now.",
    ),
):
    """Capture the transcript delta past this session's watermark.

    Reads the appended lines, sends them to the server's capture_transcript tool,
    and advances the watermark once the delta is durably captured (server ack or
    spool). Accepts hook JSON on stdin (Claude Code or Codex — same fields) so a
    hook body needs no argument plumbing.
    """
    hook = _read_hook_stdin()
    path = transcript_path or hook.get("transcript_path")
    session_id = (
        session
        or hook.get("session_id")
        or hook.get("thread_id")
        or os.environ.get("CODEX_THREAD_ID")
    )
    trig = trigger or hook.get("hook_event_name") or "manual"
    src = source or hook.get("source")
    if not path and src == "codex":
        codex_path = _find_codex_transcript(session_id)
        if codex_path is not None:
            path = str(codex_path)

    if not path or not session_id:
        _quiet_print(
            quiet, "capture-transcript: need a transcript path and session id."
        )
        raise typer.Exit(0)  # fail soft — never break a hook
    if not src:
        # No source label: refuse loudly rather than silently mislabel the
        # capture (Codex thoughts stamped claude-code, or vice versa). Every
        # shipped hook passes --source; this only trips a hand-rolled one.
        _quiet_print(
            quiet, "capture-transcript: need a --source label (e.g. claude-code|codex)."
        )
        raise typer.Exit(0)
    if not Path(path).expanduser().exists():
        _quiet_print(quiet, f"capture-transcript: transcript not found: {path}")
        raise typer.Exit(0)

    watermark = state.get_watermark(session_id)
    delta = transcript.read_delta(path, watermark)
    if not delta.text.strip():
        _quiet_print(quiet, "capture-transcript: no new content past watermark.")
        # Nothing to capture; keep the watermark current so we don't rescan.
        _set_watermark_or_report(quiet, session_id, delta.total_lines)
        raise typer.Exit(0)

    arguments = {
        "content": delta.text,
        "session_id": session_id,
        "trigger": trig,
        "source": src,
    }
    if defer:
        try:
            spool.enqueue("capture_transcript", arguments, _now())
        except OSError as exc:
            _quiet_print(quiet, f"capture-transcript: spool failed ({exc})")
            raise typer.Exit(0)
        _set_watermark_or_report(quiet, session_id, delta.total_lines)
        _quiet_print(quiet, "capture-transcript: queued for sweep.", stderr=False)
        raise typer.Exit(0)

    online, message = _capture_or_spool("capture_transcript", arguments)
    # Advance the watermark once the delta is durably committed — server ack
    # (stored or trivial-skip) or a durable spool entry. Exactly-once, no rescan.
    _set_watermark_or_report(quiet, session_id, delta.total_lines)
    if not quiet:
        (console if online else err).print(message)
    raise typer.Exit(0)


def _infer_project(cwd: str | None) -> str | None:
    if not cwd:
        return None
    name = Path(cwd).name
    return name or None


# Total wall-clock budget for `brief`, spent across all of its round trips. The
# Claude Code SessionStart hook is killed at 10s, so this leaves ~4s of margin
# for interpreter startup, config load, and emitting the JSON envelope. A
# per-request timeout cannot bound this: three sequential calls at 8s each is
# 24s against a 10s limit.
BRIEF_BUDGET_SECONDS = 6.0

# Below this much remaining budget, skip the recall call rather than start a
# request that would only be cut off. The briefing is the payload; recall is
# enrichment.
BRIEF_MIN_RECALL_SECONDS = 1.0

# The sweep timer flushes the spool daily, so a queued capture older than this
# means the sweep has been failing.
SPOOL_STALE_DAYS = 2


def _spool_warning() -> str:
    """Name a stale spool, or return "" when the sweep is keeping up."""
    pending = spool.pending()
    if not pending:
        return ""
    age_days = (time.time() - pending[0].stat().st_mtime) / 86400
    if age_days < SPOOL_STALE_DAYS:
        return ""
    return (
        f"openbrain: {len(pending)} captures queued, the oldest {age_days:.0f} days old. "
        "The sweep is not sending them; run `openbrain sweep` to see why."
    )


def _emit_session_start(context: str, warning: str) -> None:
    """Print the SessionStart hook JSON: context for the model, warning for the user."""
    output: dict = {}
    if warning:
        output["systemMessage"] = warning
        context = "\n\n".join(b for b in (warning, context) if b)
    if context:
        output["hookSpecificOutput"] = {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    if output:
        # Raw stdout (typer.echo, not the rich Console): the SessionStart hook
        # parses this as JSON, and recall text can contain markup-like brackets
        # (e.g. "[2026-07-01]") that rich would mangle or wrap.
        typer.echo(json.dumps(output))


@app.command()
def brief(
    cwd: str = typer.Option(None, "--cwd", help="Working dir to scope recall"),
    query: str = typer.Option(None, "--query", help="Explicit recall query"),
):
    """Print a briefing plus project-relevant recall (SessionStart context).

    Emits the documented Claude Code SessionStart hook shape on stdout —
    `{"hookSpecificOutput": {"hookEventName": "SessionStart",
    "additionalContext": ...}}` — so the briefing and project-relevant recall are
    injected into the session as context (push recall).

    The whole command runs against a single wall-clock budget
    (`BRIEF_BUDGET_SECONDS`) rather than a per-request timeout: the remaining
    budget is handed to each successive round trip, and recall is dropped when
    too little is left. Recall is best-effort: if it fails the briefing is still
    emitted. If the briefing itself fails, or the spool has gone stale, emits a
    `systemMessage` warning naming the failure. Always exits 0.
    """
    deadline = time.monotonic() + BRIEF_BUDGET_SECONDS
    hook = _read_hook_stdin()
    scope = cwd or hook.get("cwd")
    recall_query = query or _infer_project(scope)
    try:
        # Floor the initial timeout above zero: httpx treats a non-positive
        # value as "give up immediately", and a request never attempted is
        # strictly worse than one that fails fast on connect.
        cfg = load_config(timeout=max(deadline - time.monotonic(), 0.1))
        with MCPClient(cfg) as client:
            briefing = client.call_tool("get_briefing", {"days": 1})
            recall = ""
            remaining = deadline - time.monotonic()
            if recall_query and remaining > BRIEF_MIN_RECALL_SECONDS:
                client.set_timeout(remaining)
                # Recall is enrichment, so a failure here degrades rather than
                # discards: the briefing is already in hand and is the payload
                # the session actually needs. Only a failed briefing is fatal.
                try:
                    recall = client.call_tool("search_brain", {"query": recall_query})
                except (MCPError, MCPToolError):
                    recall = ""
    except (ConfigError, MCPError, MCPToolError) as exc:
        reason = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        _emit_session_start("", f"openbrain: briefing failed: {reason}")
        raise typer.Exit(0)

    blocks = [b for b in (briefing.strip(), recall.strip()) if b]
    _emit_session_start("\n\n".join(blocks), _spool_warning())
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


def _sweep_transcripts(client: MCPClient, dirs: dict[str, Path]) -> int:
    """Capture any session whose watermark lags its transcript. Returns captures.

    `dirs` maps a source label to a transcript root; each swept directory tags
    its captures with its own label so Codex and Claude Code sessions stay
    distinct in a single pass.
    """
    captured = 0
    for source_label, base in dirs.items():
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
                        "source": source_label,
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
    if transcript_dirs:
        dirs = {_source_for_dir(Path(d)): Path(d) for d in transcript_dirs}
    else:
        dirs = DEFAULT_TRANSCRIPT_DIRS
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


DEFAULT_PROJECTS_FILE = Path("~/workspace/PROJECTS.md")

# One line of the server's list_projects reply: `name [status] — description`.
_PROJECT_LINE = re.compile(
    r"^(.+?) \[(active|paused|completed|archived)\](?: — (.*))?$"
)


def _read_registry(path: Path) -> dict[str, str]:
    """Read {name: Purpose} from PROJECTS.md, joining wrapped Purpose lines."""
    registry: dict[str, str] = {}
    name = None
    in_purpose = False
    for line in path.read_text().splitlines():
        if line.startswith("## "):
            name = line[3:].strip()
            registry[name] = ""
            in_purpose = False
        elif name and line.startswith("- Purpose:"):
            registry[name] = line.removeprefix("- Purpose:").strip()
            in_purpose = True
        elif name and in_purpose and line.startswith((" ", "\t")) and line.strip():
            registry[name] += " " + line.strip()
        else:
            in_purpose = False
    return registry


def _parse_projects(reply: str) -> dict[str, tuple[str, str]]:
    """Parse list_projects text into {name: (status, description)}."""
    projects: dict[str, tuple[str, str]] = {}
    last = None
    for line in reply.splitlines():
        match = _PROJECT_LINE.match(line)
        if match:
            last = match.group(1)
            projects[last] = (match.group(2), match.group(3) or "")
        elif last:  # continuation of a multi-line description
            status, description = projects[last]
            projects[last] = (status, f"{description}\n{line}")
    return projects


@app.command(name="sync-projects")
def sync_projects(
    file: str = typer.Option(
        str(DEFAULT_PROJECTS_FILE), "--file", help="Workspace project registry"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the changes without making them"
    ),
):
    """Mirror the PROJECTS.md registry into openbrain's projects table.

    Registry projects become active with their Purpose as description; active
    projects missing from the registry are archived. Never deletes. Exits 0 when
    the registry file is absent, so the timer passes on machines without it.
    """
    path = Path(file).expanduser()
    if not path.exists():
        console.print(f"sync-projects: {path} not found; nothing to sync.")
        raise typer.Exit(0)
    registry = _read_registry(path)
    try:
        cfg = load_config()
    except ConfigError as exc:
        err.print(str(exc))
        raise typer.Exit(1)
    failed = False
    try:
        with MCPClient(cfg) as client:
            existing = _parse_projects(client.call_tool("list_projects", {}))
            changes: list[tuple[str, str, dict]] = []
            for name, purpose in registry.items():
                if name not in existing:
                    changes.append(
                        (
                            f"add {name}",
                            "add_project",
                            {"name": name, "description": purpose, "status": "active"},
                        )
                    )
                    continue
                status, description = existing[name]
                fields = {}
                if description != purpose:
                    fields["description"] = purpose
                if status != "active":
                    fields["status"] = "active"
                if fields:
                    changes.append(
                        (
                            f"update {name}: {', '.join(fields)}",
                            "update_project",
                            {"name": name, **fields},
                        )
                    )
            for name, (status, _) in existing.items():
                if status == "active" and name not in registry:
                    changes.append(
                        (
                            f"archive {name}",
                            "update_project",
                            {"name": name, "status": "archived"},
                        )
                    )
            for line, tool, arguments in changes:
                console.print(line)
                if dry_run:
                    continue
                reply = client.call_tool(tool, arguments)
                # The server reports a failed lookup (no match, or a partial
                # name matching several projects) as a normal reply.
                if not reply.startswith(("Added:", "Updated:")):
                    err.print(f"sync-projects: {line}: {reply}")
                    failed = True
    except (MCPError, MCPToolError) as exc:
        err.print(f"sync-projects: server unreachable: {exc}")
        raise typer.Exit(1)
    raise typer.Exit(1 if failed else 0)


@app.command()
def status():
    """Report connectivity, spool depth, and watermark summary."""
    console.print(f"Spool depth: {spool.depth()}")
    watermarks = state.all_watermarks()
    console.print(f"Tracked sessions: {len(watermarks)}")
    try:
        cfg = load_config(timeout=8.0)
    except ConfigError as exc:
        console.print(
            f"Config: [red]not configured[/red] — {exc.args[0].splitlines()[0]}"
        )
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

"""Offline tests for the openbrain thin client (issue #012).

Covers config resolution, transcript delta + watermark, spool fail-soft ordering,
the MCP SSE parser, and the packaging import guard (client pulls no server-only
modules). Nothing here touches the network or a database.
"""

import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from typer.testing import CliRunner

from openbrain.client import cli as cli_mod
from openbrain.client import config as config_mod
from openbrain.client import mcp as mcp_mod
from openbrain.client import spool as spool_mod
from openbrain.client import state as state_mod
from openbrain.client import transcript as transcript_mod


class _FakeClient:
    """Stand-in MCP client: records tool calls and returns scripted replies."""

    def __init__(self, replies):
        self._replies = replies
        self.calls = []
        self.timeouts = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        reply = self._replies.get(name, "")
        # A scripted reply may be an exception to simulate that one tool call
        # failing while the rest of the sequence succeeds.
        if isinstance(reply, Exception):
            raise reply
        return reply

    def set_timeout(self, seconds):
        self.timeouts.append(seconds)


def _fake_time(values):
    """A stand-in `time` module whose `monotonic` walks a scripted sequence.

    Returns each value in turn and then repeats the last one, so a test only has
    to script the readings it cares about.
    """
    seq = list(values)

    def monotonic():
        return seq.pop(0) if len(seq) > 1 else seq[0]

    return SimpleNamespace(monotonic=monotonic)


def _line(type_, role, content):
    return json.dumps({"type": type_, "message": {"role": role, "content": content}})


def _codex_msg(role, text, block_type="input_text"):
    """One Codex `response_item` message line, matching the real schema."""
    return json.dumps(
        {
            "timestamp": "2026-06-22T17:23:41.041Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": role,
                "content": [{"type": block_type, "text": text}],
            },
        }
    )


class TranscriptDeltaTests(unittest.TestCase):
    def _write(self, tmp, lines):
        p = Path(tmp) / "t.jsonl"
        p.write_text("\n".join(lines) + "\n")
        return p

    def test_extracts_user_and_assistant_text_only(self):
        with TemporaryDirectory() as tmp:
            lines = [
                json.dumps({"type": "mode"}),
                _line("user", "user", "please fix the bug"),
                _line(
                    "assistant",
                    "assistant",
                    [
                        {"type": "thinking", "thinking": "hmm"},
                        {"type": "tool_use", "name": "Edit"},
                        {"type": "text", "text": "Fixed the off-by-one."},
                    ],
                ),
                _line("user", "user", [{"type": "tool_result", "content": "noise"}]),
            ]
            p = self._write(tmp, lines)
            delta = transcript_mod.read_delta(p, 0)
            self.assertIn("please fix the bug", delta.text)
            self.assertIn("Fixed the off-by-one.", delta.text)
            self.assertNotIn("hmm", delta.text)
            self.assertNotIn("noise", delta.text)
            self.assertEqual(delta.total_lines, 4)

    def test_watermark_skips_already_seen_lines(self):
        with TemporaryDirectory() as tmp:
            lines = [
                _line("user", "user", "first"),
                _line("user", "user", "second"),
            ]
            p = self._write(tmp, lines)
            delta = transcript_mod.read_delta(p, 1)
            self.assertNotIn("first", delta.text)
            self.assertIn("second", delta.text)

    def test_no_new_lines_yields_empty_delta(self):
        with TemporaryDirectory() as tmp:
            p = self._write(tmp, [_line("user", "user", "hi")])
            delta = transcript_mod.read_delta(p, 1)
            self.assertEqual(delta.text, "")
            self.assertEqual(delta.total_lines, 1)


class CodexTranscriptTests(unittest.TestCase):
    """Codex JSONL has its own schema: `response_item` -> `payload` message with
    `input_text`/`output_text` blocks. Only user/assistant survive; `developer`
    boilerplate (injected AGENTS.md every turn) is dropped."""

    def _write(self, tmp, lines):
        p = Path(tmp) / "rollout-2026-06-22T17-23-40-abc.jsonl"
        p.write_text("\n".join(lines) + "\n")
        return p

    def test_extracts_user_and_assistant_excludes_developer(self):
        with TemporaryDirectory() as tmp:
            lines = [
                json.dumps({"type": "session_meta", "payload": {"id": "x"}}),
                _codex_msg("developer", "<permissions instructions> ... boilerplate"),
                _codex_msg("user", "please fix the parser"),
                _codex_msg("assistant", "Fixed the Codex branch.", "output_text"),
                json.dumps({"type": "turn_context", "payload": {}}),
            ]
            p = self._write(tmp, lines)
            delta = transcript_mod.read_delta(p, 0)
            self.assertEqual(
                delta.text,
                "user: please fix the parser\n\nassistant: Fixed the Codex branch.",
            )
            self.assertNotIn("boilerplate", delta.text)
            self.assertNotIn("permissions", delta.text)
            self.assertEqual(delta.total_lines, 5)

    def test_unrecognized_line_is_fail_soft(self):
        with TemporaryDirectory() as tmp:
            lines = [
                "not json at all",
                json.dumps({"type": "response_item", "payload": {"type": "reasoning"}}),
                _codex_msg("user", "still captured"),
            ]
            p = self._write(tmp, lines)
            delta = transcript_mod.read_delta(p, 0)
            self.assertEqual(delta.text, "user: still captured")


class StateTests(unittest.TestCase):
    def test_watermark_roundtrip(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            self.assertEqual(state_mod.get_watermark("s1", path), 0)
            state_mod.set_watermark("s1", 42, path)
            self.assertEqual(state_mod.get_watermark("s1", path), 42)
            state_mod.set_watermark("s2", 7, path)
            self.assertEqual(state_mod.get_watermark("s1", path), 42)
            self.assertEqual(len(state_mod.all_watermarks(path)), 2)


class SpoolTests(unittest.TestCase):
    def test_enqueue_and_fifo_order(self):
        with TemporaryDirectory() as tmp:
            d = Path(tmp)
            spool_mod.enqueue("capture_thought", {"content": "a"}, "t1", spool_dir=d)
            spool_mod.enqueue("capture_thought", {"content": "b"}, "t2", spool_dir=d)
            self.assertEqual(spool_mod.depth(d), 2)
            entries = [spool_mod.load(p) for p in spool_mod.pending(d)]
            self.assertEqual([e["arguments"]["content"] for e in entries], ["a", "b"])

    def test_cross_process_order_is_by_time_not_pid(self):
        # An older capture from a *higher*-pid process must sort before a newer
        # capture from a *lower*-pid process. Pid-prefixed names got this wrong;
        # a time-based prefix fixes it.
        with TemporaryDirectory() as tmp:
            d = Path(tmp)
            # Enqueue order: high pid at t=100 (older), then low pid at t=200
            # (newer). Constant patches — os.getpid is also called internally by
            # tempfile, so an iterator would exhaust.
            with (
                patch.object(spool_mod.os, "getpid", lambda: 9999999),
                patch.object(spool_mod.time, "time_ns", lambda: 100),
            ):
                spool_mod.enqueue(
                    "capture_thought", {"content": "older"}, "t1", spool_dir=d
                )
            with (
                patch.object(spool_mod.os, "getpid", lambda: 1000000),
                patch.object(spool_mod.time, "time_ns", lambda: 200),
            ):
                spool_mod.enqueue(
                    "capture_thought", {"content": "newer"}, "t2", spool_dir=d
                )
            entries = [spool_mod.load(p) for p in spool_mod.pending(d)]
            self.assertEqual(
                [e["arguments"]["content"] for e in entries], ["older", "newer"]
            )

    def test_remove(self):
        with TemporaryDirectory() as tmp:
            d = Path(tmp)
            spool_mod.enqueue("capture_thought", {"content": "a"}, "t1", spool_dir=d)
            spool_mod.remove(spool_mod.pending(d)[0])
            self.assertEqual(spool_mod.depth(d), 0)

    def test_enqueue_is_atomic_no_temp_leftovers(self):
        with TemporaryDirectory() as tmp:
            d = Path(tmp)
            path = spool_mod.enqueue(
                "capture_thought", {"content": "a"}, "t1", spool_dir=d
            )
            # Exactly one file exists — the final entry, no stray temp leftovers —
            # and it is always complete, parseable JSON.
            self.assertEqual(list(d.iterdir()), [path])
            self.assertEqual(spool_mod.load(path)["arguments"]["content"], "a")


class FlushSpoolTests(unittest.TestCase):
    class _FakeClient:
        def __init__(self):
            self.calls = []

        def call_tool(self, tool, arguments):
            self.calls.append((tool, arguments))
            return {"ok": True}

    def test_flush_tolerates_truncated_spool_file(self):
        from openbrain.client import cli as cli_mod

        with TemporaryDirectory() as tmp:
            d = Path(tmp)
            spool_mod.enqueue("capture_thought", {"content": "a"}, "t1", spool_dir=d)
            spool_mod.enqueue("capture_thought", {"content": "b"}, "t2", spool_dir=d)
            # Simulate a crash mid-write: a partial JSON that sorts FIRST, so an
            # unguarded flush would abort before reaching the valid entries.
            (d / "0000000-0000.json").write_text('{"tool": "capture_thought", "argum')
            client = self._FakeClient()
            with (
                patch.object(spool_mod, "pending", lambda *a: sorted(d.glob("*.json"))),
                patch.object(
                    spool_mod, "depth", lambda *a: len(list(d.glob("*.json")))
                ),
            ):
                flushed, remaining = cli_mod._flush_spool(client)
            # Both valid entries flushed despite the malformed file up front.
            self.assertEqual(flushed, 2)
            self.assertEqual([c[1]["content"] for c in client.calls], ["a", "b"])
            # The malformed file is left on disk, never silently discarded.
            self.assertEqual(remaining, 1)
            self.assertTrue((d / "0000000-0000.json").exists())


class CaptureTranscriptCommandTests(unittest.TestCase):
    def test_quiet_missing_context_emits_nothing(self):
        runner = CliRunner()

        with patch.dict("os.environ", {}, clear=True):
            result = runner.invoke(
                cli_mod.app,
                [
                    "capture-transcript",
                    "--trigger",
                    "stop",
                    "--source",
                    "codex",
                    "--quiet",
                ],
            )

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def test_codex_thread_env_finds_nested_transcript(self):
        runner = CliRunner()
        client = _FakeClient({"capture_transcript": "stored"})
        thread_id = "019f3870-8424-7113-96b5-dc368a258efd"

        with TemporaryDirectory() as tmp:
            codex_root = Path(tmp) / ".codex" / "sessions"
            nested = codex_root / "2026" / "07" / "06"
            nested.mkdir(parents=True)
            tpath = nested / f"rollout-2026-07-06T11-18-45-{thread_id}.jsonl"
            tpath.write_text(_codex_msg("user", "capture this codex turn") + "\n")

            with (
                patch.dict("os.environ", {"CODEX_THREAD_ID": thread_id}, clear=True),
                patch.object(cli_mod, "CODEX_SESSIONS", codex_root),
                patch.object(cli_mod, "load_config", return_value=object()),
                patch.object(cli_mod, "MCPClient", return_value=client),
                patch.object(cli_mod.state, "get_watermark", return_value=0),
                patch.object(cli_mod.state, "set_watermark"),
            ):
                result = runner.invoke(
                    cli_mod.app,
                    [
                        "capture-transcript",
                        "--trigger",
                        "stop",
                        "--source",
                        "codex",
                        "--quiet",
                    ],
                )

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")
        self.assertEqual(len(client.calls), 1)
        tool, args = client.calls[0]
        self.assertEqual(tool, "capture_transcript")
        self.assertEqual(args["session_id"], thread_id)
        self.assertEqual(args["source"], "codex")
        self.assertIn("capture this codex turn", args["content"])

    def test_defer_queues_without_opening_mcp_client(self):
        runner = CliRunner()
        queued = []
        watermarks = []

        with TemporaryDirectory() as tmp:
            tpath = Path(tmp) / "rollout-x.jsonl"
            tpath.write_text(_codex_msg("user", "capture later") + "\n")

            def enqueue(tool, arguments, queued_at):
                queued.append((tool, arguments, queued_at))
                return Path(tmp) / "queued.json"

            with (
                patch.object(
                    cli_mod, "MCPClient", side_effect=AssertionError("no MCP")
                ),
                patch.object(cli_mod.spool, "enqueue", side_effect=enqueue),
                patch.object(cli_mod.state, "get_watermark", return_value=0),
                patch.object(
                    cli_mod.state,
                    "set_watermark",
                    side_effect=lambda session, lines: watermarks.append(
                        (session, lines)
                    ),
                ),
            ):
                result = runner.invoke(
                    cli_mod.app,
                    [
                        "capture-transcript",
                        str(tpath),
                        "--session",
                        "session-1",
                        "--trigger",
                        "stop",
                        "--source",
                        "codex",
                        "--quiet",
                        "--defer",
                    ],
                )

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")
        self.assertEqual(len(queued), 1)
        tool, args, _ = queued[0]
        self.assertEqual(tool, "capture_transcript")
        self.assertEqual(args["session_id"], "session-1")
        self.assertEqual(args["source"], "codex")
        self.assertIn("capture later", args["content"])
        self.assertEqual(watermarks, [("session-1", 1)])

    def test_quiet_server_and_spool_failure_still_emits_nothing(self):
        runner = CliRunner()

        with TemporaryDirectory() as tmp:
            tpath = Path(tmp) / "rollout-x.jsonl"
            tpath.write_text(_codex_msg("user", "capture despite outage") + "\n")
            with (
                patch.object(
                    cli_mod,
                    "load_config",
                    side_effect=config_mod.ConfigError("missing"),
                ),
                patch.object(
                    cli_mod.spool, "enqueue", side_effect=OSError("read-only")
                ),
                patch.object(
                    cli_mod.state, "set_watermark", side_effect=OSError("read-only")
                ),
            ):
                result = runner.invoke(
                    cli_mod.app,
                    [
                        "capture-transcript",
                        str(tpath),
                        "--session",
                        "session-1",
                        "--trigger",
                        "stop",
                        "--source",
                        "codex",
                        "--quiet",
                    ],
                )

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")


class ConfigTests(unittest.TestCase):
    def test_env_wins(self):
        env = {"OPENBRAIN_MCP_URL": "https://x/mcp", "OPENBRAIN_MCP_KEY": "k"}
        with patch.dict("os.environ", env, clear=False):
            with patch.object(config_mod, "_load_file", return_value={}):
                cfg = config_mod.load_config()
        self.assertEqual(cfg.url, "https://x/mcp")
        self.assertEqual(cfg.key, "k")
        self.assertTrue(cfg.verify_tls)

    def test_missing_raises_with_guidance(self):
        with patch.dict("os.environ", {}, clear=True):
            with patch.object(config_mod, "_load_file", return_value={}):
                with self.assertRaises(config_mod.ConfigError) as ctx:
                    config_mod.load_config()
        self.assertIn("not configured", str(ctx.exception))

    def test_insecure_from_env(self):
        env = {
            "OPENBRAIN_MCP_URL": "https://x/mcp",
            "OPENBRAIN_MCP_KEY": "k",
            "OPENBRAIN_MCP_INSECURE": "1",
        }
        with patch.dict("os.environ", env, clear=False):
            with patch.object(config_mod, "_load_file", return_value={}):
                cfg = config_mod.load_config()
        self.assertFalse(cfg.verify_tls)


class MCPParseTests(unittest.TestCase):
    def test_parse_sse_message(self):
        body = 'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n\n'
        msg = mcp_mod._parse_sse(body)
        self.assertEqual(msg["result"], {"ok": True})

    def test_text_from_content_flattens_blocks(self):
        content = [
            {"type": "text", "text": "hello"},
            {"type": "other"},
            {"type": "text", "text": "world"},
        ]
        self.assertEqual(mcp_mod._text_from_content(content), "hello\nworld")


class MCPTimeoutTests(unittest.TestCase):
    """A bare float timeout in httpx applies to every phase of every request, so
    an unreachable endpoint spends the whole budget waiting to connect. Connect
    gets its own short allowance instead."""

    def _client(self, timeout):
        cfg = config_mod.ClientConfig(url="https://x/mcp", key="k", timeout=timeout)
        return mcp_mod.MCPClient(cfg)

    def test_connect_timeout_is_distinct_from_read(self):
        client = self._client(6.0)
        try:
            timeout = client._client.timeout
        finally:
            client.close()
        self.assertEqual(timeout.connect, mcp_mod.CONNECT_TIMEOUT)
        self.assertEqual(timeout.read, 6.0)
        self.assertLess(timeout.connect, timeout.read)

    def test_set_timeout_rebudgets_the_live_client(self):
        client = self._client(6.0)
        try:
            client.set_timeout(3.0)
            rebudgeted = client._client.timeout
            # A remaining budget under the connect allowance caps connect too:
            # never wait longer than the caller actually has left.
            client.set_timeout(0.5)
            squeezed = client._client.timeout
        finally:
            client.close()
        self.assertEqual(rebudgeted.read, 3.0)
        self.assertEqual(rebudgeted.connect, mcp_mod.CONNECT_TIMEOUT)
        self.assertEqual(squeezed.read, 0.5)
        self.assertEqual(squeezed.connect, 0.5)


class PackagingGuardTests(unittest.TestCase):
    def test_client_imports_no_server_modules(self):
        # Fresh interpreter: importing the client must not drag in server deps.
        code = (
            "import sys, openbrain.client.cli;"
            "banned=[m for m in ('openbrain.brain','fastmcp','asyncpg','pgvector',"
            "'openai','voyageai','pdfplumber','bibtexparser') if m in sys.modules];"
            "assert not banned, banned;"
            "print('ok')"
        )
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("ok", out.stdout)


class BriefCommandTests(unittest.TestCase):
    """`openbrain brief` powers the SessionStart hook: it must emit the documented
    additionalContext envelope on success and a systemMessage warning (exit 0)
    on any failure."""

    def setUp(self):
        # The real spool lives in the user's home; never let it leak into tests.
        patcher = patch.object(cli_mod, "_spool_warning", return_value="")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, replies=None, raises=None, args=("--cwd", "/tmp/myproj")):
        cfg = config_mod.ClientConfig(url="https://x/mcp", key="k")
        runner = CliRunner()
        with patch.object(cli_mod, "load_config", return_value=cfg):
            if raises is not None:
                with patch.object(cli_mod, "MCPClient", side_effect=raises):
                    return runner.invoke(cli_mod.app, ["brief", *args])
            client = _FakeClient(replies or {})
            with patch.object(cli_mod, "MCPClient", return_value=client):
                result = runner.invoke(cli_mod.app, ["brief", *args])
            self._last_client = client
            return result

    def test_emits_additional_context_envelope(self):
        result = self._run(
            replies={
                "get_briefing": "BRIEFING — today",
                "search_brain": "THOUGHTS: ...",
            }
        )
        self.assertEqual(result.exit_code, 0)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "SessionStart")
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        self.assertIn("BRIEFING — today", ctx)
        self.assertIn("THOUGHTS: ...", ctx)

    def test_recall_query_inferred_from_cwd(self):
        self._run(replies={"get_briefing": "b", "search_brain": "r"})
        search_calls = [
            a for name, a in self._last_client.calls if name == "search_brain"
        ]
        self.assertEqual(search_calls, [{"query": "myproj"}])

    def test_no_content_emits_nothing(self):
        result = self._run(replies={"get_briefing": "", "search_brain": ""})
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stdout.strip(), "")

    def test_server_failure_warns_the_user(self):
        result = self._run(raises=mcp_mod.MCPError("tunnel down"))
        self.assertEqual(result.exit_code, 0)
        payload = json.loads(result.stdout)
        self.assertEqual(
            payload["systemMessage"], "openbrain: briefing failed: tunnel down"
        )

    def test_stale_spool_warns_alongside_the_briefing(self):
        with patch.object(
            cli_mod, "_spool_warning", return_value="openbrain: 9 captures queued"
        ):
            result = self._run(
                replies={"get_briefing": "BRIEFING — today", "search_brain": ""}
            )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["systemMessage"], "openbrain: 9 captures queued")
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        self.assertIn("9 captures queued", ctx)
        self.assertIn("BRIEFING — today", ctx)

    def test_recall_gets_the_remaining_budget_not_a_fresh_one(self):
        # 1.5s elapsed by the time the briefing lands → recall may spend the
        # 4.5s that is left, not another full request timeout.
        result, client, load_config = self._run_budgeted(
            [0.0, 0.0, 1.5],
            replies={"get_briefing": "b", "search_brain": "r"},
        )
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(
            [name for name, _ in client.calls], ["get_briefing", "search_brain"]
        )
        self.assertEqual(client.timeouts, [4.5])
        # The first request is bounded by the same budget, not the old flat 8s.
        self.assertLessEqual(
            load_config.call_args.kwargs["timeout"], cli_mod.BRIEF_BUDGET_SECONDS
        )

    def test_exhausted_budget_skips_recall_and_still_emits_briefing(self):
        # Briefing lands with 0.5s left — under the recall floor. Starting a
        # search would only get cut off, and the briefing is the real payload.
        result, client, _ = self._run_budgeted(
            [0.0, 0.0, 5.5],
            replies={
                "get_briefing": "BRIEFING — today",
                "search_brain": "THOUGHTS: ...",
            },
        )
        self.assertEqual(result.exit_code, 0)
        self.assertEqual([name for name, _ in client.calls], ["get_briefing"])
        ctx = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(ctx, "BRIEFING — today")
        self.assertNotIn("THOUGHTS", ctx)

    def test_overrun_budget_is_fail_soft(self):
        # The budget is already blown before the first request: nothing may
        # raise, the exit code stays 0, and the timeout handed to httpx stays
        # positive (a non-positive value would abandon the call outright).
        result, client, load_config = self._run_budgeted(
            [0.0, 6.4, 9.9], replies={"get_briefing": "b", "search_brain": "r"}
        )
        self.assertEqual(result.exit_code, 0)
        self.assertIsNone(result.exception)
        self.assertEqual([name for name, _ in client.calls], ["get_briefing"])
        self.assertGreater(load_config.call_args.kwargs["timeout"], 0)

    def test_recall_failure_still_emits_the_briefing(self):
        # The briefing succeeded and is the payload the session needs; a failed
        # recall must degrade to briefing-only, not discard what we already have.
        result = self._run(
            replies={
                "get_briefing": "BRIEFING — today",
                "search_brain": mcp_mod.MCPError("tunnel down mid-call"),
            }
        )
        self.assertEqual(result.exit_code, 0)
        self.assertIsNone(result.exception)
        ctx = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(ctx, "BRIEFING — today")

    def test_recall_tool_error_still_emits_the_briefing(self):
        # Same contract for a tool-level error (isError: true), not just a
        # transport failure.
        result = self._run(
            replies={
                "get_briefing": "BRIEFING — today",
                "search_brain": mcp_mod.MCPToolError("search failed"),
            }
        )
        self.assertEqual(result.exit_code, 0)
        ctx = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(ctx, "BRIEFING — today")

    def test_briefing_failure_is_fatal_and_warns(self):
        # The inverse: recall is best-effort, the briefing is not. A failed
        # briefing gives the model only the warning, never a partial context.
        result = self._run(
            replies={
                "get_briefing": mcp_mod.MCPError("tunnel down"),
                "search_brain": "r",
            }
        )
        self.assertEqual(result.exit_code, 0)
        payload = json.loads(result.stdout)
        warning = "openbrain: briefing failed: tunnel down"
        self.assertEqual(payload["systemMessage"], warning)
        self.assertEqual(payload["hookSpecificOutput"]["additionalContext"], warning)

    def _run_budgeted(self, clock_readings, replies):
        """Invoke `brief` with a scripted clock. Returns (result, client, mock).

        `clock_readings` are consumed in order: deadline start, the reading that
        sizes the first request, then the one taken after the briefing returns.
        """
        cfg = config_mod.ClientConfig(url="https://x/mcp", key="k")
        client = _FakeClient(replies)
        runner = CliRunner()
        with (
            patch.object(cli_mod, "time", _fake_time(clock_readings)),
            patch.object(cli_mod, "load_config", return_value=cfg) as load_config,
            patch.object(cli_mod, "MCPClient", return_value=client),
        ):
            result = runner.invoke(cli_mod.app, ["brief", "--cwd", "/tmp/myproj"])
        return result, client, load_config


if __name__ == "__main__":
    unittest.main()

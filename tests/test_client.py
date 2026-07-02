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

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return self._replies.get(name, "")


def _line(type_, role, content):
    return json.dumps({"type": type_, "message": {"role": role, "content": content}})


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
            self.assertEqual(
                [e["arguments"]["content"] for e in entries], ["a", "b"]
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
            path = spool_mod.enqueue("capture_thought", {"content": "a"}, "t1", spool_dir=d)
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
            with patch.object(spool_mod, "pending", lambda *a: sorted(d.glob("*.json"))), \
                 patch.object(spool_mod, "depth", lambda *a: len(list(d.glob("*.json")))):
                flushed, remaining = cli_mod._flush_spool(client)
            # Both valid entries flushed despite the malformed file up front.
            self.assertEqual(flushed, 2)
            self.assertEqual([c[1]["content"] for c in client.calls], ["a", "b"])
            # The malformed file is left on disk, never silently discarded.
            self.assertEqual(remaining, 1)
            self.assertTrue((d / "0000000-0000.json").exists())


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
    additionalContext envelope on success and nothing (exit 0) on any failure."""

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
            replies={"get_briefing": "BRIEFING — today", "search_brain": "THOUGHTS: ..."}
        )
        self.assertEqual(result.exit_code, 0)
        payload = json.loads(result.stdout)
        self.assertEqual(
            payload["hookSpecificOutput"]["hookEventName"], "SessionStart"
        )
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        self.assertIn("BRIEFING — today", ctx)
        self.assertIn("THOUGHTS: ...", ctx)

    def test_recall_query_inferred_from_cwd(self):
        self._run(replies={"get_briefing": "b", "search_brain": "r"})
        search_calls = [a for name, a in self._last_client.calls if name == "search_brain"]
        self.assertEqual(search_calls, [{"query": "myproj"}])

    def test_no_content_emits_nothing(self):
        result = self._run(replies={"get_briefing": "", "search_brain": ""})
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stdout.strip(), "")

    def test_server_failure_is_silent(self):
        result = self._run(raises=mcp_mod.MCPError("tunnel down"))
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()

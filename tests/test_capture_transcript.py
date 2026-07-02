"""Offline tests for the server-side capture_transcript tool + summarizer (#012).

Drives the registered tool against a fake MCP with brain.summarize_transcript
and brain.store_thought monkeypatched, so no model or database is touched. Also
checks the trivial-delta contract of brain.summarize_transcript itself.
"""

import asyncio
import os
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

# Bootstrap DB/embedding stubs before importing brain-backed modules (mirrors
# tests/test_driver.py) so this runs under the plain unittest runner too.
_asyncpg = types.ModuleType("asyncpg")
_asyncpg.Pool = object
_asyncpg.create_pool = AsyncMock()
sys.modules.setdefault("asyncpg", _asyncpg)
_pgvector = types.ModuleType("pgvector")
_pgvector_asyncpg = types.ModuleType("pgvector.asyncpg")
_pgvector_asyncpg.register_vector = AsyncMock()
_pgvector.asyncpg = _pgvector_asyncpg
sys.modules.setdefault("pgvector", _pgvector)
sys.modules.setdefault("pgvector.asyncpg", _pgvector_asyncpg)
_bibtexparser = types.ModuleType("bibtexparser")
_bibtexparser.loads = lambda _: types.SimpleNamespace(entries=[])
sys.modules.setdefault("bibtexparser", _bibtexparser)
os.environ.setdefault("DATABASE_URL", "postgresql://test/openbrain")

from openbrain import brain  # noqa: E402
from openbrain.tools import capture  # noqa: E402


class _FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, fn):
        self.tools[fn.__name__] = fn
        return fn


def _register():
    mcp = _FakeMCP()
    capture.register(mcp)
    return mcp.tools


class CaptureTranscriptToolTests(unittest.TestCase):
    def test_stores_summary_via_write_path(self):
        tools = _register()
        with patch.object(
            brain, "summarize_transcript", AsyncMock(return_value="A durable summary.")
        ), patch.object(
            brain, "store_thought", AsyncMock(return_value="tid-1")
        ) as store:
            out = asyncio.run(
                tools["capture_transcript"](
                    content="user: hi\nassistant: solved it",
                    session_id="sess-1",
                    trigger="stop",
                )
            )
        self.assertIn("tid-1", out)
        # Stored as a bare conversation thought so the #010 write path enriches it.
        content_arg, meta_arg = store.call_args.args
        self.assertEqual(content_arg, "A durable summary.")
        self.assertEqual(meta_arg["type"], "conversation")
        self.assertEqual(meta_arg["session_id"], "sess-1")
        self.assertEqual(meta_arg["trigger"], "stop")
        self.assertNotIn("enriched", meta_arg)  # left for async enrichment

    def test_trivial_delta_stores_nothing(self):
        tools = _register()
        with patch.object(
            brain, "summarize_transcript", AsyncMock(return_value=None)
        ), patch.object(brain, "store_thought", AsyncMock()) as store:
            out = asyncio.run(
                tools["capture_transcript"](
                    content="user: ok", session_id="s", trigger="auto"
                )
            )
        self.assertIn("skipped", out.lower())
        store.assert_not_called()


class SummarizeTranscriptTests(unittest.TestCase):
    def test_empty_content_is_trivial(self):
        result = asyncio.run(brain.summarize_transcript("   \n  "))
        self.assertIsNone(result)

    def test_nothing_sentinel_is_trivial(self):
        with patch.object(
            brain, "complete_extraction_prompt", AsyncMock(return_value="NOTHING")
        ):
            result = asyncio.run(brain.summarize_transcript("user: hi"))
        self.assertIsNone(result)

    def test_summary_returned(self):
        with patch.object(
            brain,
            "complete_extraction_prompt",
            AsyncMock(return_value="  Decided X.  "),
        ):
            result = asyncio.run(brain.summarize_transcript("user: long content"))
        self.assertEqual(result, "Decided X.")


if __name__ == "__main__":
    unittest.main()

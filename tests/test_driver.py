"""Offline tests for the external-driver MCP tools (issue #007).

Each tool wraps a deterministic primitive; here we drive the registered tool
functions directly against a fake MCP and monkeypatched brain, so no
live network or database is touched. The result keys must match the
agentic-researcher Openbrain client contract exactly: get_citations ->
{citations}, score_relevance -> {scores}, store_papers -> {stored, deduped,
results}, find_papers -> {ids}.
"""

import asyncio
import json
import os
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

# Bootstrap DB/embedding stubs before importing brain-backed modules, so this
# module imports under both pytest (conftest also does it) and the project's
# unittest runner (which ignores conftest). Mirrors tests/test_acquire.py.
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

from openbrain.tools import driver, library  # noqa: E402


class _FakeMCP:
    """Captures @mcp.tool-decorated functions so tests can call them directly."""

    def __init__(self):
        self.tools = {}

    def tool(self, fn):
        self.tools[fn.__name__] = fn
        return fn


def _register(module):
    mcp = _FakeMCP()
    module.register(mcp)
    return mcp.tools


def _row(library_id, **over):
    base = {
        "library_id": library_id,
        "title": "A Paper",
        "authors": ["Ada L."],
        "year": 2021,
        "source": "openalex",
        "similarity": 0.88,
        "section": "abstract",
        "chunk_text": "water demand elasticity\nsecond line",
    }
    base.update(over)
    return base


class GetCitationsTests(unittest.TestCase):
    def test_wraps_neighbors_under_citations_key(self):
        neighbors = [{"title": "Ref A", "doi": "10.1/a"}, {"title": "Citing B"}]
        tools = _register(driver)
        with patch(
            "openbrain.brain.get_citations",
            new=AsyncMock(return_value=neighbors),
        ) as nb:
            out = asyncio.run(tools["get_citations"]("W123"))
        nb.assert_awaited_once_with("W123")
        self.assertEqual(json.loads(out), {"citations": neighbors})

    def test_empty_neighbors_serialize_to_empty_list(self):
        tools = _register(driver)
        with patch(
            "openbrain.brain.get_citations", new=AsyncMock(return_value=[])
        ):
            out = asyncio.run(tools["get_citations"]("W0"))
        self.assertEqual(json.loads(out), {"citations": []})


class ScoreRelevanceTests(unittest.TestCase):
    def test_wraps_scores_in_order(self):
        tools = _register(driver)
        with patch(
            "openbrain.brain.relevance",
            new=AsyncMock(return_value=[0.91, 0.12]),
        ) as rel:
            out = asyncio.run(tools["score_relevance"](["a1", "a2"], ["t1", "t2"]))
        rel.assert_awaited_once_with(["a1", "a2"], ["t1", "t2"])
        self.assertEqual(json.loads(out), {"scores": [0.91, 0.12]})

    def test_empty_or_no_embedding_surfaces_as_error(self):
        tools = _register(driver)
        with patch(
            "openbrain.brain.relevance",
            new=AsyncMock(side_effect=ValueError("no anchors")),
        ):
            with self.assertRaises(ValueError):
                asyncio.run(tools["score_relevance"]([], ["t1"]))


class StorePapersTests(unittest.TestCase):
    def test_returns_store_rollup(self):
        tools = _register(driver)
        ret = {
            "stored": 1,
            "deduped": 0,
            "results": [{"id": "lib-9", "title": "Stored", "deduped": False}],
        }
        with patch(
            "openbrain.brain.store_papers", new=AsyncMock(return_value=ret)
        ) as store:
            out = asyncio.run(tools["store_papers"]([{"title": "Stored"}]))
        store.assert_awaited_once_with([{"title": "Stored"}])
        self.assertEqual(json.loads(out), ret)


class SearchLibraryIdsTests(unittest.TestCase):
    def test_returns_distinct_ids_in_rank_order(self):
        rows = [_row("id1"), _row("id1"), _row("id2")]  # id1 duplicated
        tools = _register(library)
        with patch(
            "openbrain.brain.search_library_vec",
            new=AsyncMock(return_value=rows),
        ) as vec:
            out = asyncio.run(tools["find_papers"]("q", limit=10))
        vec.assert_awaited_once_with("q", 0.4, 10, None, None)
        data = json.loads(out)
        self.assertEqual(data["ids"], ["id1", "id2"])
        self.assertTrue(all("id" in r for r in data["results"]))

    def test_limit_caps_distinct_ids(self):
        rows = [_row("id1"), _row("id2"), _row("id3")]
        tools = _register(library)
        with patch(
            "openbrain.brain.search_library_vec",
            new=AsyncMock(return_value=rows),
        ):
            out = asyncio.run(tools["find_papers"]("q", limit=1))
        self.assertEqual(json.loads(out)["ids"], ["id1"])

    def test_empty_matches_give_empty_ids(self):
        tools = _register(library)
        with patch(
            "openbrain.brain.search_library_vec",
            new=AsyncMock(return_value=[]),
        ):
            out = asyncio.run(tools["find_papers"]("q"))
        self.assertEqual(json.loads(out), {"anchors": [], "ids": [], "results": []})


if __name__ == "__main__":
    unittest.main()

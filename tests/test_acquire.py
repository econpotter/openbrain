import os
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

# Bootstrap the DB/embedding stubs before importing brain-backed modules, so
# this module imports under both `pytest` (conftest also does it) and the
# project's `python -m unittest discover` runner (which ignores conftest).
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

from typer.testing import CliRunner  # noqa: E402

from openbrain.cli import app  # noqa: E402


class CliTests(unittest.TestCase):
    def test_cli_query_library_runs(self):
        async def fake(**k):
            return [{"title": "A", "year": 2021}]

        with patch("openbrain.brain.query_library_struct", fake):
            res = CliRunner().invoke(app, ["query-library", "--venue", "AJAE"])
        self.assertEqual(res.exit_code, 0)
        self.assertIn("A", res.stdout)

    def test_cli_seed_venues_runs(self):
        async def fake():
            return 4

        with patch("openbrain.brain.seed_venues", fake):
            res = CliRunner().invoke(app, ["seed-venues"])
        self.assertEqual(res.exit_code, 0)
        self.assertIn("Seeded 4 venue(s)", res.stdout)

    def test_cli_citations_emits_neighbors(self):
        neighbors = [{"title": "Neighbor", "doi": "10.1/n"}]
        with patch(
            "openbrain.sources.openalex_citation_neighbors", lambda _id: neighbors
        ):
            res = CliRunner().invoke(app, ["citations", "W1"])
        self.assertEqual(res.exit_code, 0)
        self.assertIn("neighbors", res.stdout)
        self.assertIn("Neighbor", res.stdout)

    def test_cli_relevance_emits_cosines_in_text_order(self):
        async def fake(anchors, texts):
            self.assertEqual(anchors, ["lib-1", "lib-2"])
            self.assertEqual(texts, ["one", "two"])
            return [0.9, 0.1]

        with patch("openbrain.brain.relevance", fake):
            res = CliRunner().invoke(
                app,
                [
                    "relevance",
                    "--anchor",
                    "lib-1",
                    "--anchor",
                    "lib-2",
                    "--text",
                    "one",
                    "--text",
                    "two",
                ],
            )
        self.assertEqual(res.exit_code, 0)
        self.assertIn("cosines", res.stdout)
        self.assertIn("0.9", res.stdout)

    def test_cli_store_papers_emits_rollup(self):
        async def fake(papers, **k):
            return {
                "stored": len(papers),
                "deduped": 0,
                "results": [
                    {"id": "lib-9", "title": papers[0]["title"], "deduped": False}
                ],
            }

        with patch("openbrain.brain.store_papers", fake):
            res = CliRunner().invoke(
                app, ["store-papers", "--json", '[{"title": "A Paper"}]']
            )
        self.assertEqual(res.exit_code, 0)
        self.assertIn("lib-9", res.stdout)
        self.assertIn("A Paper", res.stdout)

    def test_cli_search_library_emits_distinct_ids_top_k(self):
        async def fake(query, count=10):
            # Two chunks of the same paper collapse to one id; order preserved.
            return [
                {"library_id": "lib-1"},
                {"library_id": "lib-1"},
                {"library_id": "lib-2"},
            ]

        with patch("openbrain.brain.search_library_vec", fake):
            res = CliRunner().invoke(app, ["search-library", "water", "--limit", "5"])
        self.assertEqual(res.exit_code, 0)
        self.assertIn("ids", res.stdout)
        self.assertIn("lib-1", res.stdout)
        self.assertIn("lib-2", res.stdout)


if __name__ == "__main__":
    unittest.main()

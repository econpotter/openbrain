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

from openbrain import acquire  # noqa: E402
from openbrain.cli import app  # noqa: E402


class DedupTests(unittest.TestCase):
    def test_dedup_drops_doi_and_title_year_dups(self):
        recs = [
            {"doi": "10.1/a", "title": "X", "year": 2020},
            {"doi": "10.1/a", "title": "X dup", "year": 2020},  # same DOI
            {"doi": None, "title": "Y Study", "year": 2019},
            {"doi": None, "title": "y study", "year": 2019},  # same title+year
        ]
        out = acquire.dedup_records(recs)
        self.assertEqual(len(out), 2)


class PrefilterTests(unittest.TestCase):
    def setUp(self):
        self.base = {
            "abstract": "water demand elasticity",
            "year": 2021,
            "venue_abbrev": "AJAE",
        }

    def test_drops_when_no_abstract(self):
        self.assertEqual(
            acquire.prefilter({**self.base, "abstract": None}, "water", None, 2, None),
            "no_abstract",
        )

    def test_drops_old_papers(self):
        self.assertEqual(
            acquire.prefilter({**self.base, "year": 2000}, "water", 2015, 2, None),
            "old",
        )

    def test_keeps_when_overlap_meets_threshold(self):
        self.assertIsNone(acquire.prefilter(self.base, "water demand", None, 2, None))

    def test_drops_low_overlap(self):
        self.assertEqual(
            acquire.prefilter(
                self.base, "quantum chromodynamics physics", None, 2, None
            ),
            "low_overlap",
        )

    def test_drops_venue_not_in_filter(self):
        self.assertEqual(
            acquire.prefilter(self.base, "water demand", None, 2, {"WRR"}), "venue"
        )


class AcquirePipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_dry_run_stores_nothing(self):
        recs = [
            {
                "title": "Water demand",
                "abstract": "water demand elasticity",
                "year": 2021,
                "doi": "10.1/a",
                "issn_l": "0002-9092",
                "venue_source_id": "S123",
                "concepts": ["Water"],
                "authors": [],
                "source": "AJAE",
            }
        ]
        stored = []

        async def fake_store(meta, **k):
            stored.append(meta)
            return {"id": "x", "embedded": True, "deduped": False}

        with (
            patch.object(acquire.brain, "get_pool", AsyncMock(return_value=object())),
            patch.object(
                acquire.sources,
                "openalex_search_paged",
                lambda *a, **k: (recs, False),
            ),
            patch.object(acquire.sources, "crossref_lookup", lambda *a, **k: None),
            patch.object(acquire.venues_mod, "match", AsyncMock(return_value="AJAE")),
            patch.object(acquire.brain, "store_acquired_paper", fake_store),
        ):
            report = await acquire.acquire("water demand", dry_run=True)

        self.assertEqual(report["found"], 1)
        self.assertEqual(report["stored"], 0)
        self.assertEqual(stored, [])

    async def test_real_run_stores_survivors(self):
        recs = [
            {
                "title": "Water demand",
                "abstract": "water demand elasticity",
                "year": 2021,
                "doi": "10.1/a",
                "concepts": ["Water"],
                "authors": [],
                "source": "AJAE",
            },
            {
                "title": "Quantum chromodynamics",
                "abstract": "gluon field lattice",
                "year": 2021,
                "doi": "10.1/b",
                "concepts": [],
                "authors": [],
                "source": "PhysRev",
            },
        ]
        stored = []

        async def fake_store(meta, **k):
            stored.append(meta["title"])
            return {"id": "x", "embedded": True, "deduped": False}

        with (
            patch.object(acquire.brain, "get_pool", AsyncMock(return_value=object())),
            patch.object(
                acquire.sources,
                "openalex_search_paged",
                lambda *a, **k: (recs, False),
            ),
            patch.object(acquire.sources, "crossref_lookup", lambda *a, **k: None),
            patch.object(acquire.venues_mod, "match", AsyncMock(return_value=None)),
            patch.object(acquire.brain, "store_acquired_paper", fake_store),
        ):
            report = await acquire.acquire("water demand", min_overlap=2)

        # Only the water paper meets the overlap gate; the physics paper drops.
        self.assertEqual(report["stored"], 1)
        self.assertEqual(stored, ["Water demand"])
        self.assertEqual(report["prefiltered_out"]["low_overlap"], 1)


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

    def test_cli_store_paper_emits_id_title_deduped(self):
        async def fake(meta):
            return {"id": "lib-9", "title": meta["title"], "deduped": False}

        with patch("openbrain.brain.store_one", fake):
            res = CliRunner().invoke(
                app, ["store-paper", "--json", '{"title": "A Paper"}']
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

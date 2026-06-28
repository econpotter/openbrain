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


if __name__ == "__main__":
    unittest.main()

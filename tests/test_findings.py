import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

# Bootstrap the DB/embedding stubs before importing brain-backed modules, so this
# module imports under both pytest (conftest also does it) and the project's
# `python -m unittest discover` runner (which ignores conftest).
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

from openbrain import brain, findings  # noqa: E402
from openbrain.cli import app  # noqa: E402

CORE = Path(__file__).resolve().parents[1] / "schemas" / "core"


# ---------------------------------------------------------------------------
# Fake DB plumbing (mirrors tests/test_brain.py — no live Postgres in tests).
# ---------------------------------------------------------------------------


class _Tx:
    async def __aenter__(self):
        return None

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


class _FindingsConn:
    def __init__(
        self,
        *,
        paper=None,
        body=None,
        pending_count=0,
        pending_rows=None,
        search_rows=None,
    ):
        self.paper = paper
        self.body = body
        self.pending_count = pending_count
        self.pending_rows = pending_rows or []
        self.search_rows = search_rows or []
        self.executed = []

    async def fetchrow(self, query, *args):
        if "SELECT title, abstract FROM library" in query:
            return self.paper
        return None

    async def fetchval(self, query, *args):
        if "string_agg" in query:
            return self.body
        if "count(*) FROM library" in query:
            return self.pending_count
        return None

    async def fetch(self, query, *args):
        if "FROM findings f" in query:
            return self.search_rows
        if "SELECT id FROM library" in query:
            return self.pending_rows
        return []

    async def execute(self, query, *args):
        self.executed.append((query, args))

    def transaction(self):
        return _Tx()


def _executed(conn, needle):
    return [c for c in conn.executed if needle in c[0]]


# ---------------------------------------------------------------------------
# Schema (Tasks 0 + 1)
# ---------------------------------------------------------------------------


class SchemaTests(unittest.TestCase):
    def test_library_has_findings_status(self):
        sql = (CORE / "003_library.sql").read_text()
        self.assertIn("findings_status", sql)
        self.assertIn("'pending','done','error'", sql)

    def test_findings_baseline_files_present(self):
        self.assertTrue((CORE / "009_variables.sql").exists())
        self.assertTrue((CORE / "010_findings.sql").exists())
        findings_sql = (CORE / "010_findings.sql").read_text()
        self.assertIn("REFERENCES library(id)", findings_sql)
        self.assertIn("cause_var_id", findings_sql)


# ---------------------------------------------------------------------------
# Parse / prompt (Task 2, pure functions)
# ---------------------------------------------------------------------------


class ParseTests(unittest.TestCase):
    def test_parse_findings_validates_and_drops_bad(self):
        raw = """[
          {"cause_text":"snowpack","effect_text":"streamflow","magnitude_value":0.8,"magnitude_unit":"elasticity","magnitude_stat":"elasticity","raw_quote":"..."},
          {"cause_text":"","effect_text":"x"},
          {"effect_text":"missing cause"},
          {"cause_text":"price","effect_text":"demand","magnitude_value":"not a number"}
        ]"""
        out = findings.parse_findings(raw)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["cause_text"], "snowpack")
        self.assertEqual(out[0]["magnitude_value"], 0.8)
        self.assertIsNone(out[1]["magnitude_value"])

    def test_parse_findings_handles_prose_wrapper(self):
        raw = 'Here are the findings:\n[{"cause_text":"a","effect_text":"b"}]\nDone.'
        out = findings.parse_findings(raw)
        self.assertEqual(len(out), 1)

    def test_parse_findings_empty_on_garbage(self):
        self.assertEqual(findings.parse_findings("not json at all"), [])
        self.assertEqual(findings.parse_findings('{"not":"a list"}'), [])

    def test_build_prompt_includes_title_and_text(self):
        p = findings.build_prompt("My Title", "Body text here")
        self.assertIn("My Title", p)
        self.assertIn("Body text here", p)
        self.assertIn("JSON array", p)


# ---------------------------------------------------------------------------
# extract_findings (Task 2)
# ---------------------------------------------------------------------------


class ExtractTests(unittest.IsolatedAsyncioTestCase):
    async def test_inserts_rows_and_marks_done(self):
        conn = _FindingsConn(paper={"title": "T", "abstract": "snowpack raises streamflow"})
        raw = '[{"cause_text":"snowpack","effect_text":"streamflow","magnitude_value":0.8,"magnitude_stat":"elasticity","raw_quote":"q"}]'
        with (
            patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(conn))),
            patch.object(brain, "complete_extraction_prompt", AsyncMock(return_value=raw)),
        ):
            res = await findings.extract_findings("pid-1")
        self.assertEqual(res["extracted"], 1)
        self.assertEqual(res["status"], "done")
        self.assertEqual(len(_executed(conn, "INSERT INTO findings")), 1)
        done = _executed(conn, "findings_status = 'done'")
        self.assertEqual(len(done), 1)

    async def test_no_text_marks_done_without_calling_model(self):
        conn = _FindingsConn(paper={"title": "T", "abstract": None}, body=None)
        complete = AsyncMock()
        with (
            patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(conn))),
            patch.object(brain, "complete_extraction_prompt", complete),
        ):
            res = await findings.extract_findings("pid-2")
        self.assertEqual(res, {"paper_id": "pid-2", "extracted": 0, "status": "done"})
        complete.assert_not_awaited()
        self.assertEqual(len(_executed(conn, "INSERT INTO findings")), 0)

    async def test_completion_failure_marks_error(self):
        conn = _FindingsConn(paper={"title": "T", "abstract": "some text"})
        with (
            patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(conn))),
            patch.object(
                brain,
                "complete_extraction_prompt",
                AsyncMock(side_effect=RuntimeError("model down")),
            ),
        ):
            res = await findings.extract_findings("pid-3")
        self.assertEqual(res["status"], "error")
        self.assertEqual(len(_executed(conn, "findings_status = 'error'")), 1)
        self.assertEqual(len(_executed(conn, "INSERT INTO findings")), 0)


# ---------------------------------------------------------------------------
# backfill_findings (Task 3)
# ---------------------------------------------------------------------------


class BackfillTests(unittest.IsolatedAsyncioTestCase):
    async def test_processes_pending_and_tallies(self):
        conn = _FindingsConn(pending_count=2, pending_rows=[{"id": "a"}, {"id": "b"}])
        extract = AsyncMock(
            side_effect=[
                {"paper_id": "a", "extracted": 1, "status": "done"},
                {"paper_id": "b", "extracted": 0, "status": "error"},
            ]
        )
        with (
            patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(conn))),
            patch.object(findings, "extract_findings", extract),
        ):
            res = await findings.backfill_findings(limit=10)
        self.assertEqual(res["pending_before"], 2)
        self.assertEqual(res["extracted_papers"], 1)
        self.assertEqual(res["failed"], 1)
        self.assertEqual(res["pending_after"], 1)

    async def test_empty_when_nothing_pending(self):
        conn = _FindingsConn(pending_count=0, pending_rows=[])
        with patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(conn))):
            res = await findings.backfill_findings()
        self.assertEqual(res["pending_before"], 0)
        self.assertEqual(res["extracted_papers"], 0)


# ---------------------------------------------------------------------------
# search_findings (Task 4)
# ---------------------------------------------------------------------------


class SearchTests(unittest.IsolatedAsyncioTestCase):
    def _row(self):
        return {
            "finding_id": "f1",
            "cause_text": "snowpack",
            "effect_text": "streamflow",
            "magnitude_value": 0.8,
            "magnitude_unit": "elasticity",
            "magnitude_stat": "elasticity",
            "conditions": None,
            "raw_quote": "q",
            "confidence": None,
            "paper_id": "p1",
            "title": "Snow study",
            "authors": ["Jane Smith"],
            "year": 2021,
            "venue_abbrev": "WRR",
            "doi": "10.1/s",
        }

    async def test_returns_finding_with_source(self):
        conn = _FindingsConn(search_rows=[self._row()])
        with patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(conn))):
            out = await brain.search_findings(text="snowpack")
        self.assertTrue(out)
        self.assertEqual(out[0]["finding"]["cause_text"], "snowpack")
        self.assertEqual(out[0]["source"]["title"], "Snow study")
        self.assertEqual(out[0]["source"]["year"], 2021)
        # Never bare: the source block carries a citekey hard-linked to the paper.
        self.assertIn("Smith", out[0]["source"]["citekey"])

    async def test_group_by_paper_collapses(self):
        conn = _FindingsConn(search_rows=[self._row(), self._row()])
        with patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(conn))):
            out = await brain.search_findings(group_by="paper")
        self.assertEqual(len(out), 1)
        self.assertEqual(len(out[0]["findings"]), 2)
        self.assertEqual(out[0]["source"]["title"], "Snow study")


# ---------------------------------------------------------------------------
# CLI (Task 5)
# ---------------------------------------------------------------------------


class CliTests(unittest.TestCase):
    def test_search_findings_runs(self):
        async def fake(**k):
            return [{"finding": {"cause_text": "snowpack"}, "source": {"title": "A"}}]

        with patch("openbrain.brain.search_findings", fake):
            res = CliRunner().invoke(app, ["search-findings", "--text", "snowpack"])
        self.assertEqual(res.exit_code, 0)
        self.assertIn("snowpack", res.stdout)

    def test_backfill_findings_runs(self):
        async def fake(**k):
            return {
                "pending_before": 1,
                "extracted_papers": 1,
                "failed": 0,
                "pending_after": 0,
            }

        with patch("openbrain.findings.backfill_findings", fake):
            res = CliRunner().invoke(app, ["backfill-findings", "--limit", "5"])
        self.assertEqual(res.exit_code, 0)
        self.assertIn("extracted_papers", res.stdout)


if __name__ == "__main__":
    unittest.main()

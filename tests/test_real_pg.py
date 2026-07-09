"""Gated real-Postgres smoke tier.

Mocked asyncpg cannot see SQL-level bugs (an invalid ``to_tsquery`` string, a
status filter that never matches). These trial runs exercise the real query
paths against a live PostgreSQL + pgvector database.

They are OFF by default so the standard suite stays green with no database. The
operator opts in by pointing an env var at the ai-stack Postgres, e.g.::

    OPENBRAIN_TEST_DATABASE_URL=postgresql://openbrain:...@localhost/openbrain \
        uv run python -m pytest tests/test_real_pg.py

``tests/conftest.py`` sees that variable and loads the genuine drivers (instead
of the offline stubs) before ``openbrain.brain`` is imported. Only model calls
are stubbed; every SQL statement runs for real.
"""

import json
import os
import unittest
from unittest.mock import AsyncMock, patch

from openbrain import brain, findings

_REAL_PG_URL = os.environ.get("OPENBRAIN_TEST_DATABASE_URL")
_EMBED_DIM = 768


@unittest.skipUnless(
    _REAL_PG_URL,
    "Set OPENBRAIN_TEST_DATABASE_URL to run the real-Postgres smoke tier.",
)
class SearchBrainFtsTests(unittest.IsolatedAsyncioTestCase):
    """search_brain's FTS fallback must not hand an invalid tsquery to Postgres."""

    async def _search(self, query):
        # Zero vector stands in for the embedding model (unavailable in the trial
        # run); the point is to drive the real to_tsquery, not to rank by cosine.
        with (
            patch.object(brain, "embed", AsyncMock(return_value=[0.0] * _EMBED_DIM)),
            patch.object(brain, "RERANK_CONFIG", {}),
        ):
            return await brain.search_brain(query, threshold=0.99, count=3)

    async def test_punctuation_only_query_returns_empty_not_error(self):
        # "!!!" sanitizes to an empty tsquery — must match nothing, not raise a
        # Postgres syntax error the way the raw string would.
        result = await self._search("!!! ??? ...")
        self.assertEqual(result["thoughts"], [])
        self.assertEqual(result["library"], [])

    async def test_short_token_query_does_not_error(self):
        # All tokens <= 2 chars: the >2 filter empties, the fallback keeps the raw
        # word tokens ("ok | go") — still valid tsquery syntax, no crash.
        result = await self._search("ok go")
        self.assertIn("thoughts", result)
        self.assertIn("library", result)

    async def test_normal_query_runs(self):
        result = await self._search("snowpack streamflow elasticity")
        self.assertIn("thoughts", result)
        self.assertIn("library", result)


@unittest.skipUnless(
    _REAL_PG_URL,
    "Set OPENBRAIN_TEST_DATABASE_URL to run the real-Postgres smoke tier.",
)
class BackfillErrorRetryTests(unittest.IsolatedAsyncioTestCase):
    """error -> retried -> done, proven against the real status filter + UPDATE."""

    _MARKER = "openbrain-issue-018-real-pg-trial"

    async def asyncTearDown(self):
        pool = await brain.get_pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM library WHERE title = $1", self._MARKER)

    async def test_include_errors_retries_stranded_paper(self):
        pool = await brain.get_pool()
        async with pool.acquire() as conn:
            paper_id = await conn.fetchval(
                "INSERT INTO library (title, doc_type, abstract, findings_status) "
                "VALUES ($1, 'paper', 'snowpack raises streamflow', 'error') "
                "RETURNING id",
                self._MARKER,
            )

        raw = (
            '[{"cause_text":"snowpack","effect_text":"streamflow",'
            '"magnitude_value":0.8,"magnitude_stat":"elasticity","raw_quote":"q"}]'
        )
        with patch.object(
            brain, "complete_extraction_prompt", AsyncMock(return_value=raw)
        ):
            # Default (pending-only) leaves the error paper stranded...
            skipped = await findings.backfill_findings(include_errors=False)
            async with pool.acquire() as conn:
                status = await conn.fetchval(
                    "SELECT findings_status FROM library WHERE id = $1", paper_id
                )
            self.assertEqual(status, "error")

            # ...include_errors retries it and flips error -> done.
            res = await findings.backfill_findings(include_errors=True)

        self.assertGreaterEqual(res["extracted_papers"], 1)
        async with pool.acquire() as conn:
            status = await conn.fetchval(
                "SELECT findings_status FROM library WHERE id = $1", paper_id
            )
            count = await conn.fetchval(
                "SELECT count(*) FROM findings WHERE paper_id = $1", paper_id
            )
        self.assertEqual(status, "done")
        self.assertGreaterEqual(count, 1)
        # The pending-only pass never counted this error paper.
        self.assertIsInstance(skipped["extracted_papers"], int)


@unittest.skipUnless(
    _REAL_PG_URL,
    "Set OPENBRAIN_TEST_DATABASE_URL to run the real-Postgres smoke tier.",
)
class ArchiveThoughtsTests(unittest.IsolatedAsyncioTestCase):
    """archive_thoughts' extraction_run_id param sits inside jsonb_build_object
    with no other reference to pin its type — mocked asyncpg can't see Postgres
    reject that as an unresolvable parameter type ("could not determine data
    type of parameter $2"). Only a real connection exercises this."""

    _MARKER = "openbrain-issue-real-pg-archive-thoughts-trial"

    async def test_archive_thoughts_sets_metadata_against_real_postgres(self):
        thought_id = await brain.store_thought(
            self._MARKER, {"type": "note", "source": "test"}
        )

        await brain.archive_thoughts([thought_id], "run-real-pg-trial")

        pool = await brain.get_pool()
        async with pool.acquire() as conn:
            metadata = await conn.fetchval(
                "SELECT metadata FROM thoughts WHERE id = $1", thought_id
            )
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        self.assertTrue(metadata["archived"])
        self.assertEqual(metadata["extraction_run_id"], "run-real-pg-trial")


if __name__ == "__main__":
    unittest.main()

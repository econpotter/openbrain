import asyncio
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


# Keep these unit tests runnable before the optional local virtual environment
# has been installed. The tests replace database and embedding calls below.
asyncpg = types.ModuleType("asyncpg")
asyncpg.Pool = object
asyncpg.create_pool = AsyncMock()
sys.modules.setdefault("asyncpg", asyncpg)

pgvector = types.ModuleType("pgvector")
pgvector_asyncpg = types.ModuleType("pgvector.asyncpg")
pgvector_asyncpg.register_vector = AsyncMock()
pgvector.asyncpg = pgvector_asyncpg
sys.modules.setdefault("pgvector", pgvector)
sys.modules.setdefault("pgvector.asyncpg", pgvector_asyncpg)

bibtexparser = types.ModuleType("bibtexparser")
bibtexparser.loads = lambda _: types.SimpleNamespace(entries=[])
sys.modules.setdefault("bibtexparser", bibtexparser)

os.environ.setdefault("DATABASE_URL", "postgresql://test/openbrain")

from openbrain import brain
from openbrain.tools import search as search_tools


class _Acquire:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Pool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return _Acquire(self.connection)


class _ThoughtConnection:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def fetchval(self, query, *args):
        self.calls.append((query, args))
        return self.result


class _IngestConnection:
    def __init__(self):
        self.fetchrow_calls = []
        self.execute_calls = []
        self.library_records = {}

    async def fetchrow(self, query, *args):
        self.fetchrow_calls.append((query, args))
        if "SELECT id, filepath FROM library" in query:
            return self.library_records.get(args[0])
        if "INSERT INTO library" in query:
            row = {"id": "library-1", "title": args[0], "filepath": args[6]}
            self.library_records[args[7]] = row
            return row
        raise AssertionError(f"Unexpected query: {query}")

    async def execute(self, query, *args):
        self.execute_calls.append((query, args))


class _FetchConnection:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    async def fetch(self, query, *args):
        self.calls.append((query, args))
        return self.rows


class _BriefingConnection:
    async def fetch(self, query, *args):
        if "FROM projects" in query:
            return [{"name": "Policy Trial", "description": "Evaluate policy.", "status": "active"}]
        if "FROM thoughts" in query:
            return [{
                "id": "thought-1",
                "content": "Discuss estimation with Ada.",
                "metadata": {"people": ["Ada"], "projects": ["Policy Trial"]},
                "created_at": "recent",
            }]
        if "FROM library" in query:
            return [{
                "title": "A Trial Paper",
                "authors": ["Ada"],
                "year": 2026,
                "doc_type": "paper",
                "created_at": "recent",
            }]
        raise AssertionError(f"Unexpected query: {query}")


class _Mcp:
    def __init__(self):
        self.tools = {}

    def tool(self, function):
        self.tools[function.__name__] = function
        return function


class StoreThoughtTests(unittest.IsolatedAsyncioTestCase):
    async def _store(self, result, metadata):
        connection = _ThoughtConnection(result)
        refresh = AsyncMock()
        with (
            patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(connection))),
            patch.object(brain, "embed", AsyncMock(return_value=[0.1, 0.2])),
            patch.object(brain, "_refresh_mentioned_entities", refresh),
        ):
            thought_id = await brain.store_thought("A repeated thought", metadata)
            await asyncio.sleep(0)
        return thought_id, connection, refresh

    async def test_store_thought_uses_canonical_upsert(self):
        thought_id, connection, refresh = await self._store(
            {"id": "thought-1", "inserted": True, "metadata_changed": True},
            {"type": "note", "people": ["Ada"], "projects": ["Estimation"]},
        )

        self.assertEqual(thought_id, "thought-1")
        query, args = connection.calls[0]
        self.assertIn("SELECT upsert_thought", query)
        self.assertEqual(args[0], "A repeated thought")
        self.assertEqual(args[3], "note")
        refresh.assert_awaited_once_with(["Ada"], ["Estimation"])

    async def test_identical_duplicate_does_not_refresh_entities(self):
        _, connection, refresh = await self._store(
            {"id": "thought-1", "inserted": False, "metadata_changed": False},
            {"people": ["Ada"]},
        )

        self.assertIsNone(connection.calls[0][1][3])
        refresh.assert_not_awaited()

    async def test_meaningful_duplicate_merge_refreshes_entities(self):
        _, _, refresh = await self._store(
            {"id": "thought-1", "inserted": False, "metadata_changed": True},
            {"people": ["Ada", "Grace"]},
        )

        refresh.assert_awaited_once_with(["Ada", "Grace"], [])


class IngestPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_document_ingest_routes_event_through_store_thought(self):
        connection = _IngestConnection()
        store_thought = AsyncMock(return_value="thought-1")
        with tempfile.TemporaryDirectory() as tempdir:
            source = Path(tempdir) / "paper.txt"
            source.write_text("Short paper text.")
            library = Path(tempdir) / "library"
            with (
                patch.object(brain, "LIBRARY_PATH", library),
                patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(connection))),
                patch.object(brain, "embed", AsyncMock(return_value=[0.1, 0.2])),
                patch.object(brain, "store_thought", store_thought),
            ):
                result = await brain.ingest_document_pipeline(
                    str(source),
                    title="A Paper",
                )

        self.assertEqual(result["title"], "A Paper")
        store_thought.assert_awaited_once()
        content, metadata = store_thought.await_args.args
        self.assertIn("Ingested:", content)
        self.assertEqual(metadata["type"], "ingest_event")

    async def test_document_ingest_rejects_duplicate_file_hash(self):
        connection = _IngestConnection()
        fixture = Path(__file__).parent / "fixtures" / "trial-paper.txt"
        with tempfile.TemporaryDirectory() as tempdir:
            with (
                patch.object(brain, "LIBRARY_PATH", Path(tempdir) / "library"),
                patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(connection))),
                patch.object(brain, "embed", AsyncMock(return_value=[0.1, 0.2])),
                patch.object(brain, "store_thought", AsyncMock(return_value="thought-1")),
            ):
                result = await brain.ingest_document_pipeline(
                    str(fixture),
                    title="Synthetic Control Trial",
                )
                with self.assertRaisesRegex(ValueError, "Already ingested"):
                    await brain.ingest_document_pipeline(
                        str(fixture),
                        title="Synthetic Control Trial",
                    )

        self.assertEqual(result["chunks"], 1)
        chunk_inserts = [
            args for query, args in connection.execute_calls
            if "INSERT INTO chunks" in query
        ]
        self.assertEqual(len(chunk_inserts), 1)
        self.assertIn("Synthetic control methods", chunk_inserts[0][1])


class LibrarySearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_library_applies_year_and_document_type_filters(self):
        connection = _FetchConnection([{"title": "A Trial Paper"}])
        with (
            patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(connection))),
            patch.object(brain, "embed", AsyncMock(return_value=[0.1, 0.2])),
        ):
            rows = await brain.search_library_vec(
                "policy evaluation",
                threshold=0.6,
                count=4,
                year_min=2020,
                doc_type="paper",
            )

        self.assertEqual(rows, [{"title": "A Trial Paper"}])
        query, args = connection.calls[0]
        self.assertIn("l.year >= $3", query)
        self.assertIn("l.doc_type = $4", query)
        self.assertIn("LIMIT $5", query)
        self.assertEqual(args, ([0.1, 0.2], 0.6, 2020, "paper", 4))


class ThoughtConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_thought_connections_excludes_restricted_by_default(self):
        connection = _FetchConnection([{"id": "thought-2"}])
        with patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(connection))):
            rows = await brain.get_thought_connections("thought-1", limit=4)

        self.assertEqual(rows, [{"id": "thought-2"}])
        query, args = connection.calls[0]
        self.assertIn("SELECT * FROM get_thought_connections", query)
        self.assertEqual(args, ("thought-1", 4, True))

    async def test_mcp_tool_formats_shared_metadata(self):
        mcp = _Mcp()
        search_tools.register(mcp)
        rows = [{
            "overlap_count": 2,
            "preview": "Compare estimators.",
            "shared_topics": ["causal inference"],
            "shared_people": ["Ada"],
            "shared_tags": [],
            "shared_projects": [],
        }]
        with patch.object(brain, "get_thought_connections", AsyncMock(return_value=rows)):
            result = await mcp.tools["get_thought_connections"]("thought-1")

        self.assertIn("[2 overlap] Compare estimators.", result)
        self.assertIn("topics: causal inference", result)
        self.assertIn("people: Ada", result)


class TableStatsTests(unittest.IsolatedAsyncioTestCase):
    async def test_table_stats_uses_aggregate_function(self):
        stats = {
            "thoughts_total": 3,
            "thoughts_last_24h": 2,
            "library_total": 1,
            "types": {"note": 3},
            "tags": {"policy": 2},
            "topics": {},
            "sources": {"claude": 3},
            "sensitivity_tiers": {"standard": 3},
        }
        connection = _ThoughtConnection(stats)
        with patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(connection))):
            result = await brain.table_stats()

        self.assertEqual(result, stats)
        self.assertIn("SELECT brain_stats_aggregate()", connection.calls[0][0])

    async def test_get_stats_tool_formats_grouped_counts(self):
        mcp = _Mcp()
        search_tools.register(mcp)
        stats = {
            "thoughts_total": 3,
            "thoughts_last_24h": 2,
            "library_total": 1,
            "types": {"note": 3},
            "tags": {"policy": 2, "causal": 1},
            "topics": {},
            "sources": {"claude": 3},
            "sensitivity_tiers": {"standard": 3},
        }
        with patch.object(brain, "table_stats", AsyncMock(return_value=stats)):
            result = await mcp.tools["get_stats"]()

        self.assertIn("Library documents: 1", result)
        self.assertIn("Tags: policy: 2, causal: 1", result)
        self.assertIn("Sources: claude: 3", result)


class BriefingTests(unittest.IsolatedAsyncioTestCase):
    async def test_briefing_combines_projects_thoughts_library_and_follow_ups(self):
        follow_ups = [{"name": "Ada", "follow_up_date": "soon"}]
        people_with_follow_ups = AsyncMock(return_value=follow_ups)
        with (
            patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(_BriefingConnection()))),
            patch.object(brain, "people_with_follow_ups", people_with_follow_ups),
        ):
            result = await brain.get_briefing(days=7)

        self.assertEqual(result["projects"][0]["name"], "Policy Trial")
        self.assertEqual(result["thoughts"][0]["id"], "thought-1")
        self.assertEqual(result["library"][0]["title"], "A Trial Paper")
        self.assertEqual(result["people"], [("Ada", 1)])
        self.assertEqual(result["follow_ups"], follow_ups)
        people_with_follow_ups.assert_awaited_once_with(within_days=7)


if __name__ == "__main__":
    unittest.main()

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

from openbrain import brain  # noqa: E402
from openbrain.tools import capture as capture_tools  # noqa: E402
from openbrain.tools import library as library_tools  # noqa: E402
from openbrain.tools import maintenance as maintenance_tools  # noqa: E402
from openbrain.tools import search as search_tools  # noqa: E402


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


class _ExecuteConnection:
    def __init__(self):
        self.calls = []

    async def execute(self, query, *args):
        self.calls.append((query, args))


class _BriefingConnection:
    async def fetch(self, query, *args):
        if "FROM projects" in query:
            return [
                {
                    "name": "Policy Trial",
                    "description": "Evaluate policy.",
                    "status": "active",
                }
            ]
        if "FROM thoughts" in query:
            return [
                {
                    "id": "thought-1",
                    "content": "Discuss estimation with Ada.",
                    "metadata": {"people": ["Ada"], "projects": ["Policy Trial"]},
                    "created_at": "recent",
                }
            ]
        if "FROM library" in query:
            return [
                {
                    "title": "A Trial Paper",
                    "authors": ["Ada"],
                    "year": 2026,
                    "doc_type": "paper",
                    "created_at": "recent",
                }
            ]
        raise AssertionError(f"Unexpected query: {query}")


class _Mcp:
    def __init__(self):
        self.tools = {}
        self.resources = {}

    def tool(self, function):
        self.tools[function.__name__] = function
        return function

    def resource(self, uri, **kwargs):
        def _decorator(function):
            self.resources[uri] = {"function": function, "kwargs": kwargs}
            return function

        return _decorator


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

    async def test_capture_tool_exposes_structured_metadata(self):
        mcp = _Mcp()
        capture_tools.register(mcp)
        store_thought = AsyncMock(return_value="thought-1")
        with patch.object(brain, "store_thought", store_thought):
            result = await mcp.tools["capture_thought"](
                "Decision made.",
                importance=5,
                sensitivity_tier="personal",
                quality_score=90,
                action_items=["Follow up"],
            )

        self.assertEqual(result, "Stored thought thought-1")
        _, metadata = store_thought.await_args.args
        self.assertEqual(metadata["importance"], 5)
        self.assertEqual(metadata["sensitivity_tier"], "personal")
        self.assertEqual(metadata["quality_score"], 90)
        self.assertEqual(metadata["action_items"], ["Follow up"])

    async def test_task_shaped_capture_tools_set_metadata(self):
        mcp = _Mcp()
        capture_tools.register(mcp)
        store_thought = AsyncMock(return_value="thought-1")
        with patch.object(brain, "store_thought", store_thought):
            await mcp.tools["capture_decision"](
                "Use dry-run maintenance by default.",
                rationale="Avoid accidental archival.",
                projects=["openbrain"],
            )

        content, metadata = store_thought.await_args.args
        self.assertIn("Rationale:", content)
        self.assertIn("decision", metadata["tags"])
        self.assertEqual(metadata["projects"], ["openbrain"])

    async def test_project_update_includes_project_in_content_and_validates_status(
        self,
    ):
        mcp = _Mcp()
        capture_tools.register(mcp)
        store_thought = AsyncMock(return_value="thought-1")
        find_project = AsyncMock(return_value=[])
        with (
            patch.object(brain, "store_thought", store_thought),
            patch.object(brain, "find_project_by_name", find_project),
        ):
            result = await mcp.tools["capture_project_update"](
                "openbrain",
                "Ready for review.",
                status="active",
            )

        self.assertEqual(result, "Stored project update thought-1")
        content, metadata = store_thought.await_args.args
        self.assertEqual(
            content, "Project update for openbrain [active]: Ready for review."
        )
        self.assertEqual(metadata["projects"], ["openbrain"])

        store_thought.reset_mock()
        result = await mcp.tools["capture_project_update"](
            "openbrain",
            "Ready for review.",
            status="unknown",
        )
        self.assertIn("Invalid status", result)
        store_thought.assert_not_awaited()


class ModelFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_embed_uses_fallback_when_primary_fails(self):
        embed_config = {
            "provider": "voyage",
            "model": "primary",
            "fallback": {"provider": "litellm", "model": "fallback"},
        }
        calls = []

        async def _embed(_, config):
            calls.append(config["model"])
            if config["model"] == "primary":
                raise RuntimeError("primary unavailable")
            return [0.1, 0.2]

        with (
            patch.object(
                brain,
                "_cfg",
                {"embed": embed_config, "litellm_url": "http://litellm:4000"},
            ),
            patch.object(brain, "_embed_with_config", _embed),
        ):
            result = await brain.embed("test")

        self.assertEqual(result, [0.1, 0.2])
        self.assertEqual(calls, ["primary", "fallback"])

    async def test_embed_applies_task_prefixes(self):
        embed_config = {
            "provider": "litellm",
            "model": "nomic",
            "document_prefix": "search_document: ",
            "query_prefix": "search_query: ",
        }
        sent = []

        async def _embed(text, _config):
            sent.append(text)
            return [0.0]

        with (
            patch.object(brain, "_cfg", {"embed": embed_config}),
            patch.object(brain, "_embed_with_config", _embed),
        ):
            await brain.embed("hello", task="document")
            await brain.embed("hello", task="query")

        self.assertEqual(sent, ["search_document: hello", "search_query: hello"])

    async def test_embed_without_prefix_config_is_noop(self):
        embed_config = {"provider": "litellm", "model": "voyage-style"}
        sent = []

        async def _embed(text, _config):
            sent.append(text)
            return [0.0]

        with (
            patch.object(brain, "_cfg", {"embed": embed_config}),
            patch.object(brain, "_embed_with_config", _embed),
        ):
            await brain.embed("hello", task="query")

        self.assertEqual(sent, ["hello"])

    async def test_litellm_url_comes_from_top_level_config(self):
        config = {"provider": "litellm", "model": "model"}
        with patch.object(brain, "_cfg", {"litellm_url": "http://litellm:4000"}):
            self.assertEqual(
                brain._litellm_url(config),
                "http://litellm:4000",
            )

    async def test_extraction_uses_fallback_when_primary_fails(self):
        config = {
            "provider": "litellm",
            "model": "primary",
            "fallback": {"provider": "openrouter", "model": "fallback"},
        }
        calls = []

        async def _complete(_, provider_config):
            calls.append(provider_config["model"])
            if provider_config["model"] == "primary":
                raise RuntimeError("primary unavailable")
            return "fallback synthesis"

        with (
            patch.object(brain, "EXTRACTION_CONFIG", config),
            patch.object(brain, "_complete_extraction_with_config", _complete),
        ):
            result = await brain.complete_extraction_prompt("prompt")

        self.assertEqual(result, "fallback synthesis")
        self.assertEqual(calls, ["primary", "fallback"])


class IngestPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_prepare_ingest_staging_path_uses_configured_directory(self):
        with tempfile.TemporaryDirectory() as tempdir:
            with (
                patch.object(brain, "INGEST_STAGING_PATH", Path(tempdir)),
                patch.object(brain, "INGEST_CONFIG", {"staging_host": "rhyme"}),
            ):
                result = brain.prepare_ingest_staging_path("A Trial Paper.pdf")

            staged_path = Path(result["filepath"])
            self.assertEqual(staged_path.parent, Path(tempdir))
            self.assertTrue(staged_path.name.startswith("A-Trial-Paper-"))
            self.assertEqual(staged_path.suffix, ".pdf")
            self.assertEqual(result["host"], "rhyme")
            self.assertEqual(result["scp_destination"], f"rhyme:{staged_path}")

    async def test_is_ingest_staged_path_requires_staging_directory(self):
        with tempfile.TemporaryDirectory() as tempdir:
            staging = Path(tempdir) / "staging"
            outside = Path(tempdir) / "outside.pdf"
            inside = staging / "paper.pdf"
            with patch.object(brain, "INGEST_STAGING_PATH", staging):
                self.assertTrue(brain.is_ingest_staged_path(inside))
                self.assertFalse(brain.is_ingest_staged_path(outside))

    async def test_prepare_document_ingest_tool_formats_scp_instruction(self):
        mcp = _Mcp()
        library_tools.register(mcp)
        prepared = {
            "filepath": "/tmp/openbrain-ingest/paper-a1b2c3d4.pdf",
            "host": "rhyme",
            "scp_destination": "rhyme:/tmp/openbrain-ingest/paper-a1b2c3d4.pdf",
        }
        with patch.object(brain, "prepare_ingest_staging_path", return_value=prepared):
            result = await mcp.tools["prepare_document_ingest"]("paper.pdf")

        self.assertIn("scp /path/to/paper.pdf rhyme:/tmp/openbrain-ingest", result)
        self.assertIn("ingest_staged_document", result)

    async def test_ingest_staged_document_rejects_non_staged_path(self):
        mcp = _Mcp()
        library_tools.register(mcp)
        with patch.object(brain, "is_ingest_staged_path", return_value=False):
            result = await mcp.tools["ingest_staged_document"]("/tmp/paper.pdf")

        self.assertIn("Not an openbrain staged file", result)

    async def test_ingest_document_cleans_only_staged_files(self):
        mcp = _Mcp()
        library_tools.register(mcp)
        result = {"title": "Paper", "filepath": "/library/paper.pdf", "chunks": 1}
        with tempfile.TemporaryDirectory() as tempdir:
            staging = Path(tempdir) / "staging"
            staging.mkdir()
            staged_file = staging / "paper.pdf"
            staged_file.write_text("pdf")
            outside_file = Path(tempdir) / "outside.pdf"
            outside_file.write_text("pdf")

            with (
                patch.object(brain, "INGEST_STAGING_PATH", staging),
                patch.object(
                    brain, "ingest_document_pipeline", AsyncMock(return_value=result)
                ),
            ):
                await mcp.tools["ingest_document"](str(staged_file))
                await mcp.tools["ingest_document"](str(outside_file))

            self.assertFalse(staged_file.exists())
            self.assertTrue(outside_file.exists())

    async def test_document_ingest_routes_event_through_store_thought(self):
        connection = _IngestConnection()
        store_thought = AsyncMock(return_value="thought-1")
        with tempfile.TemporaryDirectory() as tempdir:
            source = Path(tempdir) / "paper.txt"
            source.write_text("Short paper text.")
            library = Path(tempdir) / "library"
            with (
                patch.object(brain, "LIBRARY_PATH", library),
                patch.object(
                    brain, "get_pool", AsyncMock(return_value=_Pool(connection))
                ),
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
                patch.object(
                    brain, "get_pool", AsyncMock(return_value=_Pool(connection))
                ),
                patch.object(brain, "embed", AsyncMock(return_value=[0.1, 0.2])),
                patch.object(
                    brain, "store_thought", AsyncMock(return_value="thought-1")
                ),
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
            args
            for query, args in connection.execute_calls
            if "INSERT INTO chunks" in query
        ]
        self.assertEqual(len(chunk_inserts), 1)
        self.assertIn("Synthetic control methods", chunk_inserts[0][1])


class _PaperConnection:
    def __init__(self, existing=None):
        self.existing = existing
        self.fetchrow_calls = []
        self.execute_calls = []

    async def fetchrow(self, query, *args):
        self.fetchrow_calls.append((query, args))
        if "SELECT id, title FROM library WHERE doi" in query:
            return self.existing
        if "INSERT INTO library" in query:
            return {"id": "library-9", "title": args[0]}
        raise AssertionError(f"Unexpected query: {query}")

    async def execute(self, query, *args):
        self.execute_calls.append((query, args))


class _BackfillConnection:
    def __init__(self, pending, rows):
        self.pending = pending
        self.rows = rows
        self.updates = []

    async def fetchval(self, query, *args):
        return self.pending

    async def fetch(self, query, *args):
        return self.rows

    async def execute(self, query, *args):
        self.updates.append(args)


class ChunkingTests(unittest.IsolatedAsyncioTestCase):
    def test_rebalance_splits_into_even_pieces_under_max(self):
        text = " ".join(f"w{i}" for i in range(900))
        chunks = brain._rebalance(text, max_words=300, overlap=50)
        # 900 / 300 -> 3 pieces, each <= 300 + overlap, never a thin tail
        self.assertEqual(len(chunks), 3)
        for c in chunks:
            self.assertLessEqual(len(c.split()), 350)

    def test_rebalance_short_text_is_single_chunk(self):
        chunks = brain._rebalance("only a few words", max_words=300, overlap=50)
        self.assertEqual(len(chunks), 1)

    def test_split_sections_detects_headings_and_labels_preamble(self):
        text = (
            "Some opening text before any heading.\n"
            "Introduction\n"
            "We motivate the work here.\n"
            "Methods\n"
            "We describe the approach.\n"
        )
        sections = brain._split_sections(text)
        names = [name for name, _ in sections]
        self.assertEqual(names[0], "body")
        self.assertIn("introduction", names)
        self.assertIn("methods", names)

    def test_split_sections_fewer_than_two_headings_falls_back(self):
        text = "Introduction\nJust one heading and a lot of prose follows here."
        sections = brain._split_sections(text)
        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0][0], "body")

    def test_chunk_document_skips_references_and_titles_sections(self):
        cfg = {
            "max_words": 300,
            "overlap": 50,
            "min_section_words": 5,
            "skip_sections": ["references"],
        }
        body = " ".join(["lorem"] * 60)
        refs = " ".join(["citation"] * 60)
        text = f"Introduction\n{body}\nResults\n{body}\nReferences\n{refs}\n"
        with patch.object(brain, "_cfg", {"chunk": cfg}):
            chunks = brain._chunk_document(text)
        sections = {section for section, _ in chunks}
        self.assertIn("Introduction", sections)
        self.assertIn("Results", sections)
        self.assertNotIn("References", sections)
        self.assertTrue(all("citation" not in c for _, c in chunks))

    def test_chunk_document_merges_stray_short_section(self):
        cfg = {
            "max_words": 300,
            "overlap": 50,
            "min_section_words": 80,
            "skip_sections": [],
        }
        long_body = " ".join(["lorem"] * 100)
        text = f"Introduction\n{long_body}\nMethods\nbrief.\n"
        with patch.object(brain, "_cfg", {"chunk": cfg}):
            chunks = brain._chunk_document(text)
        # "Methods" is too short -> merged into Introduction, never its own chunk
        self.assertNotIn("Methods", {section for section, _ in chunks})


class FetchPaperTests(unittest.IsolatedAsyncioTestCase):
    async def test_store_paper_inserts_record_abstract_chunk_and_event(self):
        connection = _PaperConnection(existing=None)
        store_thought = AsyncMock(return_value="thought-1")
        meta = {
            "title": "A Paper",
            "authors": ["Ada Lovelace"],
            "year": 2024,
            "source": "Journal of Trials",
            "abstract": "The abstract.",
            "doi": "10.1/x",
            "bibtex": "@article{...}",
        }
        with (
            patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(connection))),
            patch.object(brain, "embed", AsyncMock(return_value=[0.1, 0.2])),
            patch.object(brain, "store_thought", store_thought),
        ):
            result = await brain._store_paper(meta)

        self.assertEqual(result["title"], "A Paper")
        self.assertTrue(result["abstract_stored"])
        self.assertTrue(result["embedded"])
        self.assertFalse(result["deduped"])
        chunk_inserts = [
            (query, args)
            for query, args in connection.execute_calls
            if "INSERT INTO chunks" in query
        ]
        self.assertEqual(len(chunk_inserts), 1)
        self.assertEqual(chunk_inserts[0][1][1], "The abstract.")  # content arg
        self.assertIn("'abstract'", chunk_inserts[0][0])  # kind in SQL
        content, metadata = store_thought.await_args.args
        self.assertIn("Fetched:", content)
        self.assertEqual(metadata["type"], "ingest_event")

    async def test_store_paper_dedups_existing_doi_without_insert(self):
        connection = _PaperConnection(existing={"id": "library-1", "title": "A Paper"})
        with (
            patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(connection))),
            patch.object(brain, "embed", AsyncMock(return_value=[0.1, 0.2])),
            patch.object(brain, "store_thought", AsyncMock()),
        ):
            result = await brain._store_paper({"doi": "10.1/x", "title": "A Paper"})

        self.assertTrue(result["deduped"])
        self.assertFalse(result["abstract_stored"])
        self.assertFalse(
            any("INSERT INTO library" in q for q, _ in connection.fetchrow_calls)
        )

    async def test_fetch_paper_topic_returns_candidates_without_storing(self):
        candidates = [{"title": "A Paper", "doi": "10.1/x", "authors": ["Ada"]}]
        store_paper = AsyncMock()
        with (
            patch.object(brain.sources, "resolve", return_value=candidates),
            patch.object(brain, "_store_paper", store_paper),
        ):
            result = await brain.fetch_paper(topic="bandwidth selection")

        self.assertEqual(result["mode"], "topic")
        self.assertEqual(result["candidates"], candidates)
        store_paper.assert_not_awaited()

    async def test_fetch_paper_lookup_stores_resolved_paper(self):
        meta = {"title": "A Paper", "doi": "10.1/x"}
        store_paper = AsyncMock(return_value={"title": "A Paper", "deduped": False})
        with (
            patch.object(brain.sources, "resolve", return_value=meta),
            patch.object(brain, "_store_paper", store_paper),
        ):
            result = await brain.fetch_paper(identifier="10.1/x")

        self.assertTrue(result["found"])
        store_paper.assert_awaited_once()

    async def test_fetch_paper_tool_formats_lookup_result(self):
        mcp = _Mcp()
        library_tools.register(mcp)
        result = {
            "mode": "lookup",
            "found": True,
            "title": "A Paper",
            "authors": ["Ada Lovelace"],
            "year": 2024,
            "abstract_stored": True,
            "embedded": True,
            "deduped": False,
        }
        with patch.object(brain, "fetch_paper", AsyncMock(return_value=result)):
            out = await mcp.tools["fetch_paper"](identifier="10.1/x")

        self.assertIn("Fetched: A Paper", out)
        self.assertIn("Abstract: stored and embedded", out)

    async def test_store_paper_defers_embedding_when_model_unavailable(self):
        connection = _PaperConnection(existing=None)
        meta = {"title": "A Paper", "abstract": "The abstract.", "doi": "10.1/x"}
        with (
            patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(connection))),
            patch.object(brain, "embed", AsyncMock(side_effect=RuntimeError("down"))),
            patch.object(brain, "store_thought", AsyncMock(return_value="thought-1")),
        ):
            result = await brain._store_paper(meta)

        self.assertTrue(result["abstract_stored"])
        self.assertFalse(result["embedded"])
        chunk_inserts = [
            args
            for query, args in connection.execute_calls
            if "INSERT INTO chunks" in query
        ]
        self.assertEqual(len(chunk_inserts), 1)
        self.assertIsNone(chunk_inserts[0][2])  # embedding arg is NULL

    async def test_fetch_paper_tool_reports_deferred_embedding(self):
        mcp = _Mcp()
        library_tools.register(mcp)
        result = {
            "mode": "lookup",
            "found": True,
            "title": "A Paper",
            "authors": ["Ada Lovelace"],
            "year": 2024,
            "abstract_stored": True,
            "embedded": False,
            "deduped": False,
        }
        with patch.object(brain, "fetch_paper", AsyncMock(return_value=result)):
            out = await mcp.tools["fetch_paper"](identifier="10.1/x")

        self.assertIn("embedding deferred", out)

    async def test_fetch_papers_processes_each_identifier_independently(self):
        outcomes = {
            "10.1/a": {
                "mode": "lookup",
                "found": True,
                "title": "A",
                "abstract_stored": True,
                "embedded": True,
                "deduped": False,
            },
            "10.1/b": {
                "mode": "lookup",
                "found": True,
                "title": "B",
                "abstract_stored": True,
                "embedded": False,
                "deduped": False,
            },
            "10.1/c": {"mode": "lookup", "found": False},
        }
        fetch_one = AsyncMock(side_effect=lambda identifier, **kw: outcomes[identifier])
        with patch.object(brain, "fetch_paper", fetch_one):
            result = await brain.fetch_papers(
                identifiers=["10.1/a", "10.1/b", "10.1/c", "  "]
            )

        self.assertEqual(fetch_one.await_count, 3)  # blank skipped
        self.assertEqual(result["summary"]["fetched"], 2)
        self.assertEqual(result["summary"]["not_found"], 1)
        self.assertEqual(result["summary"]["abstracts_deferred"], 1)

    async def test_fetch_paper_tool_batch_formats_summary(self):
        mcp = _Mcp()
        library_tools.register(mcp)
        batch = {
            "mode": "batch",
            "results": [
                {
                    "identifier": "10.1/a",
                    "found": True,
                    "deduped": False,
                    "title": "A",
                    "abstract_stored": True,
                    "embedded": True,
                },
                {"identifier": "10.1/c", "found": False},
            ],
            "summary": {
                "requested": 2,
                "fetched": 1,
                "deduped": 0,
                "not_found": 1,
                "abstracts_deferred": 0,
            },
        }
        with patch.object(brain, "fetch_papers", AsyncMock(return_value=batch)):
            out = await mcp.tools["fetch_paper"](identifiers=["10.1/a", "10.1/c"])

        self.assertIn("1 fetched", out)
        self.assertIn("fetched: A", out)
        self.assertIn("not found: 10.1/c", out)

    async def test_backfill_embeds_null_chunks_and_counts(self):
        connection = _BackfillConnection(
            pending=2,
            rows=[
                {"id": "c1", "content": "alpha"},
                {"id": "c2", "content": "beta"},
            ],
        )
        with (
            patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(connection))),
            patch.object(brain, "embed", AsyncMock(return_value=[0.1, 0.2])),
        ):
            result = await brain.backfill_chunk_embeddings()

        self.assertEqual(result["embedded"], 2)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["pending_after"], 0)
        self.assertEqual(len(connection.updates), 2)

    async def test_backfill_skips_chunks_that_fail_to_embed(self):
        connection = _BackfillConnection(
            pending=2,
            rows=[
                {"id": "c1", "content": "alpha"},
                {"id": "c2", "content": "beta"},
            ],
        )
        embed = AsyncMock(side_effect=[[0.1, 0.2], RuntimeError("down")])
        with (
            patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(connection))),
            patch.object(brain, "embed", embed),
        ):
            result = await brain.backfill_chunk_embeddings()

        self.assertEqual(result["embedded"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["pending_after"], 1)
        self.assertEqual(len(connection.updates), 1)


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

    async def test_update_library_status_alias_uses_library_update(self):
        mcp = _Mcp()
        library_tools.register(mcp)
        matches = [{"id": "library-1", "title": "Paper"}]
        updated = {"title": "Paper", "status": "read"}
        with (
            patch.object(
                brain, "find_library_records", AsyncMock(return_value=matches)
            ),
            patch.object(
                brain, "update_library_record", AsyncMock(return_value=updated)
            ),
        ):
            result = await mcp.tools["update_library_status"]("Paper", "read")

        self.assertEqual(result, "Updated: Paper → read")

    async def test_get_library_record_formats_metadata(self):
        mcp = _Mcp()
        library_tools.register(mcp)
        record = {
            "title": "Paper",
            "authors": ["Ada"],
            "year": 2026,
            "source": "journal",
            "doi": "10.123/test",
            "doc_type": "paper",
            "status": "read",
            "filepath": "/tmp/paper.pdf",
            "abstract": "Abstract text.",
        }
        with patch.object(brain, "get_library_record", AsyncMock(return_value=record)):
            result = await mcp.tools["get_library_record"]("Paper")

        self.assertIn("Authors: Ada", result)
        self.assertIn("Path: /tmp/paper.pdf", result)


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
        rows = [
            {
                "overlap_count": 2,
                "preview": "Compare estimators.",
                "shared_topics": ["causal inference"],
                "shared_people": ["Ada"],
                "shared_tags": [],
                "shared_projects": [],
            }
        ]
        with patch.object(
            brain, "get_thought_connections", AsyncMock(return_value=rows)
        ):
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


class ExtractionTests(unittest.IsolatedAsyncioTestCase):
    async def test_extract_thoughts_excludes_synthesis_and_archived_metadata(self):
        connection = _FetchConnection([{"id": "thought-1"}])
        with patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(connection))):
            rows = await brain.extract_thoughts(since_hours=12, limit=7)

        self.assertEqual(rows, [{"id": "thought-1"}])
        query, args = connection.calls[0]
        self.assertIn("type NOT IN ('synthesis', 'archived')", query)
        self.assertIn("metadata->>'archived'", query)
        self.assertEqual(args, (12, 7))

    async def test_run_extraction_writes_synthesis_then_archives_inputs(self):
        thoughts = [
            {
                "id": "thought-1",
                "content": "Ada prefers the short estimator note.",
                "metadata": {"people": ["Ada"]},
                "created_at": "recent",
            }
        ]
        store_thought = AsyncMock(return_value="synthesis-1")
        archive_thoughts = AsyncMock()
        with (
            patch.object(brain, "extract_thoughts", AsyncMock(return_value=thoughts)),
            patch.object(
                brain,
                "complete_extraction_prompt",
                AsyncMock(return_value="Remember Ada's estimator preference."),
            ),
            patch.object(brain, "store_thought", store_thought),
            patch.object(brain, "archive_thoughts", archive_thoughts),
        ):
            result = await brain.run_extraction_once()

        self.assertEqual(result, {"processed": 1, "syntheses": 1})
        store_thought.assert_awaited_once()
        content, metadata = store_thought.await_args.args
        self.assertEqual(content, "Remember Ada's estimator preference.")
        self.assertEqual(metadata["type"], "synthesis")
        self.assertEqual(metadata["source_type"], "extraction")
        self.assertEqual(metadata["thought_ids"], ["thought-1"])
        archive_thoughts.assert_awaited_once_with(
            ["thought-1"], metadata["extraction_run_id"]
        )

    async def test_preview_extraction_returns_synthesis_without_archiving(self):
        thoughts = [{"id": "thought-1", "content": "A", "metadata": {}}]
        with (
            patch.object(brain, "extract_thoughts", AsyncMock(return_value=thoughts)),
            patch.object(
                brain,
                "complete_extraction_prompt",
                AsyncMock(return_value="Synthesis."),
            ),
        ):
            result = await brain.preview_extraction_once()

        self.assertEqual(result["processed"], 1)
        self.assertEqual(result["synthesis"], "Synthesis.")
        self.assertEqual(result["thought_ids"], ["thought-1"])

    async def test_apply_extraction_uses_approved_synthesis_and_ids(self):
        store_thought = AsyncMock(return_value="synthesis-1")
        archive_thoughts = AsyncMock()
        with (
            patch.object(brain, "store_thought", store_thought),
            patch.object(brain, "archive_thoughts", archive_thoughts),
        ):
            result = await brain.apply_extraction_synthesis(
                ["thought-1"],
                "Approved synthesis.",
            )

        self.assertEqual(result, {"processed": 1, "syntheses": 1})
        content, metadata = store_thought.await_args.args
        self.assertEqual(content, "Approved synthesis.")
        self.assertEqual(metadata["thought_ids"], ["thought-1"])
        archive_thoughts.assert_awaited_once_with(
            ["thought-1"], metadata["extraction_run_id"]
        )

    async def test_memory_extraction_tool_defaults_to_dry_run(self):
        mcp = _Mcp()
        maintenance_tools.register(mcp)
        preview = {
            "processed": 1,
            "synthesis": "Synthesis.",
            "thought_ids": ["thought-1"],
        }
        with patch.object(
            brain, "preview_extraction_once", AsyncMock(return_value=preview)
        ):
            result = await mcp.tools["run_memory_extraction"]()

        self.assertIn("DRY RUN", result)
        self.assertIn("thought-1", result)

    async def test_memory_extraction_apply_requires_approved_preview(self):
        mcp = _Mcp()
        maintenance_tools.register(mcp)
        apply_extraction = AsyncMock()
        with patch.object(brain, "apply_extraction_synthesis", apply_extraction):
            result = await mcp.tools["run_memory_extraction"](dry_run=False)

        self.assertIn("Run dry_run=True first", result)
        apply_extraction.assert_not_awaited()

    async def test_memory_extraction_apply_uses_approved_preview(self):
        mcp = _Mcp()
        maintenance_tools.register(mcp)
        apply_extraction = AsyncMock(return_value={"processed": 1, "syntheses": 1})
        with patch.object(brain, "apply_extraction_synthesis", apply_extraction):
            result = await mcp.tools["run_memory_extraction"](
                dry_run=False,
                approved_synthesis="Approved synthesis.",
                thought_ids=["thought-1"],
            )

        self.assertIn("Processed 1 thought", result)
        apply_extraction.assert_awaited_once_with(
            ["thought-1"],
            "Approved synthesis.",
        )

    async def test_archive_thoughts_sets_archived_metadata(self):
        connection = _ExecuteConnection()
        with patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(connection))):
            await brain.archive_thoughts(["thought-1"], "run-1")

        query, args = connection.calls[0]
        self.assertIn("'archived', true", query)
        self.assertIn("'extraction_run_id', $2", query)
        self.assertEqual(args, (["thought-1"], "run-1"))


class BriefingTests(unittest.IsolatedAsyncioTestCase):
    async def test_briefing_combines_projects_thoughts_library_and_follow_ups(self):
        follow_ups = [{"name": "Ada", "follow_up_date": "soon"}]
        people_with_follow_ups = AsyncMock(return_value=follow_ups)
        with (
            patch.object(
                brain, "get_pool", AsyncMock(return_value=_Pool(_BriefingConnection()))
            ),
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

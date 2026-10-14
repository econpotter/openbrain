import asyncio
import json
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
        # Invalidate-on-upgrade lookups: default to "no abstract-only match" so
        # ingest takes the INSERT path. Tests that exercise upgrade override this.
        if "SELECT id FROM library WHERE lower(doi)" in query:
            return getattr(self, "doi_upgrade", None)
        if "SELECT id FROM library WHERE lower(title)" in query:
            return getattr(self, "title_upgrade", None)
        if "UPDATE library SET" in query:
            return {
                "id": getattr(self, "upgrade_id", "library-up"),
                "title": args[0] if args else "T",
            }
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


class _EntityConnection:
    def __init__(self):
        self.fetchrow_calls = []
        self.execute_calls = []

    async def fetchrow(self, query, *args):
        self.fetchrow_calls.append((query, args))
        if "INSERT INTO people" in query:
            return {
                "id": "person-1",
                "name": args[0],
                "affiliation": args[1],
                "relationship": args[2],
                "notes": args[3],
            }
        if "INSERT INTO projects" in query:
            return {
                "id": "project-1",
                "name": args[0],
                "description": args[1],
                "status": args[2],
                "start_date": args[3],
            }
        raise AssertionError(f"Unexpected query: {query}")

    async def execute(self, query, *args):
        self.execute_calls.append((query, args))


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
            patch.object(brain, "_link_thought_entities", AsyncMock()),
            patch.object(brain, "extract_thought_metadata", AsyncMock()),
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

    async def test_store_thought_defers_embedding_when_model_unavailable(self):
        connection = _ThoughtConnection(
            {"id": "thought-1", "inserted": True, "metadata_changed": False}
        )
        with (
            patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(connection))),
            patch.object(
                brain,
                "embed",
                AsyncMock(side_effect=RuntimeError("All embedding providers failed")),
            ),
            patch.object(brain, "_refresh_mentioned_entities", AsyncMock()),
            patch.object(brain, "extract_thought_metadata", AsyncMock()),
        ):
            thought_id = await brain.store_thought(
                "Capture even when embeddings are down.",
                {"type": "thought", "source": "claude"},
            )

        self.assertEqual(thought_id, "thought-1")
        query, args = connection.calls[0]
        self.assertIn("SELECT upsert_thought", query)
        self.assertEqual(args[0], "Capture even when embeddings are down.")
        self.assertIsNone(args[1])
        self.assertEqual(json.loads(args[2])["source"], "claude")
        self.assertEqual(args[3], "thought")

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

    async def test_background_tasks_hold_ref_until_done_and_log_errors(self):
        # The deferred enrichment task must not be garbage-collected mid-flight:
        # store_thought holds a strong reference until it completes, then discards
        # it and logs any exception instead of swallowing it silently.
        brain._background_tasks.clear()
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_extract(_thought_id):
            started.set()
            await release.wait()
            raise RuntimeError("extraction blew up")

        connection = _ThoughtConnection(
            {"id": "thought-1", "inserted": True, "metadata_changed": False}
        )
        with (
            patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(connection))),
            patch.object(brain, "embed", AsyncMock(return_value=[0.1, 0.2])),
            patch.object(brain, "extract_thought_metadata", slow_extract),
        ):
            with self.assertLogs("openbrain.brain", level="WARNING") as cm:
                thought_id = await brain.store_thought("Some thought", {"type": "note"})
                await started.wait()
                # Strong reference is held while the task is still in flight.
                self.assertTrue(brain._background_tasks)
                release.set()
                # Drain the loop so the task finishes and its callback fires.
                for _ in range(5):
                    await asyncio.sleep(0)

        self.assertEqual(thought_id, "thought-1")
        # Completed task is discarded from the registry (no leak).
        self.assertEqual(brain._background_tasks, set())
        # Its exception was logged loudly, not silently dropped.
        self.assertTrue(any("extract_thought_metadata" in line for line in cm.output))

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


class _ExtractConnection:
    """Fake conn for extract_thought_metadata: one thought row, records upserts."""

    def __init__(self, row):
        self.row = row
        self.fetchval_calls = []

    async def fetchrow(self, query, *args):
        return self.row

    async def fetchval(self, query, *args):
        self.fetchval_calls.append((query, args))
        return {"id": "thought-1", "inserted": False, "metadata_changed": True}


class ThoughtDerivationTests(unittest.IsolatedAsyncioTestCase):
    async def _store_and_capture_schedule(self, metadata):
        connection = _ThoughtConnection(
            {"id": "thought-1", "inserted": True, "metadata_changed": False}
        )
        extract = AsyncMock()
        with (
            patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(connection))),
            patch.object(brain, "embed", AsyncMock(return_value=[0.1, 0.2])),
            patch.object(brain, "extract_thought_metadata", extract),
        ):
            thought_id = await brain.store_thought("Sparse thought", metadata)
            # Deferred: store_thought schedules extraction but does not await it,
            # so its write latency never includes an extraction-model round trip.
            extract.assert_not_awaited()
            await asyncio.sleep(0)
        return thought_id, extract, connection

    async def test_sparse_capture_schedules_extraction_deferred(self):
        thought_id, extract, connection = await self._store_and_capture_schedule(
            {"type": "thought"}
        )
        self.assertEqual(thought_id, "thought-1")
        extract.assert_awaited_once_with("thought-1")
        # Sparse capture carries no importance, so no caller marker is written and
        # extraction stays free to fill the model's importance.
        stored = json.loads(connection.calls[0][1][2])
        self.assertNotIn("importance_source", stored)

    async def test_enriched_metadata_opts_out_of_extraction(self):
        _, extract, _ = await self._store_and_capture_schedule(
            {"type": "thought", "enriched": True}
        )
        extract.assert_not_awaited()

    async def test_caller_importance_is_marked_for_extraction(self):
        # A capture that sets importance stamps importance_source=caller so the
        # later extraction knows not to override it (AC #2).
        _, _, connection = await self._store_and_capture_schedule(
            {"type": "thought", "importance": 2}
        )
        stored = json.loads(connection.calls[0][1][2])
        self.assertEqual(stored["importance_source"], "caller")

    async def test_extract_enriches_via_canonical_upsert(self):
        connection = _ExtractConnection(
            {
                "content": "Met Ada re SDC",
                "embedding": [0.1, 0.2],
                "enriched": False,
                "metadata": {},
            }
        )
        refresh = AsyncMock()
        link = AsyncMock()
        raw = (
            '{"tags":["sdc"],"people":["Ada"],"projects":["SDC"],'
            '"action_items":["email Ada"],"importance":5}'
        )
        with (
            patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(connection))),
            patch.object(
                brain, "complete_extraction_prompt", AsyncMock(return_value=raw)
            ),
            patch.object(brain, "_refresh_mentioned_entities", refresh),
            patch.object(brain, "_link_thought_entities", link),
        ):
            res = await brain.extract_thought_metadata("thought-1")

        self.assertEqual(res["status"], "done")
        query, args = connection.fetchval_calls[0]
        self.assertIn("SELECT upsert_thought", query)
        self.assertEqual(args[0], "Met Ada re SDC")
        payload = json.loads(args[2])
        self.assertTrue(payload["enriched"])
        self.assertEqual(payload["tags"], ["sdc"])
        # Sparse capture: the caller set no importance, so the model's value lands.
        self.assertEqual(payload["importance"], 5)
        # p_type is NULL so extraction never overwrites the caller's thought type.
        self.assertIsNone(args[3])
        # Scalar mode 'overwrite' lets the extracted importance land verbatim.
        self.assertEqual(args[4], "overwrite")
        refresh.assert_awaited_once_with(["Ada"], ["SDC"])
        link.assert_awaited_once_with("thought-1", ["Ada"], ["SDC"])

    async def test_extract_preserves_caller_importance(self):
        # Caller marked a low, deliberate importance; the model wants to raise it.
        # The caller's value must win (AC #2), so extraction drops its importance
        # and never sends it to the merge.
        connection = _ExtractConnection(
            {
                "content": "Ephemeral note about Ada",
                "embedding": [0.1, 0.2],
                "enriched": False,
                "metadata": {"importance": 2, "importance_source": "caller"},
            }
        )
        raw = '{"tags":["ada"],"people":["Ada"],"importance":5}'
        with (
            patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(connection))),
            patch.object(
                brain, "complete_extraction_prompt", AsyncMock(return_value=raw)
            ),
            patch.object(brain, "_refresh_mentioned_entities", AsyncMock()),
            patch.object(brain, "_link_thought_entities", AsyncMock()),
        ):
            res = await brain.extract_thought_metadata("thought-1")

        self.assertEqual(res["status"], "done")
        _, args = connection.fetchval_calls[0]
        payload = json.loads(args[2])
        # Importance stripped: the merge keeps the caller's stored value untouched.
        self.assertNotIn("importance", payload)
        # Arrays still union — the model's discovered tags/people are additive.
        self.assertEqual(payload["tags"], ["ada"])
        self.assertTrue(payload["enriched"])

    async def test_extract_sparse_importance_can_go_below_default(self):
        # Sparse capture, model returns an importance below the seeded default.
        # 'overwrite' mode must let it land (GREATEST would have pinned it up).
        connection = _ExtractConnection(
            {
                "content": "Trivial passing remark",
                "embedding": [0.1, 0.2],
                "enriched": False,
                "metadata": {"importance": 3},  # seeded default, not caller-set
            }
        )
        raw = '{"tags":["chatter"],"importance":1}'
        with (
            patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(connection))),
            patch.object(
                brain, "complete_extraction_prompt", AsyncMock(return_value=raw)
            ),
            patch.object(brain, "_refresh_mentioned_entities", AsyncMock()),
            patch.object(brain, "_link_thought_entities", AsyncMock()),
        ):
            res = await brain.extract_thought_metadata("thought-1")

        self.assertEqual(res["status"], "done")
        _, args = connection.fetchval_calls[0]
        payload = json.loads(args[2])
        self.assertEqual(payload["importance"], 1)
        self.assertEqual(args[4], "overwrite")

    async def test_extract_model_down_leaves_straggler(self):
        connection = _ExtractConnection(
            {"content": "x", "embedding": None, "enriched": False}
        )
        with (
            patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(connection))),
            patch.object(
                brain,
                "complete_extraction_prompt",
                AsyncMock(side_effect=RuntimeError("extraction model down")),
            ),
        ):
            res = await brain.extract_thought_metadata("thought-1")

        self.assertEqual(res["status"], "error")
        self.assertEqual(connection.fetchval_calls, [])

    async def test_extract_off_schema_leaves_straggler(self):
        connection = _ExtractConnection(
            {"content": "x", "embedding": None, "enriched": False}
        )
        with (
            patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(connection))),
            patch.object(
                brain,
                "complete_extraction_prompt",
                AsyncMock(return_value="sorry, no structured data here"),
            ),
        ):
            res = await brain.extract_thought_metadata("thought-1")

        self.assertEqual(res["status"], "error")
        self.assertEqual(connection.fetchval_calls, [])

    async def test_extract_skips_already_enriched(self):
        connection = _ExtractConnection(
            {"content": "x", "embedding": [0.1], "enriched": True}
        )
        prompt = AsyncMock()
        with (
            patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(connection))),
            patch.object(brain, "complete_extraction_prompt", prompt),
        ):
            res = await brain.extract_thought_metadata("thought-1")

        self.assertEqual(res["status"], "skipped")
        prompt.assert_not_awaited()
        self.assertEqual(connection.fetchval_calls, [])

    def test_parse_keeps_arrays_and_coerces_importance(self):
        out = brain.parse_thought_metadata(
            'prefix {"tags":["a","b"],"importance":"4","people":[],"junk":1} suffix'
        )
        self.assertEqual(out["tags"], ["a", "b"])
        self.assertEqual(out["importance"], 4)
        self.assertNotIn("people", out)  # empty array dropped

    def test_parse_off_schema_returns_none(self):
        self.assertIsNone(brain.parse_thought_metadata("no json at all"))
        self.assertIsNone(brain.parse_thought_metadata('["not an object"]'))

    def test_parse_valid_but_empty_object_is_enrichable(self):
        # Model ran, found nothing to add: empty dict (not None) so the thought
        # still gets marked enriched rather than looping as a straggler forever.
        self.assertEqual(brain.parse_thought_metadata("{}"), {})

    async def test_backfill_thought_embeddings_fills_null_rows(self):
        connection = _BackfillConnection(
            pending=2,
            rows=[{"id": "t1", "content": "a"}, {"id": "t2", "content": "b"}],
        )
        with (
            patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(connection))),
            patch.object(brain, "embed", AsyncMock(return_value=[0.1, 0.2])),
        ):
            result = await brain.backfill_thought_embeddings()

        self.assertEqual(result["embedded"], 2)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["pending_after"], 0)
        self.assertEqual(len(connection.updates), 2)

    async def test_backfill_thought_extractions_tallies_outcomes(self):
        connection = _BackfillConnection(pending=2, rows=[{"id": "t1"}, {"id": "t2"}])
        extract = AsyncMock(
            side_effect=[
                {"thought_id": "t1", "status": "done"},
                {"thought_id": "t2", "status": "error"},
            ]
        )
        with (
            patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(connection))),
            patch.object(brain, "extract_thought_metadata", extract),
        ):
            result = await brain.backfill_thought_extractions()

        self.assertEqual(result["extracted"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["pending_after"], 1)

    async def test_list_pending_thoughts_names_pending_steps(self):
        connection = _BackfillConnection(
            pending=0,
            rows=[
                {
                    "id": "t1",
                    "content": "alpha",
                    "needs_embedding": True,
                    "needs_extraction": False,
                },
                {
                    "id": "t2",
                    "content": "beta",
                    "needs_embedding": False,
                    "needs_extraction": True,
                },
            ],
        )
        with patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(connection))):
            result = await brain.list_pending_thoughts()

        self.assertEqual(
            result,
            [
                {
                    "id": "t1",
                    "content": "alpha",
                    "needs_embedding": True,
                    "needs_extraction": False,
                },
                {
                    "id": "t2",
                    "content": "beta",
                    "needs_embedding": False,
                    "needs_extraction": True,
                },
            ],
        )

    async def test_thought_maintenance_tools_report_counts(self):
        mcp = _Mcp()
        maintenance_tools.register(mcp)
        with patch.object(
            brain,
            "backfill_thought_embeddings",
            AsyncMock(
                return_value={
                    "pending_before": 3,
                    "embedded": 2,
                    "failed": 1,
                    "pending_after": 1,
                }
            ),
        ):
            out = await mcp.tools["backfill_thought_embeddings"]()
        self.assertIn("2 embedded", out)
        self.assertIn("1 still pending", out)

        with patch.object(
            brain,
            "backfill_thought_extractions",
            AsyncMock(
                return_value={
                    "pending_before": 2,
                    "extracted": 2,
                    "failed": 0,
                    "pending_after": 0,
                }
            ),
        ):
            out = await mcp.tools["backfill_thought_extractions"]()
        self.assertIn("2 enriched", out)

        with patch.object(
            brain,
            "list_pending_thoughts",
            AsyncMock(
                return_value=[
                    {
                        "id": "t1",
                        "content": "alpha",
                        "needs_embedding": True,
                        "needs_extraction": True,
                    }
                ]
            ),
        ):
            out = await mcp.tools["list_pending_thoughts"]()
        self.assertIn("embedding", out)
        self.assertIn("extraction", out)
        self.assertIn("t1", out)


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

    async def test_embed_cuts_prefixed_text_to_max_input_chars(self):
        embed_config = {
            "provider": "litellm",
            "model": "nomic",
            "document_prefix": "doc: ",
            "max_input_chars": 8,
        }
        sent = []

        async def _embed(text, _config):
            sent.append(text)
            return [0.0]

        with (
            patch.object(brain, "_cfg", {"embed": embed_config}),
            patch.object(brain, "_embed_with_config", _embed),
        ):
            await brain.embed("hello world", task="document")

        self.assertEqual(sent, ["doc: hel"])

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

    async def test_litellm_embedding_uses_local_placeholder_key(self):
        created = {}

        class _Client:
            def __init__(self, api_key, base_url, timeout):
                created["api_key"] = api_key
                created["base_url"] = base_url
                created["timeout"] = timeout
                self.embeddings = types.SimpleNamespace(create=self._create)

            async def _create(self, input, model, encoding_format):
                created["encoding_format"] = encoding_format
                return types.SimpleNamespace(
                    data=[types.SimpleNamespace(embedding=[0.1, 0.2])]
                )

        openai = types.ModuleType("openai")
        openai.AsyncOpenAI = _Client

        with (
            patch.dict(sys.modules, {"openai": openai}),
            patch.dict(os.environ, {}, clear=True),
        ):
            result = await brain._embed_with_config(
                "test",
                {
                    "provider": "litellm",
                    "model": "moon8-embeddings",
                    "litellm_url": "http://litellm:4000",
                },
            )

        self.assertEqual(result, [0.1, 0.2])
        self.assertEqual(created["api_key"], "local-litellm")
        self.assertEqual(created["base_url"], "http://litellm:4000")
        # Must be sent explicitly — llama.cpp 500s on the null litellm injects.
        self.assertEqual(created["encoding_format"], "float")

    async def test_litellm_extraction_uses_local_placeholder_key(self):
        created = {}

        class _Client:
            def __init__(self, api_key, base_url):
                created["api_key"] = api_key
                created["base_url"] = base_url
                completion = types.SimpleNamespace(
                    message=types.SimpleNamespace(content="synthesis")
                )
                self.chat = types.SimpleNamespace(
                    completions=types.SimpleNamespace(
                        create=AsyncMock(
                            return_value=types.SimpleNamespace(choices=[completion])
                        )
                    )
                )

        openai = types.ModuleType("openai")
        openai.AsyncOpenAI = _Client

        with (
            patch.dict(sys.modules, {"openai": openai}),
            patch.dict(os.environ, {}, clear=True),
        ):
            result = await brain._complete_extraction_with_config(
                "prompt",
                {
                    "provider": "litellm",
                    "model": "moon8-fast",
                    "litellm_url": "http://litellm:4000",
                },
            )

        self.assertEqual(result, "synthesis")
        self.assertEqual(created["api_key"], "local-litellm")
        self.assertEqual(created["base_url"], "http://litellm:4000")

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


class EntityWriteTests(unittest.IsolatedAsyncioTestCase):
    async def test_add_person_defers_embedding_when_model_unavailable(self):
        connection = _EntityConnection()
        with (
            patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(connection))),
            patch.object(brain, "embed", AsyncMock(side_effect=RuntimeError("down"))),
        ):
            result = await brain.add_person(
                "Ada Lovelace",
                affiliation="Analytical Engine",
                relationship="contact",
                notes="Computing pioneer.",
            )

        self.assertEqual(result["id"], "person-1")
        self.assertEqual(result["name"], "Ada Lovelace")
        self.assertEqual(connection.execute_calls, [])

    async def test_add_project_defers_embedding_when_model_unavailable(self):
        connection = _EntityConnection()
        with (
            patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(connection))),
            patch.object(brain, "embed", AsyncMock(side_effect=RuntimeError("down"))),
        ):
            result = await brain.add_project(
                "openbrain",
                description="Personal memory MCP server.",
            )

        self.assertEqual(result["id"], "project-1")
        self.assertEqual(result["name"], "openbrain")
        self.assertEqual(connection.execute_calls, [])


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

    async def test_ingest_upgrades_abstract_only_record_in_place(self):
        # An abstract-only record (from fetch_paper) exists for this title; the
        # PDF/text ingest must UPGRADE it in place (UPDATE + drop chunks/findings),
        # not INSERT a duplicate.
        connection = _IngestConnection()
        connection.title_upgrade = {"id": "lib-existing"}
        with tempfile.TemporaryDirectory() as tempdir:
            source = Path(tempdir) / "paper.txt"
            source.write_text("Full text of the paper, much longer than an abstract.")
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
                await brain.ingest_document_pipeline(str(source), title="A Paper")

        queries = [q for q, _ in connection.fetchrow_calls]
        self.assertTrue(any("UPDATE library SET" in q for q in queries))
        self.assertFalse(any("INSERT INTO library" in q for q in queries))
        execs = [q for q, _ in connection.execute_calls]
        self.assertTrue(any("DELETE FROM chunks WHERE library_id" in q for q in execs))
        self.assertTrue(any("DELETE FROM findings WHERE paper_id" in q for q in execs))

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
    def __init__(self, existing=None, title_existing=None):
        self.existing = existing
        self.title_existing = title_existing
        self.fetchrow_calls = []
        self.execute_calls = []

    async def fetchrow(self, query, *args):
        self.fetchrow_calls.append((query, args))
        if "WHERE doi" in query or "WHERE lower(doi)" in query:
            return self.existing
        if "WHERE lower(title)" in query:
            return self.title_existing
        if "INSERT INTO library" in query:
            return {"id": "library-9", "title": args[0]}
        raise AssertionError(f"Unexpected query: {query}")

    async def fetchval(self, query, *args):
        # Venue registry lookup (venues.match) — unregistered venue → None, so the
        # citekey falls back to a source-derived token.
        return None

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

    async def test_store_paper_dedups_on_title_year_when_no_doi(self):
        # A paper fetched by title may carry a different or no DOI, so a DOI-only
        # check misses it. Match on case-insensitive title + year instead.
        connection = _PaperConnection(
            existing=None,
            title_existing={"id": "library-7", "title": "A Paper"},
        )
        meta = {"title": "A Paper", "year": 2024}  # no doi
        with (
            patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(connection))),
            patch.object(brain, "embed", AsyncMock(return_value=[0.1, 0.2])),
            patch.object(brain, "store_thought", AsyncMock()),
        ):
            result = await brain._store_paper(meta)

        self.assertTrue(result["deduped"])
        self.assertEqual(result["id"], "library-7")
        self.assertFalse(
            any("INSERT INTO library" in q for q, _ in connection.fetchrow_calls)
        )

    async def test_store_paper_title_dedup_skipped_with_force(self):
        connection = _PaperConnection(
            existing=None,
            title_existing={"id": "library-7", "title": "A Paper"},
        )
        meta = {"title": "A Paper", "year": 2024}
        with (
            patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(connection))),
            patch.object(brain, "embed", AsyncMock(return_value=[0.1, 0.2])),
            patch.object(brain, "store_thought", AsyncMock()),
        ):
            result = await brain._store_paper(meta, force=True)

        self.assertFalse(result["deduped"])
        self.assertTrue(
            any("INSERT INTO library" in q for q, _ in connection.fetchrow_calls)
        )

    async def test_fetch_paper_topic_returns_candidates_without_storing(self):
        candidates = [{"title": "A Paper", "doi": "10.1/x", "authors": ["Ada"]}]
        store_paper_mock = AsyncMock()
        with (
            patch.object(brain.papers, "search_topic", return_value=candidates),
            patch.object(brain, "_store_paper", store_paper_mock),
        ):
            result = await brain.fetch_papers(topic="bandwidth selection")

        self.assertEqual(result["mode"], "topic")
        self.assertEqual(result["candidates"], candidates)
        self.assertEqual(result["stored"], 0)
        store_paper_mock.assert_not_awaited()

    async def test_fetch_paper_topic_store_true_bulk_stores(self):
        candidates = [{"title": "A Paper", "doi": "10.1/x"}]
        report = {"stored": 1, "deduped": 0, "results": [{"id": "id-1"}]}
        with (
            patch.object(brain.papers, "search_topic", return_value=candidates),
            patch.object(brain, "store_papers", AsyncMock(return_value=report)),
        ):
            result = await brain.fetch_papers(topic="water", store=True)

        self.assertEqual(result["stored"], 1)
        self.assertEqual(result["stored_ids"], ["id-1"])

    async def test_fetch_paper_lookup_stores_resolved_paper(self):
        meta = {"title": "A Paper", "doi": "10.1/x"}
        store_paper_mock = AsyncMock(
            return_value={"title": "A Paper", "deduped": False}
        )
        with (
            patch.object(brain.papers, "resolve_known_item", return_value=meta),
            patch.object(brain, "_store_paper", store_paper_mock),
        ):
            result = await brain.fetch_papers(identifier="10.1/x")

        self.assertTrue(result["found"])
        store_paper_mock.assert_awaited_once()

    async def test_known_item_passes_threshold_and_corroboration(self):
        captured = {}

        def fake_resolve(identifier, **kw):
            captured.update(kw)
            captured["identifier"] = identifier
            return {"title": "A", "doi": "10.1/a"}

        with (
            patch.object(brain.papers, "resolve_known_item", fake_resolve),
            patch.object(
                brain,
                "_store_paper",
                AsyncMock(
                    return_value={
                        "id": "1",
                        "title": "A",
                        "abstract_stored": True,
                        "embedded": True,
                        "deduped": False,
                    }
                ),
            ),
        ):
            out = await brain.fetch_papers(
                identifier="A title", year=2015, authors=["Doe"], title_threshold=0.7
            )

        self.assertTrue(out["found"])
        self.assertEqual(captured["min_score"], 0.7)
        self.assertEqual(captured["year"], 2015)
        self.assertEqual(captured["authors"], ["Doe"])

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
        with patch.object(brain, "fetch_papers", AsyncMock(return_value=result)):
            out = await mcp.tools["fetch_papers"](identifier="10.1/x")

        payload = json.loads(out)
        self.assertEqual(payload["title"], "A Paper")
        self.assertTrue(payload["embedded"])

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
        with patch.object(brain, "fetch_papers", AsyncMock(return_value=result)):
            out = await mcp.tools["fetch_papers"](identifier="10.1/x")

        payload = json.loads(out)
        self.assertTrue(payload["abstract_stored"])
        self.assertFalse(payload["embedded"])

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
        fetch_one = AsyncMock(
            side_effect=lambda *, identifier, **kw: outcomes[identifier]
        )
        with patch.object(brain, "_fetch_one", fetch_one):
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
            out = await mcp.tools["fetch_papers"](identifiers=["10.1/a", "10.1/c"])

        payload = json.loads(out)
        self.assertEqual(payload["mode"], "batch")
        self.assertEqual(payload["summary"]["fetched"], 1)
        self.assertEqual(payload["summary"]["not_found"], 1)
        self.assertEqual(
            [r["identifier"] for r in payload["results"]], ["10.1/a", "10.1/c"]
        )

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

    async def test_list_pending_embeddings_returns_records(self):
        connection = _BackfillConnection(
            pending=0,
            rows=[
                {"id": "lib-1", "title": "Paper A", "pending": 2},
                {"id": "lib-2", "title": "Paper B", "pending": 1},
            ],
        )
        with patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(connection))):
            result = await brain.list_pending_embeddings()

        self.assertEqual(
            result,
            [
                {"id": "lib-1", "title": "Paper A", "pending": 2},
                {"id": "lib-2", "title": "Paper B", "pending": 1},
            ],
        )

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


class StorePapersTests(unittest.IsolatedAsyncioTestCase):
    async def test_enriches_then_stores_and_summarizes(self):
        seen = []

        async def fake_store(meta, *, force=False):
            seen.append(meta)
            return {
                "id": f"id-{len(seen)}",
                "title": meta["title"],
                "embedded": True,
                "deduped": False,
            }

        with (
            patch.object(brain, "store_acquired_paper", fake_store),
            patch.object(
                brain, "get_pool", AsyncMock(return_value=_Pool(_FetchConnection([])))
            ),
            patch.object(brain.papers, "lookup", lambda d: {"abstract": "filled"}),
            patch.object(brain.venues, "match", AsyncMock(return_value="AJAE")),
        ):
            out = await brain.store_papers(
                # no abstract -> enriched
                [{"title": "A", "doi": "10.1/a", "authors": ["Jane Doe"], "year": 2021}]
            )
        self.assertEqual(out["stored"], 1)
        self.assertEqual(seen[0]["abstract"], "filled")
        self.assertEqual(seen[0]["venue_abbrev"], "AJAE")
        # Results carry the full citation, not just the uuid.
        row = out["results"][0]
        self.assertEqual(row["doi"], "10.1/a")
        self.assertEqual(row["authors"], ["Jane Doe"])
        self.assertEqual(row["year"], 2021)
        self.assertEqual(row["venue"], "AJAE")

    async def test_empty_is_noop(self):
        out = await brain.store_papers([])
        self.assertEqual(out, {"stored": 0, "deduped": 0, "results": []})


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

    async def test_get_papers_formats_metadata(self):
        mcp = _Mcp()
        library_tools.register(mcp)
        record = {
            "id": "library-1",
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
        with patch.object(brain, "resolve_reference", AsyncMock(return_value=[record])):
            result = await mcp.tools["get_papers"](title="Paper")

        self.assertIn("Authors: Ada", result)
        self.assertIn("Path: /tmp/paper.pdf", result)


class _AnchorSearchConnection:
    """First fetch returns anchor embeddings; second returns candidate chunk rows."""

    def __init__(self, anchor_embeddings, candidate_rows):
        self.anchor_embeddings = anchor_embeddings
        self.candidate_rows = candidate_rows
        self.calls = []

    async def fetch(self, query, *args):
        self.calls.append((query, args))
        if "SELECT c.embedding FROM chunks c" in query:
            return [{"embedding": e} for e in self.anchor_embeddings]
        return self.candidate_rows


class AnchorSearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_centroid_search_excludes_anchors_and_returns_rows(self):
        cand = [
            {
                "chunk_id": "c1",
                "library_id": "lib-2",
                "title": "Neighbor",
                "authors": ["A"],
                "year": 2020,
                "source": "J",
                "doc_type": "paper",
                "status": "ingested",
                "chunk_text": "text",
                "section": None,
                "similarity": 0.9,
            }
        ]
        conn = _AnchorSearchConnection([[1.0, 0.0], [0.0, 1.0]], cand)
        with patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(conn))):
            rows = await brain.search_library_by_anchors(["lib-1"], count=5)
        self.assertEqual([r["library_id"] for r in rows], ["lib-2"])
        # The candidate query must carry an exclude-ids clause for the anchors.
        candidate_query = conn.calls[1][0]
        self.assertIn("!= ALL", candidate_query)

    async def test_raises_when_no_anchor_embeddings(self):
        conn = _AnchorSearchConnection([], [])
        with patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(conn))):
            with self.assertRaises(ValueError):
                await brain.search_library_by_anchors(["lib-1"])

    async def test_raises_when_no_anchor_ids(self):
        with self.assertRaises(ValueError):
            await brain.search_library_by_anchors([])


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


class SearchThoughtsToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_str_metadata_from_asyncpg_is_parsed(self):
        # asyncpg returns jsonb columns as raw JSON strings (no codec is
        # registered), so the tool must json.loads metadata before .get().
        mcp = _Mcp()
        search_tools.register(mcp)
        rows = [
            {
                "similarity": 0.91,
                "content": "Prefer walk-forward validation.",
                "metadata": '{"tags": ["forecasting", "validation"]}',
            }
        ]
        with patch.object(brain, "search_thoughts_vec", AsyncMock(return_value=rows)):
            result = await mcp.tools["search_thoughts"]("validation")

        self.assertIn("[0.91]", result)
        self.assertIn("forecasting, validation", result)
        self.assertIn("Prefer walk-forward validation.", result)

    async def test_dict_metadata_still_supported(self):
        mcp = _Mcp()
        search_tools.register(mcp)
        rows = [
            {
                "similarity": 0.80,
                "content": "Already-parsed metadata.",
                "metadata": {"tags": ["policy"]},
            }
        ]
        with patch.object(brain, "search_thoughts_vec", AsyncMock(return_value=rows)):
            result = await mcp.tools["search_thoughts"]("policy")

        self.assertIn("policy", result)

    async def test_null_metadata_is_safe(self):
        mcp = _Mcp()
        search_tools.register(mcp)
        rows = [{"similarity": 0.75, "content": "No metadata.", "metadata": None}]
        with patch.object(brain, "search_thoughts_vec", AsyncMock(return_value=rows)):
            result = await mcp.tools["search_thoughts"]("x")

        self.assertIn("No metadata.", result)


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


class LibraryPathOverrideTest(unittest.TestCase):
    """The container mounts the library at a different path than the host.

    config.yaml stores the host-side path (used by scripts/backup.sh); the
    server reads OPENBRAIN_LIBRARY_PATH so document ingest writes to the path
    that is actually mounted inside the container.
    """

    def _reload_brain(self):
        import importlib

        importlib.reload(brain)
        # Restore the module to its config-driven state for other tests.
        self.addCleanup(self._reload_clean)

    def _reload_clean(self):
        import importlib

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENBRAIN_LIBRARY_PATH", None)
            importlib.reload(brain)

    def test_env_override_takes_precedence_over_config(self):
        with patch.dict(
            os.environ, {"OPENBRAIN_LIBRARY_PATH": "/data/openbrain/library"}
        ):
            self._reload_brain()
            self.assertEqual(brain.LIBRARY_PATH, Path("/data/openbrain/library"))

    def test_falls_back_to_config_when_env_unset(self):
        env = {k: v for k, v in os.environ.items() if k != "OPENBRAIN_LIBRARY_PATH"}
        with patch.dict(os.environ, env, clear=True):
            self._reload_brain()
            self.assertEqual(brain.LIBRARY_PATH, Path(brain._cfg["library_path"]))


class LibraryPathOverrideCopyTargetTest(unittest.IsolatedAsyncioTestCase):
    async def test_copy_target_uses_overridden_library_path(self):
        with tempfile.TemporaryDirectory() as tempdir:
            mount = Path(tempdir) / "data" / "openbrain" / "library"
            with patch.dict(os.environ, {"OPENBRAIN_LIBRARY_PATH": str(mount)}):
                import importlib

                importlib.reload(brain)
            self.addCleanup(self._reload_clean)

            source = Path(tempdir) / "paper.txt"
            source.write_text("Short paper text.")
            connection = _IngestConnection()
            with (
                patch.object(
                    brain, "get_pool", AsyncMock(return_value=_Pool(connection))
                ),
                patch.object(brain, "embed", AsyncMock(return_value=[0.1, 0.2])),
                patch.object(
                    brain, "store_thought", AsyncMock(return_value="thought-1")
                ),
            ):
                result = await brain.ingest_document_pipeline(
                    str(source),
                    title="A Paper",
                )

            dest = Path(result["filepath"])
            self.assertEqual(dest.parent, mount)
            self.assertTrue(dest.exists())

    def _reload_clean(self):
        import importlib

        os.environ.pop("OPENBRAIN_LIBRARY_PATH", None)
        importlib.reload(brain)


def _lib_row(**kw):
    base = {
        "id": "x",
        "title": "T",
        "authors": [],
        "year": None,
        "source": None,
        "doc_type": "paper",
        "status": "ingested",
        "filepath": None,
        "doi": None,
        "abstract": None,
    }
    base.update(kw)
    return base


class ReferenceResolveTests(unittest.IsolatedAsyncioTestCase):
    async def test_doi_is_exact_and_authoritative(self):
        rows = [_lib_row(id="1", title="Whatever", doi="10.1/x")]
        conn = _FetchConnection(rows)
        with patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(conn))):
            out = await brain.resolve_reference(doi="https://doi.org/10.1/x")
        self.assertEqual([r["id"] for r in out], ["1"])
        self.assertIn("doi =", conn.calls[0][0])  # took the exact-DOI branch

    async def test_title_apostrophe_noise_matches(self):
        rows = [
            _lib_row(id="1", title="Cattlemen's Association", year=1990),
            _lib_row(id="2", title="Unrelated Paper", year=1990),
        ]
        conn = _FetchConnection(rows)
        with patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(conn))):
            out = await brain.resolve_reference(title="cattlemens association")
        self.assertEqual([r["id"] for r in out], ["1"])

    async def test_title_fuzzy_match_tolerates_misspelling(self):
        rows = [
            _lib_row(id="1", title="Cattlemen's Association", year=1990),
            _lib_row(id="2", title="Totally Unrelated", year=1990),
        ]
        conn = _FetchConnection(rows)
        with patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(conn))):
            out = await brain.resolve_reference(title="catllemens asociation")  # typos
        self.assertEqual([r["id"] for r in out], ["1"])

    async def test_author_surname_corroboration_ranks_first(self):
        # With a title given, author is a soft bonus: both same-title rows are
        # returned, but the matching-author row ranks first (not dropped).
        rows = [
            _lib_row(id="1", title="Shared Title", authors=["Jane Doe"], year=2001),
            _lib_row(id="2", title="Shared Title", authors=["John Smith"], year=2001),
        ]
        conn = _FetchConnection(rows)
        with patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(conn))):
            out = await brain.resolve_reference(title="shared title", authors=["Doe"])
        self.assertEqual(out[0]["id"], "1")  # Doe ranked first by corroboration
        self.assertEqual({r["id"] for r in out}, {"1", "2"})

    async def test_resolves_by_uuid(self):
        rows = [_lib_row(id="u-1", title="Whatever")]
        conn = _FetchConnection(rows)
        with patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(conn))):
            out = await brain.resolve_reference(paper_id="u-1")
        self.assertEqual([r["id"] for r in out], ["u-1"])
        self.assertIn("WHERE id = $1::uuid", conn.calls[0][0])

    async def test_returns_multiple_when_ambiguous(self):
        rows = [
            _lib_row(id="1", title="Shared Title", authors=["Jane Doe"]),
            _lib_row(id="2", title="Shared Title", authors=["John Smith"]),
        ]
        conn = _FetchConnection(rows)
        with patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(conn))):
            out = await brain.resolve_reference(title="shared title")
        self.assertEqual({r["id"] for r in out}, {"1", "2"})

    async def test_nonmatching_title_returns_empty(self):
        rows = [_lib_row(id="1", title="Some Paper")]
        conn = _FetchConnection(rows)
        with patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(conn))):
            out = await brain.resolve_reference(title="a totally different title")
        self.assertEqual(out, [])


if __name__ == "__main__":
    unittest.main()


class _FetchRecorder:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.calls = []

    async def fetch(self, query, *args):
        self.calls.append((query, args))
        return self.rows


class StoreAcquiredPaperTests(unittest.IsolatedAsyncioTestCase):
    async def test_keeps_raw_source_and_acquire_columns(self):
        conn = _PaperConnection(existing=None)
        meta = {
            "title": "T",
            "authors": ["A B"],
            "year": 2021,
            "source": "American Journal of Agricultural Economics",
            "abstract": "water demand",
            "doi": "10.1/x",
            "issn_l": "0002-9092",
            "venue_source_id": "S123",
            "venue_abbrev": "AJAE",
            "concepts": ["Water"],
        }
        with (
            patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(conn))),
            patch.object(brain, "embed", AsyncMock(return_value=[0.1, 0.2])),
        ):
            res = await brain.store_acquired_paper(meta)

        self.assertFalse(res["deduped"])
        insert = next(
            args for q, args in conn.fetchrow_calls if "INSERT INTO library" in q
        )
        # positional order: title, authors, year, source, abstract, doi, bibtex,
        # issn, venue_source_id, venue_abbrev, concepts
        self.assertEqual(insert[3], "American Journal of Agricultural Economics")
        self.assertEqual(insert[7], "0002-9092")
        self.assertEqual(insert[9], "AJAE")
        self.assertEqual(insert[10], ["Water"])

    async def test_dedups_existing_doi(self):
        conn = _PaperConnection(existing={"id": "lib-1", "title": "T"})
        with patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(conn))):
            res = await brain.store_acquired_paper({"doi": "10.1/x", "title": "T"})
        self.assertTrue(res["deduped"])
        self.assertFalse(
            any("INSERT INTO library" in q for q, _ in conn.fetchrow_calls)
        )

    async def test_normalizes_doi_before_dedup_and_store(self):
        conn = _PaperConnection(existing=None)
        with (
            patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(conn))),
            patch.object(brain, "embed", AsyncMock(return_value=[0.1, 0.2])),
        ):
            await brain.store_acquired_paper(
                {"doi": "https://doi.org/10.1257/AER.123", "title": "T"}
            )
        # dedup looks up case-insensitively; the stored DOI is the bare,
        # lowercased form (DOIs are case-insensitive — citer normalizes them).
        self.assertTrue(any("lower(doi)" in q for q, _ in conn.fetchrow_calls))
        insert = next(
            args for q, args in conn.fetchrow_calls if "INSERT INTO library" in q
        )
        self.assertEqual(insert[5], "10.1257/aer.123")  # prefix stripped, lowercased

    async def test_dedups_on_title_year_when_no_doi(self):
        conn = _PaperConnection(
            existing=None, title_existing={"id": "lib-7", "title": "T"}
        )
        with patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(conn))):
            res = await brain.store_acquired_paper({"title": "T", "year": 2020})
        self.assertTrue(res["deduped"])
        self.assertFalse(
            any("INSERT INTO library" in q for q, _ in conn.fetchrow_calls)
        )


class QueryLibraryStructTests(unittest.IsolatedAsyncioTestCase):
    async def test_builds_parameterized_filters(self):
        conn = _FetchRecorder(rows=[])
        with patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(conn))):
            await brain.query_library_struct(venue="AJAE", year_min=2020)
        query, args = conn.calls[0]
        self.assertIn("l.venue_abbrev = $1", query)
        self.assertIn("l.year >= $2", query)
        self.assertEqual(args, ("AJAE", 2020))

    async def test_sort_column_is_whitelisted(self):
        conn = _FetchRecorder(rows=[])
        with patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(conn))):
            await brain.query_library_struct(sort="year; DROP TABLE library")
        query, _ = conn.calls[0]
        self.assertIn("ORDER BY l.year", query)
        self.assertNotIn("DROP", query)


class CitekeyTests(unittest.TestCase):
    def test_two_authors(self):
        self.assertEqual(
            brain.citekey(["Jane Smith", "Bob Lee"], 2021, "AJAE"),
            "Smith-Lee-2021-AJAE",
        )

    def test_three_or_more_authors_use_etal(self):
        self.assertEqual(brain.citekey(["A", "B", "C"], 2020, "WRR"), "A-ETAL-2020-WRR")

    def test_falls_back_to_source_token_when_no_venue_abbrev(self):
        # arXiv (single word) keeps its name+case instead of degrading to NA.
        self.assertEqual(
            brain.citekey(["Jane Smith"], 2026, None, "arXiv"),
            "Smith-2026-arXiv",
        )

    def test_source_token_acronyms_multiword_venue(self):
        self.assertEqual(
            brain.citekey(
                ["A", "B", "C"],
                2018,
                None,
                "American Journal of Agricultural Economics",
            ),
            "A-ETAL-2018-AJAE",
        )

    def test_venue_abbrev_wins_over_source(self):
        self.assertEqual(
            brain.citekey(
                ["Jane Smith"],
                2021,
                "AJAE",
                "American Journal of Agricultural Economics",
            ),
            "Smith-2021-AJAE",
        )

    def test_na_only_when_no_venue_and_no_source(self):
        self.assertEqual(
            brain.citekey(["Jane Smith"], 2021, None, None), "Smith-2021-NA"
        )

    def test_venue_token_helper(self):
        self.assertEqual(brain._venue_token("arXiv"), "arXiv")
        self.assertEqual(brain._venue_token("Science"), "Science")
        self.assertEqual(
            brain._venue_token("American Journal of Agricultural Economics"), "AJAE"
        )
        self.assertIsNone(brain._venue_token(None))
        self.assertIsNone(brain._venue_token("   "))

    def test_render_bibtex_minimal(self):
        entry = brain._render_bibtex(
            {"title": "T", "authors": ["Jane Smith"], "year": 2021, "source": "AJAE"},
            "Smith-2021-AJAE",
        )
        self.assertIn("@article{Smith-2021-AJAE,", entry)
        self.assertIn("title = {T}", entry)


class RelevanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_anchors_raises(self):
        with self.assertRaises(ValueError):
            await brain.relevance([], ["some text"])

    async def test_no_stored_embeddings_raises(self):
        connection = _FetchConnection([])
        with patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(connection))):
            with self.assertRaises(ValueError):
                await brain.relevance(["lib-1"], ["text"])

    async def test_centroid_is_mean_and_cosines_in_order(self):
        # Two anchor embeddings -> centroid is their component-wise mean = [1, 0].
        connection = _FetchConnection(
            [{"embedding": [1.0, 1.0]}, {"embedding": [1.0, -1.0]}]
        )
        # Candidate 1 aligns with the centroid (cos 1.0); candidate 2 is orthogonal.
        embeddings = {"aligned": [2.0, 0.0], "orthogonal": [0.0, 5.0]}

        async def fake_embed(text, task="document"):
            return embeddings[text]

        with (
            patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(connection))),
            patch.object(brain, "embed", fake_embed),
        ):
            scores = await brain.relevance(
                ["lib-1", "lib-2"], ["aligned", "orthogonal"]
            )
        self.assertEqual(len(scores), 2)
        self.assertAlmostEqual(scores[0], 1.0)
        self.assertAlmostEqual(scores[1], 0.0)


class StorePapersWrapTests(unittest.IsolatedAsyncioTestCase):
    async def test_store_papers_wraps_store_acquired_paper(self):
        async def fake_store(meta, *, force=False):
            return {
                "id": "lib-9",
                "title": meta["title"],
                "embedded": True,
                "deduped": False,
            }

        with (
            patch.object(brain, "store_acquired_paper", fake_store),
            patch.object(
                brain, "get_pool", AsyncMock(return_value=_Pool(_FetchConnection([])))
            ),
            patch.object(brain.venues, "match", AsyncMock(return_value=None)),
        ):
            out = await brain.store_papers(
                # abstract present -> skip the crossref enrich (no network in tests)
                [{"title": "A Paper", "doi": "10.1/p", "year": 2019, "abstract": "x"}]
            )
        self.assertEqual(out["stored"], 1)
        self.assertEqual(out["deduped"], 0)
        self.assertEqual(
            out["results"],
            [
                {
                    "id": "lib-9",
                    "title": "A Paper",
                    "authors": [],
                    "year": 2019,
                    "doi": "10.1/p",
                    "venue": None,
                    "bibtex": None,
                    "deduped": False,
                }
            ],
        )


class _DeleteConnection:
    def __init__(self, row=None):
        self.row = row
        self.executed = []

    async def fetchrow(self, query, *args):
        return self.row

    async def execute(self, query, *args):
        self.executed.append((query, args))


class DeletePaperTests(unittest.IsolatedAsyncioTestCase):
    async def test_deletes_by_uuid(self):
        conn = _DeleteConnection(row={"id": "u-1", "title": "T"})
        with patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(conn))):
            res = await brain.delete_paper(
                paper_id="11111111-1111-1111-1111-111111111111"
            )
        self.assertEqual(res, {"deleted": 1, "id": "u-1", "title": "T"})

    async def test_uuid_no_match(self):
        conn = _DeleteConnection(row=None)
        with patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(conn))):
            res = await brain.delete_paper(
                paper_id="11111111-1111-1111-1111-111111111111"
            )
        self.assertEqual(res, {"deleted": 0, "reason": "no match"})

    async def test_deletes_by_reference_single_match(self):
        conn = _DeleteConnection()
        with (
            patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(conn))),
            patch.object(
                brain,
                "resolve_reference",
                AsyncMock(return_value=[{"id": "u-9", "title": "A Paper"}]),
            ),
        ):
            res = await brain.delete_paper(title="A Paper")
        self.assertEqual(res, {"deleted": 1, "id": "u-9", "title": "A Paper"})
        self.assertTrue(any("DELETE FROM library" in q for q, _ in conn.executed))

    async def test_refuses_ambiguous_reference(self):
        conn = _DeleteConnection()
        with (
            patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(conn))),
            patch.object(
                brain,
                "resolve_reference",
                AsyncMock(
                    return_value=[
                        {"id": "u-1", "title": "A"},
                        {"id": "u-2", "title": "B"},
                    ]
                ),
            ),
        ):
            res = await brain.delete_paper(title="shared")
        self.assertEqual(res["deleted"], 0)
        self.assertEqual({m["id"] for m in res["multiple"]}, {"u-1", "u-2"})
        self.assertEqual(conn.executed, [])  # nothing deleted


class GetCitationsTests(unittest.IsolatedAsyncioTestCase):
    """brain.get_citations resolves the input to an identifier/descriptor and
    forwards it to papers.citation_neighbors (which walks the graph via citer).
    The fuzzy-threshold and graph behavior itself is covered in test_papers.py."""

    async def test_wid_forwarded_as_openalex_id(self):
        captured = {}

        def fake(**kw):
            captured.update(kw)
            return [{"title": "Neighbor", "doi": "10.1/n"}]

        with patch.object(brain.papers, "citation_neighbors", fake):
            out = await brain.get_citations("W1")
        self.assertEqual(captured["openalex_id"], "W1")
        self.assertNotIn("doi", captured)
        self.assertEqual(out[0]["doi"], "10.1/n")

    async def test_uuid_forwards_stored_doi(self):
        conn = _DeleteConnection(row={"doi": "10.1/x"})
        captured = {}

        def fake(**kw):
            captured.update(kw)
            return [{"title": "N", "doi": "10.1/n"}]

        with (
            patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(conn))),
            patch.object(brain.papers, "citation_neighbors", fake),
        ):
            out = await brain.get_citations("11111111-1111-1111-1111-111111111111")
        self.assertEqual(captured["doi"], "10.1/x")
        self.assertEqual(out[0]["title"], "N")

    async def test_uuid_missing_row_returns_empty(self):
        conn = _DeleteConnection(row=None)
        with patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(conn))):
            out = await brain.get_citations("11111111-1111-1111-1111-111111111111")
        self.assertEqual(out, [])

    async def test_doi_less_forwards_title_descriptor(self):
        conn = _DeleteConnection(
            row={
                "doi": None,
                "title": "Endogenous Property Rights",
                "authors": ["Jane Doe"],
                "year": 2011,
            }
        )
        captured = {}

        def fake(**kw):
            captured.update(kw)
            return [{"title": "N", "doi": "10.1/n"}]

        with (
            patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(conn))),
            patch.object(brain.papers, "citation_neighbors", fake),
        ):
            out = await brain.get_citations("11111111-1111-1111-1111-111111111111")
        self.assertEqual(captured["title"], "Endogenous Property Rights")
        self.assertEqual(captured["year"], 2011)
        self.assertEqual(captured["authors"], ["Jane Doe"])
        self.assertNotIn("doi", captured)
        self.assertEqual(out[0]["title"], "N")

    async def test_bare_doi_forwarded_normalized(self):
        captured = {}

        def fake(**kw):
            captured.update(kw)
            return []

        with patch.object(brain.papers, "citation_neighbors", fake):
            out = await brain.get_citations("https://doi.org/10.1257/AER.123")
        self.assertEqual(
            captured["doi"], "10.1257/aer.123"
        )  # prefix stripped, lowercased
        self.assertEqual(out, [])


class RerankTests(unittest.IsolatedAsyncioTestCase):
    _CFG = {"enabled": True, "provider": "litellm", "model": "bge-reranker"}

    async def test_disabled_returns_none(self):
        with patch.object(brain, "RERANK_CONFIG", {"enabled": False}):
            self.assertIsNone(await brain.rerank("q", ["a", "b"], 2))

    async def test_empty_documents_returns_none(self):
        with patch.object(brain, "RERANK_CONFIG", self._CFG):
            self.assertIsNone(await brain.rerank("q", [], 5))

    async def test_reorders_and_caps_to_top_n(self):
        async def fake(query, documents, top_n, config):
            return [2, 0, 1]

        with (
            patch.object(brain, "RERANK_CONFIG", self._CFG),
            patch.object(brain, "_rerank_with_config", fake),
        ):
            order = await brain.rerank("q", ["a", "b", "c"], 2)
        self.assertEqual(order, [2, 0])

    async def test_drops_out_of_range_index(self):
        async def fake(query, documents, top_n, config):
            return [5, 1, 0]  # 5 is out of range for 2 documents

        with (
            patch.object(brain, "RERANK_CONFIG", self._CFG),
            patch.object(brain, "_rerank_with_config", fake),
        ):
            order = await brain.rerank("q", ["a", "b"], 2)
        self.assertEqual(order, [1, 0])

    async def test_falls_back_to_none_on_error(self):
        async def boom(*args, **kwargs):
            raise RuntimeError("reranker down")

        with (
            patch.object(brain, "RERANK_CONFIG", self._CFG),
            patch.object(brain, "_rerank_with_config", boom),
        ):
            self.assertIsNone(await brain.rerank("q", ["a", "b"], 2))

    async def test_uses_fallback_provider(self):
        cfg = {
            "enabled": True,
            "provider": "litellm",
            "model": "primary",
            "fallback": {"provider": "litellm", "model": "secondary"},
        }
        calls = []

        async def fake(query, documents, top_n, config):
            calls.append(config["model"])
            if config["model"] == "primary":
                raise RuntimeError("down")
            return [0, 1]

        with (
            patch.object(brain, "RERANK_CONFIG", cfg),
            patch.object(brain, "_rerank_with_config", fake),
        ):
            order = await brain.rerank("q", ["a", "b"], 2)
        self.assertEqual(order, [0, 1])
        self.assertEqual(calls, ["primary", "secondary"])

    def test_candidate_count_overfetches_only_when_enabled(self):
        with patch.object(brain, "RERANK_CONFIG", {"enabled": False}):
            self.assertEqual(brain._candidate_count(5), 5)
        with patch.object(brain, "RERANK_CONFIG", {"enabled": True, "candidates": 30}):
            self.assertEqual(brain._candidate_count(5), 30)
            self.assertEqual(brain._candidate_count(40), 40)  # count beats candidates

    async def test_reranked_reorders_rows_via_text_of(self):
        rows = [{"t": "a"}, {"t": "b"}, {"t": "c"}]

        async def fake(query, documents, top_n, config):
            self.assertEqual(documents, ["a", "b", "c"])  # text_of applied
            return [2, 0]

        with (
            patch.object(brain, "RERANK_CONFIG", self._CFG),
            patch.object(brain, "_rerank_with_config", fake),
        ):
            out = await brain._reranked("q", rows, lambda r: r["t"], 2)
        self.assertEqual(out, [{"t": "c"}, {"t": "a"}])

    async def test_reranked_falls_back_to_cosine_order(self):
        rows = [{"t": "a"}, {"t": "b"}, {"t": "c"}]
        with patch.object(brain, "RERANK_CONFIG", {"enabled": False}):
            out = await brain._reranked("q", rows, lambda r: r["t"], 2)
        self.assertEqual(out, [{"t": "a"}, {"t": "b"}])  # cosine order, trimmed

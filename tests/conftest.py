"""Shared test bootstrap: stub the database/embedding modules and provide a
dummy DATABASE_URL so openbrain.brain imports without a live Postgres. Mirrors
the inline setup historically done at the top of tests/test_brain.py so any test
module (e.g. test_acquire) can import brain-backed code safely offline.

The gated real-Postgres tier (tests/test_real_pg.py) needs the genuine asyncpg /
pgvector drivers instead of these stubs. When ``OPENBRAIN_TEST_DATABASE_URL`` is
set, import the real drivers here so they populate ``sys.modules`` before any
test module's inline ``setdefault`` stub can shadow them, and point
``DATABASE_URL`` at the operator-supplied database. Default runs stay fully
offline.
"""

import os
import sys
import types
from unittest.mock import AsyncMock

_REAL_PG_URL = os.environ.get("OPENBRAIN_TEST_DATABASE_URL")

if _REAL_PG_URL:
    # Real-Postgres tier: load the genuine drivers so brain talks to a live DB.
    import asyncpg  # noqa: F401
    import pgvector.asyncpg  # noqa: F401

    os.environ["DATABASE_URL"] = _REAL_PG_URL
else:
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

# Default reranking OFF for the suite: it's an opt-in feature whose live config
# (config.yaml) ships enabled, but search tests assert cosine behaviour and must
# not make real /rerank calls. RerankTests re-enable it per-test via patch.object.
from openbrain import brain  # noqa: E402

brain.RERANK_CONFIG = {}

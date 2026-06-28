"""Shared test bootstrap: stub the database/embedding modules and provide a
dummy DATABASE_URL so openbrain.brain imports without a live Postgres. Mirrors
the inline setup historically done at the top of tests/test_brain.py so any test
module (e.g. test_acquire) can import brain-backed code safely offline.
"""

import os
import sys
import types
from unittest.mock import AsyncMock

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

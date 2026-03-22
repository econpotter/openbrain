"""
brain.py — core logic: embed, store, retrieve.

All MCP tools call into this module. Database pool and embedding model
are initialized lazily on first use.

Embedding backend is controlled by EMBED_BACKEND in .env:
  local   — sentence-transformers running on this machine (default, free)
  voyage  — Voyage AI API (requires VOYAGE_API_KEY)
"""

import asyncio
import os
import asyncpg
import pgvector.asyncpg
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]
EMBED_BACKEND = os.getenv("EMBED_BACKEND", "local")
EMBED_MODEL_LOCAL = os.getenv("EMBED_MODEL_LOCAL", "BAAI/bge-large-en-v1.5")
VOYAGE_API_KEY = os.getenv("VOYAGE_API_KEY", "")
VOYAGE_MODEL = os.getenv("VOYAGE_MODEL", "voyage-large-2")

_pool: asyncpg.Pool | None = None
_st_model = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            DATABASE_URL,
            init=pgvector.asyncpg.register_vector,
        )
    return _pool


def _load_st_model():
    global _st_model
    if _st_model is None:
        from sentence_transformers import SentenceTransformer
        _st_model = SentenceTransformer(EMBED_MODEL_LOCAL)
    return _st_model


async def embed(text: str) -> list[float]:
    if EMBED_BACKEND == "voyage":
        import voyageai
        client = voyageai.AsyncClient(api_key=VOYAGE_API_KEY)
        result = await client.embed([text], model=VOYAGE_MODEL)
        return result.embeddings[0]
    else:
        loop = asyncio.get_event_loop()
        model = await loop.run_in_executor(None, _load_st_model)
        embedding = await loop.run_in_executor(None, lambda: model.encode(text).tolist())
        return embedding


async def store_thought(content: str, metadata: dict | None = None) -> str:
    """Embed and persist a thought. Returns the new UUID as a string."""
    pool = await get_pool()
    embedding = await embed(content)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO thoughts (content, embedding, metadata)
            VALUES ($1, $2, $3)
            RETURNING id
            """,
            content,
            embedding,
            metadata or {},
        )
    return str(row["id"])


async def search_thoughts_vec(
    query: str,
    threshold: float = 0.5,
    count: int = 10,
) -> list[dict]:
    """Semantic search over thoughts. Returns rows with similarity score."""
    pool = await get_pool()
    embedding = await embed(query)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM match_thoughts($1, $2, $3)",
            embedding,
            threshold,
            count,
        )
    return [dict(r) for r in rows]


async def recent_thoughts(since_hours: int = 24, limit: int = 50) -> list[dict]:
    """Return thoughts created in the last N hours, newest first."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, content, metadata, created_at
            FROM thoughts
            WHERE created_at >= now() - make_interval(hours := $1)
            ORDER BY created_at DESC
            LIMIT $2
            """,
            since_hours,
            limit,
        )
    return [dict(r) for r in rows]


async def table_stats() -> dict:
    """Return row counts and recent activity metrics."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        thoughts_total = await conn.fetchval("SELECT count(*) FROM thoughts")
        thoughts_24h = await conn.fetchval(
            "SELECT count(*) FROM thoughts "
            "WHERE created_at >= now() - interval '24 hours'"
        )
        try:
            library_total = await conn.fetchval("SELECT count(*) FROM library")
        except Exception:
            library_total = None
    return {
        "thoughts_total": thoughts_total,
        "thoughts_last_24h": thoughts_24h,
        "library_total": library_total,
    }

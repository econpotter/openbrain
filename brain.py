"""
brain.py — core logic: embed, store, retrieve.

All MCP tools call into this module. Database pool and embedding client
are initialized lazily on first use.
"""

import os
import asyncpg
import pgvector.asyncpg
import voyageai
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]
VOYAGE_API_KEY = os.environ["VOYAGE_API_KEY"]
EMBED_MODEL = os.getenv("EMBED_MODEL", "voyage-large-2")

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            DATABASE_URL,
            init=pgvector.asyncpg.register_vector,
        )
    return _pool


async def embed(text: str) -> list[float]:
    client = voyageai.AsyncClient(api_key=VOYAGE_API_KEY)
    result = await client.embed([text], model=EMBED_MODEL)
    return result.embeddings[0]


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

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


async def _entity_text(table: str, row: dict) -> str:
    """Concatenate the meaningful text fields of a people or projects row for embedding."""
    if table == "people":
        parts = [row.get("name") or "", row.get("affiliation") or "",
                 row.get("relationship") or "", row.get("notes") or ""]
    else:
        parts = [row.get("name") or "", row.get("description") or ""]
    return " ".join(p for p in parts if p)


async def list_people(search: str | None = None) -> list[dict]:
    """Return all people, optionally filtered by name."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if search:
            rows = await conn.fetch(
                "SELECT id, name, affiliation, relationship, notes FROM people "
                "WHERE name ILIKE $1 ORDER BY name",
                f"%{search}%",
            )
        else:
            rows = await conn.fetch(
                "SELECT id, name, affiliation, relationship, notes FROM people ORDER BY name"
            )
    return [dict(r) for r in rows]


async def update_person(person_id: str, updates: dict) -> dict | None:
    """Update fields on a person row. Refreshes embedding. Returns updated row or None."""
    allowed = {"name", "affiliation", "relationship", "notes"}
    fields = {k: v for k, v in updates.items() if k in allowed}
    if not fields:
        return None
    pool = await get_pool()
    set_clause = ", ".join(f"{k} = ${i+2}" for i, k in enumerate(fields))
    values = [person_id] + list(fields.values())
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"UPDATE people SET {set_clause} WHERE id = $1 "
            "RETURNING id, name, affiliation, relationship, notes",
            *values,
        )
    if row:
        updated = dict(row)
        text = await _entity_text("people", updated)
        emb = await embed(text)
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE people SET embedding = $1 WHERE id = $2", emb, person_id
            )
        return updated
    return None


async def search_people_vec(query: str, threshold: float = 0.3, count: int = 10) -> list[dict]:
    """Semantic search over people."""
    pool = await get_pool()
    embedding = await embed(query)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM match_people($1, $2, $3)", embedding, threshold, count
        )
    return [dict(r) for r in rows]


async def find_person_by_name(name: str) -> list[dict]:
    """Find people whose name matches (case-insensitive)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, name, affiliation, relationship, notes FROM people "
            "WHERE name ILIKE $1 ORDER BY name",
            f"%{name}%",
        )
    return [dict(r) for r in rows]


async def list_projects(status: str | None = None) -> list[dict]:
    """Return all projects, optionally filtered by status."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if status:
            rows = await conn.fetch(
                "SELECT id, name, description, status, start_date FROM projects "
                "WHERE status = $1 ORDER BY name",
                status,
            )
        else:
            rows = await conn.fetch(
                "SELECT id, name, description, status, start_date FROM projects ORDER BY name"
            )
    return [dict(r) for r in rows]


async def update_project(project_id: str, updates: dict) -> dict | None:
    """Update fields on a project row. Refreshes embedding. Returns updated row or None."""
    allowed = {"name", "description", "status", "start_date"}
    fields = {k: v for k, v in updates.items() if k in allowed}
    if not fields:
        return None
    pool = await get_pool()
    set_clause = ", ".join(f"{k} = ${i+2}" for i, k in enumerate(fields))
    values = [project_id] + list(fields.values())
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"UPDATE projects SET {set_clause} WHERE id = $1 "
            "RETURNING id, name, description, status, start_date",
            *values,
        )
    if row:
        updated = dict(row)
        text = await _entity_text("projects", updated)
        emb = await embed(text)
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE projects SET embedding = $1 WHERE id = $2", emb, project_id
            )
        return updated
    return None


async def search_projects_vec(query: str, threshold: float = 0.3, count: int = 10) -> list[dict]:
    """Semantic search over projects."""
    pool = await get_pool()
    embedding = await embed(query)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM match_projects($1, $2, $3)", embedding, threshold, count
        )
    return [dict(r) for r in rows]


async def find_project_by_name(name: str) -> list[dict]:
    """Find projects whose name matches (case-insensitive)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, name, description, status, start_date FROM projects "
            "WHERE name ILIKE $1 ORDER BY name",
            f"%{name}%",
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

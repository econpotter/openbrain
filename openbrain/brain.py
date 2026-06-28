"""
brain.py — core logic: embed, store, retrieve.

All MCP tools call into this module. Database pool is initialized lazily on
first use. Configuration is loaded from config.yaml (non-secrets) and .env
(API keys and DATABASE_URL).
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import socket
import urllib.request
import urllib.error
from pathlib import Path

import asyncpg
import bibtexparser
import pgvector.asyncpg
import yaml
from dotenv import load_dotenv

from openbrain import sources
from openbrain import venues as venues_mod

load_dotenv()

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"


def _load_config() -> dict:
    if not _CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"config.yaml not found at {_CONFIG_PATH}. "
            "Copy config.yaml.example and fill it in."
        )
    with open(_CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    if not cfg:
        raise ValueError("config.yaml is empty or invalid.")
    return cfg


_cfg = _load_config()

DATABASE_URL: str = os.environ["DATABASE_URL"]
# library_path in config.yaml is the host-side path (used by scripts/backup.sh,
# which runs on the host). Inside the Docker container the same files are bind-
# mounted at a different path, so the server reads OPENBRAIN_LIBRARY_PATH when
# set and falls back to config.yaml otherwise. Without this override the server
# would try to write ingested documents to the host path inside the container,
# which is not mounted there and fails with EACCES.
LIBRARY_PATH: Path = Path(
    os.environ.get("OPENBRAIN_LIBRARY_PATH") or _cfg["library_path"]
).expanduser()
INGEST_CONFIG: dict = _cfg.get("ingest", {})
INGEST_STAGING_PATH: Path = Path(
    INGEST_CONFIG.get("staging_path", "/tmp/openbrain-ingest")
).expanduser()
EXTRACTION_CONFIG: dict = _cfg.get("extraction", {})

# Per-request embedding timeout (seconds). Without it a dead or unsupported
# endpoint (e.g. OpenRouter, which has no /embeddings route) hangs until the
# default client timeout, blocking every capture. Override via embed.timeout.
EMBED_TIMEOUT: float = float(_cfg.get("embed", {}).get("timeout", 30))

_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "nor",
        "for",
        "yet",
        "so",
        "of",
        "in",
        "on",
        "at",
        "to",
        "by",
        "up",
        "as",
        "is",
        "it",
        "its",
        "from",
        "with",
        "into",
        "onto",
        "upon",
        "over",
        "under",
        "about",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "shall",
        "can",
        "this",
        "that",
        "these",
        "those",
        "their",
        "not",
        "no",
        "via",
        "per",
        "vs",
        "between",
        "each",
    }
)

_DOI_RE = re.compile(r"\b(10\.\d{4,}(?:\.\d+)*/\S+)\b")

_pool: asyncpg.Pool | None = None


def _parse_bibtex(bibtex_str: str) -> dict:
    bib = bibtexparser.loads(bibtex_str)
    if not bib.entries:
        return {}
    entry = bib.entries[0]

    raw_authors = entry.get("author", "")
    authors = []
    if raw_authors:
        for part in re.split(r"\s+and\s+", raw_authors, flags=re.IGNORECASE):
            part = part.strip()
            if "," in part:
                last, first = part.split(",", 1)
                authors.append(f"{first.strip()} {last.strip()}")
            else:
                authors.append(part)

    year_str = entry.get("year", "")
    year = int(year_str) if year_str.isdigit() else None

    source = entry.get("journal") or entry.get("booktitle") or entry.get("publisher")

    return {
        "title": entry.get("title", "").strip("{}"),
        "authors": authors,
        "year": year,
        "source": source,
        "abstract": entry.get("abstract"),
        "doi": entry.get("doi"),
        "bibtex": bibtex_str,
    }


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            DATABASE_URL,
            init=pgvector.asyncpg.register_vector,
        )
    return _pool


def _configs_with_fallback(config: dict) -> list[dict]:
    fallback = config.get("fallback")
    return [config, fallback] if fallback else [config]


def _litellm_url(config: dict) -> str | None:
    return config.get("litellm_url") or _cfg.get("litellm_url")


async def _embed_with_config(text: str, config: dict) -> list[float]:
    provider = config["provider"]
    model = config["model"]
    if provider == "voyage":
        import voyageai

        client = voyageai.AsyncClient(api_key=os.environ["VOYAGE_API_KEY"])
        result = await client.embed([text], model=model)
        return result.embeddings[0]

    if provider == "openrouter":
        from openai import AsyncOpenAI

        client = AsyncOpenAI(
            api_key=os.environ["OPENROUTER_API_KEY"],
            base_url="https://openrouter.ai/api/v1",
            timeout=EMBED_TIMEOUT,
        )
        result = await client.embeddings.create(input=[text], model=model)
        return result.data[0].embedding

    if provider == "litellm":
        from openai import AsyncOpenAI

        litellm_url = _litellm_url(config)
        if not litellm_url:
            raise ValueError(
                "config.yaml: litellm_url is required when embed.provider is litellm"
            )
        client = AsyncOpenAI(
            api_key=os.environ.get("LITELLM_API_KEY", "local-litellm"),
            base_url=litellm_url,
            timeout=EMBED_TIMEOUT,
        )
        result = await client.embeddings.create(input=[text], model=model)
        return result.data[0].embedding

    raise ValueError(
        f"Unknown embed.provider: {provider!r}. "
        "Expected one of: voyage, openrouter, litellm"
    )


def _apply_task_prefix(text: str, task: str) -> str:
    """
    Prepend the nomic-style task prefix for `task` ("document" | "query"), read
    from config (embed.document_prefix / embed.query_prefix). Empty/missing
    prefixes are a no-op, so providers that use an input_type parameter instead
    (voyage, openai) are unaffected.
    """
    key = "query_prefix" if task == "query" else "document_prefix"
    prefix = _cfg["embed"].get(key, "")
    return f"{prefix}{text}" if prefix else text


async def embed(text: str, task: str = "document") -> list[float]:
    text = _apply_task_prefix(text, task)
    errors = []
    for config in _configs_with_fallback(_cfg["embed"]):
        try:
            return await asyncio.wait_for(
                _embed_with_config(text, config),
                timeout=EMBED_TIMEOUT + 5,
            )
        except asyncio.TimeoutError:
            errors.append(
                f"{config['provider']}/{config['model']}: timed out after "
                f"{EMBED_TIMEOUT + 5}s"
            )
        except Exception as exc:
            errors.append(f"{config['provider']}/{config['model']}: {exc}")
    raise RuntimeError("All embedding providers failed: " + "; ".join(errors))


async def _embed_or_none(text: str, task: str = "document") -> list[float] | None:
    """
    Best-effort embedding for the write path. Returns the vector on success, or
    None if the embed model is unavailable. Callers store the chunk text anyway
    (NULL embedding); the maintenance backfill sweep embeds it later. This keeps
    fetch/ingest from crashing on an embed outage, and avoids substituting a
    different model whose vectors would be incompatible with the index.
    """
    try:
        return await embed(text, task=task)
    except Exception as exc:
        logger.warning("Embedding deferred (model unavailable): %s", exc)
        return None


async def _enriched_entity_text(table: str, entity_id: str) -> str:
    pool = await get_pool()
    if table == "people":
        async with pool.acquire() as conn:
            row = dict(
                await conn.fetchrow(
                    "SELECT name, affiliation, relationship, notes FROM people WHERE id = $1",
                    entity_id,
                )
            )
        base = await _entity_text("people", row)
        name, kind = row["name"], "person"
    else:
        async with pool.acquire() as conn:
            row = dict(
                await conn.fetchrow(
                    "SELECT name, description FROM projects WHERE id = $1", entity_id
                )
            )
        base = await _entity_text("projects", row)
        name, kind = row["name"], "project"

    thoughts = await thoughts_mentioning(name, kind, limit=10)
    if thoughts:
        snippets = " ".join(t["content"][:200] for t in thoughts)
        return f"{base}. Related: {snippets}"
    return base


async def _refresh_entity_embedding(table: str, entity_id: str) -> None:
    text = await _enriched_entity_text(table, entity_id)
    emb = await embed(text)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE {table} SET embedding = $1 WHERE id = $2", emb, entity_id
        )


async def _refresh_mentioned_entities(people: list[str], projects: list[str]) -> None:
    pool = await get_pool()
    for name in people:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id FROM people WHERE name ILIKE $1", f"%{name}%"
            )
        for row in rows:
            try:
                await _refresh_entity_embedding("people", str(row["id"]))
            except Exception:
                pass
    for name in projects:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id FROM projects WHERE name ILIKE $1", f"%{name}%"
            )
        for row in rows:
            try:
                await _refresh_entity_embedding("projects", str(row["id"]))
            except Exception:
                pass


async def add_link(
    source_table: str,
    source_id: str,
    target_table: str,
    target_id: str,
    relationship: str,
    metadata: dict | None = None,
) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO links (source_table, source_id, target_table, target_id, relationship, metadata)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb)
            ON CONFLICT (source_table, source_id, target_table, target_id, relationship) DO NOTHING
            """,
            source_table,
            source_id,
            target_table,
            target_id,
            relationship,
            json.dumps(metadata or {}),
        )


async def _link_thought_entities(
    thought_id: str, people: list[str], projects: list[str]
) -> None:
    for name in people:
        matches = await find_person_by_name(name)
        if len(matches) == 1:
            await add_link(
                "thoughts", thought_id, "people", str(matches[0]["id"]), "mentions"
            )
    for name in projects:
        matches = await find_project_by_name(name)
        if len(matches) == 1:
            await add_link(
                "thoughts", thought_id, "projects", str(matches[0]["id"]), "mentions"
            )


async def backfill_links(limit: int = 1000) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, metadata FROM thoughts
            WHERE (metadata->'people' IS NOT NULL OR metadata->'projects' IS NOT NULL)
              AND NOT EXISTS (
                  SELECT 1 FROM links
                  WHERE links.source_table = 'thoughts' AND links.source_id = thoughts.id
              )
            ORDER BY created_at
            LIMIT $1
            """,
            limit,
        )

    processed = 0
    linked = 0
    for row in rows:
        meta = row["metadata"]
        if isinstance(meta, str):
            meta = json.loads(meta)
        people = meta.get("people") or []
        projects = meta.get("projects") or []
        if not people and not projects:
            continue
        before = await _count_links("thoughts", str(row["id"]))
        await _link_thought_entities(str(row["id"]), people, projects)
        after = await _count_links("thoughts", str(row["id"]))
        linked += after - before
        processed += 1

    return {"processed": processed, "linked": linked}


async def _count_links(table: str, entity_id: str) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT count(*) FROM links WHERE source_table = $1 AND source_id = $2",
            table,
            entity_id,
        )


async def traverse_graph(
    table: str, entity_id: str, depth: int = 2, limit: int = 50
) -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM traverse_links($1, $2, $3, $4)",
            table,
            entity_id,
            depth,
            limit,
        )

    results = []
    for row in rows:
        node_table = row["node_table"]
        node_id = row["node_id"]
        async with pool.acquire() as conn:
            if node_table == "thoughts":
                rec = await conn.fetchrow(
                    "SELECT content FROM thoughts WHERE id = $1", node_id
                )
                label = rec["content"][:200] if rec else str(node_id)
            elif node_table in ("people", "projects"):
                rec = await conn.fetchrow(
                    f"SELECT name FROM {node_table} WHERE id = $1", node_id
                )
                label = rec["name"] if rec else str(node_id)
            else:
                label = str(node_id)
        results.append(
            {
                "table": node_table,
                "id": str(node_id),
                "label": label,
                "relationship": row["relationship"],
                "depth": row["depth"],
            }
        )
    return results


async def resolve_entity(name: str) -> dict | None:
    person_matches = await find_person_by_name(name)
    project_matches = await find_project_by_name(name)
    total = len(person_matches) + len(project_matches)
    if total == 0:
        return None
    if total > 1:
        return {
            "multiple": [r["name"] for r in person_matches]
            + [r["name"] for r in project_matches]
        }
    if person_matches:
        return {"table": "people", **dict(person_matches[0])}
    return {"table": "projects", **dict(project_matches[0])}


async def refresh_all_embeddings(entity_type: str = "all") -> dict:
    pool = await get_pool()
    counts: dict = {}
    if entity_type in ("all", "people"):
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT id FROM people")
        for row in rows:
            await _refresh_entity_embedding("people", str(row["id"]))
        counts["people"] = len(rows)
    if entity_type in ("all", "projects"):
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT id FROM projects")
        for row in rows:
            await _refresh_entity_embedding("projects", str(row["id"]))
        counts["projects"] = len(rows)
    return counts


async def store_thought(content: str, metadata: dict | None = None) -> str:
    pool = await get_pool()
    embedding = await _embed_or_none(content)
    meta = metadata or {}
    thought_type = meta.get("type")
    async with pool.acquire() as conn:
        result = await conn.fetchval(
            """
            SELECT upsert_thought($1, $2, $3::jsonb, $4)
            """,
            content,
            embedding,
            json.dumps(meta),
            thought_type,
        )
    # upsert_thought RETURNS JSONB; asyncpg gives it back as a str (no jsonb
    # codec registered on the pool), so decode before subscripting.
    if isinstance(result, str):
        result = json.loads(result)
    thought_id = str(result["id"])

    people = meta.get("people") or []
    projects = meta.get("projects") or []
    if result["metadata_changed"] and (people or projects):
        asyncio.create_task(_refresh_mentioned_entities(people, projects))
        asyncio.create_task(_link_thought_entities(thought_id, people, projects))

    return thought_id


async def search_thoughts_vec(
    query: str,
    threshold: float = 0.5,
    count: int = 10,
) -> list[dict]:
    pool = await get_pool()
    embedding = await embed(query, task="query")
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM match_thoughts($1, $2, $3)",
            embedding,
            threshold,
            count,
        )
    return [dict(r) for r in rows]


async def get_thought_connections(
    thought_id: str,
    limit: int = 20,
    include_restricted: bool = False,
) -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM get_thought_connections($1, $2, $3)",
            thought_id,
            limit,
            not include_restricted,
        )
    return [dict(r) for r in rows]


async def recent_thoughts(since_hours: int = 24, limit: int = 50) -> list[dict]:
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


async def extract_thoughts(
    since_hours: int = 24,
    limit: int = 50,
) -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, content, metadata, created_at
            FROM thoughts
            WHERE created_at >= now() - make_interval(hours := $1)
              AND type NOT IN ('synthesis', 'archived')
              AND COALESCE(metadata->>'archived', 'false') <> 'true'
            ORDER BY created_at ASC
            LIMIT $2
            """,
            since_hours,
            limit,
        )
    return [dict(r) for r in rows]


def _extraction_prompt(thoughts: list[dict]) -> str:
    lines = [
        "Consolidate these openbrain memory records.",
        "",
        "Tasks:",
        "- Deduplicate redundant entries.",
        "- Resolve conflicts cautiously; say when evidence is uncertain.",
        "- Extract durable facts, project milestones, decisions, and constraints.",
        "- Omit transient chatter and implementation noise.",
        "",
        "Return concise prose or bullets suitable for long-term memory.",
        "",
        "Records:",
    ]
    for i, thought in enumerate(thoughts, start=1):
        metadata = thought.get("metadata") or {}
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        metadata_json = json.dumps(metadata, sort_keys=True)
        lines.append(f"{i}. id={thought['id']} metadata={metadata_json}")
        lines.append(str(thought["content"]))
        lines.append("")
    return "\n".join(lines)


async def _complete_extraction_with_config(prompt: str, config: dict) -> str:
    provider = config.get("provider", "litellm")
    model = config.get("model")
    if not model:
        raise ValueError("config.yaml: extraction.model is required")

    if provider == "litellm":
        base_url = _litellm_url(config)
        api_key_name = "LITELLM_API_KEY"
        if not base_url:
            raise ValueError(
                "config.yaml: litellm_url is required "
                "when extraction.provider is litellm"
            )
    elif provider == "openrouter":
        base_url = "https://openrouter.ai/api/v1"
        api_key_name = "OPENROUTER_API_KEY"
    else:
        raise ValueError(
            f"Unknown extraction.provider: {provider!r}. "
            "Expected one of: openrouter, litellm"
        )

    from openai import AsyncOpenAI

    api_key = os.environ.get(api_key_name)
    if provider == "litellm":
        api_key = api_key or "local-litellm"
    if not api_key:
        raise KeyError(api_key_name)

    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    result = await client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": (
                    "You consolidate a personal research memory log. "
                    "Preserve only information likely to matter later."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        temperature=config.get("temperature", 0.2),
    )
    return (result.choices[0].message.content or "").strip()


async def complete_extraction_prompt(prompt: str) -> str:
    errors = []
    for config in _configs_with_fallback(EXTRACTION_CONFIG):
        try:
            return await _complete_extraction_with_config(prompt, config)
        except Exception as exc:
            errors.append(f"{config['provider']}/{config['model']}: {exc}")
    raise RuntimeError("All extraction providers failed: " + "; ".join(errors))


async def archive_thoughts(thought_ids: list[str], extraction_run_id: str) -> None:
    if not thought_ids:
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE thoughts
            SET metadata = metadata || jsonb_build_object(
                    'archived', true,
                    'archived_at', now(),
                    'extraction_run_id', $2
                )
            WHERE id = ANY($1::uuid[])
            """,
            thought_ids,
            extraction_run_id,
        )


async def run_extraction_once() -> dict:
    since_hours = int(EXTRACTION_CONFIG.get("lookback_hours", 24))
    batch_size = int(EXTRACTION_CONFIG.get("batch_size", 50))
    thoughts = await extract_thoughts(since_hours, batch_size)
    if not thoughts:
        return {"processed": 0, "syntheses": 0}

    prompt = _extraction_prompt(thoughts)
    synthesis = await complete_extraction_prompt(prompt)
    if not synthesis:
        return {"processed": 0, "syntheses": 0}

    run_id = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
    thought_ids = [str(thought["id"]) for thought in thoughts]
    await store_thought(
        synthesis,
        {
            "type": "synthesis",
            "source": "openbrain-extraction",
            "source_type": "extraction",
            "thought_ids": thought_ids,
            "extraction_run_id": run_id,
        },
    )
    await archive_thoughts(thought_ids, run_id)
    return {"processed": len(thoughts), "syntheses": 1}


async def preview_extraction_once() -> dict:
    since_hours = int(EXTRACTION_CONFIG.get("lookback_hours", 24))
    batch_size = int(EXTRACTION_CONFIG.get("batch_size", 50))
    thoughts = await extract_thoughts(since_hours, batch_size)
    if not thoughts:
        return {"processed": 0, "synthesis": "", "thought_ids": []}

    prompt = _extraction_prompt(thoughts)
    synthesis = await complete_extraction_prompt(prompt)
    return {
        "processed": len(thoughts),
        "synthesis": synthesis,
        "thought_ids": [str(thought["id"]) for thought in thoughts],
    }


async def apply_extraction_synthesis(thought_ids: list[str], synthesis: str) -> dict:
    if not thought_ids or not synthesis.strip():
        return {"processed": 0, "syntheses": 0}

    run_key = "\n".join([synthesis.strip(), *sorted(thought_ids)])
    run_id = hashlib.sha256(run_key.encode("utf-8")).hexdigest()[:16]
    await store_thought(
        synthesis.strip(),
        {
            "type": "synthesis",
            "source": "openbrain-extraction",
            "source_type": "extraction",
            "thought_ids": thought_ids,
            "extraction_run_id": run_id,
        },
    )
    await archive_thoughts(thought_ids, run_id)
    return {"processed": len(thought_ids), "syntheses": 1}


async def _entity_text(table: str, row: dict) -> str:
    if table == "people":
        parts = [
            row.get("name") or "",
            row.get("affiliation") or "",
            row.get("relationship") or "",
            row.get("notes") or "",
        ]
    else:
        parts = [row.get("name") or "", row.get("description") or ""]
    return " ".join(p for p in parts if p)


async def add_person(
    name: str,
    affiliation: str | None = None,
    relationship: str | None = None,
    notes: str | None = None,
) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO people (name, affiliation, relationship, notes) "
            "VALUES ($1, $2, $3, $4) RETURNING id, name, affiliation, relationship, notes",
            name,
            affiliation,
            relationship,
            notes,
        )
    inserted = dict(row)
    text = await _entity_text("people", inserted)
    emb = await _embed_or_none(text)
    if emb is not None:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE people SET embedding = $1 WHERE id = $2",
                emb,
                str(inserted["id"]),
            )
    return inserted


async def add_project(
    name: str,
    description: str | None = None,
    status: str = "active",
    start_date: str | None = None,
) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO projects (name, description, status, start_date) "
            "VALUES ($1, $2, $3, $4) RETURNING id, name, description, status, start_date",
            name,
            description,
            status,
            start_date,
        )
    inserted = dict(row)
    text = await _entity_text("projects", inserted)
    emb = await _embed_or_none(text)
    if emb is not None:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE projects SET embedding = $1 WHERE id = $2",
                emb,
                str(inserted["id"]),
            )
    return inserted


async def list_people(search: str | None = None) -> list[dict]:
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
    allowed = {"name", "affiliation", "relationship", "notes", "follow_up_date"}
    fields = {k: v for k, v in updates.items() if k in allowed}
    if not fields:
        return None
    pool = await get_pool()
    set_clause = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(fields))
    values = [person_id] + list(fields.values())
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"UPDATE people SET {set_clause} WHERE id = $1 "
            "RETURNING id, name, affiliation, relationship, notes, follow_up_date",
            *values,
        )
    if row:
        updated = dict(row)
        text = await _entity_text("people", updated)
        emb = await _embed_or_none(text)
        if emb is not None:
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE people SET embedding = $1 WHERE id = $2", emb, person_id
                )
        return updated
    return None


async def search_people_vec(
    query: str, threshold: float = 0.3, count: int = 10
) -> list[dict]:
    pool = await get_pool()
    embedding = await embed(query, task="query")
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM match_people($1, $2, $3)", embedding, threshold, count
        )
    return [dict(r) for r in rows]


async def find_person_by_name(name: str) -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, name, affiliation, relationship, notes FROM people "
            "WHERE name ILIKE $1 ORDER BY name",
            f"%{name}%",
        )
    return [dict(r) for r in rows]


async def list_projects(status: str | None = None) -> list[dict]:
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
    allowed = {"name", "description", "status", "start_date"}
    fields = {k: v for k, v in updates.items() if k in allowed}
    if not fields:
        return None
    pool = await get_pool()
    set_clause = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(fields))
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
        emb = await _embed_or_none(text)
        if emb is not None:
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE projects SET embedding = $1 WHERE id = $2", emb, project_id
                )
        return updated
    return None


async def search_projects_vec(
    query: str, threshold: float = 0.3, count: int = 10
) -> list[dict]:
    pool = await get_pool()
    embedding = await embed(query, task="query")
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM match_projects($1, $2, $3)", embedding, threshold, count
        )
    return [dict(r) for r in rows]


async def find_project_by_name(name: str) -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, name, description, status, start_date FROM projects "
            "WHERE name ILIKE $1 ORDER BY name",
            f"%{name}%",
        )
    return [dict(r) for r in rows]


async def get_project_profile(name: str) -> dict | None:
    matches = await find_project_by_name(name)
    if not matches:
        return None
    if len(matches) > 1:
        return {"multiple": [r["name"] for r in matches]}
    project = dict(matches[0])

    thoughts = await thoughts_mentioning(project["name"], "project", limit=50)
    cooc = _extract_cooccurrences(thoughts, exclude=project["name"])
    docs = await find_library_records(project["name"])

    return {
        "project": project,
        "thoughts": thoughts,
        "cooccurring_people": cooc["people"],
        "cooccurring_projects": cooc["projects"],
        "library": docs,
    }


# ---------------------------------------------------------------------------
# Slug / filename utilities
# ---------------------------------------------------------------------------


def _clean_words(text: str) -> list[str]:
    return [
        w for w in re.sub(r"[^\w\s]", "", text.lower()).split() if w not in _STOPWORDS
    ]


def _title_slug(title: str, max_words: int = 5) -> str:
    return "-".join(_clean_words(title)[:max_words])


def _source_slug(source: str) -> str:
    return "-".join(w[:4] for w in _clean_words(source))


def _author_slug(authors: list[str]) -> str:
    def last(name: str) -> str:
        return name.split(",")[0].strip() if "," in name else name.strip().split()[-1]

    if not authors:
        return "Unknown"
    if len(authors) <= 2:
        return "".join(last(a).capitalize() for a in authors)
    return last(authors[0]).capitalize() + "ETAL"


def _make_filename(
    title: str, authors: list[str], year: int | None, source: str | None, ext: str
) -> str:
    parts = [f"{_author_slug(authors)}{year or ''}"]
    if source:
        slug = _source_slug(source)
        if slug:
            parts.append(slug)
    parts.append(_title_slug(title) or re.sub(r"[^\w]", "-", title.lower())[:40])
    name = re.sub(r"[^\w\-]", "", "-".join(parts))
    return name + ext


def _safe_staged_filename(filename: str) -> str:
    path = Path(filename)
    stem = re.sub(r"[^\w\-]+", "-", path.stem).strip("-") or "document"
    suffix = re.sub(r"[^\w.]", "", path.suffix.lower())
    return f"{stem}-{secrets.token_hex(4)}{suffix}"


def prepare_ingest_staging_path(filename: str) -> dict:
    INGEST_STAGING_PATH.mkdir(parents=True, exist_ok=True)
    staged_path = (INGEST_STAGING_PATH / _safe_staged_filename(filename)).resolve()
    host = INGEST_CONFIG.get("staging_host") or socket.gethostname()
    return {
        "filepath": str(staged_path),
        "host": host,
        "scp_destination": f"{host}:{staged_path}",
    }


def is_ingest_staged_path(filepath: str | Path) -> bool:
    path = Path(filepath).expanduser().resolve()
    staging_path = INGEST_STAGING_PATH.resolve()
    return path == staging_path or staging_path in path.parents


# ---------------------------------------------------------------------------
# PDF / text extraction and chunking
# ---------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def _extract_pdf(filepath: str) -> tuple[str, str]:
    import pdfplumber

    pages = []
    with pdfplumber.open(filepath) as pdf:
        for page in pdf.pages:
            pages.append(page.extract_text() or "")
    first = pages[0] if pages else ""
    return first, "\n".join(pages)


def _extract_doi(text: str) -> str | None:
    m = _DOI_RE.search(text)
    return m.group(1).rstrip(".,)") if m else None


# Canonical section names we recognize as headings. A line is treated as a
# heading only if it is short and, after stripping any leading numbering, either
# matches this vocabulary or is a short numbered line ("3.1 Experimental Setup").
_HEADING_VOCAB = {
    "abstract",
    "introduction",
    "background",
    "related work",
    "prior work",
    "motivation",
    "method",
    "methods",
    "methodology",
    "materials and methods",
    "approach",
    "model",
    "data",
    "dataset",
    "experiments",
    "experimental setup",
    "evaluation",
    "results",
    "findings",
    "analysis",
    "discussion",
    "limitations",
    "future work",
    "conclusion",
    "conclusions",
    "references",
    "bibliography",
    "acknowledgment",
    "acknowledgments",
    "acknowledgement",
    "acknowledgements",
    "appendix",
}

_NUMBERED_HEADING_RE = re.compile(r"^\s*\d+(?:\.\d+)*\.?\s+\S")


def _normalize_heading(line: str) -> str | None:
    """
    Return the canonical lowercased section name if `line` looks like a heading,
    else None. Best-effort: detects a known heading vocabulary plus short
    numbered headings, and ignores ordinary prose (too long) and numbered list
    items that run into a sentence.
    """
    s = line.strip()
    if not s or len(s.split()) > 8:
        return None
    body = re.sub(r"^\s*\d+(?:\.\d+)*\.?\s*", "", s).strip(" .:\t").lower()
    if body in _HEADING_VOCAB:
        return body
    if _NUMBERED_HEADING_RE.match(s):
        return body or None
    return None


def _split_sections(text: str) -> list[tuple[str, str]]:
    """
    Split `text` into (section_name, body) pairs on detected headings. Text
    before the first heading is labeled "body". If fewer than two headings are
    found, the whole document is returned as a single "body" section so the
    caller falls back to plain balanced chunking.
    """
    sections: list[tuple[str, list[str]]] = [("body", [])]
    heading_count = 0
    for line in text.split("\n"):
        name = _normalize_heading(line)
        if name:
            heading_count += 1
            sections.append((name, []))
        else:
            sections[-1][1].append(line)
    if heading_count < 2:
        return [("body", text)]
    result = []
    for name, buf in sections:
        body = "\n".join(buf).strip()
        if body:
            result.append((name, body))
    return result or [("body", text)]


def _rebalance(text: str, max_words: int, overlap: int) -> list[str]:
    """
    Split `text` into the fewest even pieces no longer than max_words, with each
    piece overlapping the next by `overlap` words. 900 words at max_words=300
    yields three ~300-word chunks, not 400/400/100.
    """
    words = text.split()
    if not words:
        return []
    n = max(1, -(-len(words) // max_words))
    bounds = [round(k * len(words) / n) for k in range(n + 1)]
    chunks = []
    for k in range(n):
        start = bounds[k]
        end = min(len(words), bounds[k + 1] + (overlap if k < n - 1 else 0))
        piece = " ".join(words[start:end])
        if piece.strip():
            chunks.append(piece)
    return chunks


def _chunk_document(text: str) -> list[tuple[str | None, str]]:
    """
    Section-aware body chunking for file ingest. Returns (section, chunk_text)
    pairs: split on detected sections (hard boundaries), drop configured
    low-value sections, merge stray short sections into the previous one, then
    even-rebalance each kept section into <= max_words chunks. `section` is None
    for unlabeled text. Config knobs live under `chunk` in config.yaml.
    """
    cfg = _cfg.get("chunk", {})
    max_words = cfg.get("max_words", 300)
    overlap = cfg.get("overlap", 50)
    min_section_words = cfg.get("min_section_words", 80)
    skip = {s.lower() for s in cfg.get("skip_sections", [])}

    kept: list[tuple[str, str]] = []
    for name, body in _split_sections(text):
        if name.lower() in skip:
            continue
        if len(body.split()) < min_section_words and kept:
            prev_name, prev_body = kept[-1]
            kept[-1] = (prev_name, f"{prev_body}\n{body}")
        else:
            kept.append((name, body))

    out: list[tuple[str | None, str]] = []
    for name, body in kept:
        label = None if name == "body" else name.title()
        for piece in _rebalance(body, max_words, overlap):
            out.append((label, piece))
    return out


# ---------------------------------------------------------------------------
# CrossRef metadata lookup
# ---------------------------------------------------------------------------


def _crossref_lookup(doi: str) -> dict:
    result: dict = {}
    headers = {"User-Agent": "OpenBrain/1.0 (mailto:open-brain@localhost)"}

    try:
        req = urllib.request.Request(
            f"https://api.crossref.org/works/{doi}", headers=headers
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())["message"]

        titles = data.get("title", [])
        result["title"] = titles[0] if titles else None

        authors = []
        for a in data.get("author", []):
            if "family" in a:
                name = f"{a.get('given', '')} {a['family']}".strip()
                authors.append(name)
        result["authors"] = authors

        for date_key in ("published-print", "published-online", "issued"):
            parts = (data.get(date_key) or {}).get("date-parts", [[]])
            if parts and parts[0]:
                result["year"] = parts[0][0]
                break

        containers = data.get("container-title", [])
        result["source"] = containers[0] if containers else None
        result["abstract"] = data.get("abstract")
    except Exception:
        pass

    try:
        req = urllib.request.Request(
            f"https://doi.org/{doi}",
            headers={**headers, "Accept": "application/x-bibtex"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result["bibtex"] = resp.read().decode("utf-8")
    except Exception:
        pass

    return result


# ---------------------------------------------------------------------------
# Library DB functions
# ---------------------------------------------------------------------------


async def ingest_document_pipeline(
    filepath: str,
    doc_type: str = "paper",
    force: bool = False,
    bibtex_str: str | None = None,
    title: str | None = None,
) -> dict:
    path = Path(filepath).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"File not found: {filepath}")

    file_hash = _sha256(path)
    pool = await get_pool()

    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT id, filepath FROM library WHERE file_hash = $1", file_hash
        )
    if existing and not force:
        raise ValueError(
            f"Already ingested as {existing['filepath']}. Use force=True to re-ingest."
        )
    if existing and force:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM library WHERE id = $1", existing["id"])

    loop = asyncio.get_event_loop()
    ext = path.suffix.lower()
    if ext == ".pdf":
        first_page, full_text = await loop.run_in_executor(
            None, _extract_pdf, str(path)
        )
    else:
        full_text = path.read_text(errors="replace")
        first_page = full_text[:3000]

    resolved_title = authors = year = source = abstract = doi = bibtex = None
    authors = []
    doi = _extract_doi(first_page)
    if doi:
        meta = await loop.run_in_executor(None, _crossref_lookup, doi)
        resolved_title = meta.get("title")
        authors = meta.get("authors") or []
        year = meta.get("year")
        source = meta.get("source")
        abstract = meta.get("abstract")
        bibtex = meta.get("bibtex")

    if not resolved_title and bibtex_str:
        meta = _parse_bibtex(bibtex_str)
        resolved_title = meta.get("title")
        authors = meta.get("authors") or []
        year = meta.get("year")
        source = meta.get("source")
        abstract = meta.get("abstract") or abstract
        doi = meta.get("doi") or doi
        bibtex = bibtex_str

    if not resolved_title and title:
        resolved_title = title

    metadata_missing = not resolved_title
    if metadata_missing:
        resolved_title = path.stem

    title = resolved_title

    LIBRARY_PATH.mkdir(parents=True, exist_ok=True)
    filename = _make_filename(title, authors, year, source, ext)
    dest = LIBRARY_PATH / filename
    if dest.exists() and not force:
        stem, suffix = dest.stem, dest.suffix
        dest = LIBRARY_PATH / f"{stem}-2{suffix}"
    shutil.copy2(str(path), str(dest))

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO library
                (title, authors, year, source, doc_type, abstract, filepath, file_hash, doi, bibtex)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            RETURNING id, title
            """,
            title,
            authors,
            year,
            source,
            doc_type,
            abstract,
            str(dest),
            file_hash,
            doi,
            bibtex,
        )
    library_id = row["id"]

    chunks = _chunk_document(full_text)
    pending = 0
    for i, (section, chunk_content) in enumerate(chunks):
        emb = await _embed_or_none(chunk_content)
        if emb is None:
            pending += 1
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO chunks "
                "(library_id, content, embedding, chunk_index, section) "
                "VALUES ($1, $2, $3, $4, $5)",
                library_id,
                chunk_content,
                emb,
                i,
                section,
            )

    await _extract_findings_inline(library_id)

    author_str = " & ".join(authors) if authors else "Unknown"
    thought = f"Ingested: {author_str} {year or ''} — {title}".strip()
    await store_thought(
        thought,
        {
            "type": "ingest_event",
            "source": "mcp",
            "people": authors,
            "doc_type": doc_type,
        },
    )

    return {
        "id": str(library_id),
        "title": title,
        "filepath": str(dest),
        "chunks": len(chunks),
        "chunks_pending_embedding": pending,
        "metadata_missing": metadata_missing,
    }


# ---------------------------------------------------------------------------
# Paper metadata/abstract fetch (no local file)
# ---------------------------------------------------------------------------


def _paper_sources_order() -> list[str]:
    """Effective source order from config.yaml paper_sources (order ∩ enabled)."""
    cfg = _cfg.get("paper_sources") or {}
    order = cfg.get("order") or sources._DEFAULT_ORDER
    enabled = cfg.get("enabled")
    if enabled is not None:
        order = [s for s in order if s in enabled]
    return order


async def _extract_findings_inline(library_id) -> None:
    """Best-effort inline findings extraction at store time (second derivation).

    Mirrors the embedding deferral: if the extraction model is reachable, findings
    land immediately and the paper is marked ``done``; if it fails, the paper stays
    ``findings_status='pending'`` for the nightly ``backfill_findings`` to heal.
    Never lets an extraction failure abort the write — same invariant as embeddings.
    """
    try:
        from openbrain import findings as _findings

        await _findings.extract_findings(str(library_id))
    except Exception as exc:
        logger.warning("Inline findings extraction skipped for %s: %s", library_id, exc)


async def _store_paper(
    meta: dict, doc_type: str = "paper", force: bool = False
) -> dict:
    """
    Store a fetched paper as a metadata-only library record (no file), embedding
    the abstract as a chunk (kind='abstract') so it flows through existing
    semantic search. Deduplicates by DOI.
    """
    pool = await get_pool()
    doi = meta.get("doi")
    abstract = meta.get("abstract")
    source = meta.get("source")
    if source:
        source = _source_slug(source) or source

    if doi:
        async with pool.acquire() as conn:
            existing = await conn.fetchrow(
                "SELECT id, title FROM library WHERE doi = $1", doi
            )
        if existing and not force:
            return {
                "id": str(existing["id"]),
                "title": existing["title"],
                "doi": doi,
                "abstract_stored": False,
                "embedded": False,
                "deduped": True,
            }
        if existing and force:
            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM library WHERE id = $1", existing["id"])

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO library
                (title, authors, year, source, doc_type, abstract, doi, bibtex)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING id, title
            """,
            meta.get("title"),
            meta.get("authors") or [],
            meta.get("year"),
            source,
            doc_type,
            abstract,
            doi,
            meta.get("bibtex"),
        )
    library_id = row["id"]

    abstract_stored = False
    embedded = False
    if abstract:
        emb = await _embed_or_none(abstract)
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO chunks (library_id, content, embedding, chunk_index, kind) "
                "VALUES ($1, $2, $3, $4, 'abstract')",
                library_id,
                abstract,
                emb,
                0,
            )
        abstract_stored = True
        embedded = emb is not None

    await _extract_findings_inline(library_id)

    authors = meta.get("authors") or []
    author_str = " & ".join(authors) if authors else "Unknown"
    thought = f"Fetched: {author_str} {meta.get('year') or ''} — {row['title']}".strip()
    await store_thought(
        thought,
        {
            "type": "ingest_event",
            "source": "mcp",
            "people": authors,
            "doc_type": doc_type,
        },
    )

    return {
        "id": str(library_id),
        "title": row["title"],
        "authors": authors,
        "year": meta.get("year"),
        "doi": doi,
        "abstract_stored": abstract_stored,
        "embedded": embedded,
        "deduped": False,
    }


async def store_acquired_paper(meta: dict, *, force: bool = False) -> dict:
    """
    Store a paper acquired by the deterministic acquire pipeline. Like
    _store_paper, but keeps the RAW venue name in `source` (no `_source_slug`)
    and carries the acquire columns (issn, venue_source_id, venue_abbrev,
    concepts). Deduplicates by DOI. The abstract is embedded as a chunk through
    the existing _embed_or_none deferral path. No ingest_event thought is emitted
    — bulk acquisition would flood the append-only thought log.

    Returns {id, title, embedded, deduped}.
    """
    pool = await get_pool()
    doi = meta.get("doi")
    if doi:
        async with pool.acquire() as conn:
            existing = await conn.fetchrow(
                "SELECT id, title FROM library WHERE doi = $1", doi
            )
        if existing and not force:
            return {
                "id": str(existing["id"]),
                "title": existing["title"],
                "embedded": False,
                "deduped": True,
            }
        if existing and force:
            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM library WHERE id = $1", existing["id"])

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO library
                (title, authors, year, source, doc_type, abstract, doi, bibtex,
                 issn, venue_source_id, venue_abbrev, concepts)
            VALUES ($1, $2, $3, $4, 'paper', $5, $6, $7, $8, $9, $10, $11)
            RETURNING id, title
            """,
            meta.get("title"),
            meta.get("authors") or [],
            meta.get("year"),
            meta.get("source"),  # RAW venue name — NOT slugged
            meta.get("abstract"),
            doi,
            meta.get("bibtex"),
            meta.get("issn") or meta.get("issn_l"),
            meta.get("venue_source_id"),
            meta.get("venue_abbrev"),
            meta.get("concepts") or [],
        )
    library_id = row["id"]

    embedded = False
    abstract = meta.get("abstract")
    if abstract:
        emb = await _embed_or_none(abstract)
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO chunks (library_id, content, embedding, chunk_index, kind) "
                "VALUES ($1, $2, $3, 0, 'abstract')",
                library_id,
                abstract,
                emb,
            )
        embedded = emb is not None

    await _extract_findings_inline(library_id)

    return {
        "id": str(library_id),
        "title": row["title"],
        "embedded": embedded,
        "deduped": False,
    }


async def fetch_paper(
    identifier: str | None = None,
    topic: str | None = None,
    doc_type: str = "paper",
    source_order: list[str] | None = None,
    limit: int = 10,
    force: bool = False,
) -> dict:
    """
    Fetch paper metadata + abstract from external sources.

    Topic mode (topic set): return ranked candidates without storing anything.
    Lookup mode (identifier set): resolve one paper by DOI/arXiv/title and store
    it as a metadata-only library record with the abstract embedded.
    """
    loop = asyncio.get_event_loop()
    order = source_order or _paper_sources_order()

    if topic is not None:
        candidates = await loop.run_in_executor(
            None, lambda: sources.resolve(topic=topic, order=order, limit=limit)
        )
        return {"mode": "topic", "candidates": candidates or []}

    if not identifier:
        raise ValueError("fetch_paper requires either identifier or topic.")

    meta = await loop.run_in_executor(
        None, lambda: sources.resolve(identifier=identifier, order=order)
    )
    if not meta:
        return {"mode": "lookup", "found": False, "identifier": identifier}

    stored = await _store_paper(meta, doc_type, force)
    return {"mode": "lookup", "found": True, **stored}


async def fetch_papers(
    identifiers: list[str],
    doc_type: str = "paper",
    source_order: list[str] | None = None,
    force: bool = False,
) -> dict:
    """
    Batch known-item lookup: resolve and store one library record per identifier
    (DOI/arXiv/title). Each identifier is processed independently, so one bad or
    unfound identifier does not abort the rest. Returns per-identifier results
    plus a roll-up summary.
    """
    results: list[dict] = []
    fetched = deduped = not_found = abstracts_deferred = 0

    for ident in identifiers:
        ident = ident.strip()
        if not ident:
            continue
        outcome = await fetch_paper(
            identifier=ident,
            doc_type=doc_type,
            source_order=source_order,
            force=force,
        )
        outcome["identifier"] = ident
        results.append(outcome)

        if not outcome.get("found"):
            not_found += 1
        elif outcome.get("deduped"):
            deduped += 1
        else:
            fetched += 1
            if outcome.get("abstract_stored") and not outcome.get("embedded"):
                abstracts_deferred += 1

    return {
        "mode": "batch",
        "results": results,
        "summary": {
            "requested": len(results),
            "fetched": fetched,
            "deduped": deduped,
            "not_found": not_found,
            "abstracts_deferred": abstracts_deferred,
        },
    }


# ---------------------------------------------------------------------------
# Embedding backfill (heal chunks stored without an embedding)
# ---------------------------------------------------------------------------


async def count_pending_embeddings() -> dict:
    """Count chunks whose embedding is NULL (stored text, embedding deferred)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT kind, count(*) AS n FROM chunks "
            "WHERE embedding IS NULL GROUP BY kind"
        )
    by_kind = {row["kind"]: row["n"] for row in rows}
    return {"pending": sum(by_kind.values()), "by_kind": by_kind}


async def backfill_chunk_embeddings(limit: int = 500) -> dict:
    """
    Embed chunks that were stored without an embedding (e.g. the embed model was
    down at fetch/ingest time). Uses the same configured embed model, so the
    vectors stay in one geometry. Per-chunk failures are skipped and retried on a
    later run; only the embed model coming back fixes them.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        pending_before = await conn.fetchval(
            "SELECT count(*) FROM chunks WHERE embedding IS NULL"
        )
        rows = await conn.fetch(
            "SELECT id, content FROM chunks WHERE embedding IS NULL "
            "ORDER BY created_at LIMIT $1",
            limit,
        )

    embedded = 0
    failed = 0
    for row in rows:
        try:
            emb = await embed(row["content"])
        except Exception as exc:
            failed += 1
            logger.warning("Backfill embed failed for chunk %s: %s", row["id"], exc)
            continue
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE chunks SET embedding = $1 WHERE id = $2", emb, row["id"]
            )
        embedded += 1

    return {
        "pending_before": pending_before,
        "embedded": embedded,
        "failed": failed,
        "pending_after": pending_before - embedded,
    }


async def search_library_vec(
    query: str,
    threshold: float = 0.4,
    count: int = 10,
    year_min: int | None = None,
    doc_type: str | None = None,
) -> list[dict]:
    pool = await get_pool()
    embedding = await embed(query, task="query")

    conditions = ["c.embedding IS NOT NULL", "1 - (c.embedding <=> $1) > $2"]
    params: list = [embedding, threshold]
    p = 3

    if year_min is not None:
        conditions.append(f"l.year >= ${p}")
        params.append(year_min)
        p += 1
    if doc_type is not None:
        conditions.append(f"l.doc_type = ${p}")
        params.append(doc_type)
        p += 1

    where = " AND ".join(conditions)
    params.append(count)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT c.id AS chunk_id, l.id AS library_id, l.title, l.authors,
                   l.year, l.source, l.doc_type, l.status,
                   c.content AS chunk_text, c.section,
                   1 - (c.embedding <=> $1) AS similarity
            FROM chunks c
            JOIN library l ON l.id = c.library_id
            WHERE {where}
            ORDER BY similarity DESC
            LIMIT ${p}
            """,
            *params,
        )
    return [dict(r) for r in rows]


def _finding_source(row) -> dict:
    """Citation block for a finding's source paper (never a bare finding)."""
    authors = list(row["authors"] or [])
    return {
        "citekey": citekey(authors, row["year"], row["venue_abbrev"]),
        "authors": authors,
        "year": row["year"],
        "title": row["title"],
        "venue_abbrev": row["venue_abbrev"],
        "doi": row["doi"],
    }


def _finding_block(row) -> dict:
    return {
        "id": str(row["finding_id"]),
        "cause_text": row["cause_text"],
        "effect_text": row["effect_text"],
        "magnitude_value": row["magnitude_value"],
        "magnitude_unit": row["magnitude_unit"],
        "magnitude_stat": row["magnitude_stat"],
        "conditions": row["conditions"],
        "raw_quote": row["raw_quote"],
        "confidence": row["confidence"],
    }


async def search_findings(
    *,
    text: str | None = None,
    cause: str | None = None,
    effect: str | None = None,
    stat: str | None = None,
    venue: str | None = None,
    year_min: int | None = None,
    status: str | None = None,
    group_by: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Search extracted findings, each JOINed to its source paper citation.

    Findings are NEVER returned bare — every row carries a ``source`` block hard-linked
    to the paper PK (the anti-hallucination payoff). ``text`` matches either side of the
    edge (cause/effect text); ``cause``/``effect`` match one side; ``stat``/``venue``/
    ``year_min``/``status`` are structured filters. ``group_by='paper'`` collapses N
    findings under one source. This is the simple-mode (text-match) query surface;
    graph traversal over resolved variables is the deferred §6 checkpoint.
    """
    conditions: list[str] = []
    params: list = []
    p = 1

    def add(cond: str, value) -> None:
        nonlocal p
        conditions.append(cond.format(p=p))
        params.append(value)
        p += 1

    if text is not None:
        add("(f.cause_text ILIKE ${p} OR f.effect_text ILIKE ${p})", f"%{text}%")
        # ILIKE binds the one placeholder twice via the same param index reuse below.
    if cause is not None:
        add("f.cause_text ILIKE ${p}", f"%{cause}%")
    if effect is not None:
        add("f.effect_text ILIKE ${p}", f"%{effect}%")
    if stat is not None:
        add("f.magnitude_stat = ${p}", stat)
    if venue is not None:
        add("l.venue_abbrev = ${p}", venue)
    if year_min is not None:
        add("l.year >= ${p}", year_min)
    if status is not None:
        add("l.findings_status = ${p}", status)

    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT f.id AS finding_id, f.cause_text, f.effect_text, f.magnitude_value,
                   f.magnitude_unit, f.magnitude_stat, f.conditions, f.raw_quote,
                   f.confidence,
                   l.id AS paper_id, l.title, l.authors, l.year, l.venue_abbrev, l.doi
            FROM findings f
            JOIN library l ON l.id = f.paper_id
            {where}
            ORDER BY f.created_at DESC
            LIMIT ${p}
            """,
            *params,
        )

    if group_by == "paper":
        grouped: dict = {}
        for row in rows:
            key = str(row["paper_id"])
            entry = grouped.setdefault(
                key, {"source": _finding_source(row), "findings": []}
            )
            entry["findings"].append(_finding_block(row))
        return list(grouped.values())

    return [{"finding": _finding_block(row), "source": _finding_source(row)} for row in rows]


async def find_library_records(query: str) -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, title, authors, year, source, doc_type, status, filepath, doi, abstract
            FROM library
            WHERE title ILIKE $1
               OR filepath ILIKE $1
               OR doi ILIKE $1
               OR EXISTS (SELECT 1 FROM unnest(authors) a(name) WHERE a.name ILIKE $1)
            ORDER BY created_at DESC
            """,
            f"%{query}%",
        )
    return [dict(r) for r in rows]


async def get_library_record(query: str) -> dict | None:
    matches = await find_library_records(query)
    if not matches:
        return None
    if len(matches) > 1:
        return {"multiple": [r["title"] for r in matches]}
    return matches[0]


async def update_library_record(record_id: str, updates: dict) -> dict | None:
    allowed = {
        "title",
        "authors",
        "year",
        "source",
        "doc_type",
        "abstract",
        "status",
        "doi",
        "bibtex",
        "filepath",
    }
    fields = {k: v for k, v in updates.items() if k in allowed}
    if "source" in fields and fields["source"]:
        fields["source"] = _source_slug(fields["source"]) or fields["source"]
    if not fields:
        return None
    pool = await get_pool()
    set_clause = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(fields))
    values = [record_id] + list(fields.values())
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"UPDATE library SET {set_clause} WHERE id = $1 "
            "RETURNING id, title, authors, year, source, doc_type, status, filepath",
            *values,
        )
    return dict(row) if row else None


async def search_brain(
    query: str,
    threshold: float = 0.35,
    count: int = 5,
) -> dict:
    """
    Hybrid fan-out search across thoughts, library, people, and projects.

    Uses vector similarity as the primary ranker. Full-text search acts as an
    OR inclusion condition, catching exact names and terms that embeddings miss.
    Library results are deduplicated to the best matching chunk per document.
    People and projects are surfaced via the metadata bridge on matching thoughts.
    """
    pool = await get_pool()
    embedding = await embed(query, task="query")
    words = [w for w in re.sub(r"[^\w\s]", "", query).split() if len(w) > 2]
    fts_query = " | ".join(words) if words else query[:50]

    async def _thoughts():
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, content, metadata, created_at,
                       COALESCE(1 - (embedding <=> $1), 0) AS score
                FROM thoughts
                WHERE embedding IS NOT NULL
                  AND (
                    1 - (embedding <=> $1) > $3
                    OR fts @@ to_tsquery('english', $2)
                  )
                ORDER BY score DESC
                LIMIT $4
                """,
                embedding,
                fts_query,
                threshold,
                count,
            )
        return [dict(r) for r in rows]

    async def _library():
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                WITH ranked AS (
                    SELECT
                        l.id AS library_id, l.title, l.authors, l.year,
                        l.source, l.doc_type, c.content AS chunk_text,
                        COALESCE(1 - (c.embedding <=> $1), 0) AS score,
                        ROW_NUMBER() OVER (
                            PARTITION BY l.id
                            ORDER BY COALESCE(1 - (c.embedding <=> $1), 0) DESC
                        ) AS rn
                    FROM chunks c
                    JOIN library l ON l.id = c.library_id
                    WHERE c.embedding IS NOT NULL
                      AND (
                        1 - (c.embedding <=> $1) > $3
                        OR c.fts @@ to_tsquery('english', $2)
                      )
                )
                SELECT library_id, title, authors, year, source, doc_type, chunk_text, score
                FROM ranked
                WHERE rn = 1
                ORDER BY score DESC
                LIMIT $4
                """,
                embedding,
                fts_query,
                threshold,
                count,
            )
        return [dict(r) for r in rows]

    thoughts, library = await asyncio.gather(_thoughts(), _library())
    cooc = _extract_cooccurrences(thoughts, exclude="")

    return {
        "thoughts": thoughts,
        "library": library,
        "people": cooc["people"][:5],
        "projects": cooc["projects"][:5],
    }


async def add_person_note(name: str, note: str) -> dict:
    matches = await find_person_by_name(name)
    if not matches:
        raise ValueError(f"No person found matching '{name}'.")
    if len(matches) > 1:
        names = ", ".join(r["name"] for r in matches)
        raise ValueError(f"Multiple matches: {names}. Please be more specific.")
    person = matches[0]
    await store_thought(
        f"Note about {person['name']}: {note}",
        {"type": "note", "source": "claude", "people": [person["name"]]},
    )
    return person


async def get_person_profile(name: str) -> dict | None:
    matches = await find_person_by_name(name)
    if not matches:
        return None
    if len(matches) > 1:
        return {"multiple": [r["name"] for r in matches]}
    person = dict(matches[0])

    pool = await get_pool()
    async with pool.acquire() as conn:
        person = dict(
            await conn.fetchrow(
                "SELECT id, name, affiliation, relationship, notes, follow_up_date "
                "FROM people WHERE id = $1",
                person["id"],
            )
        )

    thoughts = await thoughts_mentioning(person["name"], "person", limit=50)
    cooc = _extract_cooccurrences(thoughts, exclude=person["name"])

    return {
        "person": person,
        "thoughts": thoughts,
        "cooccurring_projects": cooc["projects"],
        "cooccurring_people": cooc["people"],
    }


async def people_with_follow_ups(within_days: int = 7) -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, name, affiliation, relationship, notes, follow_up_date
            FROM people
            WHERE follow_up_date IS NOT NULL
              AND follow_up_date <= current_date + make_interval(days := $1)
            ORDER BY follow_up_date
            """,
            within_days,
        )
    return [dict(r) for r in rows]


async def thoughts_mentioning(
    name: str,
    entity_type: str = "any",
    limit: int = 20,
) -> list[dict]:
    pool = await get_pool()
    if entity_type == "person":
        keys = ["people"]
    elif entity_type == "project":
        keys = ["projects"]
    else:
        keys = ["people", "projects"]

    conditions = " OR ".join(
        f"EXISTS (SELECT 1 FROM jsonb_array_elements_text(metadata->'{k}') e WHERE e ILIKE $1)"
        for k in keys
    )
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT id, content, metadata, created_at
            FROM thoughts
            WHERE {conditions}
            ORDER BY created_at DESC
            LIMIT $2
            """,
            f"%{name}%",
            limit,
        )
    return [dict(r) for r in rows]


def _extract_cooccurrences(thoughts: list[dict], exclude: str) -> dict:
    people: dict[str, int] = {}
    projects: dict[str, int] = {}
    for t in thoughts:
        meta = t.get("metadata") or {}
        if isinstance(meta, str):
            meta = json.loads(meta)
        for p in meta.get("people", []):
            if not exclude or exclude.lower() not in p.lower():
                people[p] = people.get(p, 0) + 1
        for p in meta.get("projects", []):
            if not exclude or exclude.lower() not in p.lower():
                projects[p] = projects.get(p, 0) + 1
    return {
        "people": sorted(people.items(), key=lambda x: -x[1]),
        "projects": sorted(projects.items(), key=lambda x: -x[1]),
    }


async def get_briefing(days: int = 1) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        projects = await conn.fetch(
            "SELECT name, description, status FROM projects "
            "WHERE status IN ('active', 'paused') ORDER BY status, name"
        )
        thoughts = await conn.fetch(
            """
            SELECT id, content, metadata, created_at
            FROM thoughts
            WHERE created_at >= now() - make_interval(days := $1)
            ORDER BY created_at DESC
            """,
            days,
        )
        library = await conn.fetch(
            """
            SELECT title, authors, year, doc_type, created_at
            FROM library
            WHERE created_at >= now() - make_interval(days := $1)
            ORDER BY created_at DESC
            """,
            days,
        )

    thoughts_list = [dict(r) for r in thoughts]
    cooc = _extract_cooccurrences(thoughts_list, exclude="")
    follow_ups = await people_with_follow_ups(within_days=days)

    return {
        "days": days,
        "projects": [dict(r) for r in projects],
        "thoughts": thoughts_list,
        "people": cooc["people"],
        "library": [dict(r) for r in library],
        "follow_ups": follow_ups,
    }


async def table_stats() -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        stats = await conn.fetchval("SELECT brain_stats_aggregate()")
    # JSONB return arrives as a str (no jsonb codec on the pool).
    if isinstance(stats, str):
        stats = json.loads(stats)
    return dict(stats)


# ---------------------------------------------------------------------------
# Acquire subsystem — venue registry, structured query, coverage, saved
# queries, bibtex export. All deterministic; no LLM.
# ---------------------------------------------------------------------------

_SORT_COLUMNS = {"year", "created_at", "title"}


async def seed_venues() -> int:
    """Seed the venues table from venues.toml. Idempotent."""
    pool = await get_pool()
    return await venues_mod.seed_db(pool)


async def query_library_struct(
    *,
    venue: str | None = None,
    kind: str | None = None,
    year_min: int | None = None,
    year_max: int | None = None,
    status: str | None = None,
    concept: str | None = None,
    has_abstract: bool | None = None,
    sort: str = "year",
    limit: int = 50,
) -> list[dict]:
    """Exact structured query over the library. `kind` joins the venues table;
    `venue` filters on the canonical venue_abbrev; `concept` matches the GIN
    concepts array. Sort column is whitelisted against injection."""
    clauses: list[str] = []
    params: list = []

    def add(cond: str, val) -> None:
        params.append(val)
        clauses.append(cond.format(n=len(params)))

    if venue is not None:
        add("l.venue_abbrev = ${n}", venue)
    if kind is not None:
        add("v.kind = ${n}", kind)
    if year_min is not None:
        add("l.year >= ${n}", year_min)
    if year_max is not None:
        add("l.year <= ${n}", year_max)
    if status is not None:
        add("l.status = ${n}", status)
    if concept is not None:
        add("${n} = ANY(l.concepts)", concept)
    if has_abstract is True:
        clauses.append("l.abstract IS NOT NULL")
    elif has_abstract is False:
        clauses.append("l.abstract IS NULL")

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    sort_col = sort if sort in _SORT_COLUMNS else "year"
    order_dir = "ASC" if sort_col == "title" else "DESC"

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT l.id, l.title, l.authors, l.year, l.source, l.venue_abbrev,
                   l.issn, l.doc_type, l.status, l.doi, l.bibtex, l.concepts,
                   l.abstract
            FROM library l
            LEFT JOIN venues v ON v.abbrev = l.venue_abbrev
            {where}
            ORDER BY l.{sort_col} {order_dir} NULLS LAST
            LIMIT {int(limit)}
            """,
            *params,
        )
    return [dict(r) for r in rows]


async def coverage_stats(
    *, venue: str | None = None, concept: str | None = None
) -> list[dict]:
    """Counts grouped by publication year, optionally filtered by venue/concept."""
    clauses: list[str] = []
    params: list = []
    if venue is not None:
        params.append(venue)
        clauses.append(f"venue_abbrev = ${len(params)}")
    if concept is not None:
        params.append(concept)
        clauses.append(f"${len(params)} = ANY(concepts)")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT year, count(*) AS n FROM library{where} "
            "GROUP BY year ORDER BY year",
            *params,
        )
    return [dict(r) for r in rows]


async def list_registry_venues(
    *, field: str | None = None, kind: str | None = None
) -> list[dict]:
    """The seeded + discovered venues registry."""
    clauses: list[str] = []
    params: list = []
    if field is not None:
        params.append(field)
        clauses.append(f"field = ${len(params)}")
    if kind is not None:
        params.append(kind)
        clauses.append(f"kind = ${len(params)}")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT abbrev, full_name, field, kind, issn_l, source_id, origin "
            f"FROM venues{where} ORDER BY abbrev",
            *params,
        )
    return [dict(r) for r in rows]


async def list_unknown_venues(limit: int = 50) -> list[dict]:
    """Uncategorized venues seen during acquisition (venue_abbrev IS NULL),
    ranked by frequency — the auto-grow promotion surface (§4)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT source AS name, issn, venue_source_id AS source_id,
                   count(*) AS n
            FROM library
            WHERE venue_abbrev IS NULL
              AND (source IS NOT NULL OR issn IS NOT NULL
                   OR venue_source_id IS NOT NULL)
            GROUP BY source, issn, venue_source_id
            ORDER BY n DESC
            LIMIT $1
            """,
            limit,
        )
    return [dict(r) for r in rows]


async def delete_library_record(query: str) -> dict:
    """Resolve a single library record by query and delete it. Refuses when the
    query is ambiguous (multiple matches) or matches nothing."""
    matches = await find_library_records(query)
    if not matches:
        return {"deleted": 0, "reason": "no match"}
    if len(matches) > 1:
        return {"deleted": 0, "multiple": [r["title"] for r in matches]}
    rec = matches[0]
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM library WHERE id = $1", rec["id"])
    return {"deleted": 1, "id": str(rec["id"]), "title": rec["title"]}


# --- bibtex export ---------------------------------------------------------


def _last_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        return "Unknown"
    if "," in name:
        return name.split(",")[0].strip()
    return name.split()[-1]


def citekey(authors: list[str], year: int | None, venue_abbrev: str | None) -> str:
    """Stable citekey: <L1>-[<L2>|ETAL]-<YEAR>-<VENUE>."""
    venue = venue_abbrev or "NA"
    if not authors:
        lead = "Unknown"
    elif len(authors) <= 2:
        lead = "-".join(_last_name(a) for a in authors)
    else:
        lead = f"{_last_name(authors[0])}-ETAL"
    return f"{lead}-{year or 'nd'}-{venue}"


def _rekey_bibtex(bibtex: str, key: str) -> str:
    return re.sub(r"(@\w+\s*\{)[^,]*,", rf"\g<1>{key},", bibtex, count=1)


def _render_bibtex(rec: dict, key: str) -> str:
    fields = []
    if rec.get("title"):
        fields.append(f"  title = {{{rec['title']}}}")
    authors = rec.get("authors") or []
    if authors:
        fields.append("  author = {" + " and ".join(authors) + "}")
    if rec.get("year"):
        fields.append(f"  year = {{{rec['year']}}}")
    if rec.get("source"):
        fields.append(f"  journal = {{{rec['source']}}}")
    if rec.get("doi"):
        fields.append(f"  doi = {{{rec['doi']}}}")
    return f"@article{{{key},\n" + ",\n".join(fields) + "\n}"


async def export_bibtex_records(
    *, venue: str | None = None, year_min: int | None = None, status: str | None = None
) -> str:
    """Concatenated .bib for the matching library rows. Prefers each row's stored
    bibtex blob (re-keyed to the stable citekey), else renders a minimal entry.
    Citekey collisions get appended letters."""
    rows = await query_library_struct(
        venue=venue, year_min=year_min, status=status, limit=10000
    )
    seen: dict[str, int] = {}
    out: list[str] = []
    for rec in rows:
        key = citekey(
            rec.get("authors") or [], rec.get("year"), rec.get("venue_abbrev")
        )
        if key in seen:
            seen[key] += 1
            key = f"{key}{chr(ord('a') + seen[key] - 1)}"
        else:
            seen[key] = 0
        blob = rec.get("bibtex")
        out.append(_rekey_bibtex(blob, key) if blob else _render_bibtex(rec, key))
    return "\n\n".join(out)

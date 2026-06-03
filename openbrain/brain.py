"""
brain.py — core logic: embed, store, retrieve.

All MCP tools call into this module. Database pool is initialized lazily on
first use. Configuration is loaded from config.yaml (non-secrets) and .env
(API keys and DATABASE_URL).
"""

import asyncio
import hashlib
import json
import os
import re
import shutil
import urllib.request
import urllib.error
from pathlib import Path

import asyncpg
import bibtexparser
import pgvector.asyncpg
import yaml
from dotenv import load_dotenv

load_dotenv()

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
LIBRARY_PATH: Path = Path(_cfg["library_path"]).expanduser()
EXTRACTION_CONFIG: dict = _cfg.get("extraction", {})

_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "nor", "for", "yet", "so",
    "of", "in", "on", "at", "to", "by", "up", "as", "is", "it", "its",
    "from", "with", "into", "onto", "upon", "over", "under", "about",
    "are", "was", "were", "be", "been", "being", "have", "has", "had",
    "do", "does", "did", "will", "would", "could", "should", "may",
    "might", "shall", "can", "this", "that", "these", "those", "their",
    "not", "no", "via", "per", "vs", "between", "each",
})

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

    source = (
        entry.get("journal")
        or entry.get("booktitle")
        or entry.get("publisher")
    )

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
            api_key=os.environ["LITELLM_API_KEY"],
            base_url=litellm_url,
        )
        result = await client.embeddings.create(input=[text], model=model)
        return result.data[0].embedding

    raise ValueError(
        f"Unknown embed.provider: {provider!r}. "
        "Expected one of: voyage, openrouter, litellm"
    )


async def embed(text: str) -> list[float]:
    errors = []
    for config in _configs_with_fallback(_cfg["embed"]):
        try:
            return await _embed_with_config(text, config)
        except Exception as exc:
            errors.append(f"{config['provider']}/{config['model']}: {exc}")
    raise RuntimeError("All embedding providers failed: " + "; ".join(errors))


async def _enriched_entity_text(table: str, entity_id: str) -> str:
    pool = await get_pool()
    if table == "people":
        async with pool.acquire() as conn:
            row = dict(await conn.fetchrow(
                "SELECT name, affiliation, relationship, notes FROM people WHERE id = $1",
                entity_id,
            ))
        base = await _entity_text("people", row)
        name, kind = row["name"], "person"
    else:
        async with pool.acquire() as conn:
            row = dict(await conn.fetchrow(
                "SELECT name, description FROM projects WHERE id = $1", entity_id
            ))
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
    embedding = await embed(content)
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
    thought_id = str(result["id"])

    people = meta.get("people") or []
    projects = meta.get("projects") or []
    if result["metadata_changed"] and (people or projects):
        asyncio.create_task(_refresh_mentioned_entities(people, projects))

    return thought_id


async def search_thoughts_vec(
    query: str,
    threshold: float = 0.5,
    count: int = 10,
) -> list[dict]:
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


async def maintenance_thoughts(
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


def _maintenance_prompt(thoughts: list[dict]) -> str:
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

    client = AsyncOpenAI(api_key=os.environ[api_key_name], base_url=base_url)
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


async def archive_thoughts(thought_ids: list[str], maintenance_run_id: str) -> None:
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
                    'maintenance_run_id', $2
                )
            WHERE id = ANY($1::uuid[])
            """,
            thought_ids,
            maintenance_run_id,
        )


async def run_maintenance_once() -> dict:
    since_hours = int(EXTRACTION_CONFIG.get("lookback_hours", 24))
    batch_size = int(EXTRACTION_CONFIG.get("batch_size", 50))
    thoughts = await maintenance_thoughts(since_hours, batch_size)
    if not thoughts:
        return {"processed": 0, "syntheses": 0}

    prompt = _maintenance_prompt(thoughts)
    synthesis = await complete_extraction_prompt(prompt)
    if not synthesis:
        return {"processed": 0, "syntheses": 0}

    run_id = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
    thought_ids = [str(thought["id"]) for thought in thoughts]
    await store_thought(
        synthesis,
        {
            "type": "synthesis",
            "source": "openbrain-maintenance",
            "source_type": "maintenance",
            "thought_ids": thought_ids,
            "maintenance_run_id": run_id,
        },
    )
    await archive_thoughts(thought_ids, run_id)
    return {"processed": len(thoughts), "syntheses": 1}


async def preview_maintenance_once() -> dict:
    since_hours = int(EXTRACTION_CONFIG.get("lookback_hours", 24))
    batch_size = int(EXTRACTION_CONFIG.get("batch_size", 50))
    thoughts = await maintenance_thoughts(since_hours, batch_size)
    if not thoughts:
        return {"processed": 0, "synthesis": "", "thought_ids": []}

    prompt = _maintenance_prompt(thoughts)
    synthesis = await complete_extraction_prompt(prompt)
    return {
        "processed": len(thoughts),
        "synthesis": synthesis,
        "thought_ids": [str(thought["id"]) for thought in thoughts],
    }


async def apply_maintenance_synthesis(thought_ids: list[str], synthesis: str) -> dict:
    if not thought_ids or not synthesis.strip():
        return {"processed": 0, "syntheses": 0}

    run_key = "\n".join([synthesis.strip(), *sorted(thought_ids)])
    run_id = hashlib.sha256(run_key.encode("utf-8")).hexdigest()[:16]
    await store_thought(
        synthesis.strip(),
        {
            "type": "synthesis",
            "source": "openbrain-maintenance",
            "source_type": "maintenance",
            "thought_ids": thought_ids,
            "maintenance_run_id": run_id,
        },
    )
    await archive_thoughts(thought_ids, run_id)
    return {"processed": len(thought_ids), "syntheses": 1}


async def _entity_text(table: str, row: dict) -> str:
    if table == "people":
        parts = [row.get("name") or "", row.get("affiliation") or "",
                 row.get("relationship") or "", row.get("notes") or ""]
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
            name, affiliation, relationship, notes,
        )
    inserted = dict(row)
    text = await _entity_text("people", inserted)
    emb = await embed(text)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE people SET embedding = $1 WHERE id = $2", emb, str(inserted["id"])
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
            name, description, status, start_date,
        )
    inserted = dict(row)
    text = await _entity_text("projects", inserted)
    emb = await embed(text)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE projects SET embedding = $1 WHERE id = $2", emb, str(inserted["id"])
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
    set_clause = ", ".join(f"{k} = ${i+2}" for i, k in enumerate(fields))
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
        emb = await embed(text)
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
    embedding = await embed(query)
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


async def search_projects_vec(
    query: str, threshold: float = 0.3, count: int = 10
) -> list[dict]:
    pool = await get_pool()
    embedding = await embed(query)
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
    return [w for w in re.sub(r"[^\w\s]", "", text.lower()).split() if w not in _STOPWORDS]


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


def _chunk_text(text: str, chunk_size: int = 400, overlap: int = 50) -> list[str]:
    words = text.split()
    chunks, i = [], 0
    while i < len(words):
        chunks.append(" ".join(words[i: i + chunk_size]))
        i += chunk_size - overlap
    return [c for c in chunks if c.strip()]


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
        first_page, full_text = await loop.run_in_executor(None, _extract_pdf, str(path))
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
            title, authors, year, source, doc_type,
            abstract, str(dest), file_hash, doi, bibtex,
        )
    library_id = row["id"]

    chunks = _chunk_text(full_text)
    for i, chunk_content in enumerate(chunks):
        emb = await embed(chunk_content)
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO chunks (library_id, content, embedding, chunk_index) "
                "VALUES ($1, $2, $3, $4)",
                library_id, chunk_content, emb, i,
            )

    author_str = " & ".join(authors) if authors else "Unknown"
    thought = f"Ingested: {author_str} {year or ''} — {title}".strip()
    await store_thought(thought, {
        "type": "ingest_event",
        "source": "mcp",
        "people": authors,
        "doc_type": doc_type,
    })

    return {
        "id": str(library_id),
        "title": title,
        "filepath": str(dest),
        "chunks": len(chunks),
        "metadata_missing": metadata_missing,
    }


async def search_library_vec(
    query: str,
    threshold: float = 0.4,
    count: int = 10,
    year_min: int | None = None,
    doc_type: str | None = None,
) -> list[dict]:
    pool = await get_pool()
    embedding = await embed(query)

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
                   c.content AS chunk_text,
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
    allowed = {"title", "authors", "year", "source", "doc_type", "abstract",
               "status", "doi", "bibtex", "filepath"}
    fields = {k: v for k, v in updates.items() if k in allowed}
    if "source" in fields and fields["source"]:
        fields["source"] = _source_slug(fields["source"]) or fields["source"]
    if not fields:
        return None
    pool = await get_pool()
    set_clause = ", ".join(f"{k} = ${i+2}" for i, k in enumerate(fields))
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
    embedding = await embed(query)
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
                embedding, fts_query, threshold, count,
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
                embedding, fts_query, threshold, count,
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
        person = dict(await conn.fetchrow(
            "SELECT id, name, affiliation, relationship, notes, follow_up_date "
            "FROM people WHERE id = $1",
            person["id"],
        ))

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
    return dict(stats)

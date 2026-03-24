"""
brain.py — core logic: embed, store, retrieve.

All MCP tools call into this module. Database pool and embedding model
are initialized lazily on first use.

Embedding backend is controlled by EMBED_BACKEND in .env:
  local   — sentence-transformers running on this machine (default, free)
  voyage  — Voyage AI API (requires VOYAGE_API_KEY)
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
import pgvector.asyncpg
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]
EMBED_BACKEND = os.getenv("EMBED_BACKEND", "local")
EMBED_MODEL_LOCAL = os.getenv("EMBED_MODEL_LOCAL", "BAAI/bge-large-en-v1.5")
VOYAGE_API_KEY = os.getenv("VOYAGE_API_KEY", "")
VOYAGE_MODEL = os.getenv("VOYAGE_MODEL", "voyage-large-2")
LIBRARY_PATH = Path(os.getenv("LIBRARY_PATH", "~/library")).expanduser()

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
            json.dumps(metadata or {}),
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


async def add_person(name: str, affiliation: str | None = None, relationship: str | None = None, notes: str | None = None) -> dict:
    """Insert a new person and embed them. Returns the new row."""
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
        await conn.execute("UPDATE people SET embedding = $1 WHERE id = $2", emb, str(inserted["id"]))
    return inserted


async def add_project(name: str, description: str | None = None, status: str = "active", start_date: str | None = None) -> dict:
    """Insert a new project and embed it. Returns the new row."""
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
        await conn.execute("UPDATE projects SET embedding = $1 WHERE id = $2", emb, str(inserted["id"]))
    return inserted


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


def _make_filename(title: str, authors: list[str], year: int | None,
                   source: str | None, ext: str) -> str:
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
    """Return (first_page_text, full_text). Sync — run in executor."""
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
        chunks.append(" ".join(words[i : i + chunk_size]))
        i += chunk_size - overlap
    return [c for c in chunks if c.strip()]


# ---------------------------------------------------------------------------
# CrossRef metadata lookup
# ---------------------------------------------------------------------------

def _crossref_lookup(doi: str) -> dict:
    """Fetch metadata + BibTeX from CrossRef/doi.org. Sync — run in executor."""
    result: dict = {}
    headers = {"User-Agent": "OpenBrain/1.0 (mailto:open-brain@localhost)"}

    # JSON metadata
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

    # BibTeX
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
) -> dict:
    """Full ingest pipeline: extract → CrossRef → copy → store → chunk+embed."""
    path = Path(filepath).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"File not found: {filepath}")

    file_hash = _sha256(path)
    pool = await get_pool()

    # Deduplication check
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

    # Extract text
    loop = asyncio.get_event_loop()
    ext = path.suffix.lower()
    if ext == ".pdf":
        first_page, full_text = await loop.run_in_executor(None, _extract_pdf, str(path))
    else:
        full_text = path.read_text(errors="replace")
        first_page = full_text[:3000]

    # Metadata via DOI → CrossRef
    title = authors = year = source = abstract = doi = bibtex = None
    authors = []
    doi = _extract_doi(first_page)
    if doi:
        meta = await loop.run_in_executor(None, _crossref_lookup, doi)
        title = meta.get("title")
        authors = meta.get("authors") or []
        year = meta.get("year")
        source = meta.get("source")
        abstract = meta.get("abstract")
        bibtex = meta.get("bibtex")

    if not title:
        title = path.stem

    # Copy to library directory
    LIBRARY_PATH.mkdir(parents=True, exist_ok=True)
    filename = _make_filename(title, authors, year, source, ext)
    dest = LIBRARY_PATH / filename
    # Avoid overwriting a different file with the same slug
    if dest.exists() and not force:
        stem, suffix = dest.stem, dest.suffix
        dest = LIBRARY_PATH / f"{stem}-2{suffix}"
    shutil.copy2(str(path), str(dest))

    # Write library record
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

    # Chunk, embed, and store
    chunks = _chunk_text(full_text)
    for i, chunk_content in enumerate(chunks):
        emb = await embed(chunk_content)
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO chunks (library_id, content, embedding, chunk_index) "
                "VALUES ($1, $2, $3, $4)",
                library_id, chunk_content, emb, i,
            )

    # Write ingest event to thoughts
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
    }


async def search_library_vec(
    query: str,
    threshold: float = 0.4,
    count: int = 10,
    year_min: int | None = None,
    doc_type: str | None = None,
) -> list[dict]:
    """Semantic search over library chunks, returning paper-level results."""
    pool = await get_pool()
    embedding = await embed(query)

    conditions = ["c.embedding IS NOT NULL", f"1 - (c.embedding <=> $1) > $2"]
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
    """Flexible lookup across title, filepath, authors, and DOI."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, title, authors, year, source, doc_type, status, filepath, doi
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


async def update_library_record(record_id: str, updates: dict) -> dict | None:
    """Update fields on a library row. Auto-abbreviates source if provided."""
    allowed = {"title", "authors", "year", "source", "doc_type", "abstract",
               "status", "doi", "bibtex", "filepath"}
    fields = {k: v for k, v in updates.items() if k in allowed}
    # Auto-abbreviate source
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

    Returns a structured dict with keys: thoughts, library, people, projects.
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


async def thoughts_mentioning(
    name: str,
    entity_type: str = "any",
    limit: int = 20,
) -> list[dict]:
    """Find thoughts whose metadata people/projects arrays contain name."""
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
    """Count people and projects co-mentioned across a list of thoughts."""
    people: dict[str, int] = {}
    projects: dict[str, int] = {}
    for t in thoughts:
        meta = t.get("metadata") or {}
        if isinstance(meta, str):
            meta = json.loads(meta)
        for p in meta.get("people", []):
            if exclude.lower() not in p.lower():
                people[p] = people.get(p, 0) + 1
        for p in meta.get("projects", []):
            if exclude.lower() not in p.lower():
                projects[p] = projects.get(p, 0) + 1
    return {
        "people": sorted(people.items(), key=lambda x: -x[1]),
        "projects": sorted(projects.items(), key=lambda x: -x[1]),
    }


async def get_briefing(days: int = 1) -> dict:
    """Return a structured briefing covering the given number of past days."""
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

    return {
        "days": days,
        "projects": [dict(r) for r in projects],
        "thoughts": thoughts_list,
        "people": cooc["people"],
        "library": [dict(r) for r in library],
    }


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

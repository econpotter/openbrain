# Acquire Subsystem Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a deterministic paper-acquisition subsystem to openbrain — search journal sources for a topic, deduplicate, enrich, canonicalize venue, lexically prefilter, and bulk-store into the existing `library` table with embeddings — plus structured-query/affordance tools, saved-query incremental harvest, and bibtex export.

**Architecture:** New `openbrain/acquire.py` (pipeline) + `openbrain/venues.py` + `venues.toml` (registry seeded into a DB `venues` table), exposed via `openbrain/tools/acquire.py` (`register(mcp)`) and `openbrain/cli.py` subcommands. No LLM anywhere — acquisition is fully deterministic. Reuses existing `sources.py` connectors, `brain.py` DB pool + `_store_paper` embed path, and the consolidated nightly maintenance run for saved-query cron.

**Tech Stack:** Python 3.11, asyncpg, pgvector (VECTOR(768)), FastMCP (`@mcp.tool`), typer CLI, urllib source connectors, pytest.

**Design spec:** `docs/design/2026-06-26-acquire-subsystem.md` (read it — this plan implements it).

## Global Constraints

- **Schema = pre-alpha baseline edits, NOT migrations.** Edit `schemas/core/*.sql` in place, add new baseline files, re-run `setup_db.sh` against a blank DB. No `ALTER`, no backfill. (ROADMAP ALPHA + AGENTS.md.)
- **No LLM in acquisition.** Relevance is a deterministic lexical recall-gate only.
- **Implement fresh in openbrain.** The retired `library` repo (github.com/econpotter/library) is a *reference* for algorithms only — do not port files; write to fit openbrain's structure.
- **All DB access via `brain.py`** using `pool = await brain.get_pool()` then `async with pool.acquire() as conn:`. One `register(mcp)` per tool file; register it in `server.py`.
- **Tests:** `uv run pytest`. Source connectors are tested against recorded fixtures — **no live network** in tests.
- **Embeddings:** VECTOR(768); store-with-NULL-embedding when the embed model is down (existing `_embed_or_none` already does this).

---

## File Structure

- `schemas/core/003_library.sql` — MODIFY: add `issn`, `venue_source_id`, `venue_abbrev`, `concepts[]` columns + indexes.
- `schemas/core/007_venues.sql` — CREATE: `venues` table.
- `schemas/core/008_saved_queries.sql` — CREATE: `saved_queries` table.
- `openbrain/venues.toml` — CREATE: seed venue registry (journals + issn-less venues).
- `openbrain/venues.py` — CREATE: load toml, seed DB, normalize names, match cascade, prefilter overlap helper.
- `openbrain/sources.py` — MODIFY: capture `issn_l`/`source.id`/`concepts` (OpenAlex) + `subject`/`ISSN` (Crossref); add `openalex_search_paged` (API-side filters, cursor pagination, `truncated`); retry/backoff helper.
- `openbrain/brain.py` — MODIFY: stop slugging `source` in `_store_paper`; extend it to carry new columns; add `store_acquired_paper`, `query_library_struct`, `coverage_stats`, saved-query CRUD + `run_saved_query`, `export_bibtex_records`.
- `openbrain/acquire.py` — CREATE: the `acquire()` pipeline (search→dedup→enrich→canon→prefilter→store) + dedup/prefilter helpers.
- `openbrain/tools/acquire.py` — CREATE: `register(mcp)` with acquisition/query/affordance/saved-query/bibtex tools.
- `openbrain/server.py` — MODIFY: `acquire.register(mcp)`.
- `openbrain/cli.py` — MODIFY: add subcommands 1:1 with the tools.
- `scripts/openbrain-maintenance.py` — MODIFY: run due saved queries in `_run_once()`.
- `tests/test_venues.py`, `tests/test_acquire.py`, `tests/test_sources.py` (extend), `tests/test_brain.py` (extend) — tests.

**Dependency order across tasks:** 1 (schema) → 2 (venues) → 3 (sources ext) → 4 (store extend) → 5 (pipeline) → 6 (query/affordance tools) → 7 (saved queries + cron) → 8 (bibtex) → 9 (CLI + registration). Tasks 6–9 depend on 5; 4 must precede 5.

---

### Task 1: Schema deltas (library columns + venues + saved_queries)

**Files:**
- Modify: `schemas/core/003_library.sql`
- Create: `schemas/core/007_venues.sql`, `schemas/core/008_saved_queries.sql`
- Test: `tests/test_schema_acquire.py`

**Interfaces:**
- Produces: `library` columns `issn TEXT`, `venue_source_id TEXT`, `venue_abbrev TEXT`, `concepts TEXT[]`; `venues` table (PK `abbrev`); `saved_queries` table (PK `id`, UNIQUE `name`).

- [ ] **Step 1: Add columns to the `library` CREATE in `schemas/core/003_library.sql`**

Insert after the `bibtex TEXT,` line (before `created_at`):

```sql
    issn            TEXT,        -- journal venue key (OpenAlex issn_l); NULL for preprints/WPs
    venue_source_id TEXT,        -- OpenAlex source id (S…); issn-equivalent for issn-less venues
    venue_abbrev    TEXT,        -- registry-canonical abbrev e.g. 'AJAE','ARXIV'; NULL = uncategorized
    concepts        TEXT[],      -- OpenAlex concepts ∪ Crossref subjects ∪ source keywords
```

Add indexes after the existing `library_doi_uniq` index:

```sql
CREATE INDEX library_venue_abbrev_idx ON library (venue_abbrev);
CREATE INDEX library_issn_idx         ON library (issn);
CREATE INDEX library_concepts_idx     ON library USING gin (concepts);
```

- [ ] **Step 2: Create `schemas/core/007_venues.sql`**

```sql
-- Venue registry: maps a fetched paper's venue → canonical venue_abbrev.
-- Seeded from openbrain/venues.toml at setup; auto-grows with origin='discovered'.
CREATE TABLE venues (
    abbrev      TEXT PRIMARY KEY,
    full_name   TEXT NOT NULL,
    field       TEXT,
    kind        TEXT NOT NULL
                CHECK (kind IN ('journal','preprint','working_paper','report','book','series')),
    issn_l      TEXT UNIQUE,
    source_id   TEXT UNIQUE,
    match_names TEXT[],
    origin      TEXT DEFAULT 'seed' CHECK (origin IN ('seed','discovered'))
);
CREATE INDEX venues_kind_idx ON venues (kind);
```

- [ ] **Step 3: Create `schemas/core/008_saved_queries.sql`**

```sql
CREATE TABLE saved_queries (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name              TEXT UNIQUE NOT NULL,
    query             TEXT NOT NULL,
    venues            TEXT[],
    since             INT,
    min_overlap       INT DEFAULT 2,
    cadence           TEXT DEFAULT 'weekly'
                      CHECK (cadence IN ('nightly','weekly','monthly')),
    harvested_through TIMESTAMPTZ,
    last_run          TIMESTAMPTZ,
    last_yield        JSONB DEFAULT '{}',
    created_at        TIMESTAMPTZ DEFAULT now()
);
```

- [ ] **Step 4: Write the failing schema test**

`tests/test_schema_acquire.py`:

```python
import subprocess
from pathlib import Path

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas" / "core"

def test_baseline_files_present():
    assert (SCHEMA_DIR / "007_venues.sql").exists()
    assert (SCHEMA_DIR / "008_saved_queries.sql").exists()

def test_library_has_new_columns():
    text = (SCHEMA_DIR / "003_library.sql").read_text()
    for col in ("venue_abbrev", "venue_source_id", "issn", "concepts"):
        assert col in text, f"missing column {col}"
```

- [ ] **Step 5: Run the test**

Run: `uv run pytest tests/test_schema_acquire.py -v`
Expected: PASS (files + columns present).

- [ ] **Step 6: Verify the schema applies on a blank DB**

Run: `bash setup_db.sh` against a throwaway/blank DB (or `psql` each file in order).
Expected: no errors; `\d library` shows the four new columns; `\d venues` and `\d saved_queries` exist.

- [ ] **Step 7: Commit**

```bash
git add schemas/core/003_library.sql schemas/core/007_venues.sql schemas/core/008_saved_queries.sql tests/test_schema_acquire.py
git commit -m "feat(acquire): schema deltas for venues, saved_queries, library venue columns"
```

---

### Task 2: Venue registry (`venues.toml` + `venues.py`)

**Files:**
- Create: `openbrain/venues.toml`, `openbrain/venues.py`
- Test: `tests/test_venues.py`

**Interfaces:**
- Produces:
  - `venues.normalize_name(name: str) -> str` — lowercase, strip punctuation/“the/of/and/journal”, collapse spaces.
  - `venues.load_seeds(path: str) -> list[dict]` — parse toml → seed dicts with keys `abbrev, full_name, field, kind, issn_l, source_id, match_names`.
  - `async venues.seed_db(pool)` — upsert seeds into `venues` (origin='seed'); idempotent.
  - `async venues.match(pool, *, issn_l=None, source_id=None, name=None) -> str | None` — cascade issn_l → source_id → normalized name in `match_names` → None.
  - `venues.lexical_overlap(query: str, text: str) -> int` — count of distinct normalized query terms present in text.

- [ ] **Step 1: Create `openbrain/venues.toml` (seed set)**

```toml
# Journals keyed by issn_l; issn-less venues by source_id/match_names.
[[venue]]
abbrev = "AJAE"
full_name = "American Journal of Agricultural Economics"
field = "agricultural economics"
kind = "journal"
issn_l = "0002-9092"
match_names = ["american journal of agricultural economics", "amer j agr econ"]

[[venue]]
abbrev = "WRR"
full_name = "Water Resources Research"
field = "water"
kind = "journal"
issn_l = "0043-1397"
match_names = ["water resources research"]

[[venue]]
abbrev = "ARXIV"
full_name = "arXiv"
field = "preprint"
kind = "preprint"
source_id = "S4306400194"
match_names = ["arxiv"]

[[venue]]
abbrev = "NBER"
full_name = "NBER Working Papers"
field = "economics"
kind = "working_paper"
source_id = "S4306402521"
match_names = ["nber working paper", "national bureau of economic research"]
```

(Extend with AER, QJE, JEEM, JAERE, Land Economics, ERE, ERAE, SSRN, USDA-ERS per the spec §4 once the cascade is proven.)

- [ ] **Step 2: Write the failing test for `normalize_name` + `lexical_overlap`**

`tests/test_venues.py`:

```python
from openbrain import venues

def test_normalize_name_strips_noise():
    assert venues.normalize_name("The American Journal of Agricultural Economics") \
        == "american agricultural economics"

def test_lexical_overlap_counts_distinct_query_terms():
    assert venues.lexical_overlap("water demand price", "Estimating water demand elasticity") == 2
```

- [ ] **Step 3: Run it — expect failure**

Run: `uv run pytest tests/test_venues.py -v`
Expected: FAIL (`ModuleNotFoundError: openbrain.venues`).

- [ ] **Step 4: Implement `normalize_name` + `lexical_overlap` in `openbrain/venues.py`**

```python
"""venues.py — venue registry: load/seed, match cascade, prefilter helpers."""
import re
import tomllib
from pathlib import Path

_STOP = {"the", "of", "and", "journal", "review", "for", "a", "an"}

def normalize_name(name: str) -> str:
    words = re.sub(r"[^\w\s]", " ", (name or "").lower()).split()
    return " ".join(w for w in words if w not in _STOP)

def _terms(text: str) -> set[str]:
    return {w for w in re.sub(r"[^\w\s]", " ", (text or "").lower()).split() if len(w) > 2}

def lexical_overlap(query: str, text: str) -> int:
    q = _terms(query)
    t = _terms(text)
    return len(q & t)
```

- [ ] **Step 5: Run the test — expect pass**

Run: `uv run pytest tests/test_venues.py -v`
Expected: PASS.

- [ ] **Step 6: Add `load_seeds` + failing test**

Test:

```python
def test_load_seeds_parses_toml(tmp_path):
    p = tmp_path / "v.toml"
    p.write_text('[[venue]]\nabbrev="AJAE"\nfull_name="x"\nkind="journal"\nissn_l="0002-9092"\nmatch_names=["amer j agr econ"]\n')
    seeds = venues.load_seeds(str(p))
    assert seeds[0]["abbrev"] == "AJAE"
    assert seeds[0]["match_names"] == ["amer j agr econ"]
```

Implementation:

```python
def load_seeds(path: str) -> list[dict]:
    data = tomllib.loads(Path(path).read_text())
    seeds = []
    for v in data.get("venue", []):
        seeds.append({
            "abbrev": v["abbrev"],
            "full_name": v.get("full_name", v["abbrev"]),
            "field": v.get("field"),
            "kind": v["kind"],
            "issn_l": v.get("issn_l"),
            "source_id": v.get("source_id"),
            "match_names": [normalize_name(n) for n in v.get("match_names", [])],
        })
    return seeds
```

- [ ] **Step 7: Add `seed_db` + `match` (DB-backed; test against the test DB)**

```python
VENUES_TOML = str(Path(__file__).with_name("venues.toml"))

async def seed_db(pool, path: str = VENUES_TOML) -> int:
    seeds = load_seeds(path)
    async with pool.acquire() as conn:
        for s in seeds:
            await conn.execute(
                """INSERT INTO venues (abbrev, full_name, field, kind, issn_l, source_id, match_names, origin)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,'seed')
                   ON CONFLICT (abbrev) DO UPDATE SET
                     full_name=EXCLUDED.full_name, field=EXCLUDED.field, kind=EXCLUDED.kind,
                     issn_l=EXCLUDED.issn_l, source_id=EXCLUDED.source_id, match_names=EXCLUDED.match_names""",
                s["abbrev"], s["full_name"], s["field"], s["kind"],
                s["issn_l"], s["source_id"], s["match_names"],
            )
    return len(seeds)

async def match(pool, *, issn_l=None, source_id=None, name=None) -> str | None:
    async with pool.acquire() as conn:
        if issn_l:
            r = await conn.fetchval("SELECT abbrev FROM venues WHERE issn_l = $1", issn_l)
            if r: return r
        if source_id:
            r = await conn.fetchval("SELECT abbrev FROM venues WHERE source_id = $1", source_id)
            if r: return r
        if name:
            r = await conn.fetchval(
                "SELECT abbrev FROM venues WHERE $1 = ANY(match_names)", normalize_name(name))
            if r: return r
    return None
```

Test (uses the configured test DB pool via `brain.get_pool()`; mark `@pytest.mark.asyncio`, follow the existing `tests/test_brain.py` DB-fixture pattern):

```python
import pytest
from openbrain import brain, venues

@pytest.mark.asyncio
async def test_match_cascade_issn_then_name():
    pool = await brain.get_pool()
    await venues.seed_db(pool)
    assert await venues.match(pool, issn_l="0002-9092") == "AJAE"
    assert await venues.match(pool, name="Amer J Agr Econ") == "AJAE"
    assert await venues.match(pool, name="Totally Unknown Venue") is None
```

- [ ] **Step 8: Run tests, then commit**

Run: `uv run pytest tests/test_venues.py -v` → PASS.

```bash
git add openbrain/venues.py openbrain/venues.toml tests/test_venues.py
git commit -m "feat(acquire): venue registry — toml seeds, db seeding, match cascade, overlap helper"
```

---

### Task 3: Extend `sources.py` (capture venue keys + concepts; paged search; retry)

**Files:**
- Modify: `openbrain/sources.py`
- Test: `tests/test_sources.py` (extend)

**Interfaces:**
- Produces (new `PaperMeta` keys): `issn_l: str | None`, `venue_source_id: str | None`, `concepts: list[str]`.
- Produces: `openalex_search_paged(query, *, venues=None, since=None, limit=50, per_page=200) -> tuple[list[PaperMeta], bool]` — returns `(records, truncated)`, API-side filters, cursor pagination.
- Produces: `_with_retry(fn, *, attempts=3, base_delay=1.0)` — retry/backoff wrapper for 429/transient.

- [ ] **Step 1: Failing test — `_openalex_record` captures issn_l/source_id/concepts**

Add to `tests/test_sources.py` (build a minimal `work` dict like the OpenAlex shape):

```python
from openbrain import sources

def test_openalex_record_captures_venue_keys_and_concepts():
    work = {
        "display_name": "Water demand",
        "publication_year": 2021,
        "authorships": [],
        "primary_location": {"source": {
            "display_name": "American Journal of Agricultural Economics",
            "issn_l": "0002-9092", "id": "https://openalex.org/S123"}},
        "concepts": [{"display_name": "Water"}, {"display_name": "Demand"}],
        "abstract_inverted_index": None, "doi": None,
    }
    rec = sources._openalex_record(work)
    assert rec["issn_l"] == "0002-9092"
    assert rec["venue_source_id"] == "S123"
    assert rec["concepts"] == ["Water", "Demand"]
```

- [ ] **Step 2: Run — expect failure** (`KeyError`/missing keys).

Run: `uv run pytest tests/test_sources.py::test_openalex_record_captures_venue_keys_and_concepts -v`

- [ ] **Step 3: Extend `_openalex_record`** — add the three keys

In `_openalex_record`, after computing `venue`, add:

```python
    source_id = venue.get("id")
    if source_id:
        source_id = source_id.replace("https://openalex.org/", "")
    concepts = [c["display_name"] for c in work.get("concepts", []) if c.get("display_name")]
```

and add to the returned `PaperMeta(...)`:

```python
        issn_l=venue.get("issn_l"),
        venue_source_id=source_id,
        concepts=concepts,
```

Add the keys to the `PaperMeta` TypedDict: `issn_l: str | None`, `venue_source_id: str | None`, `concepts: list[str]`.

- [ ] **Step 4: Failing test — `_crossref_record` captures subject + ISSN**

```python
def test_crossref_record_captures_subject_and_issn():
    item = {"title": ["X"], "author": [], "container-title": ["AJAE"],
            "subject": ["Economics", "Water"], "ISSN": ["0002-9092"]}
    rec = sources._crossref_record(item)
    assert rec["concepts"] == ["Economics", "Water"]
    assert rec["issn_l"] == "0002-9092"
```

- [ ] **Step 5: Extend `_crossref_record`** — add `concepts=item.get("subject", [])` and `issn_l=(item.get("ISSN") or [None])[0]`, `venue_source_id=None`.

- [ ] **Step 6: Add `_with_retry` + `openalex_search_paged` with a fixture-driven test**

Implement `_with_retry` (catches exceptions / HTTP 429, sleeps `base_delay * 2**attempt`, re-raises after `attempts`). Implement `openalex_search_paged` building the OpenAlex `filter=` string (`primary_location.source.issn:<csv>` when `venues=` resolved to issns; `,from_publication_date:<since>-01-01`) and looping with `cursor=*` until `limit` survivors or no `next_cursor`, setting `truncated=True` if more remained.

Test against a recorded fixture (two pages) under `tests/fixtures/` by monkeypatching `sources._get_json` to return canned pages:

```python
def test_openalex_search_paged_paginates_and_flags_truncation(monkeypatch):
    pages = [
        {"results": [{"display_name": f"p{i}", "authorships": [], "primary_location": {},
                      "concepts": [], "abstract_inverted_index": None, "doi": None}
                     for i in range(2)],
         "meta": {"next_cursor": "C2"}},
        {"results": [{"display_name": "p2", "authorships": [], "primary_location": {},
                      "concepts": [], "abstract_inverted_index": None, "doi": None}],
         "meta": {"next_cursor": None}},
    ]
    calls = {"i": 0}
    def fake_get_json(url, headers=None):
        out = pages[calls["i"]]; calls["i"] += 1; return out
    monkeypatch.setattr(sources, "_get_json", fake_get_json)
    recs, truncated = sources.openalex_search_paged("water", limit=2, per_page=2)
    assert len(recs) == 2
    assert truncated is True
```

- [ ] **Step 7: Run all source tests, commit**

Run: `uv run pytest tests/test_sources.py -v` → PASS.

```bash
git add openbrain/sources.py tests/test_sources.py
git commit -m "feat(acquire): sources capture venue keys + concepts; paged openalex search; retry"
```

---

### Task 4: Stop slugging `source`; extend `_store_paper` to carry new columns

**Files:**
- Modify: `openbrain/brain.py` (`_store_paper` ~1447; leave `_source_slug` for filename/citekey use only)
- Test: `tests/test_brain.py` (extend)

**Interfaces:**
- Produces: `store_acquired_paper(meta: dict, *, force=False) -> dict` — like `_store_paper` but stores raw `source`, plus `issn`, `venue_source_id`, `venue_abbrev`, `concepts`; embeds abstract chunk via `_embed_or_none`. Returns `{id, title, embedded, deduped}`.

- [ ] **Step 1: Failing test — raw venue name is preserved (not slugged)**

```python
@pytest.mark.asyncio
async def test_store_acquired_paper_keeps_raw_source_and_columns():
    meta = {"title": "T", "authors": ["A B"], "year": 2021,
            "source": "American Journal of Agricultural Economics",
            "abstract": "water demand", "doi": "10.1/x",
            "issn_l": "0002-9092", "venue_source_id": "S123",
            "venue_abbrev": "AJAE", "concepts": ["Water"]}
    res = await brain.store_acquired_paper(meta)
    pool = await brain.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT source, issn, venue_abbrev, concepts FROM library WHERE id=$1", res["id"])
    assert row["source"] == "American Journal of Agricultural Economics"  # NOT slugged
    assert row["venue_abbrev"] == "AJAE"
    assert row["concepts"] == ["Water"]
```

- [ ] **Step 2: Run — expect failure** (`AttributeError: store_acquired_paper`).

- [ ] **Step 3: Implement `store_acquired_paper`** (mirror `_store_paper`, but no `_source_slug`, extra columns)

```python
async def store_acquired_paper(meta: dict, *, force: bool = False) -> dict:
    pool = await get_pool()
    doi = meta.get("doi")
    if doi:
        async with pool.acquire() as conn:
            existing = await conn.fetchrow("SELECT id FROM library WHERE doi = $1", doi)
        if existing and not force:
            return {"id": str(existing["id"]), "deduped": True, "embedded": False}
        if existing and force:
            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM library WHERE id = $1", existing["id"])
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO library
                 (title, authors, year, source, doc_type, abstract, doi, bibtex,
                  issn, venue_source_id, venue_abbrev, concepts)
               VALUES ($1,$2,$3,$4,'paper',$5,$6,$7,$8,$9,$10,$11)
               RETURNING id, title""",
            meta.get("title"), meta.get("authors") or [], meta.get("year"),
            meta.get("source"),  # RAW — not slugged
            meta.get("abstract"), doi, meta.get("bibtex"),
            meta.get("issn_l"), meta.get("venue_source_id"),
            meta.get("venue_abbrev"), meta.get("concepts") or [],
        )
    library_id = row["id"]
    embedded = False
    if meta.get("abstract"):
        emb = await _embed_or_none(meta["abstract"])
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO chunks (library_id, content, embedding, chunk_index, kind) "
                "VALUES ($1,$2,$3,0,'abstract')", library_id, meta["abstract"], emb)
        embedded = emb is not None
    return {"id": str(library_id), "title": row["title"], "embedded": embedded, "deduped": False}
```

- [ ] **Step 4: Run test → PASS. Commit.**

```bash
git add openbrain/brain.py tests/test_brain.py
git commit -m "feat(acquire): store_acquired_paper — raw venue name + venue/concept columns"
```

---

### Task 5: The `acquire()` pipeline

**Files:**
- Create: `openbrain/acquire.py`
- Test: `tests/test_acquire.py`

**Interfaces:**
- Consumes: `sources.openalex_search_paged`, `sources.crossref_lookup`, `venues.match`, `venues.lexical_overlap`, `brain.store_acquired_paper`, `brain.get_pool`.
- Produces: `async acquire(query, *, venues_filter=None, since=None, limit=50, min_overlap=2, dry_run=False) -> dict` returning the report `{found, deduped, prefiltered_out:{no_abstract,old,venue,low_overlap}, stored, truncated, unknown_venues:[…], sample:[…]}`.
- Produces helpers: `dedup_records(records) -> list` (DOI then title+year), `prefilter(rec, query, since, min_overlap, venues_filter) -> str|None` (returns drop-reason or None to keep).

- [ ] **Step 1: Failing test — dedup by DOI then title/year**

```python
from openbrain import acquire

def test_dedup_drops_doi_and_title_year_dups():
    recs = [
        {"doi": "10.1/a", "title": "X", "year": 2020},
        {"doi": "10.1/a", "title": "X dup", "year": 2020},     # same DOI
        {"doi": None, "title": "Y Study", "year": 2019},
        {"doi": None, "title": "y study", "year": 2019},        # same title+year
    ]
    out = acquire.dedup_records(recs)
    assert len(out) == 2
```

- [ ] **Step 2: Run — fail. Step 3: Implement `dedup_records`.**

```python
import re
def _norm_title(t): return re.sub(r"\W+", " ", (t or "").lower()).strip()
def dedup_records(records):
    seen, out = set(), []
    for r in records:
        key = ("doi:" + r["doi"].lower()) if r.get("doi") else f"ty:{_norm_title(r.get('title'))}:{r.get('year')}"
        if key in seen: continue
        seen.add(key); out.append(r)
    return out
```

- [ ] **Step 4: Failing test — prefilter drop reasons**

```python
def test_prefilter_drop_reasons():
    base = {"abstract": "water demand elasticity", "year": 2021, "venue_abbrev": "AJAE"}
    assert acquire.prefilter({**base, "abstract": None}, "water", None, 2, None) == "no_abstract"
    assert acquire.prefilter({**base, "year": 2000}, "water", 2015, 2, None) == "old"
    assert acquire.prefilter(base, "water demand", None, 2, None) is None          # 2 overlap → keep
    assert acquire.prefilter(base, "quantum chromodynamics physics", None, 2, None) == "low_overlap"
    assert acquire.prefilter(base, "water demand", None, 2, {"WRR"}) == "venue"     # not in filter
```

- [ ] **Step 5: Implement `prefilter`**

```python
from openbrain import venues as venues_mod
def prefilter(rec, query, since, min_overlap, venues_filter):
    if not rec.get("abstract"): return "no_abstract"
    if since and (rec.get("year") or 0) < since: return "old"
    if venues_filter and rec.get("venue_abbrev") not in venues_filter: return "venue"
    hay = " ".join(filter(None, [rec.get("title"), rec.get("abstract"), " ".join(rec.get("concepts") or [])]))
    if venues_mod.lexical_overlap(query, hay) < min_overlap: return "low_overlap"
    return None
```

- [ ] **Step 6: Failing end-to-end test (stubbed sources + store)**

Monkeypatch `sources.openalex_search_paged` to return canned records + `truncated`, `crossref_lookup` to no-op, and `brain.store_acquired_paper` to record calls. Assert the report counts and that `dry_run=True` stores nothing.

```python
@pytest.mark.asyncio
async def test_acquire_dry_run_stores_nothing(monkeypatch):
    recs = [{"title": "Water demand", "abstract": "water demand elasticity",
             "year": 2021, "doi": "10.1/a", "issn_l": "0002-9092",
             "venue_source_id": "S123", "concepts": ["Water"], "authors": []}]
    monkeypatch.setattr(acquire.sources, "openalex_search_paged", lambda *a, **k: (recs, False))
    monkeypatch.setattr(acquire.sources, "crossref_lookup", lambda *a, **k: None)
    stored = []
    async def fake_store(meta, **k): stored.append(meta); return {"id": "x", "embedded": True, "deduped": False}
    monkeypatch.setattr(acquire.brain, "store_acquired_paper", fake_store)
    report = await acquire.acquire("water demand", dry_run=True)
    assert report["found"] == 1 and report["stored"] == 0 and stored == []
```

- [ ] **Step 7: Implement `acquire()`** — SEARCH → DEDUP → ENRICH → CANON (`venues.match`, collect unknowns) → PREFILTER (tally reasons) → STORE (skip when `dry_run`). Return the report dict. Use `brain.get_pool()` for the `venues.match` calls.

- [ ] **Step 8: Run all acquire tests → PASS. Commit.**

```bash
git add openbrain/acquire.py tests/test_acquire.py
git commit -m "feat(acquire): deterministic pipeline — search/dedup/enrich/canon/prefilter/store"
```

---

### Task 6: Query + affordance tools (brain helpers + MCP wrappers)

**Files:**
- Modify: `openbrain/brain.py` (add `query_library_struct`, `coverage_stats`, `list_unknown_venues`)
- Create: `openbrain/tools/acquire.py` (`register(mcp)`)
- Test: `tests/test_brain.py` (extend)

**Interfaces:**
- Produces: `async query_library_struct(*, venue=None, kind=None, year_min=None, year_max=None, status=None, concept=None, has_abstract=None, sort='year', limit=50) -> list[dict]` — exact structured filter (joins `venues` for `kind`).
- Produces: `async coverage_stats(*, venue=None, concept=None) -> list[dict]` — counts grouped by year (and venue).
- Produces MCP tools: `search_library` (structured part — delegates to `query_library_struct`), `coverage`, `list_venues`, `list_concepts`, `acquire` (dry-run preview + real), `remove_record`.

- [ ] **Step 1: Failing test — `query_library_struct` exact venue + year filter**

```python
@pytest.mark.asyncio
async def test_query_library_struct_filters_exact_venue_year():
    await brain.store_acquired_paper({"title":"A","authors":[],"year":2021,
        "source":"AJAE","abstract":"x","doi":"10.1/q1","venue_abbrev":"AJAE","concepts":[]})
    await brain.store_acquired_paper({"title":"B","authors":[],"year":2010,
        "source":"AJAE","abstract":"x","doi":"10.1/q2","venue_abbrev":"AJAE","concepts":[]})
    rows = await brain.query_library_struct(venue="AJAE", year_min=2020)
    titles = {r["title"] for r in rows}
    assert "A" in titles and "B" not in titles
```

- [ ] **Step 2: Run — fail. Step 3: Implement `query_library_struct`** building a parameterized `WHERE` from the non-None filters (`venue_abbrev = $n`, `year >= $n`, `kind` via `JOIN venues v ON v.abbrev = l.venue_abbrev`, `concept = ANY(concepts)` via `$n = ANY(l.concepts)`, `has_abstract` → `abstract IS NOT NULL`), `ORDER BY {sort}` (whitelist `year|created_at|title`), `LIMIT`. Return `list[dict]`.

- [ ] **Step 4: Implement `coverage_stats`** (`SELECT year, count(*) ... GROUP BY year ORDER BY year`, filtered by venue/concept) + test asserting grouped counts.

- [ ] **Step 5: Implement `openbrain/tools/acquire.py`** wrapping the brain helpers + `acquire.acquire`. Pattern (mirror `tools/library.py`):

```python
from openbrain import acquire as acquire_mod, brain, venues

def register(mcp):
    @mcp.tool
    async def acquire(query: str, venues: list[str] | None = None, since: int | None = None,
                      limit: int = 50, min_overlap: int = 2, dry_run: bool = False) -> str:
        """Search journal sources for `query`, prefilter, and store survivors. dry_run=True previews only."""
        report = await acquire_mod.acquire(query, venues_filter=set(venues) if venues else None,
                                           since=since, limit=limit, min_overlap=min_overlap, dry_run=dry_run)
        import json
        return json.dumps(report, indent=2)

    @mcp.tool
    async def search_library(venue: str | None = None, kind: str | None = None,
                             year_min: int | None = None, year_max: int | None = None,
                             status: str | None = None, concept: str | None = None,
                             has_abstract: bool | None = None, sort: str = "year", limit: int = 50) -> str:
        """Deterministic structured query over the library (exact venue/year/status/concept)."""
        import json
        rows = await brain.query_library_struct(venue=venue, kind=kind, year_min=year_min,
            year_max=year_max, status=status, concept=concept, has_abstract=has_abstract, sort=sort, limit=limit)
        return json.dumps(rows, indent=2, default=str)
    # ... coverage, list_venues, list_concepts, remove_record likewise
```

(`list_concepts(term)` calls a new `sources.openalex_concepts(term)` helper — OpenAlex `/concepts?search=`; `remove_record` wraps existing delete-by-id; `list_venues` reads the `venues` table incl. `list_unknown_venues` for `unknown=True`.)

- [ ] **Step 6: Run tests → PASS. Commit.**

```bash
git add openbrain/brain.py openbrain/tools/acquire.py tests/test_brain.py
git commit -m "feat(acquire): structured query, coverage, venue/concept affordance tools"
```

---

### Task 7: Saved queries + watermark + cron

**Files:**
- Modify: `openbrain/brain.py` (saved-query CRUD + `run_saved_query`), `openbrain/tools/acquire.py` (tools), `scripts/openbrain-maintenance.py` (`_run_once`)
- Test: `tests/test_brain.py` (extend)

**Interfaces:**
- Produces: `async save_query(name, query, *, venues=None, since=None, min_overlap=2, cadence='weekly') -> dict` (upsert on `name`); `async update_query(name, **fields)`; `async list_queries() -> list[dict]`; `async delete_query(name)`; `async run_saved_query(name) -> dict` (acquire from `from_created_date=harvested_through`, advance watermark to batch-max `created_date`); `async due_saved_queries() -> list[dict]`.

- [ ] **Step 1: Failing test — watermark advances to batch-max created_date, not wall-clock**

```python
@pytest.mark.asyncio
async def test_run_saved_query_advances_watermark_to_batch_max(monkeypatch):
    await brain.save_query("q1", "water demand")
    # acquire returns two records with created_date; stub it
    async def fake_acquire(query, **k):
        return {"found": 2, "stored": 2, "max_created_date": "2026-06-20T00:00:00Z", "truncated": False}
    monkeypatch.setattr(brain, "_acquire_for_saved", fake_acquire)
    await brain.run_saved_query("q1")
    rows = await brain.list_queries()
    assert str(rows[0]["harvested_through"]).startswith("2026-06-20")
```

- [ ] **Step 2: Implement CRUD + `run_saved_query`** (acquire passes `since`/`from_created_date`; after the run, `UPDATE saved_queries SET harvested_through = $maxcreated, last_run = now(), last_yield = $yield`). Implement `due_saved_queries` (compare `last_run` + `cadence` interval to now).

- [ ] **Step 3: Add the cron step to `scripts/openbrain-maintenance.py` `_run_once()`** — after the embedding backfill log:

```python
    due = await brain.due_saved_queries()
    for q in due:
        try:
            y = await brain.run_saved_query(q["name"])
            logger.info("Saved query %s: %s found, %s stored.", q["name"], y.get("found"), y.get("stored"))
        except Exception as exc:
            logger.exception("Saved query %s failed: %s", q["name"], exc)
```

- [ ] **Step 4: Add MCP tools** `save_query`/`list_queries`/`run_query`/`update_query`/`delete_query` in `tools/acquire.py`.

- [ ] **Step 5: Run tests → PASS. Commit.**

```bash
git add openbrain/brain.py openbrain/tools/acquire.py scripts/openbrain-maintenance.py tests/test_brain.py
git commit -m "feat(acquire): saved queries with incremental harvest watermark + nightly cron"
```

---

### Task 8: bibtex export

**Files:**
- Modify: `openbrain/brain.py` (`export_bibtex_records`), `openbrain/tools/acquire.py` (tool)
- Test: `tests/test_brain.py` (extend)

**Interfaces:**
- Produces: `citekey(authors, year, venue_abbrev) -> str` (`<L1>-[<L2>|ETAL]-<YEAR>-<VENUE>`, collision-safe); `async export_bibtex_records(*, venue=None, year_min=None, status=None) -> str` (concatenated `.bib`).

- [ ] **Step 1: Failing test — citekey scheme + export concatenates `bibtex` blobs**

```python
def test_citekey_scheme():
    assert brain.citekey(["Jane Smith", "Bob Lee"], 2021, "AJAE") == "Smith-Lee-2021-AJAE"
    assert brain.citekey(["A","B","C"], 2020, "WRR") == "A-ETAL-2020-WRR"
```

- [ ] **Step 2: Implement `citekey` + `export_bibtex_records`** (query rows via `query_library_struct`, prefer each row's stored `bibtex`, else render minimal `@article{citekey, ...}`). Collision letters appended when citekeys clash.

- [ ] **Step 3: Add `export_bibtex` MCP tool** (writes to `path` if given, else returns the string).

- [ ] **Step 4: Run tests → PASS. Commit.**

```bash
git add openbrain/brain.py openbrain/tools/acquire.py tests/test_brain.py
git commit -m "feat(acquire): bibtex export with stable citekeys"
```

---

### Task 9: CLI subcommands + MCP registration

**Files:**
- Modify: `openbrain/server.py` (register), `openbrain/cli.py` (subcommands)
- Test: `tests/test_acquire.py` (CLI smoke via typer runner)

**Interfaces:**
- Consumes: all brain/acquire functions from Tasks 5–8.
- Produces: `openbrain acquire`, `openbrain query-library`, `openbrain coverage`, `openbrain save-query`, `openbrain run-query`, `openbrain list-venues`, `openbrain export-bibtex` CLI subcommands (1:1 with the MCP tools, thin wrappers over the same core fns).

- [ ] **Step 1: Register the tool module in `server.py`**

Add after `library.register(mcp)`:

```python
from openbrain.tools import acquire as acquire_tools
acquire_tools.register(mcp)
```

- [ ] **Step 2: Failing CLI test (typer runner)**

```python
from typer.testing import CliRunner
from openbrain.cli import app

def test_cli_query_library_runs(monkeypatch):
    async def fake(**k): return [{"title": "A", "year": 2021}]
    monkeypatch.setattr("openbrain.brain.query_library_struct", fake)
    res = CliRunner().invoke(app, ["query-library", "--venue", "AJAE"])
    assert res.exit_code == 0 and "A" in res.stdout
```

- [ ] **Step 3: Add the subcommands to `cli.py`** (thin `asyncio.run` wrappers calling the same brain/acquire fns; render with `rich.Table` like the existing `ingest`). Each subcommand = one core fn = one MCP tool.

- [ ] **Step 4: Run tests → PASS. Commit.**

```bash
git add openbrain/server.py openbrain/cli.py tests/test_acquire.py
git commit -m "feat(acquire): CLI subcommands + register acquire MCP tools"
```

---

## Self-Review

**Spec coverage** (`2026-06-26-acquire-subsystem.md`):
- §3 schema deltas → Task 1. §4 venue registry → Task 2. §5 pipeline (search/dedup/enrich/canon/prefilter/store) → Tasks 3–5. §6 query+affordance tools → Task 6. §7 saved queries+watermark+cron → Task 7. §8 bibtex → Task 8. §6/CLI + registration → Task 9.
- §9 synthesis (external) — correctly NOT implemented. §11 deferred (snowballing, PDF fetch, RePEc) — not implemented, by design.
- §1 "no LLM" honored — nothing in Tasks 1–9 calls an LLM.

**Open items folded into tasks, not placeholders:** OpenAlex cursor pagination + `truncated` (Task 3 Step 6), title/year dedup fallback (Task 5 Step 3), watermark = batch-max created_date (Task 7 Step 1).

**Type consistency:** `store_acquired_paper` (Task 4) is consumed by Task 5; `query_library_struct` (Task 6) consumed by Tasks 8–9; `venues.match`/`lexical_overlap` (Task 2) consumed by Task 5. Names match across tasks.

**Note for implementer:** the retired `library` repo is the reference for the *algorithms* (prefilter scoring, registry seeds, bibtex rendering). Re-derive, don't port. Seed `venues.toml` beyond the four examples before relying on the registry in real acquisition.

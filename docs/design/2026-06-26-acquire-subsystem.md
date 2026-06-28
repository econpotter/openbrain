# openbrain `acquire` Subsystem — Design Spec

**Date:** 2026-06-26
**Status:** Approved (design, rev. 2 — review fixes folded in), pending implementation plan
**Branch:** `dev` (implementation lands here)
**Supersedes:** the standalone `library` project (github.com/econpotter/library), which is
retired and absorbed here. Its deterministic modules are the reference implementation.

---

## 1. Purpose & decision

Add a deterministic paper-**acquisition** subsystem to openbrain: search journal sources for a
topic, evaluate relevance deterministically, and bulk-store the results into openbrain's existing
`library` table with embeddings. Plus the structured-query and affordance tools that let a user or
an external agent drive acquisition well.

**Decision: build this inside openbrain, not as a separate product.** A separate `library` repo
was prototyped (working, TDD'd) and then evaluated against openbrain's actual capabilities. There
is no architectural seam justifying two systems for this user:

- openbrain **already owns paper source I/O** (`sources.py`: `openalex_search`, `crossref_search`,
  `openalex_lookup`, `crossref_lookup`) and a `library` table with embeddings + FTS. The "keep
  flaky connectors out of stable memory" argument fails — the connectors are already here.
- The separate-product complexity (FastMCP client, SCP staging, host modes, cross-network
  bib-first durability, a `sync` retry command) existed **only** to bridge two systems across an
  SSH tunnel. That is integration tax the boundary imposed on itself. In-process acquisition
  deletes all of it.
- The only seam that would justify separation — wanting a portable, openbrain-independent bibtex
  tool — is not a goal. The entire value is papers *in* openbrain for synthesis.

**What is explicitly NOT in this subsystem:** any LLM. Acquisition is fully deterministic. The
LLM-as-relevance-judge was evaluated and rejected — it scores only against a bare query string,
noisily and expensively, for marginal gain over the deterministic gate. Relevance at acquisition
time is split across two cheaper mechanisms that already exist: a **lexical recall-gate** on query
results *before* ingestion (drop obvious junk — §5 prefilter), and **embeddings** for semantic work
on the **retrieval/consume** side (search_library, query_library), not at the acquisition gate.
(Lexical overlap is a deliberate cheap recall filter, NOT a relevance ranker — it bounds volume; it
does not claim to out-rank embeddings. An optional embedding-similarity floor as a second gate is
noted in §5, deferred.) The LLM's value is on the consume side (lit reviews, gap analysis), which
stays an **external agent skill** over openbrain's retrieval — out of scope here (§9).

## 2. Architecture

Three actors around one postgres store. openbrain is the only shared surface; the acquisition path
and the synthesis agent never talk to each other.

```
   user / external agent                cron                 synthesis agent + skills
   crafts query via affordances    runs saved queries        reads openbrain, reasons
   → acquire                        incrementally             (lit reviews, gaps) — external
        │                                │                              │
        └──────────────► openbrain (postgres + embeddings + FTS) ◄──────┘
                    library table · chunks · venues · saved_queries
```

Acquisition runs **in-process** inside openbrain (same DB pool, same embedding path). No network
client, no staging, no host modes. A paper is fetched → prefiltered → inserted + embedded in one
local flow, reusing the existing `ingest`/`fetch_papers` machinery.

**New modules (mirroring openbrain's `tools/` + `brain.py` split):**
- `openbrain/acquire.py` — acquisition logic (search → dedup → prefilter → store). Pure-ish; calls
  `sources.openalex_search`/`crossref_search` and the existing store path.
- `openbrain/venues.py` + `venues.toml` — the venue registry: a DB `venues` table (§4) seeded from
  `venues.toml` at setup, mapping issn_l/source_id/name → canonical abbrev + kind, plus the match
  cascade and prefilter helpers (lexical overlap). Ported from `library`.
- `openbrain/tools/acquire.py` — `register(mcp)` exposing the acquisition + structured-query +
  affordance tools.

## 3. Schema delta

openbrain's `library` table (schemas/core/003_library.sql) already has: `title, authors[], year,
source, doc_type, abstract, filepath, file_hash, status, metadata JSONB, doi, bibtex, created_at`,
plus `chunks` with `embedding VECTOR(768)` and a `fts TSVECTOR` GIN index. It already has semantic
search, full-text search, a `status` lifecycle (`ingested|reading|read|archived`), and DOI dedup.

The journal is *already* stored in `source` (from Crossref `container-title` / bibtex `journal`),
but it is unreliable for journal queries: free-text, **and `_store_paper` currently runs it through
the crude `_source_slug` (first-4-chars-per-word) before insert** — so what's stored is a lossy slug,
not the real name, with **no ISSN**, conflated with booktitle/publisher. Same journal under
different spellings does not match. (Correction to the prior draft: `source` is NOT stored
human-readable today; it is slugged. This subsystem **stops slugging journals** — `source` keeps the
raw venue name as fetched, and the registry supplies the canonical abbrev. `_source_slug` dies for
venues.)

**Schema changes are baseline edits, not migrations.** Per ROADMAP/README (ALPHA: clean rewrite over
migration-safe path) and AGENTS.md (`schemas/core/` is the pre-alpha install-time baseline — edit it
and re-run `setup_db.sh` against a blank DB until real data exists), there is **no `007_`/`008_`
ALTER migration and no backfill**. Edit `schemas/core/003_library.sql` in place and add new baseline
files. Real-data migration mechanism arrives only when ROADMAP says we hit production.

**Edit `003_library.sql` — add four real columns to the `library` CREATE:**

```sql
    issn            TEXT,        -- journal venue key (OpenAlex issn_l); NULL for preprints/WPs
    venue_source_id TEXT,        -- OpenAlex source id (S…); the issn-equivalent for issn-less venues
    venue_abbrev    TEXT,        -- registry-canonical abbrev, e.g. 'AJAE','ARXIV','NBER'; NULL = uncategorized
    concepts        TEXT[]       -- OpenAlex concepts ∪ Crossref subjects ∪ source keywords
```
```sql
CREATE INDEX library_venue_abbrev_idx ON library (venue_abbrev);
CREATE INDEX library_issn_idx         ON library (issn);
CREATE INDEX library_concepts_idx     ON library USING gin (concepts);
```

- `issn` + `venue_abbrev` make "search by journal" **exact and stable** regardless of name spelling
  (`WHERE venue_abbrev = 'AJAE'`), the gap the registry fixes.
- `venue_source_id` lets an issn-less paper (arXiv, NBER WP) still join the registry — and be
  re-canonicalized later when its venue is promoted (§4 auto-grow).
- `venue_abbrev IS NULL` means "venue not yet categorized" — the paper is still stored and embedded;
  it just won't surface in venue/journal queries (only semantic + concept search).
- `concepts` (GIN-indexed) powers the prefilter's concept-intersection filter and coverage queries —
  populated from **every** source, not OpenAlex-only (§5 ENRICH, §9-was-#9).
- `source` keeps the raw venue name as fetched (un-slugged).

**Why real columns over `metadata` JSONB:** `venue_abbrev`/`issn` are queried and filtered on every
journal/coverage query; btree/GIN columns are clean and fast. `metadata` JSONB stays for everything
incidental.

**Explicitly NOT added (and why — each died with a design decision):**
- `relscore` — relevance is *query-relative* (same paper differs per query) and recomputable; it
  is a transient gate value, not a property of the paper.
- review status (`unrated/rated/accepted/rejected`) — the LLM-judge + human-review lifecycle is
  gone; a paper is fetched-and-stored or not. `library.status` already covers reading lifecycle.
- `obstatus`/`obid` — tracked best-effort sync across the tunnel; the merge makes ingest a local
  insert. Gone with the network boundary.
- `is_oa`/`oa_status` — only gated `--fetch-pdf`, which is deferred (sources rarely allow free
  PDFs). Add with PDF fetch if ever.
- per-paper `created_date`/`indexed_date` — the incremental-harvest watermark lives on the **saved
  query** (§7), not on every paper.
- `referenced_ids` — only for v3 citation snowballing, and OpenAlex `referenced_works` are stable +
  re-fetchable by id, so no capture-now penalty. Deferred (§11).

## 4. Venue registry

The registry is the lookup that maps a fetched paper's venue → a canonical `venue_abbrev`, for
journals **and** issn-less venues (arXiv, NBER, SSRN, reports, books). It is a **DB table** (not just
a TOML map) because auto-grow (below) appends discovered venues — one queryable source of truth that
cron/tools can write. Seeded at `setup_db.sh` time from `venues.toml`.

**`schemas/core/` baseline file — `venues` table:**
```sql
CREATE TABLE venues (
    abbrev      TEXT PRIMARY KEY,             -- canonical identity: 'AJAE','ARXIV','NBER','SSRN'
    full_name   TEXT NOT NULL,
    field       TEXT,
    kind        TEXT NOT NULL                 -- journal | preprint | working_paper | report | book | series
                CHECK (kind IN ('journal','preprint','working_paper','report','book','series')),
    issn_l      TEXT UNIQUE,                  -- nullable: journals only
    source_id   TEXT UNIQUE,                  -- nullable: OpenAlex source id (arXiv, NBER, …)
    match_names TEXT[],                        -- normalized aliases for name-fallback match
    origin      TEXT DEFAULT 'seed'           -- seed | discovered
                CHECK (origin IN ('seed','discovered'))
);
```

Identity is **`abbrev`**, so a venue needs no ISSN to exist. ISSN is one matching key, not the key.
Two machine keys + a name fallback, matched as a cascade (§5 CANON): `issn_l` (journals) →
`source_id` (OpenAlex source id, present for arXiv/NBER/repositories) → normalized name in
`match_names` → no match.

**Seeds (`venues.toml`):** journals keyed by `issn_l` (AER, AEJ*, QJE, AJAE, JAERE, JEEM, JARE,
Agricultural Water Management, Water Resources Research, Land Economics, ERE, ERAE, …); issn-less
venues keyed by `source_id`/`match_names` (`ARXIV` preprint, `NBER`/`SSRN`/`CEPR`/`IZA`
working_paper, USDA-ERS report, …).

**No hard allowlist.** The registry is enrichment + an **optional** `venues=` filter, never a default
restriction — a default allowlist would silently drop relevant papers when expanding into a new
topic. Topic search stays open; volume is bounded by overlap / `since` / `limit` / dedup instead.
When `venues=` is passed it is pushed **API-side** (§5 SEARCH); when absent, fetch is open.

**Auto-grow.** Any fetched venue whose `issn_l`/`source_id`/name matches nothing → store the paper
with `venue_abbrev=NULL` (still searchable) and record the unknown `issn_l`/`source_id`/name. A
maintenance step (or `list_venues(unknown=True)`) surfaces frequent unknowns for promotion into
`venues` with `origin='discovered'`; previously-NULL library rows are re-canonicalized by
`issn`/`venue_source_id`. The registry becomes a living index seeded by what's actually acquired, not
a gate maintained up front.

Ports from `library/registry.py` + `library/venues.toml` (extended: `source_id`, `kind`, auto-grow).

## 5. Acquisition pipeline (deterministic)

`acquire(query, *, venues=None, since=None, limit=50, min_overlap=2, dry_run=False)`:

```
1. SEARCH    paginated fetch, filters pushed API-side (NOT client-side):
               openalex_search(query, venues, since): &filter=
                 primary_location.source.issn:<…>  (when venues= set)
                 ,from_publication_date:<since>     (year/since bound)
               + crossref_search (query=…, filter=from-pub-date / issn)
             page via OpenAlex cursor (per-page ≤200) until `limit` survivors OR pages exhausted;
             set truncated=True if more matches remained — never silently cut.
             extend _openalex_record to capture issn_l + source.id + concepts (today omits all);
             network calls wrapped in retry+backoff (§8) for 429/transient.
2. DEDUP     normalize DOI → drop intra-batch dups; drop those already in library.
             DOI-less / cross-source dups: fall back to (normalized title + year) match
             (OpenAlex & Crossref often return the same paper with differing/absent DOI).
             (DOI unique index enforces store-side too; check first to report counts.)
3. ENRICH    crossref_lookup(doi) fills missing abstract / issn / subjects / publisher.
             concepts populated from EVERY source: OpenAlex concepts[].display_name ∪
               Crossref subject[] ∪ any source keywords — no OpenAlex-only asymmetry.
4. CANON     registry match cascade → venue_abbrev (+ keep issn, venue_source_id):
               issn_l → source_id → norm(name) in match_names → NULL (log unknown; auto-grow §4)
5. PREFILTER deterministic GATE (no LLM):
               drop: no abstract · publication year < since · venue not in venues= (if set)
               require: lexical overlap >= min_overlap (query terms in title+abstract+concepts)
               [optional, deferred] embedding-similarity floor: embed query once, cosine vs
                 candidate abstract >= threshold — strictly better filter, small added cost.
               cap: hard `limit` (post-prefilter survivor count)
6. STORE     for each survivor: insert into library (title, authors, year, source[raw, un-slugged],
               issn, venue_source_id, venue_abbrev, concepts, abstract, doi, bibtex,
               doc_type='paper') + embed the abstract chunk — via the existing
               _store_paper abstract-embed path, EXTENDED to carry the new columns (§7-was-#7;
               today _store_paper neither carries them nor un-slugs source).
7. REPORT    {found, deduped, prefiltered_out:{no_abstract,old,venue,low_overlap}, stored,
               truncated, unknown_venues:[…]}
```

The prefilter is the **only** relevance gate, deliberately a **cheap lexical recall-gate** (not a
ranker — see §1): volume control is deterministic and explicit via `venues=` (biggest lever, pushed
API-side), `min_overlap`, `since`, hard `limit`, dedup-vs-library, and the optional embedding floor.
No citation-count floor (it would penalize fresh papers, the whole point). `dry_run=True` runs 1–5
and returns the report + sample, storing nothing.

Ports from `library`: `sources/openalex.py` field mapping (issn_l, **source.id**, concepts, abstract
reconstruction — `sources.py` already reconstructs abstracts), `sources/crossref.py` enrich (+
**subject + ISSN** capture), `store/dedup.py` (+ title/year fallback), `relevance/prefilter.py`,
`registry.py`.

## 6. Deterministic query + affordance MCP tools

The genuinely missing surface today (openbrain exposes semantic `search_library` + substring
`find_library_records`, but no structured query). New tools in `tools/acquire.py`:

- **`search_library(<text>, author=None, title=None, journal=None, abstract=None, venue=None,
  kind=None, year_min=None, year_max=None, status=None, concept=None, fuzzy=False, sort='year',
  limit=50)`** — returns papers. The structured/exact filters here (venue/kind/year/status/concept,
  exact by default, `fuzzy=True` flips text fields) are the deterministic acquire-side surface:
  "search by journal/year/status" done right (exact `venue_abbrev`/`issn`). The optional `<text>`
  argument adds **relatedness ranking** (embedding ∪ findings search, merged) — that half is
  specified in the derivations plan (`2026-06-27-derivations-findings.md` §7), not here. `kind=`
  joins `venues` to filter by venue type; `venue=` filters `venue_abbrev IS NOT NULL`
  (uncategorized-venue papers surface only via text/relatedness search). A companion
  `search_findings` (findings rows + citations) also lives in the derivations plan.
- **`acquire(... dry_run=True)`** — preview: counts + venue breakdown + sample titles, no store.
  The agent's measure-and-tune loop (too broad → tighten; too narrow → broaden).
- **`list_concepts(term)`** — OpenAlex concept lookup (+ work counts) so an agent expands a query
  with *real index vocabulary*, not hallucinated synonyms.
- **`list_venues(field=None, kind=None, unknown=False)`** — the registry, so an agent targets
  `venue=` precisely. `unknown=True` lists discovered-but-uncategorized venues (issn/source_id/name
  seen during acquisition, not yet in `venues`) for promotion — the auto-grow surface (§4).
- **`coverage(topic|venue|concept)`** — what's already in the library (counts by year/venue), so
  the agent crafts gap-filling queries instead of re-fetching.
- **`remove_record(query)`** — agent-decides-junk, openbrain-executes. The post-hoc prune for
  prefilter false-positives (water-economics vs water-chemistry): judgment stays in the agent,
  execution is deterministic here. (Wraps existing delete-by-id.)

All return structured data (the MCP text responses should be machine-parseable / JSON-friendly) so
an agent drives the loop conversationally without parsing prose.

## 7. Saved queries + watermark + cron

Recurring acquisition without re-fetching the whole corpus each run.

**`saved_queries` table** (new `schemas/core/` baseline file, not a migration — §13):
```sql
CREATE TABLE saved_queries (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name             TEXT UNIQUE NOT NULL,
    query            TEXT NOT NULL,
    venues           TEXT[],                 -- optional filter (not a default allowlist)
    since            INT,
    min_overlap      INT DEFAULT 2,
    cadence          TEXT DEFAULT 'weekly',  -- nightly | weekly | monthly
    harvested_through TIMESTAMPTZ,           -- watermark: MAX source-index created_date ingested
                                             -- so far; next run queries from_created_date > this.
                                             -- NOT wall-clock run time.
    last_run         TIMESTAMPTZ,
    last_yield       JSONB DEFAULT '{}',     -- {found, stored} per run
    created_at       TIMESTAMPTZ DEFAULT now()
);
```

- **Tools:** `save_query(name, query, …)`, `list_queries()`, `run_query(name)`, `delete_query(name)`.
- **Incremental harvest:** `run_query` calls `acquire` with `from_created_date = harvested_through`
  (OpenAlex `from_created_date`; Crossref `from-index-date`), then advances `harvested_through` to
  the **max `created_date` observed in the returned batch** — *not* wall-clock run time. Advancing to
  run-time would permanently skip papers whose index `created_date` precedes run-time but which were
  indexed after the run (OpenAlex indexing lags). Max-observed-watermark is the correct, idempotent
  advance. A mature query then returns only the few genuinely new papers. The watermark is **query
  state**, not a per-paper column.
- **cron:** a scheduled `openbrain-maintenance`-style entry runs due saved queries by cadence.
  openbrain already has `scripts/openbrain-maintenance.py` + an extraction `run_at` schedule to
  follow.

This closes the v2 adaptive loop with **zero reasoning in openbrain or cron**: the external
synthesis agent, on finding a gap, calls `save_query(...)`; cron runs it thereafter. Judgment lives
in the agent; openbrain stores and runs.

## 8. bibtex export

`export_bibtex(venue=None, year_min=None, status=None, path=None)` — generate `.bib` from the
`library` table (each record already has a `bibtex` blob; the registry gives stable citekeys via
the `library` slug scheme). bibtex is a **pure export/interchange artifact**, regenerated on demand
— not a parallel store. Ports `library/store/bibtex.py` rendering + the slug scheme
(`<L1>-[<L2>|ETAL]-<YEAR>-<VENUE_ABBREV>`, collision letters) which doubles as the stable citekey.

## 9. Synthesis — out of scope (external)

Lit reviews, state-of-literature summaries, and research-gap analysis are **not** built here. They
are an **external agent skill** that drives openbrain's retrieval (`search_library`, the new
`query_library`/`coverage`) and supplies its own reasoning. Rationale: reasoning in openbrain would
break the deterministic invariant; "write a lit review" is agent work, not store work. A future
`openbrain-synthesis` skill (like the existing `openbrain-search` skill) encodes the *method*; the
agent supplies the LLM. This spec only ensures the retrieval surface (§6) is rich enough to support
it. Optional LLM topic-expansion at acquisition is likewise external (an agent improves a query,
then calls deterministic `acquire`).

## 10. Implementation — fresh in openbrain, `library` repo as reference only

**Decision: implement fresh in openbrain. Do NOT port code.** We folded `library` into openbrain;
the win is one clean codebase, not a transplant of another repo's modules. The retired `library`
repo is a **reference** for proven logic — read it to remember *how* a piece works, then write the
openbrain implementation to fit openbrain's structure (its `brain.py`/`tools/` split, its DB access
patterns, its tests). Prioritize good architecture over preserving the reference's shape.

Logic worth re-deriving from the reference (these were TDD'd there, so the algorithms are sound):
slug scheme, dedup (+ new title/year fallback for DOI-less/cross-source dups), prefilter, the
registry match cascade + `venues.toml` seeds (now a DB `venues` table with `source_id`/`kind`/
auto-grow), bibtex render + sharding. Extend openbrain's existing `sources.py` to capture what it
omits today: OpenAlex `issn_l` + **`source.id`** + `concepts`, and Crossref **`subject[]` + `ISSN`**.
New: a small retry/backoff helper around the bulk source calls (429/transient — §5 SEARCH/ENRICH).

**Retired / deleted (integration tax):** the FastMCP openbrain client, SCP staging, host
modes, the `sync` command + `obstatus` bib fields, the `LLMJudge` + `openai` dep, `relscore`,
review-status, and the standalone CLI/config/repo. The `library` GitHub repo is archived with a
README pointer here.

## 11. Out of scope / deferred

- **Citation snowballing** (`referenced_ids`) — re-fetchable from OpenAlex by id; add with v3.
- **OA PDF fetch** (`is_oa`/`oa_status`, full-text ingest) — sources rarely permit; add if needed.
- **Post-ingest semantic prune** — the centroid approach penalizes new topics; if false positives
  prove a problem, add a query-relative (not library-centroid) prune. Deferred; `remove_record`
  (§6) covers manual pruning meanwhile.
- **RePEc source** — its own rsync-mirror subsystem (searchless API; OAI harvest-only). Separate
  spec, plugs into the same `sources` + prefilter path.
- **All LLM/synthesis** — external (§9).

## 12. Testing

Follow openbrain's existing `tests/` (`test_sources.py`, `test_brain.py`):
- **prefilter / dedup / slug / registry** — pure unit tests (port `library`'s suites; they pass).
  Add: dedup title/year fallback (DOI-less + cross-source dups); registry match cascade
  (issn_l → source_id → name → NULL) incl. issn-less venues (arXiv/NBER) and unknown→NULL.
- **sources** — extend `test_sources.py`: `_openalex_record` captures `issn_l` + `source.id` +
  `concepts`; `_crossref_record` captures `subject` + `ISSN`; concept-union across both sources;
  `openalex_search` API-side `venues=`/`since` filters + cursor pagination + `truncated` flag,
  against recorded fixtures (no live network); retry/backoff on a stubbed 429.
- **acquire** — end-to-end against fixtures with a fake/stub store: search → dedup → enrich → canon
  → prefilter → report; `dry_run` stores nothing; volume bounds (venue/overlap/since/limit) enforced;
  issn-less paper stored with `venue_abbrev=NULL` (not dropped); optional embedding-floor gate.
- **schema** — `schemas/core/` (edited `003` + new `venues`/`saved_queries`) apply on a blank DB;
  `query_library` filters (incl. `kind=` join, `venue_abbrev IS NOT NULL`) return expected rows.
- **saved-query watermark** — `run_query` advances `harvested_through` to batch-max `created_date`
  (NOT wall-clock); second run requests `from_created_date > harvested_through`; lagging-index paper
  not skipped.

## 13. Migration / retirement

Pre-alpha baseline edits, no migration/backfill (ROADMAP ALPHA + AGENTS.md): edit `schemas/core/`
in place and re-run `setup_db.sh` against a blank DB.

1. Edit `schemas/core/003_library.sql` to add `issn`/`venue_source_id`/`venue_abbrev`/`concepts` +
   indexes (§3). Add baseline files for the `venues` table (§4) and `saved_queries` table (§7).
   Re-run `setup_db.sh` against a blank DB. No ALTER, no backfill — re-derivable from re-acquisition.
2. Implement the deterministic modules fresh as `openbrain/acquire.py` + `openbrain/venues.py`
   (library prototype as reference only — §10); seed `venues` from `venues.toml` at setup. Stop
   slugging `source` in `_store_paper`; extend it to carry the new columns (§5 STORE).
3. Add `tools/acquire.py` and register it in `server.py`.
4. Wire saved-query cron into the maintenance schedule.
5. Archive the `library` repo (README → this spec).

# Spec: openbrain library API — three jobs, three verbs

Supersedes the framing in `2026-06-29-external-driver-primitives-design.md`. That
doc treated the problem as "unify the input contract + robust fuzzy matching across
~13 functions." This reframes it: the library does **three jobs**, each with its own
match mechanism that already exists. The work is naming, boundaries, and a cleaner
derivation-state model — not a new matcher.

## The three jobs

1. **Fetch (external):** query outside services (OpenAlex / Crossref / Semantic
   Scholar / arXiv) by a bibliographic reference **or** a topic/text, and store the
   result in the OB library. Matching is the **external service's** job.
2. **Get (deterministic, internal):** retrieve paper(s) from the OB library by a
   structured bibliographic reference `<doi, title, journal, authors, issue, month,
   year>`. **Deterministic** match on those fields — no embeddings, no similarity
   score, no threshold. Same input → same rows.
3. **Find (semantic, internal):** retrieve papers from the OB library *related* to
   a topic text or to anchor paper(s), via **embeddings + findings**.

## Verb scheme

One verb per job; the verb names the **source + match type**:

| Verb | Job | Source | Match |
|---|---|---|---|
| `fetch_` | 1 | external services | the service's |
| `get_` | 2 | OB library | deterministic, by reference field |
| `find_` | 3 | OB library | semantic, embeddings/findings |

Mnemonic: **fetch = outside · get = exact inside · find = related inside.**

Support verbs (act on / browse job-2 data, not a job of their own): `update_`,
`remove_` (locate via the job-2 reference), `list_` / `coverage` (facet browse).

## Target tool list

User-facing library API:

| Tool | Job | Input | Notes |
|---|---|---|---|
| `fetch_papers` | 1 | reference(s) **or** topic; `source=` opt | singular+plural in one tool; calls `store_papers` internally |
| `store_papers` | 1 | list of paper dicts / citations | also callable directly (store without fetch) |
| `get_papers` | 2 | reference `<doi,title,journal,authors,issue,month,year>` | deterministic; returns row(s); >1 match → return candidates, never guess |
| `find_papers` | 3 | `reference(s)` \| `text` \| `ids` | semantic neighbors; `reference` path delegates to the `get_papers` resolver |
| `update_paper` | (2) | reference locator + fields | |
| `update_paper_status` | (2) | reference locator + status | see "derivation state" — may not survive |
| `remove_paper` | (2) | reference locator | |
| `list_papers` | (2) | facet filters (venue/kind/year/status/concept) | was `query_library` (already done right) |
| `coverage` | (2) | venue/concept | counts grouped by year |
| `list_concepts` | (2) | term | searches `concepts[]` tags |
| `export_bibtex` | (2) | facet filters → file | |

Driver-only namespace (agentic-researcher external walk — **out** of the library
API):

| Tool | Purpose |
|---|---|
| `fetch_citations` | OpenAlex citation-graph neighbors of a work (was `get_citations`; external ⇒ `fetch_`) |
| `score_relevance` | cosine of passed-in candidate texts vs the centroid of anchor papers' stored embeddings |

### Rename map (from current)

- `fetch_paper` / `fetch_papers` → `fetch_papers` (one tool)
- `acquire` → **removed**; folded into `fetch_papers(source=…)`
- `store_paper` → `store_papers` (accepts a list; callable directly)
- `get_library_record` → `get_papers` (input → structured reference, not `query: str` ILIKE)
- `query_library` → `list_papers`
- `update_library` → `update_paper`
- `update_library_status` + `update_status` → `update_paper_status` (merge the two)
- `remove_record` → `remove_paper`
- `search_library` → `find_papers`
- `get_citations` → `fetch_citations` (driver namespace)
- `score_relevance` → unchanged (driver namespace)
- `coverage`, `list_concepts`, `export_bibtex` → unchanged

## Matching contracts

### `get_papers` (deterministic, job 2)
- DOI present → exact lookup (the ~98% case).
- No DOI → deterministic match on the **conjunction of normalized structured
  fields** (title + journal + year + authors). "Robust to misspelling / apostrophe /
  hyphen" = **better field normalization** (a deterministic transform), **not** fuzzy
  ranking. A row matches the fields or it doesn't.
- >1 deterministic match → return candidates; never auto-pick.
- The current `query: str` → `title/doi/author ILIKE %query%` behavior in
  `get_library_record` / `update_*` / `remove_record` is the defect being removed.

### `find_papers` (semantic, job 3)
- Anchor inputs: `ids` (papers in OB — use stored chunk embeddings, possibly
  full-text level), `text` (arbitrary — a topic string or bibtex entry), or
  `reference(s)` (delegates to the `get_papers` resolver, then anchors on the
  resolved rows).
- Operation: `cosine(embed(anchor), stored library embeddings)`; multiple anchors →
  centroid (mean of their embeddings).
- Returns the resolved anchors alongside the neighbors (transparency — don't swallow
  resolution). Ambiguous reference → anchor on all matches (centroid). Zero matches →
  not-found.
- `reference` resolution lives in **one** brain-level resolver that both `get_papers`
  and `find_papers` call — composition, not duplication.

### Why `fetch_citations` / `score_relevance` are not job 3
Job 3 is "relate by embeddings/findings over the OB library." Citation-graph
neighbors come from **OpenAlex** (external ⇒ `fetch_`), and `score_relevance` scores
**not-yet-stored external candidate texts** against an internal centroid. Both serve
the agentic-researcher driver's external walk, not user-facing library retrieval.

## Derivation-state model (replaces the status enums)

Drop `library.status` (`ingested/reading/read/archived`) and `library.findings_status`
(`pending/done/error`).

**Rule: `null = todo`, present = done** — the way embeddings already work
(`backfill WHERE embedding IS NULL`, no status column, no error state; failure just
retries).

- **Embeddings:** `embedding IS NULL` → needs embedding. (unchanged)
- **Findings:** `findings IS NULL` → not run; `findings = '[]'`/empty (non-null) →
  ran, found nothing. This preserves the only thing the enum bought us
  ("zero findings ≠ unprocessed") without a status column.
- **Has full text:** `filepath IS NOT NULL`. No reading enum — a record existing
  means it was ingested.

### Abstract-level vs full-text-level derivations (staleness)
When a PDF/text is added to an abstract-only record, its embeddings/findings were
derived from the abstract and must be recomputed from full text. Track this as a
**fact in the data (provenance)**, not a maintained flag:

- `chunks.source = 'abstract' | 'fulltext'` — what the chunk was built from.
- **Stale** = a query, not a flag:
  `filepath IS NOT NULL AND EXISTS (chunk WHERE source = 'abstract')`.
- Recompute keys off that mismatch, derived from data — correct regardless of which
  code path added the file. Re-embedding full text *requires* re-chunking anyway
  (new rows, `source='fulltext'`, `embedding=null`); the existing backfill then
  heals them, and the rebuild replaces the old abstract chunks.

This is robust where a "null the embeddings when filepath is set" procedural step
would be fragile (any path that forgot to null would leave silent stale embeddings).

## Out of scope / open

- The bulk-harvest semantics of `acquire` (no-LLM lexical prefilter, `min_overlap`)
  must be preserved as a mode of `fetch_papers(source=…)` — confirm the prefilter
  behavior survives the fold.
- `update_paper_status`: with the reading enum gone, what status remains to update?
  If nothing, drop the tool. Decide during implementation.
- Field-normalization rules for `get_papers` (apostrophe/hyphen/accents/`&`) — reuse
  and extend the existing `_normalize_title` transform; specify the full field set.
- Migration: this renames MCP tools and changes input contracts; the
  agentic-researcher MCP client (`src/researcher/openbrain.py`) and any skill files
  in `integrations/` must be updated in lockstep. Decide on back-compat aliases vs
  hard cut.

## Key files (for implementation)
- `openbrain/sources.py` — external lookups, `resolve()`, `_normalize_title`.
- `openbrain/brain.py` — `fetch_paper(s)`, `_store_paper`, `store_acquired_paper`,
  `find_library_records`, `query_library_struct`, `coverage_stats`, `relevance`,
  backfill helpers; the shared `resolve_reference` resolver lands here.
- `openbrain/tools/{library,acquire,driver}.py` — MCP tool wrappers.
- `schemas/core/003_library.sql` — drop status columns; add `chunks.source`.

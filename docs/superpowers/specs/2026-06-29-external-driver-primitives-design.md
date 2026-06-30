> **SUPERSEDED (2026-06-30)** by `2026-06-30-library-api-three-jobs.md`.
> That doc reframes this: the library does three jobs (fetch/get/find), each with a
> match mechanism that already exists — the work is naming, boundaries, and a cleaner
> derivation-state model, not the unified fuzzy matcher proposed below. Kept for
> history.

# Problem: robust bibliographic-reference query interface for openbrain

## Goal

Standardize the query/matching interface on openbrain's existing functions so a
caller specifies *which paper(s)* using a **bibliographic reference** — not an
internal id — matched robustly.

A reference is any subset of: `doi`, `title`, `journal`, `issue`, `year`, `authors`.

Matching rules:
- **DOI preferred** — if present, exact lookup.
- **No DOI** — match on `title` + `journal` + `issue` + `year` + `authors` together.
- **Robust to title noise** — misspellings, apostrophes, hyphens. The structured
  fields (author/year/journal) corroborate a fuzzy title hit so it doesn't resolve
  to the wrong paper.

Applies to two existing families of functions (infrastructure already exists — this
is about their input contract, not new machinery):

**1. External-fetch functions** (query outside sources — OpenAlex/Crossref/S2 — AND
save to the openbrain db):
- `fetch_paper` (`tools/library.py`)
- `acquire` (`tools/acquire.py`)
- `get_citations`, `store_paper` (`tools/driver.py`)

**2. Internal-db-query functions** (query the openbrain library itself):
- `search_library`, `get_library_record`, `update_library`, `update_library_status`,
  `update_status` (`tools/library.py`)
- `query_library`, `remove_record`, `export_bibtex` (`tools/acquire.py`)

Both families should accept the same reference and match the same robust way.

## Out of scope

Functions that find **related** papers (citation walk, relevance scoring —
`score_relevance`, the agentic-researcher driver-side `expand`) can key on whatever
internal id is convenient (uuid, OpenAlex W-id). Robustness only matters at the
**reference → paper** boundary, not in relation traversal.

## Current behavior / what's wrong

- Several functions take a single `query` string or single `identifier`, not a
  structured reference. `remove_record(query)`, `get_library_record(query)`,
  `search_library(query)` resolve via `title/doi/author ILIKE %query%`.
- `fetch_paper`'s title resolution (`sources.py` `resolve()`, ~line 608) uses an
  **exact normalized-title equality gate** (`_normalize_title`): lowercase, strip
  accents, punctuation→space, collapse whitespace. Too strict for the goal:
  punctuation→space makes `"Cattlemen's"` != `"Cattlemens"`, tolerates no
  misspellings, and ignores author/year/journal as corroborating signal.
- `get_citations(paper_id)` requires an **OpenAlex W-id** specifically — not
  reference-based, and OpenAlex-bound.

## Key files

- `openbrain/sources.py` — external source lookups + `resolve()` + `_normalize_title`
  (the matching gate, ~line 540 / ~608).
- `openbrain/brain.py` — `fetch_paper`/`fetch_papers` (~1870/1907); library helpers
  `find_library_records` (~2189), `query_library_struct` (~2513),
  `delete_library_record` (~2642).
- `openbrain/tools/{library,acquire,driver}.py` — the MCP tool wrappers above.

## Context notes

- Library DOI coverage is ~98% (542/550 rows); title-only matching is the ~2% tail
  plus any external-source title lookups.
- The agentic-researcher driver (separate repo) consumes these via an MCP client
  (`src/researcher/openbrain.py`). Its citation-walk (`expand.py`) currently passes
  library uuids to `get_citations`, which expects OpenAlex W-ids → returns nothing.
  That is the "related papers" layer (out of scope above) but explains why the id
  contract surfaced.

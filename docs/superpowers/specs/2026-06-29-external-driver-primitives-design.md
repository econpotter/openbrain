# openbrain — External-Driver Primitives Design

Date: 2026-06-29
Status: design approved, pre-implementation
Consumer: the `agentic-researcher` driver (a separate orchestra project) that grows
and expands a paper library on top of openbrain. openbrain owns the library,
retrieval, sources, and embeddings; it owns **no agenda** (issue #004). These are the
deterministic, parameterized primitives the driver needs so the agenda (which fronts,
when, what radius/budget) can live entirely in the driver.

Two phases, shipped as two orchestra issues:
- **Phase A** — `acquire` returns the stored paper IDs. Tiny; unblocks the driver's
  per-front membership tracking. Independent of B.
- **Phase B** — citation-neighbor fetch, anchor-relative relevance scoring, single-paper
  store, and a `search-library` CLI. The primitives behind the driver's citation walk
  and library-label fronts.

## Principles

- **No new agenda in openbrain.** Radius, anchors, budget, hop-depth, visited-set are
  the driver's; these primitives take policy as parameters, exactly as
  `acquire(query, min_overlap=…)` already does. No walk, no gate, no scheduler here.
- **Deterministic, no LLM.** `relevance` uses the existing `brain.embed`; cosine is
  arithmetic. No generation anywhere.
- **Embeddings stay inside openbrain.** The driver never receives raw vectors —
  `relevance` returns cosines. (Don't add a "get embedding by id" surface; it isn't
  needed and would leak vectors across the boundary.)
- **Reuse existing machinery:** `sources._openalex_record`, `brain.embed`,
  `brain.store_acquired_paper`, `query_library`/`search_library`. No new deps —
  cosine via stdlib `math`.
- **Tests stay offline** (the project convention): mocked asyncpg pool, monkeypatched
  HTTP and embeddings, no live network/DB. `ruff` clean. JSON CLI output via `_emit`.

---

## Phase A — `acquire` returns `stored_ids`

`acquire`'s report currently carries counts (`found`, `deduped`, `stored`, `sample`)
but no IDs, so an external driver cannot tell *which* papers landed. `store_acquired_paper`
already `RETURNING id, title`; capture and surface them.

- In `openbrain/acquire.py`: accumulate `stored_ids: list[str]` in the store loop —
  append the returned id wherever `stored` is incremented (the not-deduped branch).
- Add `"stored_ids": stored_ids` to the returned report dict.
- The `acquire` CLI already `_emit`s the whole report, so `stored_ids` flows out for free.

**Acceptance:**
- `acquire(...)` report includes `stored_ids`, in store order, one id per newly stored
  paper (deduped papers not included).
- Existing acquire behavior/counts unchanged.
- Offline test asserts `stored_ids` matches the mocked store returns; suite + ruff green.

---

## Phase B — citation neighbors, relevance, store-one, search-library

Exact shapes may be adjusted to openbrain conventions during implementation; the
behavior and CLI contract below are the requirement.

### B1. `sources.openalex_citation_neighbors(openalex_id: str) -> list[PaperMeta]`
One OpenAlex `works/{id}` fetch: read `referenced_works` (outbound edges) and follow
`cited_by_api_url` (inbound, first page). Return de-duplicated `PaperMeta` (the existing
TypedDict: id/title/abstract/doi/year/…) via `_openalex_record`. Network-touching code
is monkeypatched in tests (fixture work with `referenced_works` + a `cited_by` page).

### B2. `brain.relevance(anchor_ids: list[str], candidate_texts: list[str]) -> list[float]`
Centroid = component-wise mean of the stored embeddings for `anchor_ids` (read from the
library table; skip ids lacking an embedding). Embed each candidate text via
`brain.embed`. Return `cosine(centroid, candidate)` per text, order preserved. Empty
`anchor_ids` or an all-empty centroid → raise `ValueError` (the driver's walk requires
anchors). Cosine via stdlib `math`.

### B3. `brain.store_one(meta: dict) -> dict`
Thin wrapper over `store_acquired_paper(meta)`; returns `{"id", "title", "deduped"}`.

### B4. CLI commands (all `_emit` JSON)
- `citations <openalex_id>` → `{"neighbors": [PaperMeta, …]}`
- `relevance --anchor <id> [--anchor <id> …] --text <t> [--text <t> …]` → `{"cosines": [float, …]}` (order matches `--text`)
- `store-paper --json '<meta>'` → `{"id", "title", "deduped"}`
- `search-library <query> --limit K` → `{"ids": [paper_id, …]}` (top-K semantic hits, ids only) over the existing `search_library`

**Acceptance:**
- B1–B4 implemented with the signatures/CLI contract above; reuse `_openalex_record`,
  `brain.embed`, `store_acquired_paper`, existing `search_library`.
- `relevance([], …)` raises `ValueError`; `relevance` returns one cosine per text in order.
- `citations` de-duplicates references ∩ citing-works.
- Offline tests (mocked pool, monkeypatched HTTP + embeddings) cover each; no live
  network. Full suite + ruff green.

## Non-goals
- No citation walk, relevance gate, frontier/visited/budget logic — those are the driver's.
- No raw-embedding export, no batch embedding API beyond `relevance`.
- No changes to the acquire agenda (openbrain still owns no saved-query/scheduler).

# Spec: Phase 2 — `find_papers` (semantic retrieval, job 3)

Refines the job-3 slice of `2026-06-30-library-api-three-jobs.md`. Phase 1 shipped
`get_papers` + `resolve_reference`. This phase renames `search_library` → `find_papers`
and gives it the three anchor modes. Hard cut, no alias (the agentic-researcher client
migrates in lockstep).

## Contract

`find_papers` = topical/semantic retrieval over the OB library (job 3). Anchor inputs,
in precedence order:

1. **`ids`** — comma-separated library uuids. Centroid of their stored chunk embeddings
   → nearest library chunks, **excluding the anchors themselves**.
2. **reference** — any of `doi/title/journal/authors/year`. Delegates to
   `brain.resolve_reference` (Phase 1) → resolved row(s) become the anchors; then same
   centroid search. 0 matches → not-found message. >1 matches → anchor on all (centroid).
3. **`text`** — natural-language topic. Embed → cosine vs library chunks (the existing
   `search_library_vec` path).

Exactly one mode runs (ids > reference > text). None supplied → an instructional error.

## Decisions (not in the umbrella spec)

- **Reference resolution is delegated, not duplicated** — `find_papers` calls the same
  `brain.resolve_reference` that `get_papers` uses. One resolver, two callers.
- **Transparency** — the JSON result gains an `anchors` array (resolved anchor rows:
  id/title/year) alongside `ids` + `results`, so the caller sees what was anchored on.
  Text mode returns `anchors: []`.
- **Exclude anchors** from neighbor results (you want related *others*).
- **No rerank on the centroid path** — the cross-encoder reranker is text-query based;
  a centroid has no query text, so the anchor path returns cosine-ordered rows without
  rerank. Text mode keeps rerank.
- **Empty/missing embeddings** — if no anchor has a usable embedding (model was down at
  ingest), `search_library_by_anchors` raises `ValueError`; the tool returns a readable
  "cannot find related" message rather than crashing.
- **Brain-level names unchanged** — keep `search_library_vec` (text path). Add
  `search_library_by_anchors` (centroid path) and extract a shared
  `_search_chunks_by_vector` core. Only the *MCP tool* renames.

## Result shape (unchanged keys + `anchors`)

```json
{
  "anchors": [{"id": "...", "title": "...", "year": 2020}],
  "ids": ["...", "..."],
  "results": [{"id","title","authors","year","source","similarity","section","excerpt"}]
}
```

## Cross-repo migration (hard cut, lockstep)

**openbrain repo:**
- `openbrain/tools/library.py` — `search_library` tool → `find_papers`.
- `openbrain/brain.py` — `_search_chunks_by_vector` (extracted) + `search_library_by_anchors` (new).
- In-repo refs: `tests/test_driver.py` (`tools["search_library"]` → `tools["find_papers"]`),
  `tests/test_acquire.py` (cli search-library test calls the tool),
  `openbrain/server.py` + `openbrain/resources.py` (prose references),
  `integrations/skills/openbrain-search/SKILL.md` + `integrations/README.md`.
- `search_library_vec` brain fn name stays (internal) — `tests/test_brain.py` unaffected.

**agentic-researcher repo (`projects/agentic-researcher`):**
- `src/researcher/openbrain.py` — `search_library(query, limit)` method → `find_papers(text, limit)`
  calling tool `find_papers` with `{"text": ..., "limit": ...}`; update the module docstring
  tool list (line ~9–11).
- `src/researcher/cli.py:94` — `ob.search_library(...)` → `ob.find_papers(...)`.
- `tests/test_cli.py:59` — stub method rename.
- Test cmd: `uv run pytest`.

## Out of scope (later phases)
- `fetch_citations` / `score_relevance` driver split (Phase 5).
- `fetch_papers`/`store_papers`/`acquire` fold (Phase 3).
- schema/status removal + `chunks.source` (Phase 4).

## Key files
- `openbrain/brain.py` — `search_library_vec` (~2035), `relevance`/`_mean_vector`/`_cosine` (~1819), `_candidate_count`, `_reranked`.
- `openbrain/tools/library.py` — `search_library` tool (~305).
- `projects/agentic-researcher/src/researcher/{openbrain,cli}.py`, `tests/test_cli.py`.

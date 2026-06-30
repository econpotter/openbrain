# Defensive review — openbrain × agentic-researcher (2026-06-30)

Goal: make the automated research pipeline actually work, end to end, verified
against the **live deployed server** (not just unit tests).

## TL;DR

Both automated pipelines now work end-to-end against the deployed openbrain:

- **Topic-term harvest** (`collect`): topic term → `fetch_papers(topic, store=False)`
  → `score_relevance` vs the topic's core papers → keep within radius →
  `store_papers`. Live test stored 10 on-topic water-rights papers, then cleaned up.
- **Citation walk** (`expand`): seed paper → `get_citations` → `score_relevance` →
  keep on-topic → `store_papers` → walk outward. Live test followed 16 neighbors,
  stored 3 on-topic, then cleaned up.

One real bug was found and fixed (the citation walk was completely broken); every
other tool was exercised live and works.

## The bug that was breaking citation expansion

`expand` walks from papers the topic already has — those are **library uuids**.
But `get_citations` only accepted an **OpenAlex W-id**, so it 404'd and returned
zero neighbors. And openbrain's citation-neighbor records have no `id` key (they
carry `url` = the W-id), so the driver's `candidate["id"]` would have `KeyError`d
anyway. Net: citation expansion silently did nothing / crashed.

Fix (both repos, verified live):

- **openbrain** `brain.get_citations(ref)` now accepts a **library uuid** (→ its
  stored DOI → OpenAlex work), a **DOI**, or a **W-id**. Tool param kept as
  `paper_id` for client compatibility. (commit `57b3798`)
- **agentic-researcher** `expand.py` now dedups neighbors by **doi/title** (they
  have no library id), stores the on-topic keepers (which mints a library uuid),
  and walks outward from those uuids. (commit `2e9ceba`)

## Live tests run (against the deployed MCP server)

| Tool / flow | Result |
|---|---|
| `find_papers(text)` | ✅ ids + results |
| `find_papers(ids)` | ✅ neighbors (anchors echo is `[]` in ids-mode — cosmetic) |
| `get_papers(id=uuid)` | ✅ |
| `get_papers()` no-arg | ✅ guard message |
| `list_papers(limit)` | ✅ |
| `score_relevance(core_ids, texts)` | ✅ cosines |
| `fetch_papers(identifier=DOI)` | ✅ fetch+store+embed |
| `fetch_papers(topic, store=False)` | ✅ 10 candidates, stored 0 |
| `store_papers([...])` | ✅ full citation per row |
| `update_paper(id, ...)` | ✅ |
| `remove_paper(id)` | ✅ |
| `get_citations(uuid)` | ✅ 16 neighbors (was 0 before the fix) |
| `collect_for_topic` end-to-end | ✅ 10 stored, cleaned up |
| `expand_by_citations` end-to-end | ✅ 3 stored, cleaned up |

All test papers stored during live tests were removed afterward; the library is
back to its prior state.

## Known edge cases / behavior (not bugs, worth knowing)

- **DOI-less library papers can't seed the citation walk.** `get_citations(uuid)`
  resolves uuid → DOI → OpenAlex; a row with no DOI returns `[]` (no crash, just
  no expansion from it). ~98% of the library has DOIs, so this is the ~2% tail.
- **`score_relevance` raises if no core paper has an embedding.** If the embed
  model was down when the core papers were stored, they land with NULL embeddings
  and the relevance gate has no centroid → `ValueError`. Run `backfill_embeddings`
  first. (Core papers normally embed at fetch time.)
- **`find_papers(ids=...)` returns `anchors: []`** (anchors are only echoed for the
  reference path). Cosmetic — the caller passed the ids.

## State of the broader rename/cleanup (for context)

Shipped + deployed: get/find/fetch/store/update/remove all take the library uuid;
deterministic resolver shared across get/update/remove; `fetch_papers` JSON;
`store_papers` full-citation + DOI/title dedup; `list_papers`; reading-`status`
dropped; invalidate-on-upgrade ingest; citation walk fixed.

Intentionally NOT done: nothing outstanding that blocks the pipeline. The
`get_citations`→`fetch_citations` cosmetic rename was skipped to avoid a
needless lockstep break; the tool works under its current name.

# Plan F: Unify the thought write path — server-side extraction + straggler backstop

## Goal

Every captured thought — whether the caller is a human-curating agent or an
automated hook that supplies nothing but `content` — ends up with the same three
derived assets: **extracted metadata**, an **embedding**, and **entity links**.
Any step that can't run at write time (model down, latency budget) is *flagged*,
not lost, and a fast maintenance sweep fills only the missing piece. This mirrors
how the library already treats embeddings and how findings already treats
extraction: derive on write, backfill stragglers.

Nightly consolidation (`run_memory_extraction`) is unaffected — it stays a
separate synthesis pass. This plan is only about first-pass derivation.

---

## Why (current gaps)

- `brain.store_thought` (brain.py:616) embeds inline via `_embed_or_none`, which
  fail-softs to `NULL` on model outage — but **no sweep ever backfills a thought
  embedding**. `backfill_chunk_embeddings` / `list_pending_embeddings`
  (maintenance.py) cover **library chunks only**. A thought that missed its embed
  is unsearchable forever. Library has a safety net; thoughts don't.
- Thought **metadata** (`tags`/`people`/`projects`/`action_items`/`importance`)
  is **caller-supplied only**. `store_thought` stores what's passed; no model
  runs. The `capture_thought` docstring claims "Extracts and stores metadata" —
  aspirational; the code doesn't. A manual capture works because the calling
  agent curates. An **automated** capture (Stop hook, Plan G) lands raw with
  empty metadata — the exact case auto-capture must handle.
- The `thoughts.enriched BOOLEAN DEFAULT false` column (schemas/core/002_thoughts.sql)
  already exists as the extraction-done marker but is **never set** on the write
  path. Dormant flag, ready to use.

## Architecture position

openbrain is development-stage (ALPHA). Prefer the clean shape: `store_thought`
becomes the single derivation pipeline, symmetric with `ingest_document_pipeline`
and `findings.extract_findings`. No compat shim for the old "metadata is whatever
you passed" behavior — curated captures still pass metadata; the server now fills
the gaps instead of leaving them empty.

Decision (approved): extraction runs **deferred** — the write path does not block
on the extraction model. Write immediately, mark `enriched=false`, let a fast
sweep enrich. Keeps writes cheap and outage-tolerant, matching the existing
embedding degradation path. (Embedding stays inline-attempt-then-backfill as it
is today.)

---

## Design

### 1. Extraction as a per-thought derivation (mirror `findings.extract_findings`)

Add `brain.extract_thought_metadata(thought_id)` (or a `thought_meta.py` module
parallel to `findings.py`):

- Load the thought `content`.
- Build a deterministic extraction prompt (topics/tags, people, projects,
  action_items, importance 0–6, suggested type).
- Route through the existing `brain.complete_extraction_prompt(...)` (the
  configurable `extraction:` LiteLLM block — deepseek-v4-flash + gemini fallback,
  temperature 0.0). Reuse; do not add a new model config.
- Validate/parse output; off-schema → leave `enriched=false` (straggler), never
  write garbage.
- On success: merge extracted fields into `metadata` via the existing
  `merge_thought_metadata` semantics (union arrays, don't clobber caller values —
  caller-supplied metadata wins where present), set `enriched=true`, and trigger
  entity refresh/link for any newly discovered people/projects (reuse
  `_refresh_mentioned_entities` / `_link_thought_entities`).

`store_thought` change: after upsert, if the thought is not already `enriched`
and metadata is sparse, **schedule** extraction (fire-and-forget task, like the
existing `asyncio.create_task(_link_thought_entities(...))`) rather than awaiting
it. Write latency unchanged.

Define "sparse" explicitly (data in config, not magic literals): a thought needs
extraction when `enriched=false`. Curated captures that already carry rich
metadata may set `enriched=true` at the tool layer to opt out; automated/raw
captures leave it false. Do **not** guess sparsity from field counts in logic.

### 2. Straggler backstop for thoughts (mirror library backfill)

Two straggler conditions on `thoughts`:
- **Missing embedding**: `embedding IS NULL`.
- **Missing extraction**: `enriched = false`.

Extend maintenance so both are visible and fixable, parallel to the library
tools:

- `brain.list_pending_thoughts(limit)` → thoughts missing embedding and/or
  extraction, with which step is pending.
- `brain.backfill_thought_embeddings(limit)` → embed rows where `embedding IS
  NULL` (reuse `embed`; never substitute a different model — same rule as
  `backfill_chunk_embeddings`).
- `brain.backfill_thought_extractions(limit)` → run `extract_thought_metadata`
  for rows where `enriched=false`.
- Surface all three as MCP tools in `tools/maintenance.py` alongside the existing
  `backfill_embeddings` / `list_pending_embeddings` / `backfill_links`. Same
  "safe to re-run, only touches pending" contract.

### 3. Fast sweep cadence

The deferred write-path task handles the happy path within seconds. The
maintenance backfills are the **failure** backstop (model was down). Wire them
into the same runner that already schedules maintenance (systemd / the
maintenance entrypoint) on a short cadence so a straggler is fixed within
minutes, not at next nightly. Do not fold this into nightly synthesis — separate
concern, separate schedule. If a cap is applied (e.g. `limit`), the tool already
reports remaining pending — keep that (fail loud, no silent truncation).

---

## Out of scope

- The auto-capture trigger (Stop hook, global instructions) — that's **Plan G**.
- Nightly consolidation/synthesis (`run_memory_extraction`) behavior.
- Adaptive capture classification (Plan D, Step 4 — still deferred).

---

## Acceptance criteria

1. A thought stored with only `content` (no metadata) ends up, without any manual
   step, with model-extracted `tags`/`people`/`projects`/`action_items`/
   `importance` in `metadata`, `enriched=true`, a non-NULL `embedding`, and
   entity links for any people/projects — verified against a live server, not a
   mock.
2. Caller-supplied metadata is preserved (not clobbered) when extraction also
   runs; array fields union.
3. A thought whose embedding failed at write time (`embedding IS NULL`) is
   embedded by `backfill_thought_embeddings`; a thought with `enriched=false` is
   enriched by `backfill_thought_extractions`. Both are listed by
   `list_pending_thoughts` beforehand and gone after.
4. Extraction is deferred: `store_thought` write latency does not include an
   extraction-model round trip (the extraction task is scheduled, not awaited).
5. Extraction reuses the existing `extraction:` config block — no new model
   config keys.
6. The three new backstop tools are safe to re-run and report remaining pending
   counts (no silent cap).
7. Tests cover: sparse capture → full enrichment; extraction-model-down →
   straggler flagged → backfilled; embed-down → straggler flagged → backfilled;
   metadata-merge precedence.

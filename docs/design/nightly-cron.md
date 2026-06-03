# Design: Nightly Maintenance Cron

## Goal

Offload expensive memory consolidation from real-time conversations. Raw thoughts captured during the day are good enough for immediate recall, but over time they accumulate duplicates, contradictions, and noise. The nightly job cleans this up using idle local compute.

---

## What it does

Runs at ~2 AM on Computer A:

1. Queries all thoughts created in the past 24 hours with `type` not in `('synthesis', 'archived')`.
2. Batches them and sends to the configured extraction model with a consolidation prompt:
   - Deduplicate redundant entries
   - Resolve conflicting facts
   - Extract permanent rules or constraints
   - Surface project milestones or decisions worth preserving
3. Writes clean synthesized outputs back as new thoughts with `type: synthesis`.
4. Marks processed raw thoughts as archived by setting `metadata.archived: true`.

---

## Compute routing

The consolidation model is configured in `config.yaml` under an `extraction`
section. A local model routed through LiteLLM is the intended low-cost default,
but the code reads the provider, model name, URL, schedule, lookback window, and
batch size from config rather than assuming a specific model.

---

## Docker Compose scheduling

The job runs from the repository-level Docker Compose stack as the
`maintenance` service. The container runs `scripts/openbrain-maintenance.py
--loop`, sleeps until `extraction.run_at`, and runs once per day.

---

## What it does not do

- It does not delete raw thoughts — it marks them archived in metadata. The `thoughts` table is append-only; nothing is deleted.
- It does not re-embed chunks or library records — that's a separate operation triggered by `refresh_all_embeddings()`.
- It does not run during the day — synthesis is a background batch operation, not a real-time concern.

---

## Status

Implemented. The thought schema supports `type: synthesis` and
`metadata.archived`; the maintenance script and Compose service run the daily
job.

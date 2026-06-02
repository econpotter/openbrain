# Design: Nightly Maintenance Cron

## Goal

Offload expensive memory consolidation from real-time conversations. Raw thoughts captured during the day are good enough for immediate recall, but over time they accumulate duplicates, contradictions, and noise. The nightly job cleans this up using idle local compute.

---

## What it does

Runs at ~2 AM on Computer A:

1. Queries all thoughts created in the past 24 hours with `type` not in `('synthesis', 'archived')`.
2. Batches them and sends to a local model via Ollama (routed through LiteLLM) with a consolidation prompt:
   - Deduplicate redundant entries
   - Resolve conflicting facts
   - Extract permanent rules or constraints
   - Surface project milestones or decisions worth preserving
3. Writes clean synthesized outputs back as new thoughts with `type: synthesis`.
4. Marks processed raw thoughts as archived by setting `metadata.archived: true`.

---

## Compute routing

The consolidation model runs locally on a desktop (Ollama) routed through LiteLLM. This keeps token costs at zero and lets the job run as slowly as it needs to without blocking anything. The LiteLLM model name is configured in `config.yaml` under a `maintenance` section.

---

## Systemd timer

The job runs as a systemd timer on Computer A. See `scripts/openbrain-backup.timer` for the timer unit pattern. A `openbrain-maintenance.timer` / `openbrain-maintenance.service` pair should follow the same structure.

---

## What it does not do

- It does not delete raw thoughts — it marks them archived in metadata. The `thoughts` table is append-only; nothing is deleted.
- It does not re-embed chunks or library records — that's a separate operation triggered by `refresh_all_embeddings()`.
- It does not run during the day — synthesis is a background batch operation, not a real-time concern.

---

## Status

Not yet implemented. The thought schema supports `type: synthesis` and `metadata.archived`. The cron script and systemd units are to be built.

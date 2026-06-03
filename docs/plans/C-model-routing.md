# Plan C: Model Routing & Nightly Maintenance

## Goal

Route all model calls through a configurable backend (LiteLLM or direct provider), assign different operations to the right cost tier, and automate nightly thought consolidation using idle local compute.

---

## Status

- [x] openbrain MCP server running on Computer A
- [x] LiteLLM embedding backend supported through `config.yaml`
- [x] Nightly maintenance script written
- [x] Nightly maintenance runs from the Docker Compose stack

---

## Step 1: Standardize embeddings via `config.yaml`

All machines must generate vectors with the same model and dimensions or search
breaks. Set `embed.provider` and `embed.model` in `config.yaml` to one
consistent source:

```yaml
litellm_url: http://litellm:4000

embed:
  provider: litellm          # or: voyage, openrouter
  model: <1536-dimensional-embedding-model>
```

If switching from a previous backend with a different dimension, follow the migration steps in `docs/design/model-config.md` (re-embed all vectors, add dimension migration).

---

## Step 2: Cost-layering strategy

Route different memory operations to the cheapest capable tier:

| Operation | Model tier | Rationale |
|-----------|-----------|-----------|
| Embedding (ingest + search) | Voyage AI, OpenRouter, or LiteLLM-routed embedding model | Dimension consistency required |
| Runtime semantic recall | Gemini Flash or similar fast/cheap model | Large context window, low input cost, low latency |
| Nightly synthesis | Local Llama / Mistral via Ollama | Zero token cost; can run slowly overnight |

Configure model names per operation in `config.yaml` under their respective
sections (`embed`, `extraction`).

---

## Step 3: Nightly maintenance cron

See `docs/design/nightly-cron.md` for full design. Implementation steps:

1. Write `scripts/openbrain-maintenance.py`:
   - Query thoughts from past 24 hours where `type NOT IN ('synthesis', 'archived')`
   - Batch and send to local model via LiteLLM with consolidation prompt
   - Write synthesized outputs as `type: synthesis` thoughts
   - Set `metadata.archived = true` on processed raw thoughts

2. Run the maintenance script from the Docker Compose stack:
   ```bash
   docker compose up -d maintenance
   ```

---

## Notes

- The nightly job never deletes thoughts — `thoughts` is append-only. Archiving sets `metadata.archived: true` only.
- LiteLLM is one provider option. Direct Voyage AI and OpenRouter calls are
  equally valid. Local sentence-transformers embeddings are not implemented.
- The extraction synthesis model is also configured in `config.yaml`; the code
  does not assume a specific local model name.

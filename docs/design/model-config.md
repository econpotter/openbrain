# Design: Model Provider Configuration

## Goal

No model provider or model name is hardwired into Open Brain. The embedding backend, model selection, and any LLM routing are fully configurable — swappable without code changes.

---

## Config split

| File | Contents |
|------|----------|
| `config.yaml` | Non-secret settings: embedding backend, model names, LiteLLM URL, library path, chunk size, search thresholds |
| `.env` | Secrets only: API keys, `DATABASE_URL`, transport settings |

Adding a new model or switching providers should require only a change to `config.yaml`, not to code.

---

## Embedding backends

The `embed_backend` key in `config.yaml` selects the backend:

| Value | Description |
|-------|-------------|
| `local` | sentence-transformers running on the host (free, no API key) |
| `voyage` | Voyage AI API (`VOYAGE_API_KEY` in `.env`) |
| `openai` | OpenAI embeddings API (`OPENAI_API_KEY` in `.env`) |
| `litellm` | Route through a LiteLLM proxy (URL in `config.yaml`, key in `.env`) |

All backends are accessed through `brain.embed()`. Tool code never calls a provider SDK directly.

---

## LiteLLM

LiteLLM is one supported routing option, not a requirement. When `embed_backend: litellm`, all embedding calls go through the proxy at the configured URL. Direct provider calls (Voyage, OpenAI, local) are equally valid and may be preferable when latency or simplicity matters.

---

## Switching backends

Switching embedding backends requires re-embedding all existing vectors if the new model has a different output dimension. Steps:

1. Update `embed_backend` (and `embed_model` if needed) in `config.yaml`.
2. Add a migration to alter the `VECTOR(N)` dimension in the schema if it changed.
3. Run `brain.refresh_all_embeddings()` to re-embed people and projects.
4. Re-embed `thoughts` and `chunks` (no built-in bulk tool yet — add one if needed).

---

## Entity embedding enrichment

People and project embeddings are not just their structured fields. They incorporate snippets from thoughts that mention them, and are refreshed automatically in the background each time a new thought names them. This means entity search improves over time without manual intervention.

`brain.refresh_all_embeddings(entity_type="all"|"people"|"projects")` forces a full refresh — useful after a backend switch or bulk data import.

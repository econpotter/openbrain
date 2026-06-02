# Design: Model Provider Configuration

## Goal

No model provider or model name is hardwired into Open Brain. Embedding provider,
model selection, and routing are configurable without code changes.

---

## Config split

| File | Contents |
|------|----------|
| `config.yaml` | Non-secret settings: embedding provider, model name, LiteLLM URL, library path |
| `.env` | Secrets only: API keys, `DATABASE_URL`, transport settings |

Adding a new model or switching providers should require only a change to `config.yaml`, not to code.

Set `LIBRARY_PATH` to override the configured library path for a particular
runtime environment, such as the Docker data volume.

---

## Embedding providers

The `embed.provider` key in `config.yaml` selects the provider. `embed.model`
selects the model:

| Value | Description |
|-------|-------------|
| `voyage` | Voyage AI API (`VOYAGE_API_KEY` in `.env`) |
| `openrouter` | OpenRouter's OpenAI-compatible API (`OPENROUTER_API_KEY` in `.env`) |
| `litellm` | Route through a LiteLLM proxy (URL in `config.yaml`, key in `.env`) |

All providers are accessed through `brain.embed()`. Tool code never calls a
provider SDK directly.

The default configuration is:

```yaml
embed:
  provider: voyage
  model: voyage-large-2
```

The current schema uses `VECTOR(1536)` to match `voyage-large-2`.

Local sentence-transformers embeddings are not implemented. They may be added
later as another provider, but they are not a supported configuration today.

---

## LiteLLM

LiteLLM is one supported routing option, not a requirement. When
`embed.provider: litellm`, embedding calls go through the OpenAI-compatible
proxy configured at `embed.litellm_url`. Direct Voyage AI and OpenRouter calls
remain valid alternatives.

---

## Switching backends

Switching embedding models requires re-embedding all existing vectors. If the
new model has a different output dimension, it also requires a schema change.
Steps:

1. Update `embed.provider` and `embed.model` in `config.yaml`.
2. Update the `VECTOR(N)` dimension in the schema if it changed.
3. Run `brain.refresh_all_embeddings()` to re-embed people and projects.
4. Re-embed `thoughts` and `chunks` (no built-in bulk tool yet — add one if needed).

---

## Entity embedding enrichment

People and project embeddings are not just their structured fields. They incorporate snippets from thoughts that mention them, and are refreshed automatically in the background each time a new thought names them. This means entity search improves over time without manual intervention.

`brain.refresh_all_embeddings(entity_type="all"|"people"|"projects")` forces a full refresh — useful after a backend switch or bulk data import.

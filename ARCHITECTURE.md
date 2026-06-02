# ARCHITECTURE.md — Open Brain

Open Brain is a personal AI memory and knowledge base. It gives any AI assistant (Claude, Gemini) persistent, searchable memory without SaaS lock-in: one Postgres database, one MCP server, all interaction mediated through the AI. You never touch the database directly — the AI is the interface.

---

## Domain Model

The core abstraction is the **thought** — an append-only record of anything worth preserving: captured memories, conversation summaries, notes, and document ingest events. Everything else is structured on top of that log.

**Entities:**

- `thought` — atomic unit of memory. Has content, a vector embedding, a type tag, and a flexible metadata JSONB blob (people, projects, tags, source).
- `library` — catalog record for an ingested document (paper, note, conversation). One row per document; holds structured metadata (title, authors, year, source, status, filepath, DOI, BibTeX).
- `chunk` — a 400-word text window from a library document, with its own embedding. Many chunks per library record; enables passage-level semantic search.
- `person` — contact, collaborator, or author. Has an embedding derived from their fields plus co-mentioned thoughts (enriched embedding).
- `project` — research initiative or ongoing work. Same enriched-embedding treatment as people.
- `link` — generic join table for relationships between any two entities across any tables (e.g., `authored_by`, `relevant_to`).

**Key relationships:**

- `chunks` → `library` (many-to-one, cascade delete)
- `thoughts` ↔ `people` / `projects` — loose coupling through metadata JSONB arrays; entity embeddings are re-computed when new thoughts mention them
- `links` — explicit, typed relationships across any entity pair

---

## Component Map

| Component | Responsibility | Key files |
|-----------|---------------|-----------|
| **brain.py** | Core logic: embed, store, retrieve. All database access. Async, uses asyncpg. | `brain.py` |
| **MCP server** | FastMCP server exposing tools to Claude/Gemini via stdio or HTTP. | `mcp_server.py` |
| **tools/** | One file per tool group; each exports a `register(mcp)` function. | `tools/capture.py`, `tools/search.py`, `tools/library.py`, `tools/entities.py`, `tools/briefing.py` |
| **ingest.py** | Standalone CLI for ingesting documents from the local filesystem. | `ingest.py` |
| **migrations/** | Numbered SQL migration files applied in order. Never edited after applied. | `migrations/` |
| **Postgres + pgvector** | Storage: structured rows + vector indexes for semantic search. | (external) |
| **Embedding backend** | Pluggable: local sentence-transformers (default) or Voyage AI API. | `brain.py` |

---

## Data Flow

### Thought capture

```
AI (Claude/Gemini)
  → capture_thought(content, metadata)
      → brain.embed(content)                   # local ST or Voyage AI
      → INSERT INTO thoughts
      → asyncio.create_task(re-embed mentioned people/projects)
```

### Document ingest (via MCP)

```
Remote client
  → scp file to MCP server host  (/tmp/file-<random>)
  → ingest_document(filepath)
      → SHA256 hash → dedup check
      → extract text (pdfplumber / plain read)
      → extract DOI → CrossRef lookup (title, authors, year, BibTeX)
      → fallback: user BibTeX → user title → filename stem
      → copy to LIBRARY_PATH with slug filename
      → INSERT INTO library
      → chunk (400 words, 50-word overlap)
      → embed each chunk → INSERT INTO chunks
      → store_thought("Ingested: ...")
```

### Semantic search

```
search_brain(query)
  → embed(query)
  → parallel fan-out:
      thoughts: vector similarity OR full-text search
      library:  vector similarity on chunks OR FTS, deduplicated to best chunk per doc
  → co-occurrence extraction from thought metadata → surfaces people + projects
  → returns { thoughts, library, people, projects }
```

### Briefing

```
get_briefing(days=N)
  → active/paused projects
  → thoughts in the last N days + co-occurrence mining
  → library items ingested in the last N days
  → people with follow_up_date within N days
```

---

## Key Design Decisions

- **`thoughts` as the universal log.** Ingest events, conversation summaries, and memory write-backs all land in the same table. No separate audit log; the append-only thought stream is the record of everything.

- **Enriched entity embeddings.** People and project embeddings are not just their structured fields — they incorporate snippets from thoughts that mention them, and are refreshed each time a new thought names them. This makes entity search contextually richer over time.

- **Hybrid search (vector + FTS).** `search_brain` and `search_library` use vector similarity as the primary ranker but include a full-text OR condition. This catches exact names and technical terms that embeddings sometimes miss.

- **Pluggable embedding backend.** `EMBED_BACKEND=local` uses sentence-transformers (free, runs on the host); `EMBED_BACKEND=voyage` uses Voyage AI. Switching requires re-embedding all vectors. The dimension is controlled by the model choice — not hardcoded.

- **MCP as the sole interface.** No REST API, no web UI. All reads and writes go through MCP tools. The AI mediates everything. This keeps the surface area minimal and the system single-user by design.

- **Transport flexibility.** The MCP server runs stdio for local dev/testing and streamable HTTP for remote access from Claude.ai / Gemini. Controlled by `MCP_TRANSPORT` env var.

- **Metadata via CrossRef, not LLM.** Document metadata is resolved from DOI → CrossRef API, with user-supplied BibTeX and plain title as fallbacks. LLM extraction was deliberately avoided to keep ingest deterministic and cheap.

- **Provider-agnostic model configuration.** No specific embedding model or provider is hardwired. Model backends, providers, and routing are configured via a YAML config file; API keys are kept separately in `.env`. LiteLLM is a supported routing option but not a requirement — direct provider calls are equally valid. New model backends should be added as configurable options, not hardcoded replacements.

- **Nightly maintenance cron.** A scheduled job queries raw thoughts captured in the past 24 hours, consolidates duplicates, resolves conflicts, and writes clean synthesized insights back to the database. This offloads expensive reflection work from real-time conversations.

---

## What This Project Is Not

- Not a multi-tenant or SaaS system — single-user, self-hosted only.
- Not a general-purpose RAG pipeline — purpose-built for personal memory.
- Not a LiteLLM proxy — Open Brain is a client of the LiteLLM gateway, not its host.
- Not a web application — no REST API, no dashboard, no browser UI.

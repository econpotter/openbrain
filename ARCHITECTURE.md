# ARCHITECTURE.md — openbrain

openbrain is a personal AI memory and knowledge base. It gives any AI assistant (Claude, Gemini) persistent, searchable memory without SaaS lock-in: one Postgres database, one MCP server, all interaction mediated through the AI. You never touch the database directly — the AI is the interface.

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
| **brain.py** | Core logic: embed, store, retrieve, paper fetch, embedding backfill. All database access. Async, uses asyncpg. | `openbrain/brain.py` |
| **sources.py** | Pluggable paper-metadata backends (OpenAlex, CrossRef, Semantic Scholar, arXiv) for fileless fetch. Lookup by DOI/arXiv ID and topic search. | `openbrain/sources.py` |
| **MCP server** | FastMCP server exposing tools and read-only resources to Claude/Gemini via stdio or HTTP. | `openbrain/server.py` |
| **tools/** | One file per tool group; each exports a `register(mcp)` function. | `openbrain/tools/capture.py`, `openbrain/tools/search.py`, `openbrain/tools/library.py`, `openbrain/tools/entities.py`, `openbrain/tools/briefing.py`, `openbrain/tools/maintenance.py` |
| **resources.py** | Read-only MCP guidance resources for tool choice, schema context, and capture policy. | `openbrain/resources.py` |
| **ingest CLI** | Standalone CLI for ingesting documents from the local filesystem. | `openbrain/cli.py` |
| **schemas/core/** | Clean SQL baseline for initializing a new openbrain database. | `schemas/core/` |
| **Postgres + pgvector** | Storage: structured rows + vector indexes for semantic search. | (external) |
| **Embedding provider** | Pluggable: Voyage AI, OpenRouter, or an OpenAI-compatible LiteLLM gateway. | `openbrain/brain.py` |
| **Maintenance job** | Docker Compose service that consolidates recent raw thoughts with the configured extraction model and backfills missing embeddings. | `scripts/openbrain-maintenance.py` |
| **integrations/** | Provider-agnostic assistant skill files (no new capability) that teach MCP clients how to route common requests to openbrain tools. | `integrations/` |

---

## Data Flow

### MCP resources

```
AI client
  → reads openbrain://tool-guide, openbrain://schema-summary, or openbrain://capture-policy
  → uses the guidance to choose a tool
```

Resources are read-only context. Mutation, search, ingest, and maintenance
actions stay behind MCP tools.

### Thought capture

```
AI (Claude/Gemini)
  → capture_thought(content, metadata)
      → brain.embed(content)                   # Voyage AI, OpenRouter, or LiteLLM
      → upsert_thought(content, embedding, metadata, type)
      → if inserted or metadata changed: re-embed mentioned people/projects
```

### Document ingest (via MCP)

```
Remote client
  → prepare_document_ingest(filename)
  → scp file to returned MCP server staging path
  → ingest_staged_document(filepath)
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

The staged copy is temporary. PDF bytes are stored in `LIBRARY_PATH`, not in
Postgres. Postgres stores catalog metadata, file hashes/paths, and searchable
text chunks.

### Fileless paper fetch (via MCP)

```
fetch_paper(identifier | identifiers | topic)
  → sources.py resolves across backends (OpenAlex, CrossRef, Semantic Scholar, arXiv)
      identifier(s): DOI / arXiv ID / title lookup → one record each
      topic:         search → candidate list (no write)
  → INSERT INTO library (metadata + abstract, no file on disk)
  → embed abstract → searchable via search_library
      if embedding model unavailable: store with null embedding, defer to backfill
  → batch mode: each identifier resolved independently; one failure does not abort the rest
```

Fetch is fileless: no PDF is downloaded. Document ingest (with a file) and paper
fetch (metadata + abstract only) are separate paths into the same `library`.

### Acquire: capture on request, not a research agenda

`acquire(query)` is a bulk-capture verb: search journal sources, deduplicate,
enrich, canonicalize venues, deterministically prefilter (lexical overlap, no
LLM), and store survivors with embeddings. openbrain captures on request and
reasons over what the library holds. The automated research agenda — which
topics to harvest and on what cadence — is **external**: a driver (a cron, an
orchestra job, or a research agent) decides the standing topics and calls
`openbrain acquire <query>`. openbrain owns no saved-query registry or
scheduler; the agenda is the researcher's concern, not the memory system's.

### Embedding backfill

```
backfill_embeddings(limit) / brain.backfill_chunk_embeddings(limit)
  → select rows with a null embedding (thoughts, chunks, abstracts)
  → embed and update in place
```

Embedding is decoupled from the write path: captures, ingests, and fetches
persist content even when the embedding model is down, then `backfill_embeddings`
fills the gaps. The maintenance job runs this on a schedule.

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

- **Pluggable embedding provider.** `config.yaml` selects `voyage`, `openrouter`, or `litellm`. Voyage AI with `voyage-large-2` is the current default. Switching models requires re-embedding all vectors, and changing output dimensions also requires a schema change.

- **MCP as the canonical agent interface.** AI assistants read and write through
  MCP tools. Additional interfaces such as a REST API or dashboard may be added
  for concrete workflows, but they should reuse the same core domain logic
  rather than becoming a parallel implementation.

- **Transport flexibility.** The MCP server runs stdio for local dev/testing and streamable HTTP for remote access from Claude.ai / Gemini. Controlled by `server.transport` in `config.yaml`.

- **Metadata via CrossRef, not LLM.** Document metadata is resolved from DOI → CrossRef API, with user-supplied BibTeX and plain title as fallbacks. LLM extraction was deliberately avoided to keep ingest deterministic and cheap.

- **Fileless paper fetch with pluggable sources.** `fetch_paper` stores a library record from metadata + abstract alone, no PDF. `sources.py` queries OpenAlex, CrossRef, Semantic Scholar, and arXiv in a configured order; backends are added as options, not hardcoded. This makes "remember this paper" cheap and lets the library hold references the user has not downloaded.

- **Embedding decoupled from the write path.** Writes never block on the embedding model. Rows persist with a null embedding when the model is unavailable, and `backfill_embeddings` (also run by the maintenance job) fills them later. Availability of search degrades gracefully; durability of the write does not.

- **Provider-agnostic model configuration.** No specific embedding model or provider is hardwired. Model backends, providers, and routing are configured via a YAML config file; API keys are kept separately in `.env`. LiteLLM is a supported routing option but not a requirement — direct provider calls are equally valid. New model backends should be added as configurable options, not hardcoded replacements.

- **Nightly maintenance runs in Compose.** A scheduled container queries recent
  unarchived raw thoughts, consolidates duplicates, resolves conflicts, writes
  clean synthesized insights back to the database, and marks processed inputs
  with `metadata.archived = true`.

---

## What This Project Is Not

- Not a multi-tenant or SaaS system — single-user, self-hosted only.
- Not a general-purpose RAG pipeline — purpose-built for personal memory.
- Not a LiteLLM proxy — openbrain is a client of the LiteLLM gateway, not its host.
- Not primarily a web application. A REST API or dashboard may be added later
  where it supports a concrete workflow.

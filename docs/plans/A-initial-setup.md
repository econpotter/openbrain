# Open Brain — Implementation Plan

> Personal AI memory and knowledge base. One Postgres database, one MCP server,
> any AI can plug in. No middleware, no SaaS. Interact entirely through Claude.ai
> or Gemini with the MCP connected — the AI mediates all reads and writes.

---

## Philosophy

- **Semantic-first** — everything is embedded and searchable by meaning
- **Structured underneath** — SQL tables for precise filtering and relational queries
- **AI-mediated** — you never touch the database directly; Claude/Gemini is the interface
- **Single-user, self-hosted** — runs on a local Linux machine, accessible via Tailscale
- **Append-only capture** — the `thoughts` table is the living record of everything

---

## Architecture

```
Claude.ai / Gemini (subscription)
   └── MCP server (Python/FastMCP, port 8000)
          └── brain.py (core logic)
                 ├── Postgres + pgvector  (structured records + vector search)
                 └── Embeddings API       (Voyage AI or OpenAI)

ingest.py  (local script for adding documents)
   └── brain.py
```

### Infrastructure

| Component         | Choice              | Notes                                      |
|-------------------|---------------------|--------------------------------------------|
| Database          | Postgres + pgvector | Local, self-hosted                         |
| MCP server        | Python (FastMCP)    | Runs as a local process via systemd        |
| Remote access     | Tailscale           | Zero-config VPN, no public exposure        |
| Process manager   | systemd             | Keeps MCP server alive across reboots      |
| Embeddings        | Voyage AI           | Strong on technical/academic text          |
| Ingestion script  | Plain Python        | For PDFs and text documents                |

---

## Schema

### Dual-layer design

**Semantic layer** — vector embeddings in pgvector powering fuzzy, meaning-based retrieval.

**Structured SQL layer** — typed fields and foreign keys powering precise, filtered queries.

These two layers work together. Example: *"find documents about bandwidth selection
in the past 5 years"* = vector search on `chunks` filtered by `library.year >= 2020`.

---

### Tables

#### `thoughts`
Universal capture table. Every memory, note, conversation summary, ingest event,
and distilled observation lives here. The single coherent record of everything.

```sql
CREATE TABLE thoughts (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    content     TEXT NOT NULL,
    embedding   VECTOR(1024),          -- Voyage AI embed dim
    metadata    JSONB DEFAULT '{}',    -- type, tags, people, projects, source, etc.
    created_at  TIMESTAMPTZ DEFAULT now(),
    updated_at  TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX ON thoughts USING ivfflat (embedding vector_cosine_ops);
```

`metadata` JSONB keys (AI-extracted at write time):
- `type` — `thought | memory | conversation | ingest_event | note`
- `tags` — freeform list of topics
- `people` — names mentioned
- `projects` — project names mentioned
- `source` — `claude | gemini | ingest_script`

---

#### `library`
Structured catalog of all ingested documents.

```sql
CREATE TABLE library (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title           TEXT NOT NULL,
    authors         TEXT[],
    year            INT,
    journal         TEXT,
    doc_type        TEXT,              -- paper | note | conversation | web | other
    abstract        TEXT,
    filepath        TEXT,
    file_hash       TEXT UNIQUE,       -- SHA256, used for deduplication
    status          TEXT DEFAULT 'ingested'
                    CHECK (status IN ('reading_list','ingested','reading','read','archived')),
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT now()
);
```

---

#### `chunks`
Text chunks from library documents, each with its own embedding.
Foreign-keyed back to `library` so semantic search returns paper-level results.

```sql
CREATE TABLE chunks (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    library_id  UUID REFERENCES library(id) ON DELETE CASCADE,
    content     TEXT NOT NULL,
    embedding   VECTOR(1024),
    page        INT,
    chunk_index INT,
    created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX ON chunks USING ivfflat (embedding vector_cosine_ops);
```

---

#### `people`
Contacts, collaborators, and authors. Embedded at creation via a summary thought.

```sql
CREATE TABLE people (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL,
    affiliation TEXT,
    relationship TEXT,               -- collaborator | author | contact | advisor
    notes       TEXT,
    metadata    JSONB DEFAULT '{}',
    created_at  TIMESTAMPTZ DEFAULT now(),
    updated_at  TIMESTAMPTZ DEFAULT now()
);
```

---

#### `projects`
Research projects and initiatives. Embedded at creation via a summary thought.

```sql
CREATE TABLE projects (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL,
    description TEXT,
    status      TEXT DEFAULT 'active'
                CHECK (status IN ('active','paused','completed','archived')),
    start_date  DATE,
    metadata    JSONB DEFAULT '{}',
    created_at  TIMESTAMPTZ DEFAULT now(),
    updated_at  TIMESTAMPTZ DEFAULT now()
);
```

---

#### `links`
Relationships between any two entities across any tables.
Enables "what papers are relevant to my SDC project?" as a structured query.

```sql
CREATE TABLE links (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_table  TEXT NOT NULL,
    source_id     UUID NOT NULL,
    target_table  TEXT NOT NULL,
    target_id     UUID NOT NULL,
    relationship  TEXT,              -- e.g. "authored_by", "relevant_to", "cited_in"
    metadata      JSONB DEFAULT '{}',
    created_at    TIMESTAMPTZ DEFAULT now(),
    UNIQUE (source_table, source_id, target_table, target_id, relationship)
);
```

---

### Helper function — semantic search

```sql
CREATE OR REPLACE FUNCTION match_thoughts(
    query_embedding VECTOR(1024),
    match_threshold FLOAT DEFAULT 0.5,
    match_count     INT   DEFAULT 10
)
RETURNS TABLE (id UUID, content TEXT, metadata JSONB, similarity FLOAT)
LANGUAGE sql STABLE AS $$
    SELECT id, content, metadata,
           1 - (embedding <=> query_embedding) AS similarity
    FROM   thoughts
    WHERE  1 - (embedding <=> query_embedding) > match_threshold
    ORDER  BY similarity DESC
    LIMIT  match_count;
$$;
```

A parallel `match_chunks` function follows the same pattern for library search.

---

## MCP Server Tools

The MCP server (`mcp_server.py`) exposes these tools to Claude/Gemini:

### Core capture & recall (OB1 baseline)

| Tool              | Description |
|-------------------|-------------|
| `capture_thought` | Store a thought with AI-extracted metadata. Primary write tool. |
| `search_thoughts` | Semantic search over `thoughts`. Returns ranked results. |
| `browse_recent`   | Retrieve thoughts from the past N hours/days. |
| `get_stats`       | Counts, recent activity summary, table sizes. |

### Library

| Tool               | Description |
|--------------------|-------------|
| `ingest_document`  | Add a document to `library` + chunk + embed. Deduplicates by file hash. |
| `search_library`   | Semantic search over `chunks`, returns ranked `library` records. Supports `year_min` filter. |
| `update_status`    | Update `library.status` for a document. |

### Entities

| Tool            | Description |
|-----------------|-------------|
| `find_person`   | Look up a person record by name (fuzzy). |
| `find_project`  | Look up a project record by name (fuzzy). |
| `link_entities` | Create a relationship between two entities in `links`. |

### Cross-table

| Tool               | Description |
|--------------------|-------------|
| `search_brain`     | Fan-out semantic search across `thoughts`, `chunks`, `people`, `projects`. Synthesizes results. |
| `get_daily_briefing` | Surfaces active projects, recent thoughts, people flagged for follow-up. Called by "morning briefing". |

---

## Interaction Patterns

These natural-language triggers work out of the box with the MCP tools above:

| You say | What happens |
|---------|--------------|
| `"remember this"` | Claude summarizes the conversation → `capture_thought` |
| `"ingest this conversation"` | Claude writes markdown summary → `ingest_document` (type: conversation) |
| `"ingest this <file/url>"` | Claude calls `ingest_document` with the source |
| `"find documents related to <text>"` | Claude calls `search_library` → returns ranked table |
| `"find documents about <text> in the past Y years"` | `search_library` with `year_min` filter |
| `"what do I know about <x>"` | Claude calls `search_brain` → fan-out across all tables |
| `"morning briefing"` | Claude calls `get_daily_briefing` → synthesizes daily context |

---

## Ingestion Pipeline

`ingest.py` — standalone script for adding documents to the library.

```
Input: PDF / text file / directory
  → compute SHA256 hash → reject if duplicate (suggest --force flag)
  → extract text (pdfplumber for PDF, plain read for text)
  → LLM extracts metadata: title, authors, year, journal, abstract
  → write record to `library`
  → chunk text (400 words, 50-word overlap)
  → embed each chunk → write to `chunks`
  → write ingest event to `thoughts` (e.g. "Ingested: Imbens & Kalyanaraman 2012")
```

Usage:
```bash
python ingest.py paper.pdf
python ingest.py papers/              # whole directory
python ingest.py note.txt --type note
python ingest.py paper.pdf --force    # re-ingest duplicate
```

---

## Proactive Memory Write-back

At the end of each conversation, Claude (prompted via a system instruction or
project instruction in Claude.ai) calls `capture_thought` with facts worth
preserving — linking to people, projects, and library entries where relevant.

A Claude.ai project instruction skeleton:

```
At the end of our conversation, or when I say "remember this", call capture_thought
to store a distilled summary of any facts, decisions, or insights worth preserving.
Extract people, projects, and topics from the content and include them in metadata.
Link to relevant library entries where applicable.
```

---

## Hosting & Remote Access

```bash
# 1. Install Postgres + pgvector
sudo apt install postgresql
sudo -u postgres psql -c "CREATE EXTENSION vector;"

# 2. Install Tailscale
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up

# 3. systemd unit for MCP server
# /etc/systemd/system/openbrain-mcp.service
[Unit]
Description=Open Brain MCP Server
After=postgresql.service

[Service]
User=<youruser>
WorkingDirectory=/home/<youruser>/openbrain
EnvironmentFile=/home/<youruser>/openbrain/.env
ExecStart=/home/<youruser>/openbrain/.venv/bin/python mcp_server.py
Restart=on-failure

[Install]
WantedBy=multi-user.target

# 4. Enable
sudo systemctl enable --now openbrain-mcp

# 5. Connect Claude.ai
# Settings → Connectors → Add MCP server
# URL: http://<tailscale-hostname>:8000
```

---

## Build Order

Each step is independently useful — you get a working system early and layer on
complexity incrementally.

### Phase 1 — Core (working brain in ~2 hours)
- [ ] **1.1** Postgres + pgvector setup, schema migrations
- [ ] **1.2** `brain.py` — core logic: embed, store, search
- [ ] **1.3** MCP server with core OB1 tools: `capture_thought`, `search_thoughts`, `browse_recent`, `get_stats`
- [ ] **1.4** Connect to Claude.ai via Tailscale, verify end-to-end

### Phase 2 — Library
- [ ] **2.1** `library` + `chunks` tables and migrations
- [ ] **2.2** `ingest.py` — PDF/text ingestion with deduplication
- [ ] **2.3** MCP tools: `ingest_document`, `search_library`, `update_status`
- [ ] **2.4** Verify "find documents related to X" and year-filtered search

### Phase 3 — Entities & Relationships
- [ ] **3.1** `people`, `projects`, `links` tables and migrations
- [ ] **3.2** MCP tools: `find_person`, `find_project`, `link_entities`
- [ ] **3.3** Embed people/project summaries into `thoughts` at creation

### Phase 4 — Cross-table Intelligence
- [ ] **4.1** `search_brain` — fan-out search across all tables
- [ ] **4.2** `get_daily_briefing` — morning context synthesis
- [ ] **4.3** Claude.ai project instruction for proactive memory write-back

### Phase 5 — Hardening
- [ ] **5.1** systemd unit + auto-restart
- [ ] **5.2** `.env` management, secrets out of code
- [ ] **5.3** Embedding model benchmarking (Voyage vs alternatives)
- [ ] **5.4** Index tuning for query performance
- [ ] **5.5** Backup strategy for Postgres

---

## Repository Structure

```
openbrain/
├── PLAN.md                  # This file
├── README.md
├── .env.example
├── .gitignore
│
├── migrations/              # SQL migration files, applied in order
│   ├── 001_extensions.sql
│   ├── 002_thoughts.sql
│   ├── 003_library_chunks.sql
│   ├── 004_people_projects_links.sql
│   └── 005_functions.sql
│
├── brain.py                 # Core logic: embed, store, retrieve
├── mcp_server.py            # FastMCP server exposing tools
├── ingest.py                # Standalone ingestion script
│
├── tools/                   # MCP tool implementations (one file per tool group)
│   ├── capture.py
│   ├── search.py
│   ├── library.py
│   ├── entities.py
│   └── briefing.py
│
└── scripts/
    ├── setup_db.sh          # One-shot DB setup
    └── install.sh           # Full install: deps, venv, systemd
```

---

## Key Dependencies

```
# requirements.txt
fastmcp
asyncpg
pgvector
pdfplumber
voyageai          # or openai for embeddings
python-dotenv
typer
rich
```

---

## Notes & Decisions

- **Deduplication** — library documents are deduplicated by SHA256 file hash.
  Attempting to re-ingest a duplicate surfaces a warning; `--force` flag overrides.
- **Embeddings dimension** — 1024 (Voyage AI `voyage-large-2`). If switching
  providers, re-embed all vectors and update the `VECTOR(1024)` declarations.
- **No CLI framework needed** — interaction is entirely through Claude.ai / Gemini
  via MCP. `ingest.py` is the only script a human runs directly.
- **`links` table** — deliberately generic. Any two entities across any tables
  can be related. New entity types can be added without restructuring.
- **`thoughts` as the log** — ingest events, memory write-backs, and conversation
  summaries all write to `thoughts`. No separate audit log needed.
- **Tailscale over nginx** — no SSL cert management, no public exposure, no port
  forwarding. The MCP server runs on plain HTTP within the tailnet.


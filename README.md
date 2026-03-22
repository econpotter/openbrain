# Open Brain

A personal AI memory and knowledge base for researchers. One Postgres database,
one MCP server, any AI can plug in. No middleware, no SaaS, no subscriptions
beyond the AI you already use.

You interact with your memory entirely through Claude.ai or Gemini — the AI
mediates all reads and writes. You never touch the database directly.

## What it does

- **Capture thoughts** — store notes, memories, and conversation summaries with
  AI-extracted metadata (topics, people, projects)
- **Semantic search** — find anything by meaning, not just keywords
- **Library** — ingest PDFs and documents; search across them by topic and year
- **Entities** — track people, projects, and relationships between them
- **Daily briefing** — surface active projects, recent thoughts, and follow-ups
  each morning

Everything is stored in a local Postgres database. The MCP server runs on your
machine and is accessible remotely from any client you connect.

## Architecture

```
Claude.ai / Gemini
    └── MCP server  (Python/FastMCP, port 8000)
           └── brain.py  (core logic)
                  ├── Postgres + pgvector  (structured records + vector search)
                  └── Voyage AI            (embeddings)

ingest.py  (local script for adding PDFs and documents)
```

## Prerequisites

- Linux machine (the server that will host the brain)
- Python 3.11+
- PostgreSQL 15+ with the [pgvector](https://github.com/pgvector/pgvector) extension
- Network access from your client machines to the server on port 8000

**Embeddings** are used to power semantic search. The default backend is a free
local model that runs on the server. A paid API is available as an alternative.

| Backend | Model | Cost | Requires internet |
|---------|-------|------|-------------------|
| Local (default) | `BAAI/bge-large-en-v1.5` via sentence-transformers | Free | No |
| Voyage AI | `voyage-large-2` | Paid API | Yes |

### Install PostgreSQL and pgvector

```bash
sudo apt install postgresql postgresql-contrib
sudo apt install postgresql-16-pgvector   # adjust version to match your postgres
sudo systemctl enable --now postgresql
```

Create the database:

```bash
sudo -u postgres createuser --superuser $USER
createdb openbrain
```

## Deployment

### 1. Clone the repository

```bash
git clone <repo-url> ~/openbrain
cd ~/openbrain
```

### 2. Run the installer

```bash
bash scripts/install.sh
```

The installer will:
1. Create a Python virtual environment and install dependencies
2. Create `.env` from `.env.example` and **pause** — you must fill in credentials
3. Apply all database migrations
4. Install and start the `openbrain-mcp` systemd service

### 3. Fill in credentials

Open `.env` and set:

```
DATABASE_URL=postgresql://localhost/openbrain
VOYAGE_API_KEY=your_key_here
```

Then re-run the installer to continue:

```bash
bash scripts/install.sh
```

### 4. Download the embedding model

The local embedding model (~1.3 GB) is downloaded once and cached on the server.
Run this after installing dependencies:

```bash
source .venv/bin/activate
python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-large-en-v1.5')"
```

This takes a minute on first run. Subsequent starts load from the local cache at
`~/.cache/huggingface/`.

### 5. Verify the service is running

```bash
sudo systemctl status openbrain-mcp
```

### 6. Connect your AI clients

Once the service is running, connect whichever clients you use.

#### Claude.ai (web)

**Settings → Connectors → Add MCP server**

URL: `http://<your-hostname>:8000`

#### Claude Code (CLI)

Add the server to `~/.claude/settings.json` on your client machine:

```json
{
  "mcpServers": {
    "openbrain": {
      "type": "sse",
      "url": "http://<your-hostname>:8000/sse"
    }
  }
}
```

Then verify the connection:

```bash
claude mcp list
```

#### Project instruction (Claude.ai)

To make Claude automatically preserve memories at the end of conversations,
add this to your Claude.ai project instructions:

```
At the end of our conversation, or when I say "remember this", call
capture_thought to store a distilled summary of any facts, decisions, or
insights worth preserving. Extract people, projects, and topics from the
content and include them in metadata. Link to relevant library entries
where applicable.
```

## Manual deployment (without the installer)

If you prefer to run steps individually:

```bash
# 1. Create venv and install deps
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# edit .env with your DATABASE_URL and VOYAGE_API_KEY

# 3. Apply migrations
bash scripts/setup_db.sh

# 4. Start the MCP server
MCP_TRANSPORT=sse python mcp_server.py
```

## Using Voyage AI instead of the local model

If you prefer faster embedding (useful when ingesting large document batches),
set `EMBED_BACKEND=voyage` in `.env` and add your API key:

```
EMBED_BACKEND=voyage
VOYAGE_API_KEY=your_key_here
VOYAGE_MODEL=voyage-large-2
```

Note: if you switch backends after storing data, existing vectors and new vectors
will be from different models and semantic search results will be unreliable.
Re-embed all content by re-ingesting documents and re-running `capture_thought`
on existing thoughts, or wipe and rebuild the database.

## Development

Run the server locally over stdio (no HTTP, useful for testing with the MCP
inspector or a local Claude Code session):

```bash
source .venv/bin/activate
python mcp_server.py   # MCP_TRANSPORT defaults to stdio
```

Ingest a document:

```bash
python ingest.py paper.pdf
python ingest.py papers/              # whole directory
python ingest.py paper.pdf --force    # re-ingest a duplicate
```

## Implementation status

| Phase | Description | Status |
|-------|-------------|--------|
| 1 | Core capture & recall (`capture_thought`, `search_thoughts`, `browse_recent`, `get_stats`) | Complete |
| 2 | Library (`ingest_document`, `search_library`, `update_status`) | Planned |
| 3 | Entities (`find_person`, `find_project`, `link_entities`) | Planned |
| 4 | Cross-table intelligence (`search_brain`, `get_daily_briefing`) | Planned |
| 5 | Hardening (systemd, backups, index tuning) | Planned |

## Repository structure

```
migrations/          SQL migration files, applied in order by setup_db.sh
brain.py             Core logic: embed, store, retrieve
mcp_server.py        FastMCP server entry point
ingest.py            Standalone script for ingesting documents
tools/               MCP tool implementations, one file per tool group
scripts/
  setup_db.sh        Apply all migrations
  install.sh         Full install: venv, .env, DB, systemd
  openbrain-mcp.service  systemd unit template
```

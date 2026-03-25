# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Open Brain — personal AI memory and knowledge base. See PLAN.md for full architecture, schema, and build plan.

## Development Commands

```bash
# Install dependencies
python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt

# Apply migrations (in order)
psql $DATABASE_URL -f migrations/001_extensions.sql

# Start MCP server
python mcp_server.py

# Ingest documents
python ingest.py paper.pdf
python ingest.py papers/
python ingest.py paper.pdf --force    # re-ingest duplicate
```

## Conventions

- Never edit applied migrations — add new numbered files instead
- `tools/` has one file per MCP tool group (capture, search, library, entities, briefing)
- Configuration via `.env` (see `.env.example`)

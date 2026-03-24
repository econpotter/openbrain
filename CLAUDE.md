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

## Interaction Patterns

- **"What's on my plate today/this week?"** — call `get_briefing(days=1|7)` AND `gcal_list_events` for the same date range, then synthesize both into a unified response.
- **"What do I know about X?"** — call `search_brain(query)`.
- **"Remember that..."** — call `capture_thought` AND save to local memory files.
- **Mentioning a project or person in a thought** — if that project/person doesn't exist in the DB, call `add_project` or `add_person` first.

## Conventions

- Never edit applied migrations — add new numbered files instead
- `tools/` has one file per MCP tool group (capture, search, library, entities, briefing)
- Configuration via `.env` (see `.env.example`)

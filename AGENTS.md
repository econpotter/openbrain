# AGENTS.md — Open Brain

Read this before doing anything else in this repository.

---

## What this repo is

Open Brain is a personal AI memory and knowledge base. It exposes MCP tools that let any AI assistant (Claude, Gemini) read and write to a Postgres database. The AI is the interface — no web UI, no REST API. See ARCHITECTURE.md for the full picture.

---

## Repository layout

```
openbrain/
├── openbrain/         # Python package
│   ├── brain.py       #   Core logic — embed, store, retrieve. All DB access lives here.
│   ├── server.py      #   FastMCP server entry point.
│   ├── cli.py         #   Ingest CLI entry point.
│   └── tools/         #   One file per tool group, each exports register(mcp).
│       ├── capture.py #     capture_thought
│       ├── search.py  #     search_thoughts, browse_recent, get_stats
│       ├── library.py #     ingest_document, ingest_directory, search_library,
│       │              #     update_library, update_status
│       ├── entities.py#     add_person, get_person, list_people, update_person,
│       │              #     add_project, list_projects, update_project, refresh_embeddings,
│       │              #     search_people, search_projects, search_by_entity, add_note
│       └── briefing.py#     get_briefing, search_brain
├── migrations/        # Numbered SQL files. Applied via scripts/setup_db.sh. Never edit applied files.
├── scripts/           # setup_db.sh, install.sh, systemd units
├── config.yaml        # Non-secret config: embed provider, model, library path
├── .env.example       # API keys and DATABASE_URL (secrets only)
└── docs/
    ├── design/        # Per-subsystem design docs
    └── plans/         # Phased build plans
```

---

## Core invariants

- **All database access goes through `brain.py`.** Tool files call `brain.*` functions; they do not import asyncpg or write SQL directly.
- **Never edit applied migrations.** Add a new numbered file to `migrations/` instead; `scripts/setup_db.sh` applies them in order.
- **`thoughts` is append-only.** Never `DELETE` or `UPDATE` thought content. Corrections go in as new thoughts.
- **One `register(mcp)` per tool file.** Each file in `tools/` exports exactly one function that registers its tools with the FastMCP instance.
- **No hardcoded model providers or names.** Read model config from `config.yaml`. See `docs/design/model-config.md`.

---

## Running the server

```bash
# Local dev (stdio transport)
.venv/bin/python -m openbrain.server

# Remote access (HTTP transport)
MCP_TRANSPORT=http .venv/bin/python -m openbrain.server

# Ingest a document or directory
.venv/bin/python -m openbrain.cli paper.pdf
.venv/bin/python -m openbrain.cli papers/ --doc-type report

# Apply all migrations
bash scripts/setup_db.sh
```

---

## Adding a new MCP tool

1. Implement the backing function in `brain.py`.
2. Add the tool to the appropriate file in `tools/` (or create a new file with a `register(mcp)` function).
3. If it's a new file, import and call `register(mcp)` in `openbrain/server.py`.
4. If the tool needs a new table or column, add a numbered migration in `migrations/` and re-run `scripts/setup_db.sh`.

---

## Design docs

- `docs/design/model-config.md` — model provider config, YAML/env split, embedding backends
- `docs/design/ingest-pipeline.md` — document ingestion, metadata resolution, remote ingest pattern
- `docs/design/nightly-cron.md` — nightly thought consolidation job
- `docs/design/search.md` — hybrid vector + FTS search, fan-out, entity co-occurrence

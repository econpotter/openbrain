# AGENTS.md — openbrain

Read this before doing anything else in this repository.

---

## What this repo is

openbrain is a personal AI memory and knowledge base. It exposes MCP tools
that let any AI assistant (Claude, Gemini) read and write to a Postgres
database. MCP is the canonical agent interface. Additional interfaces may be
added later for concrete workflows if they reuse the same core domain logic.
See ARCHITECTURE.md for the full picture.

---

## Repository layout

```
openbrain/
├── openbrain/         # Python package
│   ├── brain.py       #   Core logic — embed, store, retrieve. All DB access lives here.
│   ├── server.py      #   FastMCP server entry point.
│   ├── cli.py         #   Ingest CLI entry point.
│   ├── resources.py   #   Read-only MCP guidance resources.
│   └── tools/         #   One file per tool group, each exports register(mcp).
│       ├── capture.py #     capture_thought
│       ├── search.py  #     search_thoughts, browse_recent, get_stats
│       ├── library.py #     ingest_document, ingest_directory, search_library,
│       │              #     prepare_document_ingest, ingest_staged_document,
│       │              #     update_library, update_status
│       ├── entities.py#     add_person, get_person, list_people, update_person,
│       │              #     add_project, list_projects, update_project, refresh_embeddings,
│       │              #     search_people, search_projects, search_by_entity, add_note
│       ├── briefing.py#     get_briefing, search_brain
│       └── maintenance.py
├── schemas/
│   ├── core/          # Clean install-time SQL baseline, applied in numeric order.
│   └── optional/      # Additional capabilities evaluated after the core baseline.
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
- **Treat `schemas/core/` as the pre-alpha install-time baseline.** Once openbrain contains real data, introduce a separate migration mechanism for later schema changes.
- **`thoughts` is append-only.** Never `DELETE` or `UPDATE` thought content. Corrections go in as new thoughts.
- **One `register(mcp)` per tool file.** Each file in `tools/` exports exactly one function that registers its tools with the FastMCP instance.
- **MCP resources are read-only guidance surfaces.** Keep mutation, search, ingest, and maintenance actions behind tools.
- **No hardcoded model providers or names.** Read model config from `config.yaml`. See `docs/design/model-config.md`.

---

## Collaboration Style

The primary user is a PhD research economist, not a software engineer. Explain
technical choices in direct, concrete language and connect implementation
details to their practical consequences. Prefer terminology that is natural in
research and analysis workflows where it remains precise.

- Prefer **trial run** or **dry run** instead of **smoke test**.
- Define software-specific shorthand when it is necessary.
- Avoid unnecessary engineering jargon when plain language is equally precise.

---

## Running the server

```bash
# Local dev: set server.transport: stdio in config.yaml
.venv/bin/python -m openbrain.server

# Remote access: set server.transport: http in config.yaml
.venv/bin/python -m openbrain.server

# Ingest a document or directory
.venv/bin/python -m openbrain.cli paper.pdf
.venv/bin/python -m openbrain.cli papers/ --doc-type report

# Install the core database schema
bash scripts/setup_db.sh
```

---

## Adding a new MCP tool

1. Implement the backing function in `brain.py`.
2. Add the tool to the appropriate file in `tools/` (or create a new file with a `register(mcp)` function).
3. If it's a new file, import and call `register(mcp)` in `openbrain/server.py`.
4. If the tool needs a new table or column before first use, update the relevant file in `schemas/core/` and re-run `scripts/setup_db.sh` against a blank database. Once real data exists, add a migration mechanism instead.

---

## Design docs

- `docs/design/model-config.md` — model provider config, YAML/env split, embedding backends
- `docs/design/ingest-pipeline.md` — document ingestion, metadata resolution, remote ingest pattern
- `docs/design/paper-fetch.md` — fileless paper metadata/abstract fetch, pluggable sources, abstract embedding
- `docs/design/nightly-cron.md` — nightly thought consolidation job
- `docs/design/search.md` — hybrid vector + FTS search, fan-out, entity co-occurrence

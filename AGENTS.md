# AGENTS.md — openbrain
---

## What this repo is

openbrain is a personal AI memory and knowledge base: MCP tools over a Postgres
database. **See ARCHITECTURE.md** for the domain model, component map, data
flow, and design rationale. This file is the operational guide — invariants and
how to work in the repo.

---

## Repository layout

```
openbrain/
├── openbrain/         # Python package
│   ├── brain.py       #   Core logic — embed, store, retrieve, fetch, backfill. All DB access lives here.
│   ├── sources.py     #   Pluggable paper-metadata backends: OpenAlex, CrossRef, Semantic Scholar, arXiv.
│   ├── server.py      #   FastMCP server entry point.
│   ├── cli.py         #   Ingest CLI entry point.
│   ├── resources.py   #   Read-only MCP guidance resources.
│   └── tools/         #   One file per tool group, each exports register(mcp).
│       ├── capture.py #     capture_thought
│       ├── search.py  #     search_thoughts, browse_recent, get_stats
│       ├── library.py #     ingest_document, ingest_directory, search_library,
│       │              #     prepare_document_ingest, ingest_staged_document,
│       │              #     fetch_paper, update_library, update_status
│       ├── entities.py#     add_person, get_person, list_people, update_person,
│       │              #     add_project, list_projects, update_project, refresh_embeddings,
│       │              #     search_people, search_projects, search_by_entity, add_note
│       ├── briefing.py#     get_briefing, search_brain
│       └── maintenance.py # run_memory_extraction, backfill_embeddings
├── integrations/      # Provider-agnostic assistant skill files routing requests to MCP tools.
├── schemas/
│   ├── core/          # Clean install-time SQL baseline, applied in numeric order.
│   └── optional/      # Additional capabilities evaluated after the core baseline.
├── scripts/           # setup_db.sh, install.sh, systemd units
├── config.yaml        # Non-secret config: embed provider, model, library path, paper sources
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
- **Embedding is decoupled from the write path.** Capture/ingest/fetch must persist content even when the embedding model is unavailable; rows land with a null embedding and are filled later by `backfill_embeddings`. Never let an embedding failure abort a write.
- **Paper fetch is fileless.** `fetch_paper` resolves metadata + abstract through `sources.py` and stores a library record without a PDF on disk. Document ingest (with a file) stays separate.

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


<!-- headroom:rtk-instructions -->
# RTK (Rust Token Killer) - Token-Optimized Commands

When running shell commands, **always prefix with `rtk`**. This reduces context
usage by 60-90% with zero behavior change. If rtk has no filter for a command,
it passes through unchanged — so it is always safe to use.

## Key Commands
```bash
# Git (59-80% savings)
rtk git status          rtk git diff            rtk git log

# Files & Search (60-75% savings)
rtk ls <path>           rtk read <file>         rtk grep <pattern>
rtk find <pattern>      rtk diff <file>

# Test (90-99% savings) — shows failures only
rtk pytest tests/       rtk cargo test          rtk test <cmd>

# Build & Lint (80-90% savings) — shows errors only
rtk tsc                 rtk lint                rtk cargo build
rtk prettier --check    rtk mypy                rtk ruff check

# Analysis (70-90% savings)
rtk err <cmd>           rtk log <file>          rtk json <file>
rtk summary <cmd>       rtk deps                rtk env

# GitHub (26-87% savings)
rtk gh pr view <n>      rtk gh run list         rtk gh issue list

# Infrastructure (85% savings)
rtk docker ps           rtk kubectl get         rtk docker logs <c>

# Package managers (70-90% savings)
rtk pip list            rtk pnpm install        rtk npm run <script>
```

## Rules
- In command chains, prefix each segment: `rtk git add . && rtk git commit -m "msg"`
- For debugging, use raw command without rtk prefix
- `rtk proxy <cmd>` runs command without filtering but tracks usage
<!-- /headroom:rtk-instructions -->

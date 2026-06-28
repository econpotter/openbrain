# Externalize the Acquire Agenda — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Remove the *automated research-agenda* layer (saved queries + watermark + scheduled harvest — §7 of the acquire spec) from openbrain. Keep on-request capture (`acquire(query)`) and everything downstream. The agenda — *which* topics to harvest and *how often* — is the researcher's/driver's concern, not the memory system's.

**Scope principle (the seam):** openbrain **captures on request and reasons over what it holds; it does not decide or schedule a research agenda.** `acquire(query)` is a memory verb (bulk capture) and stays. The standing-topics list + cadence + watermark live in an external driver (a cron, an orchestra job, or a research agent) that calls `openbrain acquire <query>`. This keeps openbrain narrow without re-introducing a separate fetch product (the connectors + library stay here, per the original acquire decision).

**Architecture:** Delete the `saved_queries` table + the saved-query CRUD/run/watermark helpers in `brain.py`, the saved-query MCP tools + CLI subcommands, and the cron hook in `scripts/openbrain-maintenance.py`. Leave `acquire()` (search→dedup→enrich→canonicalize→prefilter→store+embed), the venue registry, query/coverage/affordance tools, and bibtex export untouched.

**Tech Stack:** Python 3.11, asyncpg, FastMCP, typer CLI, pytest.

## Global Constraints

- **Schema = pre-alpha baseline edit, NOT a migration.** Delete `schemas/core/008_saved_queries.sql` and re-run `setup_db.sh` on a blank DB. No `ALTER`/`DROP TABLE` migration.
- **Do not change `acquire(query)` behavior** — only remove the agenda layer around it. The acquire pipeline + its tests stay green.
- All DB access via `brain.py` (`pool = await brain.get_pool()`). Tests: `uv run pytest`, no live network/DB (stubs). `ruff` clean.
- After removal, **no dangling references**: `grep -rniE 'saved_quer|save_query|run_query|update_query|watermark|due_saved' openbrain/ schemas/ scripts/ tests/` returns nothing (except, optionally, an intentional doc note).

## File Structure

- `schemas/core/008_saved_queries.sql` — **delete.**
- `openbrain/brain.py` — **modify.** Remove saved-query helpers: `save_query`, `update_query`, `run_query`, `due_saved_queries`, and any watermark/last-harvested logic (~13 refs). Keep `store_acquired_paper`, `query_library_struct`, `coverage_stats`, bibtex helpers.
- `openbrain/tools/acquire.py` — **modify.** Remove the saved-query MCP tools (`save_query`/`run_query` and any `list`/`update` query tools). Keep `acquire`, `query_library`, `coverage`, `list_venues`, `list_concepts`, `remove_record`, `export_bibtex`.
- `openbrain/cli.py` — **modify.** Remove `save-query` / `run-query` subcommands. Keep `acquire`, `query-library`, `coverage`, `list-venues`, `export-bibtex`.
- `scripts/openbrain-maintenance.py` — **modify.** Remove the saved-query cron hook (the `due_saved_queries` call). Leave the rest of the nightly run.
- `openbrain/sources.py` — **check.** If the from-date/watermark paging param exists *only* for saved-query incremental harvest, remove it; if `acquire()` itself uses paging, keep that path.
- `tests/` — **modify.** Delete saved-query tests (`test_*saved*`, the saved-query cases in `test_acquire.py`/`test_brain.py`/`test_schema_acquire.py`). Keep all acquire/venues/sources tests.
- `ARCHITECTURE.md` (or a short note in the acquire design spec) — **add** one paragraph: the automated agenda is external; openbrain exposes `acquire(query)` on request.

---

### Task 1: Remove the schema + cron hook

- [ ] Delete `schemas/core/008_saved_queries.sql`. Grep `setup_db.sh` / any schema loader list for `008` or `saved_queries`; remove the reference.
- [ ] In `scripts/openbrain-maintenance.py`, remove the `due_saved_queries` cron hook (and its import). Run the maintenance script's tests (if any) — they should pass without the saved-query step.
- [ ] `git commit -m "remove saved_queries schema + maintenance cron hook"`.

### Task 2: Remove the brain.py saved-query layer

- [ ] Remove `save_query`, `update_query`, `run_query`, `due_saved_queries`, and watermark helpers from `openbrain/brain.py`. Verify nothing in the acquire pipeline (`store_acquired_paper`, `acquire`) calls them.
- [ ] Delete the corresponding tests in `tests/test_brain.py` (and `tests/test_schema_acquire.py` saved-query cases).
- [ ] `uv run pytest tests/test_brain.py -q` green. Commit.

### Task 3: Remove the MCP tools + CLI subcommands

- [ ] Remove the saved-query MCP tools from `openbrain/tools/acquire.py` and the `save-query`/`run-query` subcommands from `openbrain/cli.py`. Confirm `server.py` still registers the acquire tool cleanly (no removed symbol referenced).
- [ ] Remove the saved-query cases from `tests/test_acquire.py`.
- [ ] In `openbrain/sources.py`, remove the watermark/from-date param **iff** it is saved-query-only; otherwise leave the acquire paging path.
- [ ] `uv run pytest -q` green. Commit.

### Task 4: Scope note + dangling-ref sweep

- [ ] Add one paragraph to `ARCHITECTURE.md`: *openbrain captures on request (`acquire(query)`) and reasons over the library; the automated research agenda — which topics to harvest and on what cadence — is external (a cron / orchestra job / research agent that calls `openbrain acquire <query>`). openbrain does not own a saved-query registry or scheduler.*
- [ ] `grep -rniE 'saved_quer|save_query|run_query|update_query|watermark|due_saved' openbrain/ schemas/ scripts/ tests/` → only the doc note (if any) remains.
- [ ] `uv run pytest -q` green; `ruff` clean. Commit.

## Self-Review

- `acquire(query)` and all its tests untouched and green.
- No `saved_queries` table, helper, tool, CLI command, or cron hook remains; no dangling references.
- ARCHITECTURE.md states the agenda is external.

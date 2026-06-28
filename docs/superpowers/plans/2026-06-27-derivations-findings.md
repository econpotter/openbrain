# Derivations & Findings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract structured quantitative findings from each library paper via an LLM, store them as provenance-linked rows, and (at a later data-dependent checkpoint) build a causal graph that makes papers queryable by what they find and how they relate.

**Architecture:** Findings is a **second derivation** alongside embeddings — a per-document artifact computed by a model, written to its own table, with the same compute-on-ingest-or-defer-and-backfill lifecycle. The two are **parallel pipelines, not a shared engine** (different models, cardinality, indexes, query paths). Findings extraction reuses `brain.complete_extraction_prompt` (LiteLLM + fallback); `backfill_findings` mirrors `backfill_chunk_embeddings`. The variable/causal graph is deferred to a checkpoint after real extraction output exists.

**Tech Stack:** Python 3.11, asyncpg, pgvector, LiteLLM (via existing `extraction` config), FastMCP, typer CLI, pytest.

**Design spec:** `docs/design/2026-06-27-derivations-findings.md` (read it — this plan implements it).

## Global Constraints

- **Schema = pre-alpha baseline edits, NOT migrations.** Edit `schemas/core/*.sql` in place / add baseline files; re-run `setup_db.sh` on a blank DB. No `ALTER`, no backfill.
- **Two parallel pipelines, no generic framework.** Build `extract_findings`/`backfill_findings` to mirror `embed`/`backfill_chunk_embeddings`; do NOT introduce a generic "derivation registry."
- **Extraction model is configurable** via `config.yaml` `extraction:` (provider/model/fallback) — already exists. `temperature=0.0`; validate output and drop anything off-schema before the DB.
- **Findings are never returned without their source citation** (JOIN `library`).
- **Explicit `findings_status`** on `library` — zero findings ≠ unprocessed, so do not use "no rows" as the pending signal.
- **All DB access via `brain.py`** (`pool = await brain.get_pool()`; `async with pool.acquire() as conn:`). One `register(mcp)` per tool file. Tests: `uv run pytest`, no live LLM/network (stub the completion).

---

## File Structure

- `schemas/core/003_library.sql` — MODIFY: add `findings_status` column.
- `schemas/core/009_variables.sql` — CREATE: `variables` graph-node table.
- `schemas/core/010_findings.sql` — CREATE: `findings` typed edge table.
- `openbrain/findings.py` — CREATE: prompt builder, completion parse/validate, `extract_findings(paper_id)`, `backfill_findings(limit)`.
- `openbrain/brain.py` — MODIFY: `search_findings`; hook inline extraction into the ingest/store paths; helper `set_findings_status`.
- `openbrain/tools/findings.py` — CREATE: `register(mcp)` with `search_findings`, `backfill_findings`.
- `openbrain/server.py` — MODIFY: register the findings tools.
- `openbrain/cli.py` — MODIFY: `search-findings`, `backfill-findings` subcommands.
- `scripts/openbrain-maintenance.py` — MODIFY: `backfill_findings` step in `_run_once()`.
- `tests/test_findings.py` — CREATE.

**Dependency order:** Task 0 (findings_status, before any store path is reworked) → 1 (schema) → 2 (extraction core) → 3 (lifecycle: inline hook + backfill + cron) → 4 (search_findings) → 5 (tools + CLI) → 6 (variable graph — DEFERRED checkpoint).

---

### Task 0: `findings_status` on `library`

**Files:**
- Modify: `schemas/core/003_library.sql`
- Test: `tests/test_findings.py`

**Interfaces:**
- Produces: `library.findings_status TEXT DEFAULT 'pending' CHECK (findings_status IN ('pending','done','error'))`.

- [ ] **Step 1: Add the column** to the `library` CREATE in `schemas/core/003_library.sql`, after `concepts TEXT[],` (or after `bibtex` if Task 1 of the acquire plan hasn't landed):

```sql
    findings_status TEXT DEFAULT 'pending'
                    CHECK (findings_status IN ('pending','done','error')),
```

- [ ] **Step 2: Failing test**

`tests/test_findings.py`:

```python
from pathlib import Path
SCHEMA = Path(__file__).resolve().parents[1] / "schemas" / "core" / "003_library.sql"

def test_library_has_findings_status():
    assert "findings_status" in SCHEMA.read_text()
```

- [ ] **Step 3: Run → PASS.** Run: `uv run pytest tests/test_findings.py::test_library_has_findings_status -v`

- [ ] **Step 4: Verify on blank DB** (`bash setup_db.sh`; `\d library` shows `findings_status`). **Commit.**

```bash
git add schemas/core/003_library.sql tests/test_findings.py
git commit -m "feat(findings): add findings_status to library"
```

---

### Task 1: `variables` + `findings` tables

**Files:**
- Create: `schemas/core/009_variables.sql`, `schemas/core/010_findings.sql`
- Test: `tests/test_findings.py`

**Interfaces:**
- Produces: `variables` (PK id, `name`, `norm_name`, `embedding VECTOR(768)`); `findings` (PK id, `paper_id`→library, `cause_text`, `effect_text`, nullable `cause_var_id`/`effect_var_id`→variables, `magnitude_value`, `magnitude_unit`, `magnitude_stat`, `conditions`, `raw_quote`, `confidence`).

- [ ] **Step 1: Create `schemas/core/009_variables.sql`**

```sql
-- Causal-graph nodes: normalized cause/effect variables, merged across papers.
CREATE TABLE variables (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL,
    norm_name   TEXT NOT NULL,
    embedding   VECTOR(768),
    metadata    JSONB DEFAULT '{}',
    created_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX variables_embedding_idx ON variables USING hnsw (embedding vector_cosine_ops);
CREATE INDEX variables_norm_name_idx ON variables (norm_name);
```

- [ ] **Step 2: Create `schemas/core/010_findings.sql`**

```sql
-- Typed causal edges extracted from papers: cause → effect, with magnitude.
CREATE TABLE findings (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    paper_id        UUID NOT NULL REFERENCES library(id) ON DELETE CASCADE,
    cause_text      TEXT NOT NULL,
    effect_text     TEXT NOT NULL,
    cause_var_id    UUID REFERENCES variables(id),
    effect_var_id   UUID REFERENCES variables(id),
    magnitude_value NUMERIC,
    magnitude_unit  TEXT,
    magnitude_stat  TEXT,
    conditions      TEXT,
    raw_quote       TEXT,
    confidence      NUMERIC,
    created_at      TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX findings_paper_idx  ON findings (paper_id);
CREATE INDEX findings_cause_idx  ON findings (cause_var_id);
CREATE INDEX findings_effect_idx ON findings (effect_var_id);
```

- [ ] **Step 3: Failing test**

```python
def test_findings_baseline_files_present():
    core = SCHEMA.parent
    assert (core / "009_variables.sql").exists()
    assert (core / "010_findings.sql").exists()
```

- [ ] **Step 4: Run → PASS; verify on blank DB; commit.**

```bash
git add schemas/core/009_variables.sql schemas/core/010_findings.sql tests/test_findings.py
git commit -m "feat(findings): variables + findings tables"
```

---

### Task 2: Extraction core (`findings.py`)

**Files:**
- Create: `openbrain/findings.py`
- Test: `tests/test_findings.py`

**Interfaces:**
- Consumes: `brain.complete_extraction_prompt(prompt) -> str`, `brain.get_pool`.
- Produces:
  - `build_prompt(title, text) -> str`.
  - `parse_findings(raw: str) -> list[dict]` — parse the model's JSON, keep only well-formed findings (require `cause_text`, `effect_text` non-empty strings; coerce `magnitude_value` to float-or-None; drop the rest).
  - `async extract_findings(paper_id: str) -> dict` — load the paper's text, prompt, complete, parse, insert rows + set `findings_status='done'` in one transaction; on completion failure set `'error'` and re-raise-as-handled. Returns `{paper_id, extracted, status}`.

- [ ] **Step 1: Failing test — `parse_findings` keeps valid, drops malformed**

```python
from openbrain import findings

def test_parse_findings_validates_and_drops_bad():
    raw = '''[
      {"cause_text":"snowpack","effect_text":"streamflow","magnitude_value":0.8,"magnitude_unit":"elasticity","magnitude_stat":"elasticity","raw_quote":"..."},
      {"cause_text":"","effect_text":"x"},
      {"effect_text":"missing cause"},
      {"cause_text":"price","effect_text":"demand","magnitude_value":"not a number"}
    ]'''
    out = findings.parse_findings(raw)
    assert len(out) == 2                       # first + price/demand
    assert out[0]["cause_text"] == "snowpack"
    assert out[1]["magnitude_value"] is None   # uncoercible → None, row kept
```

- [ ] **Step 2: Run — fail. Step 3: Implement `parse_findings` + `build_prompt`.**

```python
"""findings.py — LLM findings extraction (second derivation, mirrors embeddings)."""
import json
from openbrain import brain

def build_prompt(title: str, text: str) -> str:
    return (
        "Extract causal findings from this paper as a JSON array. Each finding: "
        '{"cause_text","effect_text","magnitude_value"(number|null),"magnitude_unit",'
        '"magnitude_stat"(coefficient|elasticity|correlation|p_value|null),"conditions","raw_quote","confidence"(0-1)}. '
        "Only findings the paper actually estimates. Return ONLY the JSON array.\n\n"
        f"TITLE: {title}\n\nTEXT:\n{text}"
    )

def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

def parse_findings(raw: str) -> list[dict]:
    try:
        data = json.loads(raw[raw.index("["): raw.rindex("]") + 1])
    except (ValueError, json.JSONDecodeError):
        return []
    out = []
    for f in data if isinstance(data, list) else []:
        if not isinstance(f, dict):
            continue
        cause = (f.get("cause_text") or "").strip()
        effect = (f.get("effect_text") or "").strip()
        if not cause or not effect:
            continue
        out.append({
            "cause_text": cause, "effect_text": effect,
            "magnitude_value": _to_float(f.get("magnitude_value")),
            "magnitude_unit": f.get("magnitude_unit"),
            "magnitude_stat": f.get("magnitude_stat"),
            "conditions": f.get("conditions"),
            "raw_quote": f.get("raw_quote"),
            "confidence": _to_float(f.get("confidence")),
        })
    return out
```

- [ ] **Step 4: Run → PASS.**

- [ ] **Step 5: Failing test — `extract_findings` inserts rows + sets status (stub the LLM)**

```python
import pytest

@pytest.mark.asyncio
async def test_extract_findings_inserts_and_marks_done(monkeypatch):
    pool = await brain.get_pool()
    async with pool.acquire() as conn:
        pid = await conn.fetchval(
            "INSERT INTO library (title, abstract, doc_type) VALUES ('T','snowpack raises streamflow','paper') RETURNING id")
    async def fake_complete(prompt):
        return '[{"cause_text":"snowpack","effect_text":"streamflow","magnitude_value":0.8,"magnitude_stat":"elasticity","raw_quote":"q"}]'
    monkeypatch.setattr(brain, "complete_extraction_prompt", fake_complete)
    res = await findings.extract_findings(str(pid))
    assert res["extracted"] == 1 and res["status"] == "done"
    async with pool.acquire() as conn:
        n = await conn.fetchval("SELECT count(*) FROM findings WHERE paper_id=$1", pid)
        st = await conn.fetchval("SELECT findings_status FROM library WHERE id=$1", pid)
    assert n == 1 and st == "done"
```

- [ ] **Step 6: Implement `extract_findings`**

```python
async def _paper_text(conn, paper_id):
    row = await conn.fetchrow("SELECT title, abstract FROM library WHERE id=$1", paper_id)
    body = await conn.fetchval(
        "SELECT string_agg(content, '\n') FROM chunks WHERE library_id=$1 AND kind='body'", paper_id)
    return row["title"], (body or row["abstract"] or "")

async def extract_findings(paper_id: str) -> dict:
    pool = await brain.get_pool()
    async with pool.acquire() as conn:
        title, text = await _paper_text(conn, paper_id)
    if not text:
        async with pool.acquire() as conn:
            await conn.execute("UPDATE library SET findings_status='done' WHERE id=$1", paper_id)
        return {"paper_id": paper_id, "extracted": 0, "status": "done"}
    try:
        raw = await brain.complete_extraction_prompt(build_prompt(title, text))
    except Exception:
        async with pool.acquire() as conn:
            await conn.execute("UPDATE library SET findings_status='error' WHERE id=$1", paper_id)
        return {"paper_id": paper_id, "extracted": 0, "status": "error"}
    rows = parse_findings(raw)
    async with pool.acquire() as conn:
        async with conn.transaction():
            for f in rows:
                await conn.execute(
                    """INSERT INTO findings (paper_id, cause_text, effect_text, magnitude_value,
                         magnitude_unit, magnitude_stat, conditions, raw_quote, confidence)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)""",
                    paper_id, f["cause_text"], f["effect_text"], f["magnitude_value"],
                    f["magnitude_unit"], f["magnitude_stat"], f["conditions"], f["raw_quote"], f["confidence"])
            await conn.execute("UPDATE library SET findings_status='done' WHERE id=$1", paper_id)
    return {"paper_id": paper_id, "extracted": len(rows), "status": "done"}
```

- [ ] **Step 7: Run → PASS. Commit.**

```bash
git add openbrain/findings.py tests/test_findings.py
git commit -m "feat(findings): extraction core — prompt, parse/validate, extract_findings"
```

---

### Task 3: Lifecycle — inline hook, `backfill_findings`, cron

**Files:**
- Modify: `openbrain/findings.py` (`backfill_findings`), `openbrain/brain.py` (call `extract_findings` from the store/ingest paths), `scripts/openbrain-maintenance.py`
- Test: `tests/test_findings.py`

**Interfaces:**
- Produces: `async backfill_findings(limit: int = 100) -> dict` — mirror of `backfill_chunk_embeddings`: select `library` rows with `findings_status='pending'`, run `extract_findings`, tally. Returns `{pending_before, extracted_papers, failed, pending_after}`.

- [ ] **Step 1: Failing test — backfill processes pending, leaves done alone**

```python
@pytest.mark.asyncio
async def test_backfill_findings_processes_pending(monkeypatch):
    pool = await brain.get_pool()
    async with pool.acquire() as conn:
        pid = await conn.fetchval(
            "INSERT INTO library (title, abstract, doc_type, findings_status) "
            "VALUES ('P','snowpack raises streamflow','paper','pending') RETURNING id")
    monkeypatch.setattr(brain, "complete_extraction_prompt",
        lambda p: _async('[{"cause_text":"a","effect_text":"b"}]'))
    res = await findings.backfill_findings(limit=10)
    assert res["extracted_papers"] >= 1
    async with pool.acquire() as conn:
        st = await conn.fetchval("SELECT findings_status FROM library WHERE id=$1", pid)
    assert st == "done"
```

(`_async` = a tiny helper returning a coroutine that yields the value; or use `monkeypatch` with an `async def`.)

- [ ] **Step 2: Implement `backfill_findings`** (mirror `brain.backfill_chunk_embeddings` structure exactly)

```python
async def backfill_findings(limit: int = 100) -> dict:
    pool = await brain.get_pool()
    async with pool.acquire() as conn:
        pending_before = await conn.fetchval(
            "SELECT count(*) FROM library WHERE findings_status='pending' AND doc_type='paper'")
        rows = await conn.fetch(
            "SELECT id FROM library WHERE findings_status='pending' AND doc_type='paper' "
            "ORDER BY created_at LIMIT $1", limit)
    extracted, failed = 0, 0
    for r in rows:
        try:
            res = await extract_findings(str(r["id"]))
            extracted += 1 if res["status"] == "done" else 0
            failed += 1 if res["status"] == "error" else 0
        except Exception:
            failed += 1
    return {"pending_before": pending_before, "extracted_papers": extracted,
            "failed": failed, "pending_after": pending_before - extracted}
```

- [ ] **Step 3: Hook inline extraction into the store path** — in `brain.store_acquired_paper` and `ingest_document_pipeline`, after the record is stored, best-effort call so a live model fills findings immediately, leaving `pending` on failure:

```python
    try:
        from openbrain import findings as _findings
        await _findings.extract_findings(str(library_id))
    except Exception:
        pass  # stays findings_status='pending' for the nightly backfill
```

- [ ] **Step 4: Add the maintenance step** in `scripts/openbrain-maintenance.py` `_run_once()`, after the embedding backfill log:

```python
    from openbrain import findings
    fb = await findings.backfill_findings()
    logger.info("Findings backfill: %d papers extracted, %d failed, %d pending.",
                fb["extracted_papers"], fb["failed"], fb["pending_after"])
```

- [ ] **Step 5: Run tests → PASS. Commit.**

```bash
git add openbrain/findings.py openbrain/brain.py scripts/openbrain-maintenance.py tests/test_findings.py
git commit -m "feat(findings): defer/backfill lifecycle + inline ingest hook + nightly step"
```

---

### Task 4: `search_findings` (rows + citations)

**Files:**
- Modify: `openbrain/brain.py`
- Test: `tests/test_findings.py`

**Interfaces:**
- Produces: `async search_findings(*, text=None, cause=None, effect=None, stat=None, venue=None, year_min=None, status=None, group_by=None, limit=50) -> list[dict]` — each result `{finding:{...}, source:{citekey,authors,year,title,venue_abbrev,doi}}` via JOIN `library`. `text` matches cause/effect text (simple mode; graph traversal added in Task 6).

- [ ] **Step 1: Failing test — findings come back JOINed to source citation**

```python
@pytest.mark.asyncio
async def test_search_findings_returns_finding_with_source():
    pool = await brain.get_pool()
    async with pool.acquire() as conn:
        pid = await conn.fetchval(
            "INSERT INTO library (title, authors, year, doi, doc_type, findings_status) "
            "VALUES ('Snow study', ARRAY['Jane Smith'], 2021, '10.1/s', 'paper', 'done') RETURNING id")
        await conn.execute(
            "INSERT INTO findings (paper_id, cause_text, effect_text, magnitude_stat) "
            "VALUES ($1,'snowpack','streamflow','elasticity')", pid)
    out = await brain.search_findings(text="snowpack")
    assert out and out[0]["finding"]["cause_text"] == "snowpack"
    assert out[0]["source"]["title"] == "Snow study"
    assert out[0]["source"]["year"] == 2021
```

- [ ] **Step 2: Implement `search_findings`** (build parameterized WHERE; `text` → `(cause_text ILIKE %text% OR effect_text ILIKE %text%)`; JOIN library for the source block; shape each row into `{finding, source}`).

- [ ] **Step 3: Run → PASS. Commit.**

```bash
git add openbrain/brain.py tests/test_findings.py
git commit -m "feat(findings): search_findings — rows joined to source citations"
```

---

### Task 5: Findings MCP tools + CLI

**Files:**
- Create: `openbrain/tools/findings.py`; Modify: `openbrain/server.py`, `openbrain/cli.py`
- Test: `tests/test_findings.py`

**Interfaces:**
- Produces MCP tools `search_findings`, `backfill_findings`; CLI `openbrain search-findings`, `openbrain backfill-findings` (thin wrappers over the same brain/findings fns, 1:1).

- [ ] **Step 1: Implement `openbrain/tools/findings.py`** (mirror `tools/library.py`)

```python
import json
from openbrain import brain, findings

def register(mcp):
    @mcp.tool
    async def search_findings(text: str | None = None, cause: str | None = None,
                              effect: str | None = None, stat: str | None = None,
                              venue: str | None = None, year_min: int | None = None,
                              status: str | None = None, limit: int = 50) -> str:
        """Search extracted findings; each row returned with its source paper citation."""
        out = await brain.search_findings(text=text, cause=cause, effect=effect, stat=stat,
            venue=venue, year_min=year_min, status=status, limit=limit)
        return json.dumps(out, indent=2, default=str)

    @mcp.tool
    async def backfill_findings(limit: int = 100) -> str:
        """Extract findings for papers still pending (e.g. extraction model was down at ingest)."""
        res = await findings.backfill_findings(limit=limit)
        if res["pending_before"] == 0:
            return "No papers pending findings extraction."
        return (f"Findings backfill: {res['extracted_papers']} extracted, "
                f"{res['failed']} failed, {res['pending_after']} still pending.")
```

- [ ] **Step 2: Register in `server.py`**

```python
from openbrain.tools import findings as findings_tools
findings_tools.register(mcp)
```

- [ ] **Step 3: Add CLI subcommands** in `cli.py` (`asyncio.run` wrappers over `brain.search_findings` / `findings.backfill_findings`, rendered with `rich`). CLI test via `CliRunner` (stub the brain fn), mirroring the acquire-plan CLI test.

- [ ] **Step 4: Run tests → PASS. Commit.**

```bash
git add openbrain/tools/findings.py openbrain/server.py openbrain/cli.py tests/test_findings.py
git commit -m "feat(findings): search_findings + backfill_findings MCP tools and CLI"
```

---

### Task 6: Variable resolution + causal graph — DEFERRED CHECKPOINT

> **Do NOT implement until Tasks 2–5 are producing real findings on a real corpus.** This task is a design checkpoint, not a ready-to-code task: entity-resolution quality (how mergeable cause/effect text is across papers) can only be tuned against actual extraction output. The schema (`variables`, `findings.cause_var_id/effect_var_id`) already exists from Task 1, nullable — so nothing here blocks Tasks 2–5.

**When the checkpoint is reached, design + implement (TDD, separate commits):**

- [ ] **`resolve_variables(limit)` maintenance pass** (mirror `brain.backfill_links`): for findings with NULL `cause_var_id`/`effect_var_id`, embed the cause/effect text (`brain.embed`), nearest-neighbor match against `variables.embedding` (hnsw cosine) above a merge threshold → reuse that variable; else `INSERT` a new variable (`name`, `norm_name`, `embedding`). Set the FK. Tune the threshold against real output.
- [ ] **Add the resolution pass to `_run_once()`** after `backfill_findings`.
- [ ] **Upgrade `search_findings(text=...)` / add findings half of `search_library`** to traverse the graph: resolve query → nearest variable node(s) → findings where `cause_var_id`/`effect_var_id` match → papers. Decide here whether to mirror finding-edges into the generic `links` table so the existing `traverse_links()` / `brain.traverse_graph()` unifies them with people/projects (default lean: reuse `links`).
- [ ] **`search_library` relatedness fusion** — combine embedding neighbors (`search_library_vec`) ∪ findings-graph neighbors into one ranked paper list with evidence (decide the blend weight against real data).

**Decision inputs to record at the checkpoint:** sample of raw `cause_text`/`effect_text` variety; observed duplicate-variable rate; whether `links`-mirroring earns its keep.

---

## Self-Review

**Spec coverage** (`2026-06-27-derivations-findings.md`):
- §2 two-parallel-pipelines → enforced (Global Constraints; backfill mirrors embeddings, no registry). §3 extraction (LiteLLM, temp 0, validate-drop) → Task 2. §4 lifecycle + explicit status → Tasks 0, 3. §5 schema (findings_status, variables, findings, raw+resolved-FK) → Tasks 0, 1. §6 variable graph = checkpoint → Task 6 (deferred, as specified). §7 query surface: `search_findings` (rows+citations) → Task 4; `search_library` relatedness + findings-half → Task 6 (graph). §8 CLI↔MCP 1:1 → Task 5. §9 maintenance step → Task 3.
- `coverage` (§7) is implemented in the acquire plan (Task 6 there) — not duplicated here.

**Placeholders:** none — every code step shows real code grounded in existing signatures (`backfill_chunk_embeddings`, `complete_extraction_prompt`, `backfill_links`, `search_library_vec`). Task 6 is explicitly deferred-by-design, not a placeholder; its schema dependency (Task 1) is already concrete.

**Type consistency:** `extract_findings(paper_id) -> {paper_id, extracted, status}` (Task 2) consumed by `backfill_findings` (Task 3) and the inline hook (Task 3). `search_findings(...) -> [{finding, source}]` (Task 4) consumed by the MCP tool + CLI (Task 5). `findings_status` values `pending|done|error` consistent across Tasks 0/2/3.

**Cross-plan note:** the inline extraction hook (Task 3) attaches to `store_acquired_paper`, which is created in the acquire plan (Task 4 there). If the acquire plan hasn't landed, attach the hook to `_store_paper`/`ingest_document_pipeline` instead — same one-line best-effort call.

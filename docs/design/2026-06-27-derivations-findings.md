# openbrain Derivations & Findings — Design Plan

**Date:** 2026-06-27
**Status:** Design (discussed + agreed), pending implementation.
**Relationship:** Downstream consumer of the `library` table that the `acquire` subsystem
(`2026-06-26-acquire-subsystem.md`) fills. One ordered build, two docs. No v1/v2 milestones —
build in dependency order; the variable graph (§6) is a data-dependent checkpoint, not a release.

---

## 1. Purpose

Add **structured findings extraction** over library papers, and from those findings build a
**causal knowledge graph** that makes papers queryable by *what they find* and *how they relate*,
not just by text/embedding similarity.

Distilled from `idea.md` (Geo-Academic Context Engine) after dropping everything already provided
by existing systems:

- **Spatial spine / PostGIS / `spatial_entities`** — owned by `~/orchestra/projects/data-warehouse/`.
  Not rebuilt. **No spatial tie on findings** — most journal articles have no spatial scope, so the
  link would be null for the majority and would couple openbrain to the warehouse for a minority
  payoff. Cut. The two systems stay decoupled.
- **`academic_papers` registry** — already `library`.
- **File-hash dedup** — already `library.file_hash`.

What survives is the genuinely new capability: a model reads each paper and emits
provenance-linked quantitative findings; findings become graph edges between normalized variable
nodes; papers relate through shared variables.

---

## 2. Core framing: findings is a derivation, like embeddings

A **derivation** = a per-document artifact computed by a model from a paper's text, written to its
own table, with a compute-on-ingest-or-defer-and-backfill lifecycle. openbrain already has one
derivation (embeddings). Findings is a second.

They are **two parallel pipelines, NOT a generic framework.** They share only the *lifecycle
convention*; they share nothing else, and unifying them would be abstraction two dissimilar cases
don't earn:

| | Embeddings (exists) | Findings (new) |
|---|---|---|
| Model | embedding model (Voyage / local), vector out | generative LLM via **LiteLLM**, structured rows out |
| Cardinality | **per-chunk** (N chunks/paper) | **per-paper** (full text, N findings/paper) |
| Output table | `chunks.embedding VECTOR(768)` | `findings` (typed, §5) |
| Index | hnsw vector | btree/gin + graph |
| Compute fn | `embed` | `extract_findings` |
| Backfill fn | `backfill_chunk_embeddings` | `backfill_findings` |
| Pending signal | NULL `embedding` on chunk | **explicit `findings_status`** (see §4) |

A single LLM cannot compute both jointly — different models, different infra. The pipelines are
independent at production time **and** at query time (search runs two independent searches and
merges — §7). So: build `extract_findings`/`backfill_findings` to deliberately **mirror**
`embed`/`backfill_chunk_embeddings` in shape and lifecycle, without a shared engine.

---

## 3. Extraction

- **Route:** LiteLLM (the same service the nightly memory-consolidation `extraction` config already
  uses). **Model is configurable** via the `extraction` config block — not hardcoded.
- **Deterministic:** `temperature=0.0`, structured output enforced (Pydantic / JSON schema). Output
  that fails schema validation is dropped at the validation layer before the DB.
- **Input:** the paper's full text (reuse the text already extracted at ingest — no second parse,
  no chunking).
- **Atomic commit:** a paper's findings rows + its `findings_status` update land in one transaction.

---

## 4. Lifecycle & status (the one real divergence from embeddings)

Mirror the embedding defer/backfill flow:

- On ingest, after text extraction, attempt findings extraction inline.
- Model reachable → insert findings rows, `findings_status='done'`.
- Model down / API error → paper lands, `findings_status='pending'`, no findings rows yet.
- Nightly `_run_once()` calls `backfill_findings(limit)` → processes `pending` papers; re-runnable;
  only touches pending.

**Why an explicit status column, not NULL-output (the divergence):** embeddings signal "pending"
via a NULL embedding on the chunk. Findings can't — a paper may legitimately yield **zero**
findings, so "zero rows" ≠ "not processed." Findings needs an explicit per-paper status.

**Decision:** a `findings_status` column on `library` (`pending | done | error`), set in the same
transaction as extraction. Simplest correct model for two parallel pipelines; no generic
`derivations` status table (that belongs to a generic framework we decided against — §2).

---

## 5. Schema (pre-alpha baseline edits, NOT migrations)

Per ROADMAP ALPHA + AGENTS.md: edit `schemas/core/` in place, add new baseline files, re-run
`setup_db.sh` against a blank DB. No ALTER/backfill — re-derivable by re-running extraction.

**`library` (edit `003_library.sql`):** add `findings_status TEXT DEFAULT 'pending'
CHECK (findings_status IN ('pending','done','error'))`.

**New baseline file — `variables` (causal graph nodes):**
```sql
CREATE TABLE variables (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL,             -- canonical display name
    norm_name   TEXT NOT NULL,             -- normalized key for exact-ish matching
    embedding   VECTOR(768),               -- for fuzzy resolution (same pattern as people/projects)
    metadata    JSONB DEFAULT '{}',
    created_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX variables_embedding_idx ON variables USING hnsw (embedding vector_cosine_ops);
```

**New baseline file — `findings` (typed causal edges):**
```sql
CREATE TABLE findings (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    paper_id        UUID NOT NULL REFERENCES library(id) ON DELETE CASCADE,  -- provenance
    cause_text      TEXT NOT NULL,         -- as extracted (raw)
    effect_text     TEXT NOT NULL,         -- as extracted (raw)
    cause_var_id    UUID REFERENCES variables(id),   -- NULL until resolved (§6)
    effect_var_id   UUID REFERENCES variables(id),   -- NULL until resolved (§6)
    magnitude_value NUMERIC,               -- nullable: not every finding is a number
    magnitude_unit  TEXT,                  -- %, elasticity, $/unit, …
    magnitude_stat  TEXT,                  -- coefficient | elasticity | correlation | p_value | …
    conditions      TEXT,                  -- scope/population/qualifiers (free text)
    raw_quote       TEXT,                  -- source sentence, for verification
    confidence      NUMERIC,               -- model self-reported extraction confidence
    created_at      TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX findings_paper_idx  ON findings (paper_id);
CREATE INDEX findings_cause_idx  ON findings (cause_var_id);
CREATE INDEX findings_effect_idx ON findings (effect_var_id);
```

**Pattern note — raw value + resolved reference, resolution deferred** — used three times now:
- acquire: raw `library.source` name → resolved `venue_abbrev`
- findings: raw `cause_text`/`effect_text` → resolved `cause_var_id`/`effect_var_id`

`magnitude_*` are real columns (not JSONB) because they're filtered on
(`stat='elasticity' AND value < -0.3`) — same "real columns over JSONB" call the acquire spec made
for venue.

---

## 6. Variable resolution & the causal graph — DATA-DEPENDENT CHECKPOINT

This is the hard part and is **deliberately deferred** until real extraction output exists to
design against — entity-resolution quality (how messy cause/effect text is, how mergeable variables
are across papers) can't be judged from assumptions.

Approach (to be tuned at the checkpoint, not finalized now):

- A resolution pass maps raw `cause_text`/`effect_text` → canonical `variables` nodes via embedding
  match + a merge threshold; unmatched text mints a new variable. Runs as a **maintenance pass like
  `backfill_links`**, not inline.
- Once `cause_var_id`/`effect_var_id` are populated, findings ARE the causal graph: directed edges
  (cause variable → effect variable) owned by a paper, carrying magnitude. Papers relate through
  shared variable nodes.

**Open (decide at checkpoint):** whether finding-edges also **mirror into the generic `links`
table** so the existing `traverse_links()` recursive-CTE unifies them with people/projects/thoughts
traversal — vs a findings-specific traversal. Reuse of `links`/`traverse_links` is the default lean;
confirm against real data.

Until this lands, the findings half of search (§7) runs in **simple mode** (match query against
findings' cause/effect text directly). It upgrades to graph traversal when resolution exists. Same
`search_library` signature throughout.

---

## 7. Query surface

Three objects, three methods. All share the structured-filter vocabulary
(`venue`, `kind`, `year_min`, `year_max`, `status`, `concept`, `limit`).

- **`search_library(<text>, author=, title=, journal=, abstract=, <filters>, fuzzy=False, limit=)`**
  — returns **papers**.
  - Named field params = the `WHERE` (exact by default; `fuzzy=True` flips author/title/journal;
    `abstract=` is always fuzzy).
  - `<text>` = the `ORDER BY`: relatedness ranking. Runs **two independent searches and merges** —
    embedding cosine (over chunks) ∪ findings search (over findings) — into one ranked list, each
    result carrying its evidence (score + shared variables/findings) and citation.
  - With `<text>`: ranked + evidence. Without: the filtered set sorted by `sort=`. (Return shape
    varies with `<text>` — documented.)
- **`search_findings(text=, cause=, effect=, stat=, <filters>, limit=)`** — returns **findings rows
  JOINed to their source citation** (never bare):
  ```
  { finding: {cause_text, effect_text, magnitude_value, magnitude_unit, magnitude_stat,
              conditions, raw_quote},
    source:  {citekey, authors, year, title, venue_abbrev, doi} }
  ```
  `group_by='paper'` optional to collapse N findings under one source. `citekey` = the same stable
  slug scheme bibtex export uses, so a finding and its bib entry share an identity.
- **`coverage(topic|venue|concept)`** — **aggregates** (counts by year/venue). Owns the
  "did I get everything / where are the gaps" question, so `search_library` never has to mean
  "complete set."

Findings are **never returned without their source citation.** That FK is the anti-hallucination
payoff: every fact hard-linked to a paper PK; `raw_quote` lets agent + human verify against the
source sentence.

---

## 8. MCP tools + CLI (parallel thin adapters over shared core)

Logic lives in **core module functions** (`brain.py`, a new `findings.py`). Two thin adapters wrap
them, **1:1**, consistent naming:

- `tools/*.py` `register(mcp)` → MCP tools.
- A single `openbrain` CLI (`python -m openbrain.cli`, subcommands) → host-side manual ops.

Each core fn = one MCP tool + one CLI subcommand (`openbrain search-findings …`,
`openbrain backfill-findings …`). The existing maintenance backfills fold into this — they become
CLI subcommands over the same core. CLI earns its keep for saved-query CRUD + backfills run by hand
on the host without an agent. (Exact command list = impl detail.)

---

## 9. Maintenance run (consolidated `_run_once`)

The findings backfill is one more step in the already-consolidated nightly run (the
maintenance/backup consolidation is done — see `2026-06-27-maintenance-backup-consolidation.md`):

```
_run_once():  backup → memory-extraction → embedding-backfill → findings-backfill
                                                              → saved-query harvest (acquire §7)
```

Same defer/backfill semantics for findings as embeddings. Variable resolution (§6) becomes an
additional pass here once it lands.

---

## 10. Dependency order

1. **Embeddings → derivation lifecycle** alignment + `findings_status` on `library`. Must land
   **before** acquire's store step — both touch `_store_paper`/the embed path; do this first so
   acquire plugs into a clean lifecycle instead of being reworked.
2. **Findings extraction** (`extract_findings` + `backfill_findings`) + ingest hook + maintenance
   step + `search_findings`.
3. **Variable resolution + graph** (§6) — the checkpoint; design once real findings exist; then
   `search_library` relatedness gains its findings/graph half.

---

## 11. Open questions (impl detail — resolve during build)

- Extraction prompt + Pydantic schema (the extraction contract).
- Relatedness blend formula in `search_library` (semantic vs graph-overlap weighting).
- `magnitude_*` specifics — sign handling, multiple stats per finding.
- Variable-resolution tuning + the `links`-mirror decision (§6 checkpoint).
- CLI exact command list (1:1 with tools).

---

## 12. Out of scope

- Spatial/geographic linking (cut — §1).
- LLM synthesis / lit reviews — external agent skill over the retrieval surface (as in acquire §9).
- Citation snowballing — deferred with acquire.

# openbrain Workflow Structure Evaluation

**Date:** 2026-07-09
**Status:** Evaluation / recommendation.
**Scope:** Whether openbrain should add more workflow structure, and whether that structure
should use LangGraph or a smaller local design.

---

## Recommendation

Add more structure to openbrain, but do not move the core system to LangGraph.

The useful next step is a small, DB-backed workflow/derivation layer that makes deferred
computed work explicit: pending work, attempts, errors, retry timing, and completion. Keep
Postgres as the durable state source and keep MCP tools as the external interface.

Use LangGraph later only for agentic research drivers that call openbrain tools from outside the
core system.

---

## Architecture Check

**Status quo:** `brain.py` owns most domain behavior and database access. Tool files stay thin.
Several derivations and maintenance paths already exist: embeddings, thought extraction,
findings extraction, entity embedding refresh, link backfill, paper fetch, document ingest, and
nightly maintenance.

**From-scratch ideal:** domain services plus an explicit DB-backed derivation/job model. The
services own deterministic transformations; the derivation model owns retryable deferred work.

**Recommendation:** move toward the from-scratch ideal incrementally. The current pain is not
"we need an agent graph." The current pain is "we have many persistent transformations with
similar lifecycles but different ad hoc status signals."

This meets CODING STANDARDS by making failure states explicit, keeping data in the database
rather than in hidden control flow, and improving single-responsibility boundaries without
introducing a framework before the need is proven.

---

## Why More Structure Would Help

openbrain already has several workflow-shaped paths:

- Thought capture -> metadata extraction -> links -> embeddings.
- Document ingest -> text extraction -> metadata resolution -> file storage -> library row ->
  chunking -> embeddings.
- Paper fetch/acquire -> library metadata -> abstract embedding -> findings extraction.
- Maintenance -> backup -> memory extraction -> embedding backfills -> findings backfills.
- Search and briefing -> hybrid retrieval across thoughts, entities, library records, links, and
  findings.

These are not merely helper functions. They are persistent transformations with durability,
partial failure, retry, and observability concerns.

Today the project handles this with a mix of conventions:

- Null embeddings mean pending embedding work.
- `library.findings_status` means pending/done/error findings work.
- Thought extraction and link backfill have their own maintenance paths.
- Nightly and sweep maintenance coordinate multiple backfills directly.

That works while the number of derivations is small. It will become harder to reason about as
new derived artifacts are added.

---

## Proposed Local Structure

Prefer a small internal abstraction over a general orchestration framework.

### 1. Split domain services out of `brain.py`

Keep the invariant that tool files do not write SQL directly, but allow core logic to move into
focused modules:

- `thoughts.py` for capture, extraction, recent thoughts, and append-only thought operations.
- `library.py` for library records, metadata updates, reference resolution, and exports.
- `ingest.py` for file ingest, hashing, text extraction, chunking, and metadata resolution.
- `papers.py` remains the external metadata/fetch adapter.
- `embeddings.py` for model routing, embedding calls, and embedding backfills.
- `entities.py` for people, projects, entity resolution, notes, and entity embedding refresh.
- `search.py` for vector, FTS, hybrid ranking, and briefing retrieval.
- `maintenance.py` for scheduled maintenance orchestration.

This is a module-boundary cleanup, not a behavior change.

### 2. Add an explicit derivation contract

Model deferred computed artifacts as named derivations. Candidate kinds:

- `thought_embedding`
- `thought_extraction`
- `chunk_embedding`
- `abstract_embedding`
- `findings_extraction`
- `entity_embedding`
- `link_backfill`

Each derivation should have a uniform state model:

- `pending`
- `running`
- `done`
- `error`

Track at least:

- target table / target id
- derivation kind
- status
- attempt count
- last error
- next retry time
- created / updated timestamps

The API can stay small:

```python
mark_pending(kind, target_id)
claim_pending(kind, limit)
mark_done(kind, target_id)
mark_error(kind, target_id, error)
backfill(kind, limit)
run_derivation(kind, target_id)
```

The exact schema can be decided when implementing, but the important design point is that every
deferred computation has one visible lifecycle.

### 3. Keep derivation logic concrete

The derivation model should unify lifecycle, not computation.

Embeddings, findings extraction, thought extraction, and link backfill should keep separate
implementation functions. They use different models, inputs, output tables, and validation rules.
Do not collapse them into a generic "run model on thing" framework.

This preserves the current findings design decision: findings and embeddings are parallel
pipelines with similar lifecycle conventions, not the same engine.

### 4. Make maintenance operate over the contract

Maintenance should be able to report and process work by derivation kind:

```text
pending:
  thought_embedding: 3
  thought_extraction: 1
  chunk_embedding: 42
  findings_extraction: 7
errors:
  findings_extraction: 2
```

That gives openbrain a fail-loud operational surface. It also makes "what is stuck?" answerable
without remembering which table uses which pending convention.

---

## Where LangGraph Would Help

LangGraph becomes useful if openbrain grows beyond deterministic memory operations into
long-running agentic workflows, such as:

- Research this topic until enough evidence is found.
- Monitor active projects and propose updates.
- Read papers, extract claims, compare against existing notes, and ask for human approval.
- Run a multi-step synthesis workflow with checkpoints and interruptions.
- Maintain standing agendas that decide what to fetch, read, summarize, and store next.

Those are graph-shaped agent workflows. They involve decisions, branching, intermediate state,
interruptions, and human review. LangGraph is a plausible fit there.

The better boundary is:

```text
LangGraph research driver
  -> calls openbrain MCP tools
  -> openbrain persists memory, papers, findings, links, and derivations in Postgres
```

That keeps openbrain as the memory system and lets agent orchestration live above it.

---

## Why Not LangGraph Inside Core openbrain

Putting LangGraph inside the core would add overhead before it pays for itself:

- Another state model alongside Postgres.
- Serialization/checkpointing decisions for data already stored durably.
- More framework concepts in a project whose core invariants are database invariants.
- More places to debug failures.
- Pressure to express deterministic database work as graph nodes when ordinary transactions are
  clearer.

The current bottleneck is not lack of graph execution. It is lack of one visible lifecycle for
deferred computed work.

---

## Concrete Benefits of the Local Approach

- Smaller files and clearer ownership than one large `brain.py`.
- Uniform pending/error/done reporting across all deferred work.
- Safer retries because retry state is explicit.
- Easier addition of new derived artifacts, such as paper summaries, claim extraction, concept
  tagging, citation-context extraction, or variable resolution.
- Better tests: each derivation can be tested as one deterministic unit plus one lifecycle path.
- Better operations: maintenance can show exactly what is pending, what failed, and what will be
  retried.

---

## Cost

The local derivation layer is moderate engineering work:

- Design the schema.
- Migrate existing status signals into the shared lifecycle, or wrap them with compatibility
  functions until a clean baseline reset is acceptable.
- Split `brain.py` carefully without changing behavior.
- Update maintenance and tests to use the new lifecycle.

That cost is probably worth paying if openbrain continues to add derivations.

A full LangGraph migration is higher cost and should wait until the project has real autonomous
research workflows that need graph checkpoints, branching, and human-in-the-loop control.

---

## Decision

Do not migrate openbrain core to LangGraph now.

Do add a first-class, DB-backed derivation/job structure if more workflow work is planned.

When agentic research workflows arrive, build them as an external driver over openbrain's MCP
tools. At that point LangGraph can be evaluated for the driver without making it part of
openbrain's storage and domain core.

# Plan B: OB1 Adoption

## Goal

Use [OB1](../../../OB1/) as a source of proven schema patterns, workflows, and
integrations while preserving openbrain's self-hosted Python architecture.

openbrain has not been deployed or used yet. This is the right time to replace
the historical migration sequence with a clean baseline schema, reconcile the
documentation with the implementation, and make deliberate choices about which
OB1 capabilities belong in the core.

---

## Positioning

openbrain and OB1 share the same central idea: `thoughts` are the universal
memory record, and AI clients read and write them through MCP tools.

They differ in implementation strategy:

| Area | openbrain | OB1 |
|------|------------|-----|
| Database | Self-hosted Postgres + pgvector | Supabase Postgres + pgvector |
| Server | Python + FastMCP | Deno Edge Function + MCP SDK |
| Embeddings | Configurable provider | OpenRouter by default |
| Deployment | Docker, systemd, Caddy, private network | Supabase-hosted Edge Functions |
| Core data model | Thoughts, document library, chunks, people, projects, links | Minimal thoughts table, extended through optional contributions |
| Primary interface | MCP-first, AI-mediated | MCP core with optional REST gateways and dashboards |

The target is not an OB1 fork. openbrain remains its own runtime and adopts OB1
features selectively. This avoids coupling the system to Supabase while still
benefiting from OB1's larger library of schemas, recipes, skills, and
integrations.

---

## Design Principles

- **Preserve the self-hosted runtime.** Postgres, Python, FastMCP, and private
  network deployment remain the default architecture.
- **Keep MCP canonical, not exclusive.** Agents should use MCP. Future REST APIs
  or dashboards may support concrete workflows if they reuse the same domain
  logic and database model.
- **Redesign freely before first use.** The former `migrations/` directory was
  development history, not a compatibility contract. Use a clean install-time
  schema.
- **Keep the core coherent.** Promote broadly useful foundations into the base
  schema. Keep workflow-specific tables and functions optional.
- **Use one canonical write path.** Every thought capture should flow through a
  deduplicating upsert operation.
- **Keep optional OB1 contributions recognizable.** When adapting an OB1
  capability, preserve its conceptual data shape where practical so future
  upstream improvements remain easy to evaluate.
- **Avoid parallel models for the same concept.** openbrain already has
  first-class `people`, `projects`, `links`, `library`, and `chunks` tables.
  OB1 contributions that introduce overlapping models must be reconciled rather
  than copied verbatim.
- **Make provider assumptions explicit.** Embedding model, vector dimension,
  and provider routing must agree across schema, config, docs, and runtime code.

---

## Scope

### In Scope for the Core Redesign

- Reconcile README, architecture, and model configuration docs with the current
  implementation.
- Replace `migrations/` with a fresh `schemas/core/` baseline.
- Fold the final state of the existing openbrain schema into that baseline.
- Review OB1's foundational thoughts schema for improvements.
- Add content fingerprints and a canonical `upsert_thought` function.
- Update Python write paths and setup scripts to use the new baseline.
- Add a fresh-database trial run.

### Out of Scope for the Core Redesign

The following OB1 schemas are useful candidates for later adoption, but they
should not shape the first baseline unless a core dependency requires a small
supporting field:

- Agent memory
- Entity extraction queues and workers
- Provenance chains
- Readwise books
- Recency-boosted matching
- Trigram search
- Thought audit
- Typed reasoning edges
- Workflow status

OB1 skills, recipes, dashboards, and integrations are also deferred until the
new core is stable.

---

## Target Schema Layout

Replace the historical migrations with a baseline that represents the intended
database from a blank install:

```text
schemas/
  core/
    001_extensions.sql
    002_thoughts.sql
    003_library.sql
    004_entities.sql
    005_search.sql
  optional/
    README.md
```

The initial files are install-time schema modules, not immutable production
migrations. Once openbrain is deployed and contains real data, freeze this
baseline and add a separate migration mechanism for future changes.

### `001_extensions.sql`

- Enable `vector`.
- Enable the extension used for UUID generation.
- Enable the extension required for SHA-256 fingerprint generation if needed.

### `002_thoughts.sql`

Create the universal memory log and its canonical write path.

Retain or add:

- `id`
- `content`
- `embedding`
- `metadata`
- `type`
- `source_type`
- `importance`
- `quality_score`
- `sensitivity_tier`
- `enriched`
- `content_fingerprint`
- `created_at`
- `updated_at`
- generated full-text search column
- updated-at trigger
- fingerprint unique index
- vector, metadata, type, source, timestamp, and FTS indexes
- `upsert_thought`

The following OB1 enhanced-thought fields are promoted into core:

| Field | Decision | Reason |
|-------|----------|--------|
| `importance` | Adopted | Ordinal `0-6` signal for briefing, maintenance, and later recipes |
| `quality_score` | Adopted | `0-100` capture or enrichment quality signal |
| `sensitivity_tier` | Adopted | Constrained safety foundation for future sharing and filtering |
| `enriched` | Adopted | Sticky lifecycle marker for future enrichment workers |

`upsert_thought` should:

1. Normalize content by trimming, lowercasing, and collapsing whitespace.
2. Compute a SHA-256 content fingerprint.
3. Insert new content with its embedding and metadata.
4. Merge metadata when the same normalized content is captured again.
5. Keep structured columns synchronized with their metadata equivalents.
6. Return enough information for callers to distinguish an insert from a
   duplicate merge.

The final function signature should support openbrain's embedding write path.
OB1's SQL function is a starting point, not a drop-in implementation: Open
Brain computes embeddings in Python, while OB1's RPC examples often insert
without embeddings and let surrounding services manage enrichment.

### `003_library.sql`

Preserve openbrain's document ingestion model:

- `library`
- `chunks`
- file-hash deduplication
- chunk embeddings
- chunk FTS
- library status constraint
- DOI and BibTeX fields
- foreign key cascade from `chunks` to `library`

This is an openbrain feature, not part of the minimal OB1 baseline.

### `004_entities.sql`

Preserve openbrain's explicit entities:

- `people`
- `projects`
- `links`
- enriched entity embeddings
- follow-up dates
- relationship indexes
- updated-at triggers

Do not add OB1's optional `entities`, `edges`, or `thought_entities` tables yet.
When evaluating automatic entity extraction later, decide whether the worker
should populate the existing tables, replace them with a generalized graph, or
introduce sidecar tables for extracted candidates.

### `005_search.sql`

Consolidate search functions against the final schema:

- `match_thoughts`
- `match_chunks`
- `match_people`
- `match_projects`

Review OB1's enhanced text-search function as an input to the design. openbrain
already performs hybrid vector and full-text search in Python, so avoid adding
an overlapping RPC unless it improves a specific retrieval path.

---

## Canonical Write Path

All thought creation must route through one application-level function backed
by the database upsert:

```text
MCP capture
Document ingest event
Nightly maintenance synthesis
Future imports and integrations
        |
        v
brain.store_thought(...)
        |
        v
upsert_thought(...)
```

`brain.store_thought()` remains the Python entry point because it owns embedding
generation and background entity refresh. It should call the SQL upsert rather
than issuing a raw `INSERT`.

Metadata merge behavior must be documented. A shallow JSONB merge is a sensible
initial default, but arrays such as `people`, `projects`, and `tags` need an
explicit decision: replacement is simple and predictable, while union merging
preserves information across capture sources.

---

## Adoption Matrix

### Core Baseline Review

These OB1 concepts should be evaluated while creating `schemas/core/`:

| OB1 Concept | Decision | Notes |
|-------------|----------|-------|
| Universal `thoughts` table | Adopt | Already central to openbrain |
| Normalized content fingerprints | Adopt | Prevent duplicate capture and make imports idempotent |
| `upsert_thought` RPC | Adapt | Include Python-generated embeddings and structured-column synchronization |
| Top-level `type` | Adopt | Already present in openbrain |
| Top-level `source_type` | Adopt | Better filtering than parsing JSONB |
| `importance` | Adopted | Ordinal `0-6` core ranking signal |
| `quality_score` | Adopted | `0-100` cleanup and enrichment signal |
| `sensitivity_tier` | Adopted | Constrained safety foundation |
| `enriched` | Adopted | Sticky lifecycle marker for future enrichment |
| FTS index | Adopt | Already present in openbrain |
| Dashboard-specific utility RPCs | Defer | Add only with a REST or dashboard use case |
| Supabase RLS policies | Skip for now | Single-user self-hosted deployment does not need Supabase roles |

### Later Optional Evaluation

Review these only after the core baseline and trial runs are complete:

| OB1 Area | Initial Classification | openbrain Consideration |
|----------|------------------------|--------------------------|
| Agent memory | Adapt | Strong candidate for governed agent write-back and recall traces |
| Provenance chains | Adapt | Useful for nightly synthesis and derived artifacts |
| Entity extraction | Adapt carefully | Must reconcile with existing people, projects, and links |
| Thought audit | Evaluate | Useful if multiple agents or imports write concurrently |
| Workflow status | Evaluate | Useful only if task/kanban workflows are desired |
| Trigram search | Evaluate | Add if FTS and vector search miss fuzzy-name use cases |
| Recency-boosted search | Evaluate | Consider implementing in Python search ranking first |
| Readwise schema and capture | Defer | Adopt when Readwise import becomes a real workflow |
| Auto-capture skill | Adopt later | Align with the existing auto-capture plan and MCP tool names |
| Live retrieval recipe | Adopt later | Good fit after search behavior is stable |
| Daily digest | Adapt later | May fit `get_briefing` and systemd better than Supabase scheduling |
| REST gateway and dashboards | Defer | Add when a concrete browser or integration workflow justifies the surface |
| Supabase Edge Functions | Skip by default | Translate useful logic into Python or local jobs |

---

## Execution Plan

### Phase 1: Architecture and Documentation

- [x] Confirm the supported embedding providers: Voyage AI, OpenRouter, and
      LiteLLM.
- [x] Confirm whether local embeddings are a real requirement or remove them
      from the docs until implemented.
- [x] Choose the baseline embedding model and vector dimension:
      `voyage-large-2` and `VECTOR(1536)`.
- [x] Decide the interface boundary: MCP is canonical for agents but not the
      only permitted interface.
- [x] Reconcile `README.md`, `ARCHITECTURE.md`, and
      `docs/design/model-config.md`.
- [x] Move the existing model-routing plan later in the sequence or rename it
      after this plan lands.

### Phase 2: Core Schema Redesign

- [x] Create `schemas/core/`.
- [x] Fold the final state of the current `migrations/` into clean schema files.
- [x] Review OB1's base thoughts schema field by field.
- [x] Review OB1's optional enhanced thoughts schema field by field.
- [x] Add fingerprint deduplication.
- [x] Add embedding-aware `upsert_thought`.
- [x] Add `schemas/optional/README.md` describing how optional capabilities will
      be introduced later.
- [x] Remove the historical migration files once setup scripts no longer depend
      on them.

### Phase 3: Runtime Integration

- [x] Update `scripts/setup_db.sh` to install `schemas/core/*.sql` in order.
- [x] Update `brain.store_thought()` to use the canonical SQL upsert.
- [x] Route ingest-event writes through the same path.
- [x] Verify entity refresh behavior on both inserts and duplicate merges.
- [x] Document metadata merge semantics.

### Phase 4: Verification

- [x] Add a blank-database setup trial-run script using an ephemeral
      PostgreSQL + pgvector Docker container.
- [x] Add a single test runner for Python-level checks and the optional Docker
      trial run.
- [x] Add Python-level tests for the upsert call contract, duplicate refresh
      behavior, and ingest-event routing.
- [x] Verify two normalized-equivalent captures return one thought.
- [x] Verify metadata is merged according to the documented rules.
- [x] Verify thoughts remain searchable through vector and FTS paths.
- [x] Verify library ingest deduplicates files and searches chunks.
- [x] Verify people, projects, links, follow-ups, and briefings still work.

The Python-level tests can run without a local database. The blank-database
trial run still requires PostgreSQL with `pgvector`: SQLite and DuckDB cannot
validate the baseline because it uses Postgres extensions, `JSONB`, PL/pgSQL,
generated `TSVECTOR` columns, and Postgres-specific indexing.

### Phase 5: Enhanced Thought Queries

Adapt OB1's enhanced-thought RPCs behind openbrain's Python domain layer. MCP
tools and future REST handlers should call `brain.py`; they should not treat SQL
RPCs as the application interface.

Implement in this order:

1. [x] **Thought connections**
   - Add a SQL function that finds related thoughts by shared `topics`, `tags`,
     `people`, and `projects`.
   - Add `brain.get_thought_connections(thought_id, limit)`.
   - Expose an MCP tool for retrieving related memories from a specific
     thought.
   - Keep overlap ranking in SQL and response formatting in Python.

2. [x] **Richer stats**
   - Add aggregate SQL queries or a small stats RPC only where they reduce
     repeated round trips.
   - Expand `brain.table_stats()` and the existing `get_stats` MCP tool rather
     than creating a parallel tool.
   - Include counts by type, top tags or topics, source counts, and sensitivity
     tier counts.

3. [ ] **Standalone text search**
   - Defer until a dashboard, REST API, or exact-search workflow needs Boolean
     syntax, pagination, and total counts.
   - When needed, adapt OB1's `search_thoughts_text` SQL function behind
     `brain.search_thoughts_text()`.
   - Keep `search_brain` as the primary MCP retrieval path because it already
     performs hybrid vector and full-text search across thoughts and library
     chunks.

### Phase 6: Optional OB1 Adoption

- [ ] Review optional schemas one by one.
- [ ] Port low-coupling skills and behavioral recipes.
- [ ] Adapt useful imports and background workflows to the Python/self-hosted
      runtime.
- [ ] Revisit REST and dashboard support only if a concrete browser workflow
      justifies expanding the product surface.

---

## Resolved Decisions

| Question | Decision |
|----------|----------|
| What embedding model and dimension define the initial database? | `voyage-large-2` with `VECTOR(1536)` |
| Should duplicate captures replace or union array metadata? | Union `tags`, `topics`, `people`, `projects`, and `action_items`; scalar metadata uses last-write-wins |
| Should `importance`, `quality_score`, and `sensitivity_tier` be accepted from callers, derived later, or both? | Accept caller values now, store neutral defaults, and allow future enrichment workers to derive richer values |
| Should `enriched` exist before an enrichment pipeline exists? | Yes. It is a sticky lifecycle marker with default `false` |

---

## Decisions to Make During Implementation

| Question | Why It Matters |
|----------|----------------|
| Does an ingest event need a new row every time, or should identical events deduplicate? | The universal-log model and fingerprint model can conflict for repeatable events |
| Should optional schemas use a lightweight applied-schema ledger? | Once optional modules are installed, their versions need to be inspectable |

---

## Licensing Note

OB1 is distributed under FSL-1.1-MIT. Internal use and modification are
permitted, but copied or adapted code must retain the required license notices.
When porting SQL or implementation code, record the OB1 source path in the
commit or file header where appropriate. Skills and recipes should also retain
their existing attribution.

---

## Completion Criteria

This plan's core redesign is complete when:

- A blank Postgres database can be initialized from `schemas/core/`.
- Docs, config, schema vector dimensions, and runtime providers agree.
- Every thought creation path uses the canonical deduplicating upsert.
- Duplicate capture behavior is tested and documented.
- Existing openbrain search, library, entity, and briefing features pass trial
  runs.
- Optional OB1 capabilities are listed with explicit adoption decisions rather
  than copied into the baseline implicitly.

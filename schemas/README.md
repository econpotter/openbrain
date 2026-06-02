# Database Schemas

`schemas/core/` contains the clean install-time database baseline for a new Open
Brain instance. The files are applied in numeric order.

`schemas/optional/` is reserved for capabilities that are not required by the
core runtime.

`scripts/setup_db.sh` installs this baseline directly. Once Open Brain contains
real data, later schema changes should use a separate migration mechanism.

## Canonical Thought Writes

`schemas/core/002_thoughts.sql` defines `upsert_thought(...)` as the canonical
database write path for thoughts. The Python service generates the embedding,
then passes content, embedding, metadata, and type into the SQL function.

Thoughts are deduplicated by a SHA-256 fingerprint of normalized content.
Repeated captures preserve the stored embedding and merge metadata:

- scalar metadata keys use last-write-wins semantics
- `tags`, `topics`, `people`, `projects`, and `action_items` are deduplicated
  unions
- structured top-level fields stay synchronized with their metadata equivalents
- omitted enhanced fields do not reset richer values from previous captures
- sensitivity can only escalate on duplicate capture
- `enriched` remains true once enrichment has completed

The SQL upsert reports whether metadata changed. The Python service uses that
result to refresh mentioned entity embeddings after a new insert or a meaningful
duplicate merge, but not after an identical repeated capture.

## Enhanced Thought Queries

`schemas/core/005_search.sql` adapts two OB1 enhanced-thought query patterns
behind the Python domain layer:

- `get_thought_connections(...)` ranks related thoughts by shared `topics`,
  `tags`, `people`, and `projects`. Restricted thoughts are excluded by default.
- `brain_stats_aggregate()` returns thought and library totals plus grouped
  counts for types, tags, topics, sources, and sensitivity tiers in one query.

MCP clients use `get_thought_connections` and the expanded `get_stats` tool.
They do not call these SQL functions directly.

## Structured Thought Fields

Frequently filtered metadata is promoted into first-class columns:

| Column | Default | Semantics |
|--------|---------|-----------|
| `type` | `thought` | Thought classification |
| `source_type` | `unknown` | Originating capture path or importer |
| `importance` | `3` | Ordinal importance from `0` (noise) to `6` (user-flagged critical) |
| `quality_score` | `50` | Capture or enrichment quality score from `0` to `100` |
| `sensitivity_tier` | `standard` | `standard`, `personal`, or `restricted` |
| `enriched` | `false` | Whether classification or enrichment has completed |

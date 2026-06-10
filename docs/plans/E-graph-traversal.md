# Multi-hop graph traversal over `links`

## Context

`schemas/core/004_entities.sql` already defines a generic `links` table
(source_table/source_id/target_table/target_id/relationship/metadata) — the
adjacency-list edge table that an external proposal (CHANGES2.md) suggested
building from scratch. It's currently dead: zero INSERTs or SELECTs anywhere
in the codebase.

Today, "related entity" discovery (`_extract_cooccurrences` in brain.py) is
single-hop and in-memory: it scans the `people`/`projects` string arrays in
`thoughts.metadata` for thoughts that mention a given entity, and counts
co-occurring names in those same thoughts. It can't answer "what's two hops
away" (e.g. person → project → other person who also worked on that project,
without a thought directly mentioning both).

This plan wires up `links` as real edges (thought ↔ person, thought ↔
project) and adds a recursive-CTE traversal so the agent can ask "what's
connected to X within N hops" — turning the existing co-occurrence heuristic
into actual graph traversal, without inventing new entity/relation types.

## Scope

- Edges: `thoughts ↔ people` and `thoughts ↔ projects`, relationship =
  `'mentions'`. (Library/document edges are a natural follow-up but out of
  scope — metadata doesn't currently track library mentions.)
- Depth-limited (default 2, capped) bidirectional traversal via recursive CTE
  on `links`, cycle-safe via a visited-id array.
- One new MCP tool to surface results in plain language.
- Backfill for existing thoughts (links table starts empty).

## Implementation

### 1. `schemas/core/006_graph_traversal.sql` (new file)

Idempotent (`CREATE OR REPLACE FUNCTION`), safe to apply to the live DB
directly with `psql $DATABASE_URL -f schemas/core/006_graph_traversal.sql`
(existing `links_source_idx`/`links_target_idx` already cover lookups, no new
indexes needed).

```sql
CREATE OR REPLACE FUNCTION traverse_links(
    p_table     TEXT,
    p_id        UUID,
    p_max_depth INT DEFAULT 2,
    p_limit     INT DEFAULT 50
)
RETURNS TABLE (
    node_table TEXT,
    node_id    UUID,
    relationship TEXT,
    depth      INT
)
LANGUAGE sql STABLE AS $$
    WITH RECURSIVE walk(node_table, node_id, relationship, depth, visited) AS (
        SELECT p_table, p_id, NULL::TEXT, 0, ARRAY[p_id]
        UNION ALL
        SELECT
            CASE WHEN l.source_table = walk.node_table AND l.source_id = walk.node_id
                 THEN l.target_table ELSE l.source_table END,
            CASE WHEN l.source_table = walk.node_table AND l.source_id = walk.node_id
                 THEN l.target_id ELSE l.source_id END,
            l.relationship,
            walk.depth + 1,
            walk.visited || (CASE WHEN l.source_table = walk.node_table AND l.source_id = walk.node_id
                 THEN l.target_id ELSE l.source_id END)
        FROM links l
        JOIN walk ON
            (l.source_table = walk.node_table AND l.source_id = walk.node_id)
            OR (l.target_table = walk.node_table AND l.target_id = walk.node_id)
        WHERE walk.depth < p_max_depth
          AND NOT (CASE WHEN l.source_table = walk.node_table AND l.source_id = walk.node_id
                   THEN l.target_id ELSE l.source_id END = ANY(walk.visited))
    )
    SELECT node_table, node_id, relationship, depth
    FROM walk
    WHERE depth > 0
    ORDER BY depth, node_table
    LIMIT p_limit;
$$;
```

### 2. `brain.py`

- `async def add_link(source_table, source_id, target_table, target_id, relationship, metadata=None)`
  — `INSERT ... ON CONFLICT (source_table, source_id, target_table, target_id, relationship) DO NOTHING`
  (the UNIQUE constraint already exists).

- `async def _link_thought_entities(thought_id, people, projects)`
  — for each name in `people`/`projects`, resolve via `find_person_by_name`
  / `find_project_by_name`; if exactly one match, `add_link("thoughts",
  thought_id, "people"/"projects", entity_id, "mentions")`. Skip on zero or
  ambiguous matches (mirrors existing tool behavior — no new disambiguation
  UI).

- Call `_link_thought_entities` from `store_thought`, alongside the existing
  `_refresh_mentioned_entities` call (same `result["metadata_changed"]` gate,
  fire-and-forget via `asyncio.create_task` like the embedding refresh).

- `async def backfill_links(limit=1000) -> dict`
  — iterate thoughts with non-empty `metadata.people`/`metadata.projects`
  that have no existing rows in `links` with `source_table='thoughts'`
  (`NOT EXISTS` subquery), call `_link_thought_entities`, return counts
  processed/linked. Same shape as `backfill_chunk_embeddings`.

- `async def traverse_graph(table, entity_id, depth=2, limit=50) -> list[dict]`
  — calls `traverse_links`, then hydrates each `(node_table, node_id)` with a
  label: `content[:200]` for thoughts, `name` for people/projects. Returns
  list of `{table, id, label, relationship, depth}`.

- `async def resolve_entity(name) -> dict | None`
  — checks `find_person_by_name` then `find_project_by_name`; returns
  `{"table": "people"|"projects", **row}`, or `{"multiple": [...]}` on
  ambiguity across both, mirroring `get_project_profile`/`get_person_profile`.

### 3. New MCP tool — `openbrain/tools/search.py`

`get_related_entities(name: str, depth: int = 2, limit: int = 20) -> str`

- `resolve_entity(name)` → error message on no/multiple matches (same
  wording pattern as `get_person`/`get_project`).
- `traverse_graph(table, id, depth, limit)`.
- Format grouped by depth, e.g.:
  ```
  ## Related to Jane Doe (depth ≤ 2)

  Depth 1:
    [project] ai-outliers — mentions
    [thought] 2026-03-01: "Met with Jane to discuss..." — mentions

  Depth 2:
    [people] John Smith — mentions
  ```

### 4. `openbrain/tools/maintenance.py`

Add `backfill_links(limit: int = 1000) -> str` tool, same pattern as
`backfill_embeddings`, calling `brain.backfill_links`.

### 5. Docs

- `docs/design/graph-traversal.md` — short design doc: edge population
  (capture-time + backfill), traversal function, scope/non-goals (no new
  entity types, no library edges yet).
- Mention `links` traversal in `docs/design/search.md` as a complementary
  path to vector/FTS search.

## Verification

1. Apply schema: `psql $DATABASE_URL -f schemas/core/006_graph_traversal.sql`.
2. Run `backfill_links` via MCP (or a small script calling
   `brain.backfill_links()`) — confirm `links` row count > 0 and matches
   expectations for a sample person/project.
3. Capture a new thought mentioning an existing person and project; confirm
   new `links` rows appear automatically.
4. Call `get_related_entities("<known person>")`, confirm depth-1 results
   match `get_person`'s co-occurring projects, and depth-2 surfaces
   transitively-related people/projects not directly co-mentioned.
5. `just test` / `just lint`.

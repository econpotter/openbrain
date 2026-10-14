# Design: Graph Traversal

## Overview

openbrain maintains a `links` table (source_table/source_id/target_table/target_id/relationship) as a generic adjacency list for multi-hop entity connections. Currently it stores edges between thoughts and people/projects via the relationship type `'mentions'`, enabling depth-limited graph traversal to answer "what's connected to X within N hops" — complementary to vector and full-text search.

---

## Edge creation

Edges are created at two points:

**Capture time** — When a thought is stored with `metadata.people` or `metadata.projects`, an async fire-and-forget task (`_link_thought_entities`) attempts to resolve each name to a unique person or project. If exactly one match is found, a `thought <-> entity` edge is inserted with relationship `'mentions'`. Names with zero or ambiguous matches are skipped (mirrors existing tool behavior).

**Enrichment** — Jev picks each thought's projects from the active list (`judge_thought`) and the same linking runs on its picks. `backfill_thought_judgments` does this for thoughts captured before Jev, so older thoughts need no separate link backfill.

---

## Traversal

`traverse_links(table, id, max_depth=2, limit=50)` is a SQL recursive CTE that walks the graph bidirectionally from a starting node. It maintains a visited-id array to prevent cycles and stops at `max_depth`. Results are returned as `(node_table, node_id, relationship, depth)` in depth order, capped at `limit`.

The Python `traverse_graph` function calls this and hydrates each result with a label (first 200 chars for thoughts, name for people/projects).

---

## The `get_related_entities` tool

`get_related_entities(name, depth=2, limit=20)` is the user-facing interface. It resolves a name to a unique entity via `resolve_entity` (single or multiple match), then traverses the graph and formats results grouped by depth:

```
## Related to Jane Doe (depth ≤ 2)

Depth 1:
  [project] ai-outliers — mentions
  [thought] 2026-03-01: "Met with Jane..." — mentions

Depth 2:
  [people] John Smith — mentions
```

---

## Non-goals

- **No new entity or relationship types** beyond `'mentions'`. Thoughts, people, and projects are current scope.
- **No library/document edges yet.** Metadata does not currently track library mentions, so links from thoughts to library records require additional work.

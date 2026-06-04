---
name: search
description: >
  Search openbrain — your memory (thoughts/decisions/notes) and document
  library. Triggers: /ob:search, "what do I know about", "search openbrain",
  "search the brain", "find in my notes", "did I capture anything about".
---

# /ob:search — query openbrain

Answer "what do I know about `$ARGUMENTS`?" from openbrain.

## Mechanism

Call `mcp__openbrain__search_brain(query=$ARGUMENTS)`. If the query is clearly
about a stored document/paper rather than a memory, also call
`mcp__openbrain__search_library(query=$ARGUMENTS)`. Synthesize both into one
answer — do not dump raw rows.

## Routing extras

- Query names a person or project → prefer `mcp__openbrain__search_by_entity`
  or `get_project(name)` / `get_person(name)` for focused context.
- "Where is document X stored?" → `mcp__openbrain__get_library_record(query=X)`.

## Output

Lead with the synthesized answer. Cite source ids/titles inline so the user can
follow up. If nothing matches, say so plainly — do not pad.

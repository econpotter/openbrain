---
name: openbrain-search
description: >
  Search openbrain memory and document library. Use when the user asks "what do
  I know about", "search openbrain", "search the brain", "find in my notes", or
  "did I capture anything about".
---

# openbrain search

Answer "what do I know about X?" from openbrain.

## Mechanism

Call openbrain MCP tool `search_brain(query=<query>)`. If the query is clearly
about a stored document/paper rather than a memory, also call
`search_library(query=<query>)`. Synthesize both into one answer; do not dump
raw rows.

## Routing extras

- Query names a person or project: prefer `search_by_entity`, `get_project`, or
  `get_person` for focused context.
- "Where is document X stored?": call `get_papers(title=<title>)`.

## Output

Lead with the synthesized answer. Cite source ids/titles inline so the user can
follow up. If nothing matches, say so plainly.

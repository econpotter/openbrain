---
name: capture
description: >
  Capture a thought, decision, preference, or project update into openbrain
  (personal memory). Triggers: /ob:capture, "capture this", "remember that",
  "save to openbrain", "log this decision", "note this preference",
  "record a project update". Routes to the most specific openbrain capture tool.
---

# /ob:capture — write to openbrain memory

Persist `$ARGUMENTS` (or, if empty, a summary of the current discussion) into
openbrain by calling the **most specific** capture tool. Do NOT default to
`capture_thought` for everything — route by what the content is.

## Mechanism

Call one openbrain MCP tool. If the server is not connected, its tool name will
not be available — stop and tell the user to connect the openbrain MCP server.

## Routing

| Content is… | Call | Key args |
|---|---|---|
| A stable preference / convention ("I always…", "from now on…") | `mcp__openbrain__remember_preference` | `content`, `scope?`, `projects?` |
| A choice + reason ("decided to…", "going with X because…") | `mcp__openbrain__capture_decision` | `decision`, `rationale?`, `projects?`, `people?` |
| A milestone/blocker/next-step about ONE named project | `mcp__openbrain__capture_project_update` | `project`, `update`, `status?` (active\|paused\|completed\|archived) |
| Anything else durable (fact, observation, session summary) | `mcp__openbrain__capture_thought` | `content`, `type` (thought\|note), `tags?`, `people?`, `projects?` |

Fill optional metadata the conversation makes obvious: `tags`, `people`,
`projects`, `action_items`, `importance` (0–6). Use `type: note` for
long-form/structured notes; `type: thought` for quick observations.

Before capturing a thought that mentions a project/person not yet in the DB,
add it first (`mcp__openbrain__add_project` / `add_person`), then capture.

## After

Report the returned id (e.g. `Stored thought <uuid>`). One sentence. Do not
re-capture to "verify" — the returned id IS the confirmation.

## Gotchas

- The capture tools all route through `store_thought` server-side; a server
  `TypeError` on `result["id"]` means the openbrain image predates the JSONB
  decode fix (commit e248b05) — rebuild it, don't retry.
- Duplicate captures (same content) merge metadata server-side and return the
  existing id; that's expected, not an error.

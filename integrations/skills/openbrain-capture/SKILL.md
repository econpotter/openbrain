---
name: openbrain-capture
description: >
  Capture a thought, decision, preference, or project update into openbrain
  personal memory. Use when the user says "capture this", "remember that",
  "save to openbrain", "log this decision", "note this preference", or
  "record a project update".
---

# openbrain capture

Persist the user-provided content, or if empty a summary of the current
discussion, by calling the most specific openbrain MCP capture tool. Do not
default to `capture_thought` for everything.

## Mechanism

Call one openbrain MCP tool. If the openbrain MCP server is not connected, stop
and tell the user to connect it.

## Routing

| Content is... | Call | Key args |
|---|---|---|
| Stable preference / convention ("I always...", "from now on...") | `remember_preference` | `content`, `scope?`, `projects?` |
| Choice + reason ("decided to...", "going with X because...") | `capture_decision` | `decision`, `rationale?`, `projects?`, `people?` |
| Milestone/blocker/next-step about one named project | `capture_project_update` | `project`, `update`, `status?` (`active`\|`paused`\|`completed`\|`archived`) |
| Other durable fact, observation, or session summary | `capture_thought` | `content`, `type` (`thought`\|`note`), `tags?`, `people?`, `projects?` |

Fill optional metadata the conversation makes obvious: `tags`, `people`,
`projects`, `action_items`, `importance` (0-6). Use `type: note` for
long-form/structured notes; `type: thought` for quick observations.

Before capturing a thought that mentions a project/person, check whether it
already exists (`get_project` / `get_person` or search). If absent, add it
first (`add_project` / `add_person`), then capture.

## After

Report the returned id, e.g. `Stored thought <uuid>`. Do not re-capture to
verify; returned id is confirmation.

## Gotchas

- A server `TypeError` on `result["id"]` means the openbrain image predates the
  JSONB decode fix (commit e248b05). Rebuild it; do not retry.
- Duplicate captures merge metadata server-side and return the existing id.

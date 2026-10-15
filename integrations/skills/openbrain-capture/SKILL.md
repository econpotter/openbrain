---
name: openbrain-capture
description: >
  Capture what a session settled into openbrain memory. Use when the user says
  "capture this", "remember that", "save to openbrain", or "log this".
---

# openbrain capture

Write what a colleague would need to pick this work up months from now without
the session. One capture per topic the user means; a session that changed
topics gives several.

Capture where the discussion ended, not how it got there: the idea and why it
matters, the design or argument as settled with its reasons and the
alternatives rejected, durable domain facts, preferences that hold beyond this
task in the user's words, and what was explicitly deferred. Mark each research
or empirical claim as derived, measured (name the points) or inferred. State
only what the session stated.

Leave out what is cheap to find again or will change: costs, paths, commands,
commits, status, task steps, unresolved technicalities, and rules already
written in an AGENTS.md or the coding standards.

Write plain prose in the third person ("the user"), present tense for
decisions, as long as the content needs.

## Mechanism

Call `capture_thought` once per capture with `content`, `projects` and one
`tags` entry for the kind: idea, design, preference or domain-fact. `projects`
holds every project the thought applies to, named as `list_projects` shows
them. A thought can apply to several projects or to none; leave `projects`
empty when none applies, rather than picking the nearest one. Leave `importance` unset; Jev judges it. If the
openbrain MCP server is not connected, say so and stop. Report the returned id.

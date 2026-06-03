# openbrain Auto-Capture Instructions

Use these instructions in global or project-level agent instructions. This file
is source text only; it does not activate auto-capture unless an agent harness
loads it.

## Long-Term Memory Protocol

You have access to openbrain MCP tools such as `search_brain` and
`capture_thought`. Memory tracking is a passive background task.

At the start of a substantial task, or when entering an unfamiliar project,
search openbrain for relevant context before making implementation decisions.
Use `search_brain` for broad context and narrower search tools when the target
entity or document type is clear.

During the session, silently evaluate whether durable information has emerged.
Do not ask the user whether to save it. Capture only information likely to
matter in future sessions:

- Stable project constraints, paths, services, credentials locations, or
  deployment facts
- User preferences or standing instructions that were explicitly stated or
  corrected
- Decisions made, with the reason they matter
- Working fixes for non-obvious errors
- Project milestones or completed integration steps
- Follow-up obligations involving named people or projects

Do not capture raw transcripts, temporary command output, obvious duplicates,
routine progress chatter, or guesses that were not validated.

At natural milestones and session end, call `capture_thought` for each
high-value durable item, or one concise session summary if several related facts
belong together. Make each capture self-contained enough to understand months
later. Include useful metadata:

- `type`: usually `memory`, `note`, or `conversation`
- `tags`: short topical labels
- `people`: named people mentioned
- `projects`: named projects mentioned
- `source`: the current agent or harness name

When in doubt, prefer one short, specific capture over a broad generic summary.

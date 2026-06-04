---
name: openbrain-maintenance
description: >
  Consolidate recent openbrain memory records. Use when the user asks to run
  memory maintenance, clean up openbrain memory, or consolidate recent thoughts.
  Always starts with a dry run.
---

# openbrain maintenance

Run memory maintenance only through openbrain MCP tool `run_memory_maintenance`.

## Mechanism

1. First call `run_memory_maintenance(dry_run=True)`.
2. Show the proposed synthesis and source thought IDs.
3. Apply only after the user approves that exact synthesis and exact source IDs:
   `run_memory_maintenance(dry_run=False, approved_synthesis=..., thought_ids=...)`.

## Output

For the dry run, report the synthesis and source IDs. For the apply step,
report processed count and synthesis count. Do not archive or rewrite thoughts
without explicit approval.

---
name: openbrain-extraction
description: >
  Extract recent openbrain memory records into a durable synthesis. Use when the
  user asks to run memory extraction, clean up openbrain memory, or consolidate
  recent thoughts. Always starts with a dry run.
---

# openbrain extraction

Run memory extraction only through openbrain MCP tool `run_memory_extraction`.

## Mechanism

1. First call `run_memory_extraction(dry_run=True)`.
2. Show the proposed synthesis and source thought IDs.
3. Apply only after the user approves that exact synthesis and exact source IDs:
   `run_memory_extraction(dry_run=False, approved_synthesis=..., thought_ids=...)`.

## Output

For the dry run, report the synthesis and source IDs. For the apply step,
report processed count and synthesis count. Do not archive or rewrite thoughts
without explicit approval.

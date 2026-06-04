---
name: ingest
description: >
  Ingest a document or directory into the openbrain library. Triggers:
  /ob:ingest, "ingest this paper", "add this PDF to openbrain", "ingest this
  directory", "add to my library". Handles both same-host and remote-client paths.
---

# /ob:ingest — add documents to the openbrain library

`$ARGUMENTS` = a file or directory path (PDF/text).

## Pick the path by where the file lives

**File is on the openbrain server's own filesystem** (agent runs there):
- single file → `mcp__openbrain__ingest_document(filepath=$ARGUMENTS)`
- directory → `mcp__openbrain__ingest_directory(dirpath=$ARGUMENTS)`

**File is on a remote client machine** (the openbrain server can't see it):
1. `mcp__openbrain__prepare_document_ingest(filename=<basename>)` → returns an
   SCP destination.
2. Tell the user to copy the file there (the `scp …` line). Wait for confirmation.
3. `mcp__openbrain__ingest_staged_document(filepath=<returned staged path>)`.

The pipeline copies the file to `LIBRARY_PATH` with a standardized name and
cleans up the staged copy automatically.

## Missing-metadata fix — do NOT re-ingest

If ingest reports missing metadata, the record exists but is incomplete.
Re-ingesting will not help. Instead ask the user for a BibTeX entry or title,
then patch in place:
`mcp__openbrain__update_library(query=<title>, bibtex=…)`
or `mcp__openbrain__update_library(query=<title>, title=…)`.

## Output

Report each file's status (ok / skipped-duplicate / failed) and the resulting
library title. For a directory, summarize counts.

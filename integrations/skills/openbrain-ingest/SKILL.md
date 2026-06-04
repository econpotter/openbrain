---
name: openbrain-ingest
description: >
  Ingest a document or directory into the openbrain library. Use when the user
  asks to ingest a paper, add a PDF/text file to openbrain, ingest a directory,
  or add something to the library.
---

# openbrain ingest

Use the user-provided file or directory path. Supported inputs are PDF and text
files.

## Pick the path by where the file lives

File is on the openbrain server's filesystem:

- Single file: call openbrain MCP tool `ingest_document(filepath=<path>)`.
- Directory: call openbrain MCP tool `ingest_directory(dirpath=<path>)`.

File is on a remote client machine:

1. Call `prepare_document_ingest(filename=<basename>)`; it returns an SCP
   destination.
2. Tell the user to copy the file there. Wait for confirmation.
3. Call `ingest_staged_document(filepath=<returned staged path>)`.

The pipeline copies the file to `LIBRARY_PATH` with a standardized name and
cleans up the staged copy automatically.

## Missing-metadata fix

If ingest reports missing metadata, the record exists but is incomplete.
Re-ingesting will not help. Ask the user for a BibTeX entry or title, then patch
in place with `update_library(query=<title>, bibtex=...)` or
`update_library(query=<title>, title=...)`.

## Output

Report each file's status and resulting library title. For a directory,
summarize counts.

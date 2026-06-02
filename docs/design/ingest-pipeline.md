# Design: Ingest Pipeline

## Overview

Documents enter Open Brain through `ingest_document` (MCP tool) or the
`python -m openbrain.cli` local CLI. Both use the same pipeline in
`brain.ingest_document_pipeline()`.

```
Input file (PDF or text)
  → SHA256 hash → dedup check (reject if exists, unless force=True)
  → extract text (pdfplumber for PDF, plain read otherwise)
  → extract DOI from first page → CrossRef lookup (title, authors, year, journal, BibTeX)
  → fallback: user-supplied BibTeX → user-supplied title → filename stem
  → copy to LIBRARY_PATH with slug filename
  → INSERT INTO library
  → chunk text (400 words, 50-word overlap)
  → embed each chunk → INSERT INTO chunks
  → store_thought("Ingested: ...")
```

---

## Metadata resolution

Metadata is resolved in priority order:

1. **DOI extraction + CrossRef** — a DOI regex scans the first page; if found, CrossRef returns structured metadata and BibTeX automatically.
2. **User-supplied BibTeX** — passed as `bibtex` argument to `ingest_document`.
3. **User-supplied title** — passed as `title` argument.
4. **Filename stem** — last resort; `ingest_document` returns `metadata_missing: true` to signal the caller to follow up with `update_library`.

If metadata is missing after ingest, call `update_library(query=<title>, bibtex=...)` to fix the record in place. Do not re-ingest.

---

## Filename slugging

Ingested files are copied to `LIBRARY_PATH` with a deterministic slug:

```
{AuthorLastYYYY}-{journal-abbrev}-{title-words}.{ext}
```

Example: `ImbensKalyanaraman2012-rems-bandwidth-selection-regression.pdf`

This keeps the library directory human-readable and avoids collisions from different files with the same title.

---

## Deduplication

Documents are deduplicated by SHA256 file hash. Attempting to re-ingest an identical file raises an error. Pass `force=True` to delete the existing record and re-ingest (e.g. after a metadata fix).

---

## Remote ingest pattern

The MCP server runs on the host machine. Files from remote clients (laptops, other desktops) must be copied to the server first:

```bash
# 1. SCP to the server with a random suffix to avoid /tmp collisions
scp laptop:/path/to/paper.pdf server:/tmp/paper-abc123.pdf

# 2. Call the tool — pipeline copies to LIBRARY_PATH and cleans up /tmp
ingest_document(filepath="/tmp/paper-abc123.pdf")
```

---

## Ingest event

Every successful ingest routes a thought with `type: ingest_event` through the
canonical `store_thought()` / `upsert_thought(...)` path:

```
"Ingested: Imbens & Kalyanaraman 2012 — Optimal Bandwidth Choice for the Regression Discontinuity Estimator"
```

This means the document appears in `browse_recent`, `get_briefing`, and `search_brain` results alongside ordinary thoughts — no separate tracking needed.

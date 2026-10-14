# Design: Paper Fetch

## Overview

`fetch_paper` pulls paper metadata and abstracts from external sources **without a
local file**. It complements `ingest_document` (which needs a PDF/text file): use
`fetch_paper` when you only have a DOI, an arXiv ID, a title, or a topic.

Two modes:

- **Lookup** — `fetch_paper(identifier=...)` resolves one paper by DOI / arXiv ID /
  title, stores it as a metadata-only `library` record, and embeds the abstract so it
  is semantically searchable.
- **Topic** — `fetch_paper(topic=...)` returns a ranked candidate list and **stores
  nothing**. Pick one and re-call with its DOI or title. This avoids dumping dozens of
  weakly-relevant rows into the library.

```
identifier ─┐
            ├─ sources.resolve(order=[...])  ──► merge by DOI ──► _store_paper
topic ──────┘                                └─ topic: dedup list, no store
```

## Sources (`openbrain/sources.py`)

A pure module (no DB, no brain dependency). Each source implements:

- `lookup(identifier, id_kind) -> PaperMeta | None` — known-item (doi | arxiv | title)
- `search(query, limit) -> list[PaperMeta]` — topic/keyword discovery

Built-in sources:

| source | lookup keys | abstract | key needed |
|--------|-------------|----------|------------|
| openalex | doi, title | yes (inverted index → reconstructed) | no |
| crossref | doi, title | sometimes (JATS, tag-stripped) + BibTeX | no |
| semantic_scholar | doi, arxiv, title | yes | optional `SEMANTIC_SCHOLAR_API_KEY` |
| arxiv | arxiv, title | yes | no |

`resolve()` fans out across the configured order. For lookup it **merges** records
field-by-field (earlier sources win per field, so e.g. CrossRef supplies BibTeX while
OpenAlex supplies the abstract). For topic search it concatenates results and
deduplicates by DOI → arXiv ID → normalized title.

Network calls use `urllib` (stdlib, no new dependency) and are blocking; `brain.py`
runs `resolve()` inside an executor. Identifier type is auto-detected
(`detect_identifier_kind`): DOI regex, arXiv pattern, else title.

## Configuration

`config.yaml`:

```yaml
paper_sources:
  order: [openalex, crossref, semantic_scholar, arxiv]
  enabled: [openalex, crossref, semantic_scholar, arxiv]
```

`order` is fan-out/merge priority; `enabled` filters it. The `fetch_paper(sources=...)`
argument overrides per call (comma-separated, e.g. `"openalex,arxiv"`).

## Storage

Metadata-only records reuse the existing `library` + `chunks` tables:

- `library` row has `filepath`/`file_hash` **NULL** (Postgres allows multiple NULLs
  under the `file_hash` UNIQUE constraint). Dedup is by DOI via the partial unique
  index `library_doi_uniq`.
- The abstract is stored twice: raw text in `library.abstract`, and as a `chunks` row
  with `kind = 'abstract'` carrying the embedding. Because search reads `chunks`, the
  abstract is found by `search_library`, `match_chunks`, and `search_brain` with **no
  search-code changes**.
- The embedding is decoupled from the write path. `_store_paper` (and the body-chunk loop
  in `ingest_document_pipeline`) embeds via `_embed_or_none`: on success the vector is
  written; if the embed model is unavailable the chunk is stored with `embedding = NULL`
  and the write still succeeds. A NULL-embedding chunk is immediately **FTS-searchable**
  (the `fts` column is generated from `content`) and becomes **vector-searchable** once
  `backfill_chunk_embeddings` fills it in (run via the `backfill_embeddings` MCP tool or
  the nightly maintenance job). This avoids both crashing on an embed outage and
  substituting a different model, whose vectors would be geometrically incompatible with
  the index. The fallback strategy is "defer and re-embed with the same model," not "embed
  with another model."
- No thought is stored. The `library` row is the record of the paper; search it with
  `search_library`.

`force=True` deletes the existing DOI-matched record (cascading to its chunks) and
re-fetches.

## Out of scope (follow-ups)

- Merging a later PDF ingest into an existing metadata-only row (add body chunks to the
  DOI-matched record instead of a new one).
- Backfilling embeddings for abstracts already sitting in `library.abstract` from past
  file ingests.

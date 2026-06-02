# Design: Search

## Overview

Open Brain uses hybrid search — vector similarity as the primary ranker, full-text search as an OR inclusion condition. This catches both semantic matches (embeddings) and exact name/term matches (FTS), which embeddings can miss for proper nouns and technical identifiers.

---

## `search_brain` — fan-out search

The primary cross-table search tool. Given a query, it fans out in parallel across:

| Source | Method |
|--------|--------|
| `thoughts` | Vector similarity OR FTS on `fts` column |
| `library` (via `chunks`) | Vector similarity OR FTS on chunk content, deduplicated to best chunk per document |
| `people` | Surfaced via co-occurrence mining on matching thoughts |
| `projects` | Surfaced via co-occurrence mining on matching thoughts |

People and projects are not searched directly — they emerge from the metadata of matching thoughts. This keeps entity results contextually grounded rather than returning bare records.

Returns `{ thoughts, library, people, projects }`.

---

## `search_library` — library-only search

Searches `chunks` with optional filters:

- `year_min` — exclude documents before a given year
- `doc_type` — filter by document type (paper, note, conversation, etc.)

Results are deduplicated to the best-matching chunk per library record, returning paper-level metadata alongside the matching passage.

---

## `search_thoughts`, `search_people`, `search_projects`

Single-table semantic search tools. Use when you want results from one table only. All use the same vector similarity + optional FTS pattern.

---

## Similarity thresholds

Default thresholds (configured in `config.yaml`):

| Search | Default threshold |
|--------|------------------|
| `search_brain` | 0.35 |
| `search_library` | 0.40 |
| `search_thoughts` | 0.50 |
| `search_people` / `search_projects` | 0.30 |

Lower thresholds for entities because their embeddings are enriched with thought snippets and tend to be shorter — a wider net is appropriate.

---

## Full-text search

FTS columns (`fts`) are maintained via triggers on `thoughts` and `chunks`. The FTS query is built from the non-stopword tokens of the user query joined with `|` (OR). This means a search for "Imbens bandwidth" will include any thought or chunk containing either word, even if the vector similarity is below threshold.

---

## Entity co-occurrence

`search_brain` extracts co-occurring people and projects from the metadata of matching thoughts. If thought A matches the query and its metadata lists `people: ["Alice", "Bob"]`, Alice and Bob are counted as co-occurrences. The top 5 by count are returned in the `people` and `projects` keys.

This surfaces relationship context without a separate entity search step.

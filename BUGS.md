# Known Bugs

## Document ingest fails in container: `[Errno 13] Permission denied: '/mnt/data'`

**Status:** Fixed in code (commit `e3e7030`, issue #001) — redeploy required. The
deployed container reported here ran an image built before the fix.
**Reported:** 2026-06-27
**Severity:** High — full-text PDF ingest is broken on the deployed container. Only `fetch_paper` (metadata + abstract, no file) works.

### Resolution

`brain.py` reads `OPENBRAIN_LIBRARY_PATH` (container path) and the `Dockerfile`
sets it to `/data/openbrain/library`. Rebuild + redeploy the deployment container
from the current image to pick up the fix. **Still open:** the remote-ingest
staging dir (`/tmp/openbrain-ingest`) is container-internal and unreachable by
remote scp — bind-mount it to a host path or document a host-reachable location.

### Symptom

Calling `ingest_document` / `ingest_staged_document` against the deployed openbrain
MCP server (container `b2e7deb55251` on the deployment host) fails:

```
Error calling tool 'ingest_document': [Errno 13] Permission denied: '/mnt/data'
```

The file is found (no "File not found"), so path resolution succeeds; the failure
happens during the copy-into-LIBRARY_PATH step.

### Root cause (suspected)

Host/container path mismatch in the configured `LIBRARY_PATH` (and the staging →
library copy target).

- Container mount: host `/mnt/data` → container `/data` (only this mount exists;
  verified via `docker inspect`).
- A PDF placed at host `/mnt/data/openbrain/library/X.pdf` is visible inside the
  container as `/data/openbrain/library/X.pdf`.
- Passing the **container** path `/data/openbrain/library/X.pdf` to
  `ingest_document` resolves the file (no ENOENT) but then raises
  `EACCES` on `/mnt/data` — i.e. the app is configured with the **host-style**
  `LIBRARY_PATH` (`/mnt/data/openbrain/library`), which is not accessible inside
  the container (runs as uid 1000:1000; `/mnt/data` not mounted there).

So the ingest pipeline tries to copy the resolved file to a host path that does
not exist inside the container.

### Reproduction

1. `prepare_document_ingest(filename="X.pdf")` → returns a staging path under
   `/tmp/openbrain-ingest/...` (container-internal, not reachable by remote scp).
2. Place a PDF in the library dir on the host: `scp X.pdf <deployment-host>:/mnt/data/openbrain/library/`.
3. `ingest_document(filepath="/data/openbrain/library/X.pdf")` → `EACCES /mnt/data`.

The file perms are not the issue (file + dirs are `0777`, readable by uid 1000).

### Notes / possible fixes

- Make `LIBRARY_PATH` (and the staging target) use the **container** path that the
  bind mount actually exposes (`/data/openbrain/library`), not the host path
  (`/mnt/data/openbrain/library`). Confirm what the running app reads `LIBRARY_PATH`
  as and align it with the container mount.
- The remote-ingest flow assumes `/tmp/openbrain-ingest/` is reachable from the
  client, but it is container-internal — remote clients cannot scp there. Either
  bind-mount the staging dir to a host path, or document a host-reachable staging
  location.

### Workaround

None confirmed yet. Test paper `2602.16864` ("Position: A Dynamical Systems
Perspective is Needed to Advance Time Series Modeling") has a metadata/abstract
record via `fetch_paper`, but no full text. PDF is staged at host
`/mnt/data/openbrain/library/2602.16864.pdf` awaiting a working ingest path.

## `fetch_paper` title-only lookups silently return the wrong paper

**Status:** Fixed — title lookups now require an exact (normalized) title match.
**Reported:** 2026-06-29
**Severity:** Medium — corrupts the library with confidently-wrong records; no error surfaced.

### Fix

`sources.resolve()` known-item branch now gates `kind == "title"` results: a
candidate is kept only when its normalized title (`sources._normalize_title`:
case/accents/punctuation/`&`/whitespace folded) equals the normalized query.
Non-matches are dropped before merge, so a generic/partial title returns
`not found` instead of storing an unrelated paper. DOI/arXiv lookups are exact
identifiers and skip the gate; topic mode stays fuzzy. `fetch_paper`'s docstring
now tells callers to prefer DOI/arXiv and treats title as exact-match last-resort.

### Symptom

Bulk-importing a 174-entry BibTeX file: entries with no DOI were looked up by
title. The fuzzy title search returns the nearest source (OpenAlex) hit with no
similarity floor, so generic/short titles matched unrelated papers and were
stored as `fetched` with full confidence:

- `"QuickStats"` → *QuickStats: Suicide Rates for Teens Aged 15–19 Years … United States, 1975–2015* (CDC)
- `"2017 Census of Agriculture"` → *Value-Added Agriculture … Agritourism in Tennessee*
- `"Land speculation"` → *Chinese eco-cities: … land-speculation-oriented local entrepreneurialism*
- `"Use It Or Lose It: Adverse Possession and Economic Development"` → *The Lancet Commission on global mental health and sustainable development*

### Notes / possible fixes

- Apply a title-similarity threshold to known-item title lookups; below it, return
  `not found` (or a `low_confidence` candidate list) instead of storing.
- For batch imports, prefer a structured-field match (title + first author + year)
  over a bare title query.
- Consider returning per-identifier match confidence in the batch summary so a
  caller can flag/skip weak matches.

## `fetch_paper` dedups on DOI only → duplicate records for the same paper

**Status:** Fixed — `_store_paper` now also dedups on title + year when the DOI
check misses.
**Reported:** 2026-06-29
**Severity:** Low.

### Fix

`brain._store_paper` runs a secondary dedup before insert: when no existing row
matched on DOI, it looks up `lower(title) = lower($title) AND year = $year`. A
title-sourced copy carrying a different or no DOI is now recognized as a
duplicate. Skipped under `force=True`. The bulk `store_acquired_paper` path is
intentionally left DOI-keyed (high-volume acquire; no title fallback).

### Symptom

The same paper fetched once by DOI and once by title creates two library rows —
dedup keys on DOI only, so the title-sourced copy (which may carry a different or
no DOI) is not recognized as a duplicate. Saw two rows for *Environmental Water
Rights Transfers: A Review of State Laws* after a batch import that contained both
a DOI entry and a title entry for it.

### Notes / possible fixes

- Add a secondary dedup key (normalized title + first author + year) when DOI is
  absent or differs.

## `fetch_paper` returns a prose summary, not JSON (client integration note)

**Status:** Documented — `fetch_paper` docstring now states the result is
plain-text, not JSON (don't `json.loads` it). Kept prose by design; full JSON
return deferred unless a client needs it.
**Reported:** 2026-06-29
**Severity:** Low — affects MCP clients that assume JSON tool results.

### Symptom

`fetch_paper` returns a human-readable batch summary string
(`"Batch fetch: N fetched, M already in library, …"`), unlike most openbrain tools
which return JSON (often a `json.dumps` string). A client that blindly
`json.loads()` every text result crashes with
`Expecting value: line 1 column 1 (char 0)`. The server-side fetch still
completes; only the client's response parsing fails.

### Notes / possible fixes

- Either return structured JSON (e.g. `{fetched: [...], already: [...], not_found: [...]}`)
  alongside/instead of the prose, or document that this tool's result is plain text.

## Embedding model unavailable at fetch time leaves chunks unembedded; backfill can't recover

**Status:** Partially addressed — added a tool to name the pending records; the
underlying recovery still requires the embed model to be back up (ops).
**Reported:** 2026-06-29
**Severity:** Medium — affected papers are invisible to semantic `search_library` until re-embedded.

### What changed

Not a code defect in `backfill_embeddings` — it correctly heals chunks only when
the embed model is reachable (`0 embedded, 3 failed` = model still down). Added
`list_pending_embeddings` (MCP tool + `brain.list_pending_embeddings`) so the
*which* records are pending (id + title + chunk count) is visible, not just a
count. Recovery action: confirm the embed model is healthy on the deployment
container, then run `backfill_embeddings`.

### Symptom

During the batch import several abstracts were stored with
`"abstract stored without an embedding (model unavailable)"` /
`"embedding deferred"`. A later `backfill_embeddings` run reported
`0 embedded, 3 failed, 3 still pending` — the embed model was still down, so the
backfill could not complete and the chunks remain unsearchable.

### Notes / possible fixes

- Confirm the local embedding model's availability/health on the deployed
  container; `backfill_embeddings` only succeeds when it is up.
- Consider surfacing *which* records are pending (ids/titles) so they can be
  targeted once the model is back.

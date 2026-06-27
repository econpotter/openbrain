# Known Bugs

## Document ingest fails in container: `[Errno 13] Permission denied: '/mnt/data'`

**Status:** Open
**Reported:** 2026-06-27
**Severity:** High — full-text PDF ingest is broken on the deployed (rhythm) container. Only `fetch_paper` (metadata + abstract, no file) works.

### Symptom

Calling `ingest_document` / `ingest_staged_document` against the deployed openbrain
MCP server (container `b2e7deb55251` on `rhythm.herknet`) fails:

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
2. Place a PDF in the library dir on the host: `scp X.pdf rhythm:/mnt/data/openbrain/library/`.
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

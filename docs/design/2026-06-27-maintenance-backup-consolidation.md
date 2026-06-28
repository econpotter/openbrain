# Plan: Maintenance/backup consolidation — ✅ DONE

**Date:** 2026-06-27
**Status:** DONE (implemented this session).

## Goal

One nightly maintenance job, one mechanism. Backup was a separate systemd timer; maintenance
was a compose `--loop` service. Fold backup into the nightly maintenance run so a single
compose service does backup + consolidation + embedding backfill on one schedule. No systemd
(YAGNI until openbrain runs outside Docker).

## Done

- ✅ `scripts/openbrain-maintenance.py` — `_run_backup()` added as the **first** step of
  `_run_once()` (backup → extraction → embedding-backfill). Backup failures are logged, never
  abort the rest of the run.
- ✅ `scripts/backup.sh` — honors `OPENBRAIN_BACKUP_PATH` / `OPENBRAIN_LIBRARY_PATH` container
  overrides so the in-container run writes to the bind-mounted paths (same pattern as
  `OPENBRAIN_LIBRARY_PATH` for ingest; fixes the host/container path split).
- ✅ `docker-compose.yaml` — maintenance service gets `OPENBRAIN_BACKUP_PATH=/data/openbrain/backup`.
- ✅ `Dockerfile` — `postgresql-client-16` (pgdg) for in-container `pg_dump`; client major must
  track the postgres server (`pgvector/pgvector:pg16`).
- ✅ Deleted `scripts/openbrain-backup.{service,timer}`.

## Convention preserved

Paths split three ways: `.env` = secrets + host identity; `config.yaml` = host-side
`library_path`/`backup_path`/`run_at`; compose env / Dockerfile `ENV` = container-path overrides.

## Open verification

- `docker build ./openbrain` not yet run to completion (build was killed). One full build
  confirms the pgdg `pg_dump` install before relying on the in-container nightly backup.

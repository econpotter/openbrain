#!/usr/bin/env python3
"""Live round-trip trial for the thought derivation pipeline (Plan F).

Exercises the real ``brain.store_thought`` / ``extract_thought_metadata`` /
straggler-backfill code path over asyncpg against an ephemeral
PostgreSQL + pgvector container — the same path the MCP server runs. Only the
extraction and embedding *model* calls are stubbed (deterministically), because
this harness must run with no live model or API keys. Everything downstream is
real: the asyncpg round-trip, the ``upsert_thought`` merge, the enriched
lifecycle, importance precedence, entity refresh/link, and the backfills.

This is the Python-path counterpart to ``trial-run-postgres.sh`` (which covers
the SQL merge). Run: ``python scripts/trial-run-thought-pipeline.py`` (needs
Docker). When a real extraction model *is* available, point ``DATABASE_URL`` at a
live database and drive the same functions without the stubs to close the
model leg as well.
"""

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
CONTAINER = f"openbrain-pipeline-trial-{os.getpid()}"
PORT = "55432"
PASSWORD = "openbrain-trial-password"
EMBED_DIM = 768


def _docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True)


def _cleanup() -> None:
    _docker("rm", "--force", CONTAINER)


def _start_container() -> None:
    _docker("rm", "--force", CONTAINER)
    proc = _docker(
        "run",
        "--detach",
        "--name",
        CONTAINER,
        "--env",
        "POSTGRES_USER=openbrain",
        "--env",
        f"POSTGRES_PASSWORD={PASSWORD}",
        "--env",
        "POSTGRES_DB=openbrain",
        "--publish",
        f"{PORT}:5432",
        "--volume",
        f"{PROJECT_DIR}/schemas/core:/docker-entrypoint-initdb.d:ro",
        "pgvector/pgvector:pg16",
    )
    if proc.returncode != 0:
        raise SystemExit(f"docker run failed: {proc.stderr}")


def _wait_ready() -> None:
    for _ in range(90):
        logs = _docker("logs", CONTAINER)
        if "PostgreSQL init process complete" in (logs.stdout + logs.stderr):
            ready = _docker(
                "exec", CONTAINER, "pg_isready", "-U", "openbrain", "-d", "openbrain"
            )
            if ready.returncode == 0:
                return
        if _docker(
            "inspect", "--format", "{{.State.Running}}", CONTAINER
        ).stdout.strip() != "true":
            break
        time.sleep(1)
    logs = _docker("logs", CONTAINER)
    raise SystemExit(f"Postgres did not initialize:\n{logs.stdout}\n{logs.stderr}")


class _Stub:
    """Deterministic stand-in for the embedding + extraction model calls."""

    embed_ok = True
    model_ok = True
    model_json = "{}"


async def _stub_embed(text: str, task: str = "document") -> list[float]:
    if not _Stub.embed_ok:
        raise RuntimeError("stub embed model down")
    return [0.01] * EMBED_DIM


async def _stub_extraction_prompt(prompt: str) -> str:
    if not _Stub.model_ok:
        raise RuntimeError("stub extraction model down")
    return _Stub.model_json


_failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    status = "ok  " if cond else "FAIL"
    print(f"  [{status}] {msg}")
    if not cond:
        _failures.append(msg)


async def _run_trials() -> None:
    from openbrain import brain

    brain.embed = _stub_embed  # type: ignore[assignment]
    brain.complete_extraction_prompt = _stub_extraction_prompt  # type: ignore[assignment]

    pool = await brain.get_pool()

    async def row(content: str) -> dict | None:
        async with pool.acquire() as conn:
            r = await conn.fetchrow(
                "SELECT id, enriched, importance, embedding IS NULL AS no_emb, "
                "metadata FROM thoughts WHERE content = $1",
                content,
            )
        return dict(r) if r else None

    async def settle() -> None:
        # Let the deferred fire-and-forget extraction task run to completion.
        for _ in range(50):
            await asyncio.sleep(0.02)

    # ── S1: sparse capture → full enrichment (AC #1) ──────────────────────────
    print("S1: sparse capture → enriched + embedded + extracted + linked")
    _Stub.embed_ok = _Stub.model_ok = True
    _Stub.model_json = (
        '{"tags":["sdc","synthetic-control"],"people":["Ada Lovelace"],'
        '"projects":["SDC"],"action_items":["email Ada"],"importance":6}'
    )
    # The pipeline links thoughts to entities that already exist (the documented
    # "add_person/add_project first" pattern); it does not invent entity rows.
    # Seed them so extraction's discovered names have something to link to.
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO people (name) VALUES ('Ada Lovelace')")
        await conn.execute(
            "INSERT INTO projects (name, status) VALUES ('SDC', 'active')"
        )
    c1 = "S1: Met Ada Lovelace about the SDC project"
    await brain.store_thought(c1, {"type": "thought"})
    await settle()
    r1 = await row(c1)
    check(r1 is not None, "thought stored")
    check(not r1["no_emb"], "embedding is non-NULL")
    check(r1["enriched"], "enriched flipped true")
    meta1 = r1["metadata"]
    if isinstance(meta1, str):
        meta1 = json.loads(meta1)
    check("sdc" in meta1.get("tags", []), "extracted tags present")
    check("Ada Lovelace" in meta1.get("people", []), "extracted people present")
    check("SDC" in meta1.get("projects", []), "extracted projects present")
    check("email Ada" in meta1.get("action_items", []), "extracted action_items present")
    check(r1["importance"] == 6, f"model importance landed (got {r1['importance']})")
    async with pool.acquire() as conn:
        people_n = await conn.fetchval(
            "SELECT count(*) FROM people WHERE name = 'Ada Lovelace'"
        )
        project_n = await conn.fetchval(
            "SELECT count(*) FROM projects WHERE name = 'SDC'"
        )
        link_n = await conn.fetchval(
            "SELECT count(*) FROM links WHERE source_table = 'thoughts' "
            "AND source_id = $1",
            r1["id"],
        )
    check(people_n == 1 and project_n == 1, "seeded person + project present")
    check(link_n >= 2, f"thought linked to discovered entities (got {link_n})")

    # ── S2: caller-supplied importance wins (AC #2) ───────────────────────────
    print("S2: caller importance preserved, arrays still union")
    _Stub.model_json = '{"tags":["ephemeral"],"people":[],"importance":5}'
    c2 = "S2: A deliberately low-importance passing note"
    await brain.store_thought(c2, {"type": "thought", "importance": 2, "tags": ["mine"]})
    await settle()
    r2 = await row(c2)
    check(r2["enriched"], "enriched flipped true")
    check(r2["importance"] == 2, f"caller importance 2 preserved (got {r2['importance']})")
    meta2 = r2["metadata"]
    if isinstance(meta2, str):
        meta2 = json.loads(meta2)
    check(
        "mine" in meta2.get("tags", []) and "ephemeral" in meta2.get("tags", []),
        "caller and extracted tags union",
    )

    # ── S3: embed down → straggler → backfill_thought_embeddings (AC #3) ──────
    print("S3: embedding straggler flagged then backfilled")
    _Stub.embed_ok = False
    c3 = "S3: Captured while the embedding model was down"
    # enriched=true opts out of extraction so this isolates the embedding path.
    await brain.store_thought(c3, {"type": "thought", "enriched": True})
    await settle()
    r3 = await row(c3)
    check(r3["no_emb"], "embedding is NULL after embed outage")
    pending = await brain.list_pending_thoughts()
    check(
        any(p["id"] == str(r3["id"]) and p["needs_embedding"] for p in pending),
        "list_pending_thoughts flags the embedding straggler",
    )
    _Stub.embed_ok = True
    res3 = await brain.backfill_thought_embeddings()
    check(res3["embedded"] >= 1, "backfill embedded the straggler")
    r3b = await row(c3)
    check(not r3b["no_emb"], "embedding is non-NULL after backfill")
    pending = await brain.list_pending_thoughts()
    check(
        all(not (p["id"] == str(r3["id"]) and p["needs_embedding"]) for p in pending),
        "embedding straggler gone from pending",
    )

    # ── S4: extraction down → straggler → backfill_thought_extractions (AC #3) ─
    print("S4: extraction straggler flagged then backfilled")
    _Stub.model_ok = False
    _Stub.model_json = '{"tags":["healed"],"people":[],"importance":4}'
    c4 = "S4: Captured while the extraction model was down"
    await brain.store_thought(c4, {"type": "thought"})
    await settle()
    r4 = await row(c4)
    check(not r4["no_emb"], "embedding present (embed model was up)")
    check(not r4["enriched"], "enriched stays false after extraction outage")
    pending = await brain.list_pending_thoughts()
    check(
        any(p["id"] == str(r4["id"]) and p["needs_extraction"] for p in pending),
        "list_pending_thoughts flags the extraction straggler",
    )
    _Stub.model_ok = True
    res4 = await brain.backfill_thought_extractions()
    check(res4["extracted"] >= 1, "backfill enriched the straggler")
    r4b = await row(c4)
    check(r4b["enriched"], "enriched true after backfill")
    pending = await brain.list_pending_thoughts()
    check(
        all(not (p["id"] == str(r4["id"]) and p["needs_extraction"]) for p in pending),
        "extraction straggler gone from pending",
    )

    if brain._pool is not None:
        await brain._pool.close()


def main() -> None:
    if _docker("info").returncode != 0:
        raise SystemExit("ERROR: cannot access the Docker daemon.")
    print("Starting ephemeral PostgreSQL + pgvector container...")
    _start_container()
    try:
        print("Waiting for PostgreSQL initialization...")
        _wait_ready()
        os.environ["DATABASE_URL"] = (
            f"postgresql://openbrain:{PASSWORD}@127.0.0.1:{PORT}/openbrain"
        )
        print("Driving the real thought pipeline (model calls stubbed)...")
        asyncio.run(_run_trials())
    finally:
        _cleanup()

    if _failures:
        print(f"\nRESULT: FAIL ({len(_failures)} check(s) failed)")
        sys.exit(1)
    print("\nThought pipeline live round-trip passed.")
    print("RESULT: PASS")


if __name__ == "__main__":
    main()

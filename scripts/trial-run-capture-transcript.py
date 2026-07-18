#!/usr/bin/env python3
"""Live round-trip trial for the server-side capture_transcript tool (#012).

Drives the real ``capture_transcript`` MCP tool → ``brain.summarize_transcript``
→ ``brain.store_thought`` path over asyncpg against an ephemeral PostgreSQL +
pgvector container — the same path the MCP server runs. Only the extraction and
embedding *model* calls are stubbed (deterministically); the asyncpg round-trip,
the ``upsert_thought`` fingerprint merge, and the deferred #010 enrichment
lifecycle are all real.

Verifies the acceptance criteria the deployed server can't yet (it
predates this tool): a substantive delta stores an enriched ``conversation``
thought, a repeated delta merges rather than duplicates, and a trivial delta
stores nothing. Run: ``python scripts/trial-run-capture-transcript.py`` (needs
Docker).
"""

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
CONTAINER = f"openbrain-transcript-trial-{os.getpid()}"
PORT = "55433"
PASSWORD = "openbrain-trial-password"
EMBED_DIM = 768


def _docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True)


def _cleanup() -> None:
    _docker("rm", "--force", CONTAINER)


def _start_container() -> None:
    _docker("rm", "--force", CONTAINER)
    proc = _docker(
        "run", "--detach", "--name", CONTAINER,
        "--env", "POSTGRES_USER=openbrain",
        "--env", f"POSTGRES_PASSWORD={PASSWORD}",
        "--env", "POSTGRES_DB=openbrain",
        "--publish", f"{PORT}:5432",
        "--volume", f"{PROJECT_DIR}/schemas/core:/docker-entrypoint-initdb.d:ro",
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
    summary = "Decided to ship the thin client; spool handles tunnel-down."


async def _stub_embed(text: str, task: str = "document") -> list[float]:
    return [0.01] * EMBED_DIM


async def _stub_extraction_prompt(prompt: str) -> str:
    # capture_transcript's summarizer asks for prose; enrichment asks for JSON.
    # Return the trivial-safe summary for the summary prompt, empty JSON for the
    # per-thought enrichment prompt so both legs run deterministically offline.
    if "TRANSCRIPT DELTA" in prompt:
        return _Stub.summary
    return "{}"


class _FakeMCP:
    def __init__(self) -> None:
        self.tools: dict = {}

    def tool(self, fn):
        self.tools[fn.__name__] = fn
        return fn


_failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(f"  [{'ok  ' if cond else 'FAIL'}] {msg}")
    if not cond:
        _failures.append(msg)


async def _run_trials() -> None:
    from openbrain import brain
    from openbrain.tools import capture

    brain.embed = _stub_embed  # type: ignore[assignment]
    brain.complete_extraction_prompt = _stub_extraction_prompt  # type: ignore[assignment]

    mcp = _FakeMCP()
    capture.register(mcp)
    capture_transcript = mcp.tools["capture_transcript"]

    pool = await brain.get_pool()

    async def count(content: str) -> int:
        async with pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT count(*) FROM thoughts WHERE content = $1", content
            )

    async def row(content: str) -> dict | None:
        async with pool.acquire() as conn:
            r = await conn.fetchrow(
                "SELECT id, enriched, embedding IS NULL AS no_emb, metadata, type "
                "FROM thoughts WHERE content = $1",
                content,
            )
        return dict(r) if r else None

    async def settle() -> None:
        for _ in range(50):
            await asyncio.sleep(0.02)

    delta = "user: should we ship the thin client?\nassistant: yes, with a spool."

    # ── T1: substantive delta → stored conversation, enriched via #010 ────────
    print("T1: substantive delta → enriched conversation thought")
    out1 = await capture_transcript(
        content=delta, session_id="sess-A", trigger="stop"
    )
    await settle()
    r = await row(_Stub.summary)
    check("Stored conversation summary" in out1, "tool reports a store")
    check(r is not None, "summary thought stored")
    check(r["type"] == "conversation", f"type is conversation (got {r['type']})")
    check(not r["no_emb"], "embedding is non-NULL")
    check(r["enriched"], "deferred #010 enrichment flipped enriched=true")
    meta = r["metadata"]
    if isinstance(meta, str):
        meta = json.loads(meta)
    check(meta.get("session_id") == "sess-A", "session_id recorded")
    check(meta.get("trigger") == "stop", "trigger recorded")

    # ── T2: repeated delta → fingerprint merge, not a duplicate ───────────────
    print("T2: repeated identical delta merges (no duplicate row)")
    await capture_transcript(content=delta, session_id="sess-A", trigger="stop")
    await settle()
    check(await count(_Stub.summary) == 1, "still exactly one summary row")

    # ── T3: trivial delta → nothing stored ────────────────────────────────────
    print("T3: trivial delta stores nothing")
    async with pool.acquire() as conn:
        before = await conn.fetchval("SELECT count(*) FROM thoughts")
    _Stub.summary = "NOTHING"  # summarizer treats sentinel as trivial
    out3 = await capture_transcript(
        content="user: ok thanks", session_id="sess-B", trigger="auto"
    )
    async with pool.acquire() as conn:
        after = await conn.fetchval("SELECT count(*) FROM thoughts")
    check("skipped" in out3.lower(), "tool reports a skip")
    check(after == before, f"no new thought row (before={before}, after={after})")


def main() -> None:
    print(f"Starting ephemeral Postgres container {CONTAINER} on :{PORT} …")
    _start_container()
    try:
        _wait_ready()
        os.environ["DATABASE_URL"] = (
            f"postgresql://openbrain:{PASSWORD}@localhost:{PORT}/openbrain"
        )
        asyncio.run(_run_trials())
    finally:
        _cleanup()

    if _failures:
        print(f"\n{len(_failures)} check(s) FAILED:")
        for f in _failures:
            print(f"  - {f}")
        raise SystemExit(1)
    print("\nAll capture_transcript trial checks passed.")


if __name__ == "__main__":
    main()

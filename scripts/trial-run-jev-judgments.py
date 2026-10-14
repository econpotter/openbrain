#!/usr/bin/env python3
"""Live trial of Jev judgments (importance and projects) on real thoughts.

Runs the real write path against an ephemeral PostgreSQL + pgvector container,
with live models: embeddings and DeepSeek through a litellm proxy, importance and
projects from Jev. Nothing is stubbed. Two paths are exercised:

- new thoughts: ``store_thought`` schedules enrichment (DeepSeek and Jev together)
  exactly as a capture does;
- old thoughts: stored already enriched, then ``backfill_thought_judgments`` gives
  them importance and projects, as the sweep does for the pre-Jev backlog.

Input is a JSON list of ``{"content": str, "label": int | null}``; ``label`` is a
person's 0-6 importance rating, used to report agreement. Projects come from a
workspace PROJECTS.md, as ``openbrain sync-projects`` would load them.

    TYPESAFE_API_KEY=... LITELLM_API_KEY=... uv run python \\
        scripts/trial-run-jev-judgments.py thoughts.json \\
        --litellm-url http://<rhythm>:4000 --report report.json

Needs Docker. Exits 1 if any pipeline check fails.
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
CONTAINER = f"openbrain-jev-trial-{os.getpid()}"
PORT = "55433"
PASSWORD = "openbrain-trial-password"

_failures: list[str] = []


def _docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True)


def _start_container() -> None:
    proc = _docker(
        "run",
        "--detach",
        "--rm",
        "--name",
        CONTAINER,
        "--env",
        "POSTGRES_USER=openbrain",
        "--env",
        f"POSTGRES_PASSWORD={PASSWORD}",
        "--env",
        "POSTGRES_DB=openbrain",
        "--publish",
        f"127.0.0.1:{PORT}:5432",
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
        time.sleep(1)
    raise SystemExit("Postgres did not initialize.")


def check(cond: bool, msg: str) -> None:
    print(f"  [{'ok  ' if cond else 'FAIL'}] {msg}")
    if not cond:
        _failures.append(msg)


def rank(values: list[float]) -> list[float]:
    """Ranks starting at 1, ties given their average rank."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float:
    rx, ry = rank(xs), rank(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx) ** 0.5
    vy = sum((b - my) ** 2 for b in ry) ** 0.5
    return cov / (vx * vy)


async def run(args: argparse.Namespace) -> dict:
    from openbrain import brain
    from openbrain.client.cli import _read_registry

    brain._cfg["litellm_url"] = args.litellm_url
    thoughts = json.loads(Path(args.thoughts).read_text())
    pool = await brain.get_pool()

    registry = _read_registry(Path(args.projects).expanduser())
    for name, purpose in registry.items():
        await brain.add_project(name, purpose)
    print(f"Loaded {len(registry)} active projects from {args.projects}")

    new, old = thoughts[: args.new], thoughts[args.new :]
    ids: dict[str, str] = {}

    print(f"New-thought path: storing {len(new)} thoughts for full enrichment")
    for t in new:
        ids[t["content"]] = await brain.store_thought(
            t["content"], {"type": "conversation"}
        )
    deadline = time.monotonic() + args.wait
    while time.monotonic() < deadline:
        async with pool.acquire() as conn:
            waiting = await conn.fetchval(
                "SELECT count(*) FROM thoughts WHERE id = ANY($1::uuid[]) "
                "AND NOT enriched",
                [ids[t["content"]] for t in new],
            )
        if waiting == 0:
            break
        await asyncio.sleep(2)

    print(f"Backfill path: storing {len(old)} enriched thoughts, then judging them")
    for t in old:
        ids[t["content"]] = await brain.store_thought(
            t["content"], {"type": "conversation", "enriched": True}
        )
    backfill = await brain.backfill_thought_judgments(limit=len(old))
    print(f"  backfill: {backfill}")

    rows = []
    async with pool.acquire() as conn:
        for t in thoughts:
            r = await conn.fetchrow(
                "SELECT id, enriched, importance, embedding IS NULL AS no_embedding, "
                "metadata FROM thoughts WHERE id = $1",
                ids[t["content"]],
            )
            linked = await conn.fetch(
                "SELECT p.name FROM links l JOIN projects p ON p.id = l.target_id "
                "WHERE l.source_table = 'thoughts' AND l.source_id = $1 "
                "AND l.target_table = 'projects' ORDER BY p.name",
                r["id"],
            )
            meta = r["metadata"]
            if isinstance(meta, str):
                meta = json.loads(meta)
            rows.append(
                {
                    "path": "new" if t in new else "backfill",
                    "excerpt": " ".join(t["content"].split())[:160],
                    "label": t.get("label"),
                    "enriched": r["enriched"],
                    "no_embedding": r["no_embedding"],
                    "importance": r["importance"],
                    "judge": meta.get("judge"),
                    "projects": meta.get("projects", []),
                    "linked_projects": [x["name"] for x in linked],
                    "tags": meta.get("tags", []),
                    "action_items": meta.get("action_items", []),
                }
            )
    await pool.close()

    print("Checks:")
    for path in ("new", "backfill"):
        part = [r for r in rows if r["path"] == path]
        check(
            all(r["enriched"] and r["judge"] for r in part),
            f"{path}: all {len(part)} thoughts enriched and judged",
        )
        check(
            all(sorted(r["projects"]) == r["linked_projects"] for r in part),
            f"{path}: every judged project has a link row",
        )
        check(
            all(
                r["importance"] == min(6, int(r["judge"]["importance_score"] + 0.5))
                for r in part
                if r["judge"]
            ),
            f"{path}: importance column is the rounded Jev score",
        )
    check(
        any(r["tags"] for r in rows if r["path"] == "new"),
        "new: DeepSeek tags reached the thoughts",
    )
    check(not any(r["no_embedding"] for r in rows), "all thoughts embedded")

    labelled = [r for r in rows if r["label"] is not None and r["judge"]]
    agreement = None
    if labelled:
        labels = [r["label"] for r in labelled]
        scores = [r["judge"]["importance_score"] for r in labelled]
        agreement = {
            "n": len(labelled),
            "spearman": round(spearman(labels, scores), 2),
            "exact": sum(r["label"] == r["importance"] for r in labelled),
            "within_one": sum(abs(r["label"] - r["importance"]) <= 1 for r in labelled),
        }
        print(f"Agreement with labels: {agreement}")
    return {"backfill": backfill, "agreement": agreement, "thoughts": rows}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("thoughts", help="JSON list of {content, label}")
    parser.add_argument("--litellm-url", required=True, help="litellm proxy /v1 base")
    parser.add_argument("--projects", default="~/workspace/PROJECTS.md")
    parser.add_argument("--new", type=int, default=5, help="thoughts on the new path")
    parser.add_argument("--wait", type=float, default=300, help="seconds to wait")
    parser.add_argument(
        "--report", required=True, help="where to write the JSON report"
    )
    args = parser.parse_args()
    for key in ("TYPESAFE_API_KEY", "LITELLM_API_KEY"):
        if not os.environ.get(key):
            raise SystemExit(f"ERROR: {key} is not set.")

    _start_container()
    try:
        _wait_ready()
        os.environ["DATABASE_URL"] = (
            f"postgresql://openbrain:{PASSWORD}@127.0.0.1:{PORT}/openbrain"
        )
        report = asyncio.run(run(args))
    finally:
        _docker("stop", CONTAINER)
    Path(args.report).write_text(json.dumps(report, indent=1))
    print(f"Report: {args.report}")
    if _failures:
        print(f"RESULT: FAIL ({len(_failures)} check(s) failed)")
        sys.exit(1)
    print("RESULT: PASS")


if __name__ == "__main__":
    main()

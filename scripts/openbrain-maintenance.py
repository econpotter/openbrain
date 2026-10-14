#!/usr/bin/env python3
"""Run scheduled openbrain maintenance.

Two independent schedules:
  --loop        Nightly consolidation (backup + synthesis + library/findings
                backfill) once a day at extraction.run_at.
  --sweep-loop  Fast thought-straggler backstop (embedding + extraction) every
                extraction.sweep_interval_minutes, so an outage is healed within
                minutes rather than at the next nightly run.

--sweep / (no flag) run the sweep / nightly pass once and exit.
"""

import argparse
import asyncio
from datetime import datetime, timedelta
import logging
from pathlib import Path
import subprocess
import sys

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

from openbrain import brain  # noqa: E402 — after sys.path bootstrap above

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("openbrain.maintenance")


def _seconds_until(run_at: str) -> float:
    hour, minute = [int(part) for part in run_at.split(":", maxsplit=1)]
    now = datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def _run_backup() -> None:
    """Back up DB + library before any other maintenance work.

    Runs first so a later step failing can never skip the backup. Failures here
    are logged but do not abort the rest of the run — a missed backup must not
    block nightly consolidation, and the next run retries it.
    """
    backup_script = SCRIPT_DIR / "backup.sh"
    try:
        proc = subprocess.run(
            ["bash", str(backup_script)],
            capture_output=True,
            text=True,
            check=False,
        )
        for line in proc.stdout.splitlines():
            logger.info("backup: %s", line)
        if proc.returncode != 0:
            logger.error(
                "Backup failed (exit %d): %s",
                proc.returncode,
                proc.stderr.strip() or "(no stderr)",
            )
        else:
            logger.info("Backup complete.")
    except Exception as exc:
        logger.exception("Backup step errored: %s", exc)


async def _run_once() -> None:
    logger.info("Maintenance run started.")
    _run_backup()
    result = await brain.run_extraction_once()
    logger.info(
        "Extraction complete: "
        f"{result['processed']} processed, "
        f"{result['syntheses']} synthesis thought(s) written."
    )
    backfill = await brain.backfill_chunk_embeddings()
    status = "succeeded" if backfill["failed"] == 0 else "completed with failures"
    logger.info(
        "Embedding backfill %s: "
        f"{backfill['embedded']} embedded, {backfill['failed']} failed, "
        f"{backfill['pending_after']} still pending "
        f"(of {backfill['pending_before']} before this run).",
        status,
    )

    from openbrain import findings

    fb = await findings.backfill_findings()
    fb_status = "succeeded" if fb["failed"] == 0 else "completed with failures"
    logger.info(
        "Findings backfill %s: "
        f"{fb['extracted_papers']} papers extracted, {fb['failed']} failed, "
        f"{fb['pending_after']} still pending "
        f"(of {fb['pending_before']} before this run).",
        fb_status,
    )

    logger.info("Maintenance run complete.")


async def _run_sweep() -> None:
    """Fast straggler backstop for thoughts — the failure-mode healer.

    A thought whose embed or extraction model was down at write time lands as a
    straggler (``embedding IS NULL`` / ``enriched=false``). The deferred write-path
    task normally heals extraction within seconds; this sweep catches the ones that
    task could not complete because the model was down. It runs on a *short* cadence
    (minutes) so an outage that lasted an hour is healed minutes after the model is
    back — not at the next nightly consolidation. It is deliberately cheap: only the
    thought backfills (embeddings, enrichment, Jev judgments), no backup, no
    synthesis, no library/findings work.
    """
    logger.info("Thought straggler sweep started.")
    tb = await brain.backfill_thought_embeddings()
    tb_status = "succeeded" if tb["failed"] == 0 else "completed with failures"
    logger.info(
        "Thought embedding backfill %s: "
        f"{tb['embedded']} embedded, {tb['failed']} failed, "
        f"{tb['pending_after']} still pending "
        f"(of {tb['pending_before']} before this run).",
        tb_status,
    )

    tx = await brain.backfill_thought_extractions()
    tx_status = "succeeded" if tx["failed"] == 0 else "completed with failures"
    logger.info(
        "Thought extraction backfill %s: "
        f"{tx['extracted']} enriched, {tx['failed']} failed, "
        f"{tx['pending_after']} still pending "
        f"(of {tx['pending_before']} before this run).",
        tx_status,
    )

    tj = await brain.backfill_thought_judgments()
    tj_status = "succeeded" if tj["failed"] == 0 else "completed with failures"
    logger.info(
        "Thought judgment backfill %s: "
        f"{tj['judged']} judged, {tj['failed']} failed, "
        f"{tj['pending_after']} still pending "
        f"(of {tj['pending_before']} before this run).",
        tj_status,
    )
    logger.info("Thought straggler sweep complete.")


def _sweep_interval_seconds() -> float:
    minutes = float(brain.EXTRACTION_CONFIG.get("sweep_interval_minutes", 5))
    return minutes * 60


async def _run_loop() -> None:
    run_at = brain.EXTRACTION_CONFIG.get("run_at", "02:00")
    while True:
        wait_seconds = _seconds_until(run_at)
        logger.info("Next maintenance run at %s; sleeping %.0fs.", run_at, wait_seconds)
        await asyncio.sleep(wait_seconds)
        try:
            await _run_once()
        except Exception as exc:
            logger.exception("Maintenance failed: %s", exc)
        await asyncio.sleep(60)


async def _sweep_loop() -> None:
    interval = _sweep_interval_seconds()
    while True:
        try:
            await _run_sweep()
        except Exception as exc:
            logger.exception("Thought straggler sweep failed: %s", exc)
        await asyncio.sleep(interval)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Run nightly consolidation every day at extraction.run_at.",
    )
    parser.add_argument(
        "--sweep",
        action="store_true",
        help="Run the fast thought-straggler backfills once and exit.",
    )
    parser.add_argument(
        "--sweep-loop",
        action="store_true",
        help="Run the thought-straggler sweep every extraction.sweep_interval_minutes.",
    )
    args = parser.parse_args()

    if args.sweep_loop:
        asyncio.run(_sweep_loop())
    elif args.sweep:
        asyncio.run(_run_sweep())
    elif args.loop:
        asyncio.run(_run_loop())
    else:
        asyncio.run(_run_once())


if __name__ == "__main__":
    main()

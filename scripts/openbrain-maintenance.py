#!/usr/bin/env python3
"""Run scheduled openbrain thought consolidation."""

import argparse
import asyncio
from datetime import datetime, timedelta
import logging
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openbrain import brain

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


async def _run_once() -> None:
    logger.info("Maintenance run started.")
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
    logger.info("Maintenance run complete.")


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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Run every day at extraction.run_at from config.yaml.",
    )
    args = parser.parse_args()

    if args.loop:
        asyncio.run(_run_loop())
    else:
        asyncio.run(_run_once())


if __name__ == "__main__":
    main()

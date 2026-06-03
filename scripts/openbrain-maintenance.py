#!/usr/bin/env python3
"""Run scheduled Open Brain thought consolidation."""

import argparse
import asyncio
from datetime import datetime, timedelta

from openbrain import brain


def _seconds_until(run_at: str) -> float:
    hour, minute = [int(part) for part in run_at.split(":", maxsplit=1)]
    now = datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def _run_once() -> None:
    result = await brain.run_maintenance_once()
    print(
        "Maintenance complete: "
        f"{result['processed']} processed, "
        f"{result['syntheses']} synthesis thought(s) written."
    )


async def _run_loop() -> None:
    run_at = brain.EXTRACTION_CONFIG.get("run_at", "02:00")
    while True:
        wait_seconds = _seconds_until(run_at)
        print(f"Next maintenance run at {run_at}; sleeping {wait_seconds:.0f}s.")
        await asyncio.sleep(wait_seconds)
        try:
            await _run_once()
        except Exception as exc:
            print(f"Maintenance failed: {exc}")
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

"""Fire a single analytics report immediately — debug / smoke-test helper.

    python -m birdnest_ai.tools.analytics_once [--hours N]

Reads the current SQLite state, computes a report for the last N hours
(default = ANALYTICS_REPORT_HOURS from settings), and posts it to the
analytics Discord webhook. Exits 0 on success, 1 on failure.

Useful for verifying the analytics channel wiring without waiting for the
next scheduled run, or for generating ad-hoc reports.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time

from birdnest_ai.analytics import compute_report
from birdnest_ai.config import get_settings
from birdnest_ai.notifier import Notifier
from birdnest_ai.state import StateStore


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="birdnest_ai.tools.analytics_once",
        description="Fire a single analytics report immediately.",
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=None,
        help="Window size in hours (default: ANALYTICS_REPORT_HOURS from .env).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    settings = get_settings()
    if not settings.discord_analytics_webhook_url:
        print(
            "✗ DISCORD_ANALYTICS_WEBHOOK_URL is unset in .env — "
            "cannot post. Edit .env and try again.",
            file=sys.stderr,
        )
        return 1

    hours = args.hours if args.hours is not None else settings.analytics_report_hours

    store = StateStore(settings.state_db_path)
    notifier = Notifier(
        webhook_url=settings.discord_analytics_webhook_url,
        camera_name=settings.blink_camera_name or "(camera name unset)",
    )
    try:
        # Run the compute synchronously (we're a one-shot CLI, not the
        # long-running service — no need for thread isolation here).
        report = compute_report(
            store, time.time(), hours, settings.analyzer_model,
        )
        print(f"Computed report: window={hours}h, snaps={report['system']['snaps_taken']}, "
              f"trips={report['trips']['trip_count']}, "
              f"alerts={report['alerts']['total']}")
        ok = await notifier.send_analytics_report(report)
    finally:
        await notifier.close()
        store.close()

    if ok:
        print("✓ Analytics report posted to Discord")
        return 0
    print("✗ Analytics webhook POST failed — see logs above")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

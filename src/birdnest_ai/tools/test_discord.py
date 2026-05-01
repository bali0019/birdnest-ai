"""Standalone Discord webhook smoke-test.

    python -m birdnest_ai.tools.test_discord

Exits 0 on HTTP 204 from Discord, 1 otherwise.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from birdnest_ai.config import get_settings
from birdnest_ai.notifier import Notifier


async def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = get_settings()
    if not settings.discord_webhook_url:
        print("✗ Discord webhook failed: DISCORD_WEBHOOK_URL is empty in .env")
        return 1

    notifier = Notifier(
        webhook_url=settings.discord_webhook_url,
        camera_name=settings.blink_camera_name or "(camera name unset)",
    )
    try:
        ok = await notifier.send_test()
    finally:
        await notifier.close()

    if ok:
        print("✓ Discord webhook OK")
        return 0
    print("✗ Discord webhook failed: see logs above for HTTP status / body")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

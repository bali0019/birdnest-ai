"""Pause helper for the snap loop.

Usage:
    python -m cardinal_nest_monitor.tools.pause [minutes]   # pause N minutes
    python -m cardinal_nest_monitor.tools.pause --clear     # resume now

The snap loop checks `settings.pause_lock_path.exists()` every cycle and idles
while the file is present. Use this before walking near the nest to swap
batteries so your own movement doesn't trigger an alert.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

from cardinal_nest_monitor.config import get_settings


def is_paused() -> bool:
    """True if a non-expired pause lock exists at settings.pause_lock_path."""
    settings = get_settings()
    path = settings.pause_lock_path
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text())
        expires = float(data.get("expires_ts", 0))
    except (ValueError, OSError):
        # Malformed lock file → safer default = paused.
        return True
    return time.time() < expires


def _write_lock(minutes: int) -> Path:
    settings = get_settings()
    now = time.time()
    expires = now + minutes * 60
    payload = {"created_ts": now, "expires_ts": expires}
    settings.pause_lock_path.parent.mkdir(parents=True, exist_ok=True)
    settings.pause_lock_path.write_text(json.dumps(payload, indent=2))
    return settings.pause_lock_path


def _clear_lock() -> bool:
    settings = get_settings()
    if settings.pause_lock_path.exists():
        settings.pause_lock_path.unlink()
        return True
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cardinal_nest_monitor.tools.pause",
        description="Pause (or resume) the cardinal-nest-monitor snap loop.",
    )
    parser.add_argument(
        "minutes", nargs="?", type=int, default=10,
        help="Duration in minutes (default: 10). Ignored with --clear.",
    )
    parser.add_argument("--clear", action="store_true", help="Remove any active pause lock.")
    args = parser.parse_args(argv)

    if args.clear:
        if _clear_lock():
            print("Resumed.")
        else:
            print("No pause lock present; nothing to clear.")
        return 0

    if args.minutes <= 0:
        print("ERROR: minutes must be > 0", file=sys.stderr)
        return 2

    path = _write_lock(args.minutes)
    expires_local = datetime.fromtimestamp(time.time() + args.minutes * 60)
    print(
        f"Paused snap loop until {expires_local.strftime('%Y-%m-%d %H:%M:%S')}. "
        f"Delete {path} to resume early."
    )
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    sys.exit(main())

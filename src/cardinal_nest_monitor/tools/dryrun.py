"""Local-image pipeline test tool — for prompt tuning without using Blink.

    python -m cardinal_nest_monitor.tools.dryrun --image PATH [--escalate]

Runs the prefilter on the given JPEG; if it escalates (or --escalate is set),
runs the analyzer too. Prints both JSON results.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path


async def _run(image_path: Path, force_escalate: bool) -> int:
    # Lazy imports so --help works even if these modules' deps aren't installed.
    from cardinal_nest_monitor import analyzer as analyzer_mod
    from cardinal_nest_monitor import prefilter as prefilter_mod

    if not image_path.exists():
        print(f"ERROR: image not found: {image_path}", file=sys.stderr)
        return 2
    if not image_path.is_file():
        print(f"ERROR: not a file: {image_path}", file=sys.stderr)
        return 2

    jpeg = image_path.read_bytes()
    if not jpeg:
        print(f"ERROR: empty file: {image_path}", file=sys.stderr)
        return 2

    # ── Tier 1 ───────────────────────────────────────────────────────
    pre = await prefilter_mod.prefilter(jpeg)
    print("── Prefilter ──")
    print(pre.model_dump_json(indent=2))

    should_escalate = force_escalate or pre.should_escalate
    if not should_escalate:
        print("Prefilter dropped — no escalation.")
        return 0

    # ── Tier 2 ───────────────────────────────────────────────────────
    if force_escalate and not pre.should_escalate:
        print("── Forcing analyzer despite prefilter drop (--escalate) ──")
    else:
        print("── Analyzer ──")
    obs = await analyzer_mod.analyze(jpeg)
    print(obs.model_dump_json(indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cardinal_nest_monitor.tools.dryrun",
        description="Pipe a local JPEG through the prefilter (and analyzer) for prompt tuning.",
    )
    parser.add_argument("--image", required=True, type=Path, help="Path to a JPEG image.")
    parser.add_argument(
        "--escalate", action="store_true",
        help="Force the analyzer to run regardless of prefilter result.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return asyncio.run(_run(args.image, args.escalate))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

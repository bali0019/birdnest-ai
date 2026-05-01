"""Module entrypoint: `python -m birdnest_ai`.

Parses `--role={downloader,analyzer,combined}` BEFORE importing the main
module (and therefore before `birdnest_ai.config` is loaded), so
`get_settings()` picks up the value via the `ROLE` env var. All other
flags (e.g. `--auth-only`) are passed through unchanged to the downstream
argparse in `birdnest_ai.main.main`.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys


_ROLE_CHOICES = ["downloader", "analyzer", "combined"]


def _split_role_arg(argv: list[str]) -> tuple[str, list[str]]:
    """Extract --role from argv, leaving everything else intact.

    Uses a dedicated argparse parser with `parse_known_args` so unknown
    flags (including --auth-only, -h/--help) are forwarded untouched to
    the downstream parser in `birdnest_ai.main.main`.

    Returns (role, remaining_argv).
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--role",
        choices=_ROLE_CHOICES,
        default="combined",
    )
    ns, remaining = parser.parse_known_args(argv)
    return ns.role, remaining


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    role, remaining = _split_role_arg(argv)

    # Propagate to settings BEFORE any import of birdnest_ai.main
    # (which pulls in birdnest_ai.config at module load time).
    os.environ["ROLE"] = role

    if role == "combined":
        # Byte-identical to prior behavior: delegate to the existing
        # argparse entrypoint in main.py with --role stripped from argv.
        from birdnest_ai.main import main as _main
        return _main(remaining)

    # Deferred imports so settings sees ROLE in the env before config loads.
    from birdnest_ai import main as _main_mod

    try:
        if role == "downloader":
            coro_fn = _main_mod.run_downloader  # type: ignore[attr-defined]
        elif role == "analyzer":
            coro_fn = _main_mod.run_analyzer  # type: ignore[attr-defined]
        else:  # pragma: no cover - argparse choices guard this
            raise AssertionError(f"unreachable role: {role!r}")
    except AttributeError as exc:
        # The split-role functions are owned by another agent / a later
        # wave; surface cleanly rather than silently falling back.
        raise ImportError(
            f"birdnest_ai.main.run_{role}() is not available yet; "
            f"the split-role entrypoint has not been implemented."
        ) from exc

    try:
        return asyncio.run(coro_fn())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

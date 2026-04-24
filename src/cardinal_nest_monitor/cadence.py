"""Shared cadence policy for snap-interval selection.

One pure function, ``compute_snap_interval``, used by BOTH the split-mode
downloader (``downloader_loop.py``) and the combined-mode pipeline
(``main.py``). Centralising the decision tree keeps role parity: the
documented §21 precedence — quiet > burst > absence > default — is enforced
identically regardless of which process is driving snap_loop.

Why not leave this inline in each caller?

- Before this module existed, combined mode's ``get_interval`` (main.py)
  branched only on quiet/absence/default and never checked
  ``absence_started_ts`` or ``burst_duration_seconds``. The burst cadence
  documented in CLAUDE.md §21 was silently dead in combined mode — a latent
  rollback / dev-parity bug.
- Split mode already had burst branching in ``downloader_loop.py`` but the
  two code paths could drift. Shared helper → single source of truth.

See ``reactive-tickling-rose.md`` for the full context and the mid-wait
re-evaluation that makes burst cadence actually engage in ``snap_loop``.
"""

from __future__ import annotations

from datetime import datetime

from cardinal_nest_monitor.config import Settings
from cardinal_nest_monitor.schema import NestState


def compute_snap_interval(
    settings: Settings,
    state: NestState,
    now_ts: float,
) -> tuple[int, str]:
    """Return ``(interval_seconds, label)`` per §21 precedence.

    Precedence (highest → lowest):
      1. ``quiet``   — inside configured quiet-hours window
      2. ``burst``   — ``state.in_absence`` AND within ``burst_duration_seconds``
                       of ``absence_started_ts``
      3. ``absence`` — ``state.in_absence`` but burst window has elapsed
      4. ``default`` — baseline snap_interval_seconds

    ``now_ts`` is wall-clock (``time.time()``). It's used for both the
    quiet-hours check (converted via ``datetime.fromtimestamp``) and the
    burst-window math against ``absence_started_ts``. Callers that need a
    monotonic clock for deadline math (e.g. the snap-loop wait helper)
    should keep ``time.monotonic()`` for that and pass wall-clock here.
    """
    now_time = datetime.fromtimestamp(now_ts).time()
    if settings.in_quiet_hours(now_time):
        return settings.quiet_snap_interval_seconds, "quiet"
    if state.in_absence:
        if (
            state.absence_started_ts is not None
            and (now_ts - state.absence_started_ts)
            < settings.burst_duration_seconds
        ):
            return settings.burst_snap_interval_seconds, "burst"
        return settings.absence_snap_interval_seconds, "absence"
    return settings.snap_interval_seconds, "default"

"""Shared cadence policy for snap-interval selection.

Two responsibilities:

* ``compute_snap_interval`` — pure function that picks the next snap
  interval per §21 precedence. Used by BOTH the split-mode downloader
  (``downloader_loop.py``) and the combined-mode pipeline (``main.py``).
  Centralising the decision tree keeps role parity: the documented §21
  precedence (``quiet > session-burst > burst > absence > default``) is
  enforced identically regardless of which process is driving snap_loop.

* ``arm_session_burst_if_absent`` — one-shot async helper that
  **evidence-gates** a restart-local session-burst window. Runs at
  process startup; waits for the first post-startup observation to land
  (proof the analyzer just processed a post-restart snap); if that
  observation leaves ``state.in_absence=True``, arms a 3-min burst
  window so we catch up on snap density without overwriting persisted
  ``absence_started_ts`` ground truth. See CLAUDE.md §21.

Why `session-burst` is separate from calendar-anchored `burst`:

  ``absence_started_ts`` records when mom actually left (biological
  ground truth). Calendar burst fires for the first N seconds after
  that timestamp regardless of process lifecycle. If the downloader
  restarts 10 min into an active absence, the calendar-burst window has
  biologically closed (peak raid risk was 0-3 min after departure).
  But operationally we have no fresh evidence of the current scene —
  the analyzer may have been down too. A restart-local session-burst
  catches up on snap density for the first 3 min after we have a fresh
  post-restart snap confirming she's still gone, then relaxes. It does
  NOT overwrite ``absence_started_ts`` — both timers coexist.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

from cardinal_nest_monitor.config import Settings
from cardinal_nest_monitor.schema import NestState

log = logging.getLogger(__name__)


def compute_snap_interval(
    settings: Settings,
    state: NestState,
    now_ts: float,
    *,
    session_burst_until_monotonic: float | None = None,
    now_monotonic: float | None = None,
) -> tuple[int, str]:
    """Return ``(interval_seconds, label)`` per §21 precedence.

    Precedence (highest → lowest):
      1. ``quiet``         — inside configured quiet-hours window
      2. ``session-burst`` — ``in_absence`` AND a per-process restart
                             window is armed and un-expired (see
                             ``arm_session_burst_if_absent``)
      3. ``burst``         — ``in_absence`` AND within
                             ``burst_duration_seconds`` of
                             ``state.absence_started_ts``
      4. ``absence``       — ``in_absence`` but burst windows have
                             elapsed
      5. ``default``       — baseline snap_interval_seconds

    ``now_ts`` is wall-clock (``time.time()``) and drives the
    quiet-hours check + the calendar-burst arithmetic against
    ``absence_started_ts``. ``now_monotonic`` is an optional
    ``time.monotonic()`` sample used ONLY for the session-burst
    deadline. Wall clock must not drive the session-burst deadline —
    NTP adjustments could retire or extend the window unpredictably.

    ``session_burst_until_monotonic`` is None (not armed) in:
    - test harnesses that don't exercise the restart catch-up
    - the first ~5 s of process startup before the arming task runs
    - the common case where the fresh post-startup snap shows mom on
      nest (arming task decided not to arm)
    - after the window has expired or been cleared on mom-return

    In all of those cases the decision falls through to the normal
    calendar-anchored precedence. Session-burst is purely additive: it
    never suppresses burst/absence, it only upgrades an in-progress
    absence to 30 s cadence for a bounded window after restart.
    """
    now_time = datetime.fromtimestamp(now_ts).time()
    if settings.in_quiet_hours(now_time):
        return settings.quiet_snap_interval_seconds, "quiet"
    if (
        state.in_absence
        and session_burst_until_monotonic is not None
        and now_monotonic is not None
        and now_monotonic < session_burst_until_monotonic
    ):
        return settings.burst_snap_interval_seconds, "session-burst"
    if state.in_absence:
        if (
            state.absence_started_ts is not None
            and (now_ts - state.absence_started_ts)
            < settings.burst_duration_seconds
        ):
            return settings.burst_snap_interval_seconds, "burst"
        return settings.absence_snap_interval_seconds, "absence"
    return settings.snap_interval_seconds, "default"


async def arm_session_burst_if_absent(
    store,
    settings: Settings,
    startup_wall_ts: float,
    session_state: dict[str, float | None],
    *,
    poll_interval: float = 5.0,
    max_wait_seconds: float = 60.0,
) -> None:
    """One-shot: arm the session-burst window if a post-startup snap
    confirms mom is still absent.

    Polls ``MAX(ts) FROM observations`` every ``poll_interval`` seconds
    until either (a) an observation with ``ts > startup_wall_ts`` lands
    — meaning the analyzer has processed a post-restart snap — or (b)
    ``max_wait_seconds`` elapses without one (analyzer likely down).

    On (a): read ``state.in_absence``. If True, write a monotonic
    deadline ``startup_wall_ts + burst_duration_seconds`` into
    ``session_state["until_monotonic"]`` so ``compute_snap_interval``
    will tighten to burst cadence for the next N seconds. If False
    (fresh evidence shows mom on nest), do NOT arm — no burst needed.

    On (b): skip; no session-burst. We don't want to fire catch-up
    burst based on stale pre-restart state — the whole point is
    evidence-gating on a fresh observation.

    Runs exactly once per process lifetime. After it exits, either:
    - ``session_state["until_monotonic"]`` is a future monotonic value
      (armed) — callers should also clear it when ``in_absence`` flips
      False so a subsequent absence doesn't re-fire session-burst
      without fresh evidence
    - or it's None (not armed) — the common case; no further action
      needed

    Uses ``store._ro_conn`` (analytics-thread RO connection, see §30)
    because it runs on the event loop and the writer connection is
    technically shared with the analyzer's ``record()`` calls in
    combined mode. Read-only access is a better shape here.
    """
    deadline_monotonic = time.monotonic() + max_wait_seconds
    while time.monotonic() < deadline_monotonic:
        try:
            cur = store._ro_conn.execute(
                "SELECT MAX(ts) AS latest FROM observations"
            )
            row = cur.fetchone()
            latest = row["latest"] if row is not None else None
        except Exception:
            log.exception("session-burst arming: observation query failed")
            return
        if latest is not None and float(latest) > startup_wall_ts:
            try:
                nest_state = store.get_state()
            except Exception:
                log.exception("session-burst arming: state read failed")
                return
            if nest_state.in_absence:
                session_state["until_monotonic"] = (
                    time.monotonic() + settings.burst_duration_seconds
                )
                log.info(
                    "session-burst armed for %ds (fresh post-restart snap at "
                    "%s confirms in_absence=True)",
                    settings.burst_duration_seconds,
                    datetime.fromtimestamp(float(latest)).isoformat(),
                )
            else:
                log.info(
                    "session-burst not armed: fresh post-restart snap at "
                    "%s shows mom on nest",
                    datetime.fromtimestamp(float(latest)).isoformat(),
                )
            return
        await asyncio.sleep(poll_interval)
    log.info(
        "session-burst arming: no post-startup observation landed within "
        "%.0fs; skipping (analyzer may be down)",
        max_wait_seconds,
    )

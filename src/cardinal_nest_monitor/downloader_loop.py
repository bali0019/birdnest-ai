"""Downloader-only service entrypoint.

Part of the two-service decoupled architecture (2026-04-15). This module owns
the Blink side of the pipeline: connect to Blink, drive snap_loop + motion_loop,
and write every fresh JPEG into the on-disk spool (``{spool_dir}/pending/``)
via :func:`cardinal_nest_monitor.spool.write_snap`.

It does NOT touch the analyzer, verifier, events engine, or any analytics code
— that lives in the analyzer service, which is a completely separate process
that drains the spool. The ONLY Discord traffic this module produces is its
own lifecycle (startup / shutdown / watchdog) embeds on the urgent channel,
so the user always knows whether the camera producer is alive.

Key design properties (don't regress):

* **Read-only StateStore**. We open a StateStore for cadence decisions only
  (``state.in_absence`` drives Pattern A's tight 60s absence cadence). WAL
  mode is on in state.py so a read-only connection here is safe alongside
  the analyzer's writes.

* **Staleness guard**. If the state DB hasn't been updated in more than
  10 minutes (analyzer is down / wedged / flag-rolled), the cadence
  callback returns a constant 60s. Rationale: during a prolonged analyzer
  outage the ``in_absence`` flag is frozen to whatever it was when the
  analyzer died. Defaulting to 60s is the safe compromise — we keep snaps
  flowing at a rate the spool can absorb without building an intractable
  backlog, and we don't burn battery at the 5-minute normal rate when we
  have no idea whether mom is on the nest. See CLAUDE.md §19 for the
  "default to the safe side when signal is stale" principle.

* **Per-task isolation**. Each top-level task (snap_loop, motion_loop,
  watchdog) runs under its own asyncio.create_task so one hung task can't
  cascade into freezing the others — same invariant as main.py §17 of
  CLAUDE.md.

* **Bounded network timeouts everywhere** (CLAUDE.md §19). The spool write
  is local disk I/O and therefore not subject to network timeouts, but
  every Discord embed (startup / shutdown / watchdog) is wrapped in
  ``asyncio.wait_for`` so a slow Discord can never hang the loop.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import time
from datetime import datetime
from typing import Any

from cardinal_nest_monitor import spool
from cardinal_nest_monitor.blink_client import (
    connect,
    motion_loop,
    snap_loop,
)
from cardinal_nest_monitor.cadence import (
    arm_session_burst_if_absent,
    compute_snap_interval,
)
from cardinal_nest_monitor.config import get_settings
from cardinal_nest_monitor.notifier import Notifier
from cardinal_nest_monitor.state import StateStore

log = logging.getLogger(__name__)


# ── Cadence policy ────────────────────────────────────────────────────────
# Stale-state safe fallback: when the analyzer hasn't landed an observation
# for > _STATE_STALENESS_THRESHOLD_S, get_interval() ignores the frozen
# `in_absence` flag and returns _STALE_STATE_FALLBACK_S. 60s is chosen to
# match the absence-interval floor — aggressive enough that we don't miss a
# fast predator, conservative enough that battery / disk don't suffer if the
# analyzer is down for hours.
_STATE_STALENESS_THRESHOLD_S: float = 600.0   # 10 minutes
_STALE_STATE_FALLBACK_S: int = 60

# Watchdog thresholds. Downloader's watchdog monitors spool-write cadence
# (not analyzer health). If no snap has been written to the spool for
# > _WATCHDOG_STALL_S during active hours, something is wrong with either
# Blink connectivity or our own task loop.
_WATCHDOG_POLL_S: float = 60.0
_WATCHDOG_STALL_S: float = 900.0            # 15 min
_WATCHDOG_REPOST_S: float = 1800.0           # 30 min — rate-limit distress pings

# Timeouts for the three distinct Discord embeds this module owns.
_STARTUP_EMBED_TIMEOUT_S: float = 15.0
_SHUTDOWN_EMBED_TIMEOUT_S: float = 10.0
_WATCHDOG_EMBED_TIMEOUT_S: float = 15.0


def _setup_logging() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )


def _latest_state_ts(store: StateStore) -> float | None:
    """Return the wall-clock ts of the most recent observation row, or None.

    The state row itself doesn't carry a last_update_ts column, so we use
    ``MAX(ts) FROM observations`` as the freshness proxy. The analyzer
    writes into observations on every successful snap (regardless of
    whether an alert fires), so a fresh observations row is a reliable
    heartbeat. None means no observations exist yet (cold boot before the
    analyzer has processed anything) — caller treats that as "stale".
    """
    try:
        cur = store._conn.execute("SELECT MAX(ts) AS latest FROM observations")
        row = cur.fetchone()
    except Exception:
        log.exception("downloader: SELECT MAX(ts) failed; treating state as stale")
        return None
    if row is None:
        return None
    latest = row["latest"]
    return float(latest) if latest is not None else None


async def _downloader_watchdog(
    last_spool_write_ts: dict[str, float],
    notifier: Notifier,
) -> None:
    """Dead-man's-switch: ping Discord if no spool write has happened in 15+ min.

    Distinct from main.py's watchdog (which monitors Pipeline.on_image
    completion). Here we monitor the spool-write side only — if this fires,
    either the camera is unreachable or our snap loop has wedged.
    """
    settings = get_settings()
    last_warning_ts: float = 0.0
    log.info("downloader watchdog started (poll %ds)", int(_WATCHDOG_POLL_S))
    while True:
        await asyncio.sleep(_WATCHDOG_POLL_S)
        try:
            now_ts = time.time()
            since = now_ts - last_spool_write_ts["value"]
            now_time = datetime.now().time()
            # Dynamic threshold: during quiet hours the cadence is 30 min,
            # so a 15-min fixed threshold would false-alarm every cycle.
            # Also covers the quiet→active boundary: if the last spool write
            # was during quiet hours, allow one quiet interval + buffer.
            last_write_time = datetime.fromtimestamp(
                last_spool_write_ts["value"]
            ).time()
            threshold = _WATCHDOG_STALL_S
            if settings.in_quiet_hours(now_time) or settings.in_quiet_hours(last_write_time):
                threshold = max(threshold, settings.quiet_snap_interval_seconds + 300)
            if since <= threshold:
                continue
            if not settings.in_active_hours(now_time):
                continue
            if (now_ts - last_warning_ts) < _WATCHDOG_REPOST_S:
                continue
            log.error(
                "WATCHDOG (downloader): no spool write for %ds (>15min stall "
                "during active hours)", int(since),
            )
            try:
                await asyncio.wait_for(
                    notifier.send_system_message(
                        title="🚨 WATCHDOG: downloader not writing snaps",
                        body=(
                            f"No spool write for {int(since // 60)} min. "
                            "Camera may be unreachable or snap_loop stuck. "
                            "Check downloader logs."
                        ),
                        color=0xFF0000,
                    ),
                    timeout=_WATCHDOG_EMBED_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                log.warning(
                    "watchdog: distress embed timed out after %.0fs",
                    _WATCHDOG_EMBED_TIMEOUT_S,
                )
            except Exception:
                log.exception("watchdog: distress embed raised")
            last_warning_ts = now_ts
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("downloader watchdog iteration raised (non-fatal)")


async def run_downloader_service() -> int:
    """Top-level coroutine for the downloader-only service.

    Lifecycle:
      1. Connect to Blink (via blink_client.connect, which handles persisted
         creds + 2FA retry semantics).
      2. Post the dedicated 🟢 "downloader online" embed on the urgent channel.
      3. Launch snap_loop (with spool-writing on_snap callback), motion_loop,
         and the downloader watchdog.
      4. Wait for SIGTERM/SIGINT.
      5. Cancel all tasks, post 🔴 "downloader offline", close sessions.
    """
    _setup_logging()
    settings = get_settings()
    settings.ensure_dirs()

    # Open Blink first — if this fails (stale creds / network), we want the
    # process to exit before we post a misleading "online" embed.
    blink = await connect(prompt_2fa=False)

    # StateStore opened here is used read-only (for cadence decisions only).
    # WAL mode is already on (see state.py constructor), so it coexists
    # safely with the analyzer service's writer connection.
    store = StateStore(settings.state_db_path)

    notifier = Notifier(
        webhook_url=settings.discord_webhook_url,
        camera_name=settings.blink_camera_name,
    )

    # Spool-write heartbeat used by the watchdog. Initialised to startup
    # time so the 15-min threshold isn't already exceeded at boot.
    last_spool_write_ts: dict[str, float] = {"value": time.time()}

    async def on_snap(
        jpeg: bytes,
        meta: dict[str, Any],
        state_updated: asyncio.Event | None = None,
    ) -> None:
        """Write one snap to the spool. Called by blink_client.snap_loop.

        Signature matches snap_loop's existing
        ``on_image(jpeg, meta, state_updated)`` contract verbatim. The
        downloader has nothing to wait for (no analyzer-side state update in
        this process) so we immediately SET the event. Without this, the
        snap_loop's 10s wait would expire on EVERY cycle, adding a 10s
        penalty per snap — which quietly slows the real burst/absence
        cadence below the configured values. (Codex P2.)

        Contract: on any OSError, log and drop the snap rather than raise.
        The next scheduled snap will take its place; the spool is
        transient-by-design.
        """
        try:
            spool.write_snap(jpeg, meta, settings.spool_dir)
            last_spool_write_ts["value"] = time.time()
        except OSError:
            log.exception("spool write failed; snap dropped")
        except Exception:
            # Anything else (ValueError for missing ts, etc.) is a bug in
            # the caller — still don't kill the loop over it.
            log.exception("spool write raised unexpectedly; snap dropped")
        finally:
            # Always fire, even on error: we're not going to produce a state
            # update either way, so there's nothing meaningful to wait for.
            if state_updated is not None:
                state_updated.set()

    async def _on_clip(cam, clip: dict[str, Any]) -> None:
        """No-op for motion-triggered clips in downloader mode.

        The fresh snap that motion_loop triggers via snap_now.set() lands
        in the spool via on_snap just like any scheduled snap. The MP4
        itself is not archived here (analyzer service can opt into clip
        archival if it wants; today nobody does).
        """
        ts = clip.get("time", "<unknown>")
        log.info("motion clip detected at %s; snap_now already signalled", ts)

    # Cadence callback with staleness guard. Called by snap_loop every
    # cycle before it waits for the next tick.
    _last_interval_logged: dict[str, int] = {"value": 0}

    # Session-burst state — populated by arm_session_burst_if_absent when a
    # post-restart snap confirms in_absence=True. See CLAUDE.md §21 for
    # the rationale (evidence-gated restart-local burst, distinct from the
    # calendar-anchored burst tied to absence_started_ts).
    _session_burst_state: dict[str, float | None] = {"until_monotonic": None}

    def get_interval() -> int:
        now_ts = time.time()
        # Quiet hours win regardless of state freshness. Short-circuit
        # here so we don't even probe the DB during quiet-hours cycles.
        if settings.in_quiet_hours(datetime.fromtimestamp(now_ts).time()):
            interval = settings.quiet_snap_interval_seconds
            label = "quiet"
        else:
            # Stale-state guard BEFORE delegating: if the analyzer hasn't
            # written an observation in > 10 min, its in_absence /
            # absence_started_ts are frozen on old truth — trusting them
            # would keep us on a stale cadence for hours. Fall back to a
            # safe constant instead.
            latest_ts = _latest_state_ts(store)
            if (
                latest_ts is None
                or (now_ts - latest_ts) > _STATE_STALENESS_THRESHOLD_S
            ):
                interval = _STALE_STATE_FALLBACK_S
                label = "stale-state-fallback"
            else:
                try:
                    state = store.get_state()
                except Exception:
                    log.exception(
                        "downloader: store.get_state() raised; using default interval"
                    )
                    interval = settings.snap_interval_seconds
                    label = "default"
                else:
                    # Clear session-burst on mom-return — a subsequent
                    # absence should use calendar-anchored burst with the
                    # NEW absence_started_ts, not this stale session
                    # window. Keeps arming strictly one-shot per absence
                    # that was active at startup.
                    if (
                        not state.in_absence
                        and _session_burst_state["until_monotonic"] is not None
                    ):
                        log.info(
                            "session-burst cleared (mom back on nest)"
                        )
                        _session_burst_state["until_monotonic"] = None
                    interval, label = compute_snap_interval(
                        settings, state, now_ts,
                        session_burst_until_monotonic=(
                            _session_burst_state["until_monotonic"]
                        ),
                        now_monotonic=time.monotonic(),
                    )
        if _last_interval_logged["value"] != interval:
            log.info(
                "cadence: %ds → %ds (%s)",
                _last_interval_logged["value"], interval, label,
            )
            _last_interval_logged["value"] = interval
        return interval

    # Post startup embed. Distinct title so the user can tell which
    # service just came up (vs. the combined process's "Cardinal Nest
    # Monitor online" from main.py).
    try:
        await asyncio.wait_for(
            notifier.send_system_message(
                title="🟢 Cardinal Nest Monitor downloader online",
                body=(
                    f"Downloader (Blink → spool) up. "
                    f"Camera: {settings.blink_camera_name}. "
                    f"Spool: {settings.spool_dir}. "
                    f"Snap cadence: {settings.snap_interval_seconds}s default / "
                    f"{settings.absence_snap_interval_seconds}s absence / "
                    f"{settings.quiet_snap_interval_seconds}s quiet."
                ),
                color=0x32CD32,
            ),
            timeout=_STARTUP_EMBED_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        log.warning(
            "downloader startup embed timed out after %.0fs (continuing)",
            _STARTUP_EMBED_TIMEOUT_S,
        )
    except Exception:
        log.exception("downloader startup embed failed (non-fatal)")

    snap_now = asyncio.Event()

    # Captured BEFORE snap_loop is launched so the first post-startup snap's
    # observation.ts is guaranteed > startup_wall_ts. Consumed by the
    # session-burst arming task to gate the restart-local burst window on
    # a fresh observation (CLAUDE.md §21).
    startup_wall_ts = time.time()

    tasks = [
        asyncio.create_task(
            motion_loop(blink, snap_now, _on_clip), name="motion_loop"
        ),
        asyncio.create_task(
            snap_loop(blink, snap_now, on_snap, get_interval), name="snap_loop"
        ),
        asyncio.create_task(
            _downloader_watchdog(last_spool_write_ts, notifier),
            name="downloader_watchdog",
        ),
        asyncio.create_task(
            arm_session_burst_if_absent(
                store, settings, startup_wall_ts, _session_burst_state,
            ),
            name="session_burst_arming",
        ),
    ]

    stop_event = asyncio.Event()

    def _stop(signame: str) -> None:
        log.info("downloader: received %s; shutting down", signame)
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop, sig.name)
        except NotImplementedError:
            pass  # non-POSIX platforms

    try:
        await stop_event.wait()
    finally:
        log.info("downloader: cancelling tasks")
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

        log.info("downloader: sending offline embed")
        try:
            ok = await asyncio.wait_for(
                notifier.send_system_message(
                    title="🔴 Cardinal Nest Monitor downloader offline",
                    body="Downloader shutting down (Blink → spool).",
                    color=0x808080,
                ),
                timeout=_SHUTDOWN_EMBED_TIMEOUT_S,
            )
            log.info("downloader: offline embed send result: %s", ok)
        except asyncio.TimeoutError:
            log.warning(
                "downloader: offline embed timed out after %.0fs",
                _SHUTDOWN_EMBED_TIMEOUT_S,
            )
        except Exception:
            log.exception("downloader: offline embed raised")

        log.info("downloader: closing notifier session")
        try:
            await notifier.close()
        except Exception:
            log.exception("downloader: notifier close raised (non-fatal)")

        log.info("downloader: closing state store")
        try:
            store.close()
        except Exception:
            log.exception("downloader: state store close raised (non-fatal)")

        log.info("downloader: closing blink session")
        try:
            await blink.auth.session.close()
        except Exception:
            log.exception("downloader: blink session close raised (non-fatal)")

        log.info("downloader: shutdown complete")

    return 0


async def _auth_only() -> int:
    """Mirror main.py's --auth-only behavior: connect interactively, save
    creds, exit cleanly. No loops started, no spool writes, no Discord
    embeds.
    """
    _setup_logging()
    log.info(
        "downloader --auth-only: will prompt for 2FA PIN if needed."
    )
    blink = await connect(prompt_2fa=True)
    log.info("auth complete; cameras discovered: %s", list(blink.cameras))
    await blink.auth.session.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cardinal_nest_monitor.downloader_loop",
        description=(
            "Downloader-only service: drives Blink snaps into the on-disk "
            "spool for the analyzer service to consume."
        ),
    )
    parser.add_argument(
        "--auth-only",
        action="store_true",
        help=(
            "Run interactive Blink 2FA flow once, then exit "
            "(writes blink_credentials.json). Same semantics as main.py."
        ),
    )
    args = parser.parse_args(argv)

    try:
        if args.auth_only:
            return asyncio.run(_auth_only())
        return asyncio.run(run_downloader_service())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

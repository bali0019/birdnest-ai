"""Analyzer-only service entrypoint for the decoupled two-process architecture.

The downloader process owns Blink and writes raw JPEGs + meta sidecars into
``{spool_dir}/pending/``. This analyzer process polls the spool, claims the
newest pending snap, runs the full analysis pipeline (analyzer → verifier →
state → notifier → evidence), and deletes the claim.

Responsibilities:
    * Spool recovery on startup (``recover_stranded`` + ``drop_stale``).
    * 1 Hz poll of ``spool.claim_next``; route each claim to LIVE or BACKFILL.
    * Run all analyzer-side schedulers: heartbeat, watchdog, feed_worker,
      analytics_scheduler, daily_analytics_scheduler.
    * Post 🟢 online / 🔴 offline embeds bracketing the service lifetime.
    * Own watchdog that alerts if the spool backs up without being drained.

NOT here (owned by the downloader process):
    * blink connection + snap_loop / motion_loop.
    * battery_scheduler (blink-dependent; downloader process owns it).

Import from main.py:
    Pipeline, DailyCounters, feed_worker, heartbeat_scheduler,
    analytics_scheduler, daily_analytics_scheduler, watchdog_scheduler,
    _setup_logging, _analytics_executor.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time
from pathlib import Path
from typing import Any

from cardinal_nest_monitor import spool
from cardinal_nest_monitor.config import get_settings
from cardinal_nest_monitor.evidence import EvidenceWriter
from cardinal_nest_monitor.main import (
    DailyCounters,
    Pipeline,
    _analytics_executor,
    _setup_logging,
    analytics_scheduler,
    daily_analytics_scheduler,
    feed_worker,
    heartbeat_scheduler,
    watchdog_scheduler,
)
from cardinal_nest_monitor.notifier import Notifier
from cardinal_nest_monitor.state import StateStore

log = logging.getLogger(__name__)


# Poll the spool once per second. A future upgrade to `watchdog`/fsevents
# would reduce idle CPU, but at current cadence (default 5 min between snaps)
# a 1 s poll is well under the noise floor — roughly 86 400 cheap directory
# scans/day is fine.
_SPOOL_POLL_SECONDS: float = 1.0

# Spool-drain watchdog bounds. If pending/ has items but no snap has been
# claimed in this long, something is wrong (pipeline stall inside the
# analyzer, or a subtle bug in claim_next). Alert to urgent Discord.
_SPOOL_STALL_THRESHOLD_S: int = 300   # 5 min
_SPOOL_WATCHDOG_POLL_S: int = 60       # check once a minute
_SPOOL_REPOST_INTERVAL_S: int = 1800   # re-alert every 30 min if stall persists


async def _spool_consumer(
    pipeline: Pipeline,
    spool_dir: Path,
    claim_state: dict[str, float],
) -> None:
    """Poll the spool and dispatch each claimed snap to the pipeline.

    ``claim_state`` is a shared dict updated on every successful claim; the
    spool-drain watchdog reads it to detect pipeline stalls. Keys:
      * ``last_claim_ts`` — wall-clock of the most recent successful claim.
    """
    settings = get_settings()
    log.info(
        "spool consumer started; polling %s every %.1fs "
        "(live_threshold=%ds, max_age=%ds)",
        spool_dir,
        _SPOOL_POLL_SECONDS,
        settings.backfill_live_threshold_seconds,
        settings.backfill_max_age_seconds,
    )
    while True:
        try:
            claimed = spool.claim_next(spool_dir)
        except Exception:
            log.exception("spool.claim_next raised (non-fatal); sleeping before retry")
            await asyncio.sleep(_SPOOL_POLL_SECONDS)
            continue

        if claimed is None:
            await asyncio.sleep(_SPOOL_POLL_SECONDS)
            continue

        jpeg, meta, processing_snap_path = claimed
        claim_state["last_claim_ts"] = time.time()

        ts = float(meta.get("ts", time.time()))
        age = time.time() - ts

        try:
            if age <= settings.backfill_live_threshold_seconds:
                # LIVE path — urgent channel. No backfill kwarg.
                log.debug(
                    "spool: claimed LIVE snap %s (age=%.1fs)",
                    processing_snap_path.name, age,
                )
                await pipeline.on_image(jpeg, meta)
            elif age <= settings.backfill_max_age_seconds:
                # BACKFILL path — alerts route to backfill webhook via
                # notifier.send_alert(..., backfill_age_seconds=age).
                log.info(
                    "spool: claimed BACKFILL snap %s (age=%.1fs)",
                    processing_snap_path.name, age,
                )
                await pipeline.on_image(
                    jpeg, meta, backfill_age_seconds=age,
                )
            else:
                # Should not happen: drop_stale ran at startup and the live
                # downloader shouldn't produce stale entries. If we see one,
                # log and drop without processing so the cost cap holds.
                log.warning(
                    "spool: dropping over-age snap %s (age=%.1fs > max_age=%ds); "
                    "no pipeline call",
                    processing_snap_path.name, age,
                    settings.backfill_max_age_seconds,
                )
        except asyncio.CancelledError:
            # On shutdown, re-raise after attempting to put the claim back
            # into pending/ so the next run can retry it. This preserves the
            # crash-safety invariant (nothing stays stuck in processing/).
            raise
        except Exception:
            log.exception(
                "pipeline.on_image raised on %s; claim will be deleted "
                "(evidence + state already persisted inside on_image)",
                processing_snap_path.name,
            )

        try:
            spool.mark_complete(processing_snap_path)
        except Exception:
            log.exception(
                "spool.mark_complete failed on %s (non-fatal; "
                "recover_stranded will sweep on next startup)",
                processing_snap_path.name,
            )


async def _spool_drain_watchdog(
    spool_dir: Path,
    notifier: Notifier,
    claim_state: dict[str, float],
) -> None:
    """Alert if pending/ has items but nothing has been claimed recently.

    Unlike the pipeline watchdog (which watches ``pipeline._last_successful_snap_ts``
    for mid-pipeline hangs), this watchdog watches the claim boundary — it
    catches bugs where the spool has work but the consumer loop isn't
    picking anything up (poll loop dead, FS permissions wrong, etc.).
    """
    pending_dir = spool_dir / "pending"
    last_warning_ts: float = 0.0
    log.info(
        "spool-drain watchdog started (stall_threshold=%ds, poll=%ds)",
        _SPOOL_STALL_THRESHOLD_S, _SPOOL_WATCHDOG_POLL_S,
    )
    while True:
        await asyncio.sleep(_SPOOL_WATCHDOG_POLL_S)
        try:
            # Nothing pending → nothing to stall on; reset.
            try:
                has_pending = any(pending_dir.iterdir())
            except FileNotFoundError:
                has_pending = False
            if not has_pending:
                continue

            now_ts = time.time()
            since_claim = now_ts - claim_state.get("last_claim_ts", now_ts)
            if since_claim < _SPOOL_STALL_THRESHOLD_S:
                continue

            if (now_ts - last_warning_ts) < _SPOOL_REPOST_INTERVAL_S:
                continue

            log.error(
                "SPOOL WATCHDOG: pending/ has work but no claim for %ds "
                "(>%ds threshold)",
                int(since_claim), _SPOOL_STALL_THRESHOLD_S,
            )
            try:
                await asyncio.wait_for(
                    notifier.send_system_message(
                        title="🚨 analyzer stalled — spool not draining",
                        body=(
                            f"Spool pending/ has items but no snap has been "
                            f"claimed in {int(since_claim)}s. Pipeline may be "
                            "deadlocked. Check logs and consider a restart."
                        ),
                        color=0xFF0000,
                    ),
                    timeout=15,
                )
            except asyncio.TimeoutError:
                log.error("spool watchdog: Discord embed timed out after 15s")
            except Exception:
                log.exception("spool watchdog: failed to post distress message")
            last_warning_ts = now_ts
        except Exception:
            log.exception("spool-drain watchdog iteration raised (non-fatal)")


async def run_analyzer_service() -> int:
    """Analyzer-only entrypoint (spool consumer + schedulers, no Blink)."""
    _setup_logging()
    settings = get_settings()
    settings.ensure_dirs()

    spool_dir = settings.spool_dir
    spool_dir.mkdir(parents=True, exist_ok=True)

    # Startup recovery: sweep anything a previous crash left behind in
    # processing/ back to pending/, then drop pending entries already too old
    # to be worth analyzing. Both counts are logged so ops can spot a crash
    # or a large backfill at a glance.
    try:
        recovered = spool.recover_stranded(spool_dir)
    except Exception:
        log.exception("spool.recover_stranded failed (continuing)")
        recovered = 0
    try:
        dropped = spool.drop_stale(spool_dir, settings.backfill_max_age_seconds)
    except Exception:
        log.exception("spool.drop_stale failed (continuing)")
        dropped = 0
    log.info(
        "spool recovery: recovered=%d stranded from processing/, "
        "dropped=%d stale from pending/ (older than %ds)",
        recovered, dropped, settings.backfill_max_age_seconds,
    )

    store = StateStore(settings.state_db_path)
    notifier = Notifier(
        webhook_url=settings.discord_webhook_url,
        camera_name=settings.blink_camera_name,
    )
    evidence = EvidenceWriter(settings.evidence_dir)
    counters = DailyCounters()

    # Optional snap feed (isolated from alert hot path via bounded queue)
    feed_notifier: Notifier | None = None
    feed_queue: asyncio.Queue | None = None
    if settings.discord_feed_webhook_url:
        feed_notifier = Notifier(
            webhook_url=settings.discord_feed_webhook_url,
            camera_name=settings.blink_camera_name,
        )
        feed_queue = asyncio.Queue(maxsize=100)
        log.info("snap feed enabled → posting to feed webhook")
    else:
        log.info("snap feed disabled (DISCORD_FEED_WEBHOOK_URL unset)")

    # Optional analytics channel (dedicated thread pool, fully isolated)
    analytics_notifier: Notifier | None = None
    if settings.discord_analytics_webhook_url:
        analytics_notifier = Notifier(
            webhook_url=settings.discord_analytics_webhook_url,
            camera_name=settings.blink_camera_name,
        )
        log.info(
            "analytics channel enabled → posting every %dh",
            settings.analytics_report_hours,
        )
    else:
        log.info("analytics channel disabled (DISCORD_ANALYTICS_WEBHOOK_URL unset)")

    pipeline = Pipeline(
        store=store,
        notifier=notifier,
        evidence=evidence,
        counters=counters,
        feed_queue=feed_queue,
    )

    try:
        await notifier.send_system_message(
            title="🟢 Cardinal Nest Monitor analyzer online",
            body=(
                f"Analyzer service started. Spool: {spool_dir}. "
                f"Recovered {recovered} stranded, dropped {dropped} stale."
            ),
            color=0x32CD32,
        )
    except Exception:
        log.exception("analyzer online embed failed (non-fatal)")

    # Shared state for the spool-drain watchdog. Seeded with now() so the
    # watchdog doesn't fire the instant the service boots on a pre-populated
    # pending/ directory — we give the consumer one stall-window head-start.
    claim_state: dict[str, float] = {"last_claim_ts": time.time()}

    tasks = [
        asyncio.create_task(
            _spool_consumer(pipeline, spool_dir, claim_state),
            name="spool_consumer",
        ),
        asyncio.create_task(
            _spool_drain_watchdog(spool_dir, notifier, claim_state),
            name="spool_drain_watchdog",
        ),
        asyncio.create_task(
            heartbeat_scheduler(notifier, store, counters),
            name="heartbeat",
        ),
        asyncio.create_task(
            watchdog_scheduler(pipeline, notifier),
            name="pipeline_watchdog",
        ),
    ]
    if feed_notifier is not None and feed_queue is not None:
        tasks.append(asyncio.create_task(
            feed_worker(feed_notifier, feed_queue), name="feed_worker"
        ))
    if analytics_notifier is not None:
        tasks.append(asyncio.create_task(
            analytics_scheduler(
                analytics_notifier, store, settings.analytics_report_hours
            ),
            name="analytics",
        ))
        if settings.analytics_daily_hour >= 0:
            tasks.append(asyncio.create_task(
                daily_analytics_scheduler(
                    analytics_notifier, store, settings.analytics_daily_hour
                ),
                name="daily_analytics",
            ))

    # NOTE: battery_scheduler is intentionally NOT started here. Battery
    # state is read from the live Blink object, which the analyzer process
    # does not own (the downloader process holds that connection). Battery
    # reporting belongs to the downloader-side service.

    stop_event = asyncio.Event()

    def _stop(signame: str) -> None:
        log.info("received %s; shutting down analyzer service", signame)
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop, sig.name)
        except NotImplementedError:
            pass  # not supported on Windows / some platforms

    try:
        await stop_event.wait()
    finally:
        log.info("cancelling analyzer tasks")
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        log.info("analyzer tasks cancelled; sending offline embed")
        try:
            ok = await asyncio.wait_for(
                notifier.send_system_message(
                    title="🔴 Cardinal Nest Monitor analyzer offline",
                    body="Analyzer service shutting down.",
                    color=0x808080,
                ),
                timeout=10.0,
            )
            log.info("analyzer offline embed send result: %s", ok)
        except asyncio.TimeoutError:
            log.warning("analyzer offline embed send timed out after 10s")
        except Exception:
            log.exception("analyzer offline embed send raised")
        log.info("closing notifier session")
        await notifier.close()
        if feed_notifier is not None:
            log.info("closing feed notifier session")
            await feed_notifier.close()
        # Close analytics LAST so a slow analytics shutdown can never delay
        # the critical offline embed on the primary alert channel.
        if analytics_notifier is not None:
            log.info("closing analytics notifier session")
            await analytics_notifier.close()
            _analytics_executor.shutdown(wait=False, cancel_futures=True)
        log.info("closing state store")
        store.close()
        log.info("analyzer shutdown complete")

    return 0


__all__ = ["run_analyzer_service"]

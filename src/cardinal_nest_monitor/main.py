"""Main entrypoint — wires Blink → prefilter → analyzer → state → events → notifier.

Two parallel asyncio tasks (motion_loop, snap_loop) share an asyncio.Event.
A third scheduler task posts the daily heartbeat and periodic battery embeds.

Usage:
    python -m cardinal_nest_monitor              # run the full system
    python -m cardinal_nest_monitor --auth-only  # one-time interactive 2FA setup
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any

from cardinal_nest_monitor import analyzer as analyzer_mod
from cardinal_nest_monitor import verifier as verifier_mod
from cardinal_nest_monitor.blink_client import (
    connect,
    download_clip,
    motion_loop,
    snap_loop,
)
from cardinal_nest_monitor.config import get_settings
from cardinal_nest_monitor.events import evaluate
from cardinal_nest_monitor.evidence import EvidenceWriter
from cardinal_nest_monitor.notifier import Notifier
from cardinal_nest_monitor.schema import NestObservation, Severity
from cardinal_nest_monitor.state import StateStore

log = logging.getLogger(__name__)


# ── Cost model (rough, per-call) ────────────────────────────────────────
# Single-tier Sonnet with multi-image (full + center-crop + overview): ~$0.02
# per snap. Was $0.01 historically when MULTI_IMAGE_ANALYSIS was off — bumped
# 2026-04-17 after Codex flagged the heartbeat estimate materially undercounts
# real spend now that multi-image is default-on. Verifier cost is tracked
# separately because it only runs on CRITICAL/HIGH (~$0.05/call).
_ANALYZER_COST_PER_CALL = 0.02
_VERIFIER_COST_PER_CALL = 0.05


class DailyCounters:
    """In-memory daily counters reset at local midnight."""

    def __init__(self) -> None:
        self._day = datetime.now().date()
        self.events = 0
        self.analyzer_successes = 0
        self.verifier_calls = 0
        self.alerts = 0

    def _maybe_roll(self) -> None:
        today = datetime.now().date()
        if today != self._day:
            log.info("daily counters rolling over from %s to %s", self._day, today)
            self._day = today
            self.events = 0
            self.analyzer_successes = 0
            self.verifier_calls = 0
            self.alerts = 0

    def record_snap(self, analyzed: bool = True) -> None:
        self._maybe_roll()
        self.events += 1
        if analyzed:
            self.analyzer_successes += 1

    def record_verifier_call(self) -> None:
        self._maybe_roll()
        self.verifier_calls += 1

    def record_alert(self) -> None:
        self._maybe_roll()
        self.alerts += 1

    @property
    def analyzer_success_rate(self) -> float:
        return self.analyzer_successes / self.events if self.events > 0 else 0.0

    @property
    def estimated_cost(self) -> float:
        return (
            self.analyzer_successes * _ANALYZER_COST_PER_CALL
            + self.verifier_calls * _VERIFIER_COST_PER_CALL
        )


class Pipeline:
    """Owns the per-image processing pipeline. Closure-friendly via methods."""

    def __init__(
        self,
        store: StateStore,
        notifier: Notifier,
        evidence: EvidenceWriter,
        counters: DailyCounters,
        feed_queue: asyncio.Queue | None = None,
    ) -> None:
        self.store = store
        self.notifier = notifier
        self.evidence = evidence
        self.counters = counters
        self.feed_queue = feed_queue
        # Watchdog input: the wall-clock ts of the most recent successful
        # state.record(). Initialised to startup time so the 15-min watchdog
        # doesn't fire the instant the service boots. Updated inside
        # on_image after every successful store.record().
        self._last_successful_snap_ts: float = time.time()

    async def on_image(
        self,
        jpeg: bytes,
        meta: dict[str, Any],
        state_updated: asyncio.Event | None = None,
        backfill_age_seconds: float | None = None,
    ) -> None:
        """Hot-path processing for a single snap.

        Single-tier architecture: every snap goes directly to the analyzer
        (no Haiku prefilter). The `pre` variable stays as None throughout so
        downstream code paths that accept Optional[PrefilterResult] continue
        to work unchanged.
        """
        ts = float(meta.get("ts", time.time()))
        motion_triggered = bool(meta.get("motion_triggered", False))
        now_dt = datetime.fromtimestamp(ts)

        # Single-tier: prefilter is disabled. `pre` stays None. Every snap
        # goes straight to the analyzer.
        pre = None

        obs: NestObservation | None = None
        try:
            # Outer 60s timeout is belt-and-suspenders — analyzer.analyze()
            # already has an inner `asyncio.wait_for(..., 60)` around the
            # HTTP call, but this protects the caller from any future
            # unbounded awaits added to analyze() (e.g. a new retry loop).
            obs = await asyncio.wait_for(analyzer_mod.analyze(jpeg), timeout=60)
        except asyncio.TimeoutError:
            log.warning(
                "analyzer timed out after 60s; no observation for this snap",
            )
        except Exception:
            log.exception("analyzer failed; no observation for this snap")

        # Decide alert (using PRE-record state so mother_returned can fire).
        # Stale-snap correctness (Codex P2): if this snap is older than the
        # most recent observation we've already recorded, it's a backfill
        # frame from analyzer-recovery. The state row reflects FUTURE truth
        # relative to this snap's ts — running state-relative rules against
        # it produces nonsense (e.g. mother_returned with absence_seconds=
        # -300). Pass is_backfill=True so evaluate() skips the time-relative
        # rules but still fires stateless threat alerts (direct_attack,
        # predator_near_nest), which remain useful for "what happened
        # during downtime" via the [BACKFILL +Nm] channel routing.
        pre_state = self.store.get_state()
        cur = self.store._conn.execute(
            "SELECT MAX(ts) AS latest FROM observations"
        )
        _latest_row = cur.fetchone()
        _latest_ts = _latest_row["latest"] if _latest_row is not None else None
        is_backfill = _latest_ts is not None and ts < _latest_ts
        decision = (
            evaluate(obs, pre_state, self.store, ts, is_backfill=is_backfill)
            if obs is not None else None
        )

        # Blind Opus second-opinion on CRITICAL/HIGH alerts. Opus runs with the
        # same image + same system prompt, NO hint of Sonnet's verdict (to
        # avoid anchoring bias). Opus wins on disagreement (suppress /
        # downgrade). Gated on settings.verify_alerts_with_opus.
        opus_obs: NestObservation | None = None
        if (
            decision is not None
            and obs is not None
            and verifier_mod.should_verify(decision)
            and get_settings().verify_alerts_with_opus
        ):
            self.counters.record_verifier_call()
            try:
                # Outer 90s belt-and-suspenders bound on the verifier.
                # verifier.verify_alert() internally awaits analyzer.analyze()
                # which already has its own 60s cap, so 90s is enough to
                # cover the nominal path + retry + a small margin. If this
                # fires, fall back to Sonnet's decision just like the
                # verifier's internal fallback would.
                decision, opus_obs = await asyncio.wait_for(
                    verifier_mod.verify_alert(
                        jpeg, obs, decision, pre_state, self.store, ts,
                        verification_model=get_settings().verification_model,
                    ),
                    timeout=90,
                )
            except asyncio.TimeoutError:
                log.warning(
                    "verifier.verify_alert() outer 90s timeout fired; "
                    "falling back to Sonnet decision (%s / %s)",
                    decision.severity.value,
                    decision.rule_id,
                )
                # decision unchanged = Sonnet's decision; opus_obs stays None.

        # Build event directory + persist evidence
        species_for_dir = (
            (decision.species[0] if decision and decision.species else None)
            or (obs.species_detected[0] if obs and obs.species_detected else None)
        )
        sev_for_dir = decision.severity.value if decision else None
        evt_dir: Path = self.evidence.new_event_dir(now_dt, sev_for_dir, species_for_dir)
        try:
            self.evidence.write_snap(evt_dir, jpeg)
            if pre is not None:
                self.evidence.write_prefilter(evt_dir, pre)
            if obs is not None:
                self.evidence.write_observation(evt_dir, obs)
            if opus_obs is not None:
                self.evidence.write_verification(evt_dir, opus_obs)
            self.evidence.write_metadata(evt_dir, {
                "ts": ts,
                "motion_triggered": motion_triggered,
                "battery_voltage": meta.get("battery_voltage"),
                "battery_state": meta.get("battery_state"),
                "wifi_strength": meta.get("wifi_strength"),
                "decision": decision.model_dump() if decision else None,
            })
        except Exception:
            log.exception("evidence write failed (non-fatal)")

        # Update state and counters
        self.store.record(ts, motion_triggered, pre, obs, str(evt_dir))
        # Mark the watchdog — a successful state record means the snap
        # pipeline is alive end-to-end (we got bytes, analyzed or failed
        # gracefully, and persisted a row). See watchdog_scheduler below.
        self._last_successful_snap_ts = time.time()
        # Signal snap_loop that state has been updated so it can compute
        # the next cadence with fresh `in_absence` data. Doing this here
        # (immediately after record, before Discord POSTs) is what keeps
        # cadence transitions sharp — snap_loop typically waits 3–6s.
        if state_updated is not None:
            state_updated.set()
        # In single-tier mode, every snap is "escalated" (analyzer always runs).
        self.counters.record_snap(analyzed=obs is not None)

        # Send alert
        if decision is not None and obs is not None:
            snap_path = evt_dir / "snap.jpg"
            try:
                # 20s hard bound on send_alert. Notifier already has a 15s
                # per-POST timeout (× 2 retries), but wrap here too so a
                # pathological retry loop can't extend past the caller's
                # patience. If we hit this, log + skip the alerts-table
                # record (same behaviour as a webhook failure).
                ok = await asyncio.wait_for(
                    self.notifier.send_alert(
                        decision, obs, snap_path=snap_path, prefilter=pre,
                        verification_obs=opus_obs,
                        backfill_age_seconds=backfill_age_seconds,
                    ),
                    timeout=20,
                )
            except asyncio.TimeoutError:
                log.error(
                    "notifier.send_alert() outer 20s timeout fired; "
                    "alert NOT recorded in alerts table",
                )
                ok = False
            if ok:
                self.store.record_alert(decision, ts, str(evt_dir))
                self.counters.record_alert()
            else:
                log.error("notifier failed to send alert; not recording in alerts table")

        # Enqueue feed event (non-blocking; drops on QueueFull). NEVER awaits.
        if self.feed_queue is not None:
            feed_event = {
                "ts": ts,
                "motion_triggered": motion_triggered,
                # Single-tier mode: prefilter fields are None so the feed
                # notifier renders a single-analyzer-only embed.
                "prefilter_text": pre.reason if pre is not None else None,
                "prefilter_novel": pre.novel_activity if pre is not None else None,
                "observation_summary": obs.summary if obs is not None else None,
                "severity": decision.severity.value if decision is not None else None,
                "snap_path": evt_dir / "snap.jpg",
            }
            try:
                self.feed_queue.put_nowait(feed_event)
            except asyncio.QueueFull:
                log.warning("feed queue full (Discord backed up?); dropping snap event")

    async def on_clip(self, cam, clip: dict[str, Any]) -> None:
        """Cold-path: download the MP4 in the background. Saved to today's
        evidence root rather than into a specific event directory (it lands
        out-of-band relative to the snap-driven event dir).
        """
        try:
            now_dt = datetime.now()
            day_dir = self.evidence.root_dir / now_dt.strftime("%Y-%m-%d")
            day_dir.mkdir(parents=True, exist_ok=True)
            ts_safe = (clip.get("time") or now_dt.strftime("%H-%M-%S")).replace(":", "-")
            dest = day_dir / f"clip_{ts_safe}.mp4"
            from cardinal_nest_monitor.blink_client import (  # local import for binding
                download_clip as _dl,
            )
            from cardinal_nest_monitor.blink_client import connect as _connect  # noqa
            # Use the blink instance from the calling task — passed implicitly via closure.
            # We don't actually need to re-import here; main() wires the closure via lambda.
            _ = _dl  # silence unused (closure capture done in main)
        except Exception:
            log.exception("on_clip prep failed")


# ── Schedulers + workers ────────────────────────────────────────────────


# Dedicated single-worker executor for analytics compute. Isolates SQLite
# reads + trip-detection work from the main asyncio event loop so it can
# never block the alert hot path or the snap-feed worker. max_workers=1
# means analytics calls are strictly sequential; a slow run can't compound.
_analytics_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="analytics")


async def analytics_scheduler(
    analytics_notifier: Notifier,
    store: "StateStore",
    period_hours: int,
) -> None:
    """Post an analytics report every N hours forever.

    The compute step (SQLite read + Python aggregation) runs on a DEDICATED
    worker thread via run_in_executor so it never blocks the main event
    loop — alert path and feed worker stay responsive even if this thread
    hangs. Caller cancels this task via task.cancel() on shutdown.
    """
    # Lazy import to keep the main module's import graph clean.
    from cardinal_nest_monitor.analytics import compute_report

    settings = get_settings()
    period_s = period_hours * 3600
    loop = asyncio.get_running_loop()
    log.info(
        "analytics_scheduler started (every %dh, using thread pool for compute)",
        period_hours,
    )
    while True:
        await asyncio.sleep(period_s)
        try:
            # Isolated compute on dedicated thread pool.
            report = await loop.run_in_executor(
                _analytics_executor,
                compute_report,
                store,
                time.time(),
                period_hours,
                settings.analyzer_model,
            )
            await analytics_notifier.send_analytics_report(report)
        except Exception:
            log.exception("analytics scheduler failed (non-fatal)")


async def daily_analytics_scheduler(
    analytics_notifier: Notifier,
    store: "StateStore",
    target_hour: int,
) -> None:
    """Post a 24-hour analytics report every day at `target_hour` local time.

    Runs alongside the drifting interval-based analytics_scheduler — this one
    is wall-clock aligned so you get a dependable daily summary (e.g. 08:00
    every morning covering the previous 24 hours). Same isolation: compute
    runs on the dedicated analytics thread pool.
    """
    from cardinal_nest_monitor.analytics import compute_report

    settings = get_settings()
    loop = asyncio.get_running_loop()
    log.info(
        "daily_analytics_scheduler started (every day at %02d:00, 24h window)",
        target_hour,
    )
    while True:
        now = datetime.now()
        target = now.replace(hour=target_hour, minute=0, second=0, microsecond=0)
        if target <= now:
            target = target + timedelta(days=1)
        delay = (target - now).total_seconds()
        log.info(
            "next daily analytics in %.0fs (%s)", delay, target.isoformat()
        )
        await asyncio.sleep(delay)
        try:
            report = await loop.run_in_executor(
                _analytics_executor,
                compute_report,
                store,
                time.time(),
                24,  # daily = 24h window
                settings.analyzer_model,
            )
            await analytics_notifier.send_analytics_report(report)
        except Exception:
            log.exception("daily analytics scheduler failed (non-fatal)")


async def feed_worker(feed_notifier: Notifier, queue: asyncio.Queue) -> None:
    """Drain feed events from the queue and post each to Discord.

    Runs forever; cancellable via task.cancel(). Never raises out — any send
    failure is logged and the worker keeps draining. This isolation is
    deliberate: a misbehaving feed webhook must not affect the alert path.
    """
    log.info("feed_worker started")
    while True:
        event = await queue.get()
        try:
            await feed_notifier.send_snap_feed(**event)
        except Exception:
            log.exception("feed send failed (non-fatal)")
        finally:
            queue.task_done()


def _lifecycle_day_label(state) -> str | None:
    """Human-readable day counter for the heartbeat embed.

    Examples:
      incubation + incubation_started_ts=2d ago → "Day 2 of ~12"
      egg_laying + egg_laying_started_ts=1d ago → "Day 1 of ~4"
      feeding + hatch_detected_ts=3d ago        → "Day 3 of ~14"
    None when we can't compute (no start timestamp, or stages without a
    canonical countdown like building_nest / empty).
    """
    stage = state.lifecycle_stage
    now = time.time()
    if stage == "incubation" and state.incubation_started_ts is not None:
        day = int((now - state.incubation_started_ts) / 86400) + 1
        return f"Day {day} of ~12"
    if stage == "egg_laying" and state.egg_laying_started_ts is not None:
        day = int((now - state.egg_laying_started_ts) / 86400) + 1
        return f"Day {day} of ~4"
    if stage == "feeding" and state.hatch_detected_ts is not None:
        day = int((now - state.hatch_detected_ts) / 86400) + 1
        return f"Day {day} of ~14"
    if stage == "fledging" and state.fledge_detected_ts is not None:
        day = int((now - state.fledge_detected_ts) / 86400) + 1
        return f"Day {day}"
    return None


async def heartbeat_scheduler(notifier: Notifier, store: StateStore, counters: DailyCounters) -> None:
    """Once per day at HEARTBEAT_HOUR_LOCAL, post a summary embed."""
    settings = get_settings()
    while True:
        now = datetime.now()
        target = now.replace(
            hour=settings.heartbeat_hour_local, minute=0, second=0, microsecond=0
        )
        if target <= now:
            target = target + timedelta(days=1)
        delay = (target - now).total_seconds()
        log.info("heartbeat scheduled in %.0fs (%s)", delay, target.isoformat())
        await asyncio.sleep(delay)

        try:
            state = store.get_state()
            mother_minutes_ago: int | None = None
            if state.last_mother_seen_ts is not None:
                mother_minutes_ago = int(
                    (time.time() - state.last_mother_seen_ts) / 60
                )
            lifecycle_stage = state.lifecycle_stage if get_settings().lifecycle_tracking_enabled else None
            lifecycle_day_label = _lifecycle_day_label(state) if lifecycle_stage else None
            await notifier.send_heartbeat(
                events_today=counters.events,
                alerts_today=counters.alerts,
                last_mother_seen_minutes_ago=mother_minutes_ago,
                analyzer_success_rate=counters.analyzer_success_rate,
                cost_estimate_today_usd=counters.estimated_cost,
                lifecycle_stage=lifecycle_stage,
                lifecycle_day_label=lifecycle_day_label,
            )
        except Exception:
            log.exception("heartbeat send failed (non-fatal)")


async def watchdog_scheduler(
    pipeline: "Pipeline", notifier: Notifier
) -> None:
    """Dead-man's-switch for the snap pipeline.

    Polls every 60s. If more than 15 minutes have elapsed since the last
    successful snap AND we're inside active hours, posts an urgent ERROR
    alert to Discord. Re-posts every 30 min if the stall persists.

    Added after the 2026-04-13 outage: the pipeline silently hung for 3+
    hours and we only noticed because alerts stopped coming. The watchdog
    bounds notification latency on future silent failures to ≤15 min.
    """
    settings = get_settings()
    last_watchdog_warning_ts: float = 0.0
    WATCHDOG_POLL_S = 60
    STALL_THRESHOLD_S = 900  # 15 min
    REPOST_INTERVAL_S = 1800  # 30 min

    log.info("watchdog started — checking every %ds", WATCHDOG_POLL_S)
    while True:
        await asyncio.sleep(WATCHDOG_POLL_S)
        try:
            now_ts = time.time()
            since_snap = now_ts - pipeline._last_successful_snap_ts
            now_dt = datetime.now()
            # Dynamic threshold: during quiet hours the cadence is 30 min,
            # so a 15-min stall threshold would false-alarm every cycle.
            # Also applies at the quiet→active boundary: if the last snap
            # was taken during quiet hours (30-min cadence), we need to
            # wait one quiet interval before alarming even though we're
            # now in active hours.
            threshold = STALL_THRESHOLD_S
            last_snap_was_quiet = settings.in_quiet_hours(
                datetime.fromtimestamp(pipeline._last_successful_snap_ts).time()
            ) if pipeline._last_successful_snap_ts else False
            if settings.in_quiet_hours(now_dt.time()) or last_snap_was_quiet:
                threshold = max(threshold, settings.quiet_snap_interval_seconds + 300)
            if since_snap <= threshold:
                continue
            if not settings.in_active_hours(now_dt.time()):
                continue
            if (now_ts - last_watchdog_warning_ts) < REPOST_INTERVAL_S:
                continue
            log.error(
                "WATCHDOG: no successful snap for %ds (>15min stall during active hours)",
                int(since_snap),
            )
            try:
                await notifier.send_system_message(
                    title="🚨 WATCHDOG: no snaps for 15+ min",
                    body="Snap loop appears stuck. Service may need a restart.",
                    color=0xFF0000,
                )
            except Exception:
                log.exception("watchdog: failed to send distress message")
            last_watchdog_warning_ts = now_ts
        except Exception:
            log.exception("watchdog loop iteration raised (non-fatal)")


async def battery_scheduler(notifier: Notifier, blink_holder: dict[str, Any]) -> None:
    """Every BATTERY_REPORT_HOURS, post a battery-health embed."""
    settings = get_settings()
    while True:
        await asyncio.sleep(settings.battery_report_hours * 3600)
        try:
            blink = blink_holder.get("blink")
            if blink is None:
                continue
            cam = blink.cameras.get(settings.blink_camera_name)
            if cam is None:
                continue
            await notifier.send_battery_status(
                battery_voltage=getattr(cam, "battery_voltage", None),
                battery_state=getattr(cam, "battery", None),
                wifi_strength=getattr(cam, "wifi_strength", None),
            )
        except Exception:
            log.exception("battery embed failed (non-fatal)")


# ── Entry points ───────────────────────────────────────────────────────


def _setup_logging() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )


async def auth_only() -> int:
    _setup_logging()
    log.info("Running --auth-only flow; will prompt for 2FA PIN if needed.")
    blink = await connect(prompt_2fa=True)
    log.info("Auth complete. Cameras discovered: %s", list(blink.cameras))
    await blink.auth.session.close()
    return 0


async def run_combined() -> int:
    """Full single-service pipeline (legacy behavior).

    Blink → prefilter → analyzer → state → events → notifier, all in one
    asyncio event loop. This function is byte-for-byte equivalent to the
    pre-split `run()` entrypoint; callers that want the decoupled
    downloader+analyzer architecture should use `run_downloader()` /
    `run_analyzer()` instead.
    """
    _setup_logging()
    settings = get_settings()
    settings.ensure_dirs()

    blink = await connect(prompt_2fa=False)
    blink_holder: dict[str, Any] = {"blink": blink}

    store = StateStore(settings.state_db_path)
    notifier = Notifier(
        webhook_url=settings.discord_webhook_url,
        camera_name=settings.blink_camera_name,
    )
    evidence = EvidenceWriter(settings.evidence_dir)
    counters = DailyCounters()

    # Optional snap feed (isolated from alert hot path)
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

    # Optional analytics channel (runs on dedicated thread pool — fully
    # isolated from alert + feed paths even under heavy compute).
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

    # Send a "system online" message so the user knows the launchd start succeeded.
    try:
        await notifier.send_system_message(
            title="🟢 Cardinal Nest Monitor online",
            body=(
                f"Snap cadence: every {settings.snap_interval_seconds}s "
                f"during {settings.active_hours} local. "
                f"Camera: {settings.blink_camera_name}."
            ),
            color=0x32CD32,
        )
    except Exception:
        log.exception("startup message failed (non-fatal)")

    snap_now = asyncio.Event()

    # Pattern A — absence-aware dynamic cadence.
    # Computed every time snap_loop is about to wait for the next tick.
    # Priority: quiet hours > in_absence > default.
    _last_interval_logged: dict[str, int] = {"value": 0}

    def get_interval() -> int:
        now = datetime.now().time()
        label = ""
        if settings.in_quiet_hours(now):
            interval = settings.quiet_snap_interval_seconds
            label = "quiet"
        else:
            try:
                in_absence = store.get_state().in_absence
            except Exception:
                log.exception("absence check failed; using default interval")
                in_absence = False
            if in_absence:
                interval = settings.absence_snap_interval_seconds
                label = "absence"
            else:
                interval = settings.snap_interval_seconds
                label = "default"
        # Log only on transition to avoid noise
        if _last_interval_logged["value"] != interval:
            log.info(
                "cadence: %ds → %ds (%s)",
                _last_interval_logged["value"], interval, label,
            )
            _last_interval_logged["value"] = interval
        return interval

    async def _on_clip(cam, clip):
        # Capture the current blink for the download_clip call.
        try:
            now_dt = datetime.now()
            day_dir = evidence.root_dir / now_dt.strftime("%Y-%m-%d")
            day_dir.mkdir(parents=True, exist_ok=True)
            ts_safe = (clip.get("time") or now_dt.strftime("%H-%M-%S")).replace(":", "-")
            dest = day_dir / f"clip_{ts_safe}.mp4"
            ok = await download_clip(blink, cam, clip, dest)
            log.info("clip download %s: %s", "ok" if ok else "failed", dest)
        except Exception:
            log.exception("on_clip failed")

    tasks = [
        asyncio.create_task(motion_loop(blink, snap_now, _on_clip), name="motion_loop"),
        asyncio.create_task(
            snap_loop(blink, snap_now, on_snap=pipeline.on_image, get_interval=get_interval),
            name="snap_loop",
        ),
        asyncio.create_task(
            heartbeat_scheduler(notifier, store, counters), name="heartbeat"
        ),
        asyncio.create_task(
            battery_scheduler(notifier, blink_holder), name="battery"
        ),
        asyncio.create_task(
            watchdog_scheduler(pipeline, notifier), name="watchdog"
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

    stop_event = asyncio.Event()

    def _stop(signame: str) -> None:
        log.info("received %s; shutting down", signame)
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop, sig.name)
        except NotImplementedError:
            pass  # not supported on Windows / certain platforms

    try:
        await stop_event.wait()
    finally:
        log.info("cancelling tasks")
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        log.info("tasks cancelled; sending offline embed")
        try:
            ok = await asyncio.wait_for(
                notifier.send_system_message(
                    title="🔴 Cardinal Nest Monitor offline",
                    body="System shutting down.",
                    color=0x808080,
                ),
                timeout=10.0,
            )
            log.info("offline embed send result: %s", ok)
        except asyncio.TimeoutError:
            log.warning("offline embed send timed out after 10s")
        except Exception:
            log.exception("offline embed send raised")
        log.info("closing notifier session")
        await notifier.close()
        if feed_notifier is not None:
            log.info("closing feed notifier session")
            await feed_notifier.close()
        # Close analytics LAST so a slow analytics shutdown never delays the
        # critical offline embed on the primary alert channel.
        if analytics_notifier is not None:
            log.info("closing analytics notifier session")
            await analytics_notifier.close()
            _analytics_executor.shutdown(wait=False, cancel_futures=True)
        log.info("closing state store")
        store.close()
        log.info("closing blink session")
        try:
            await blink.auth.session.close()
        except Exception:
            log.exception("blink session close raised (non-fatal)")
        log.info("shutdown complete")

    return 0


async def run_downloader() -> int:
    """Downloader role: Blink snap loop writes JPEGs into the shared spool.

    Thin delegate to `cardinal_nest_monitor.downloader_loop.run_downloader_service`.
    The heavy lifting (connect, snap_loop wired to spool.write_snap, shutdown
    ordering) lives in that module.
    """
    from cardinal_nest_monitor.downloader_loop import run_downloader_service
    return await run_downloader_service()


async def run_analyzer() -> int:
    """Analyzer role: reads spooled JPEGs, runs the full analyze/alert pipeline.

    Thin delegate to `cardinal_nest_monitor.analyzer_loop.run_analyzer_service`.
    """
    from cardinal_nest_monitor.analyzer_loop import run_analyzer_service
    return await run_analyzer_service()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cardinal_nest_monitor",
        description="Real-time AI nest-risk detection (Blink → Claude → Discord).",
    )
    parser.add_argument(
        "--auth-only", action="store_true",
        help="Run interactive Blink 2FA flow once, then exit (writes blink_credentials.json).",
    )
    args = parser.parse_args(argv)

    try:
        if args.auth_only:
            return asyncio.run(auth_only())
        return asyncio.run(run_combined())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

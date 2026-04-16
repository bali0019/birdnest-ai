"""Blink camera client: auth + motion-event polling + scheduled snap loop.

Two concurrent asyncio tasks share an `asyncio.Event`:
  - motion_loop: polls blink.refresh() every motion_poll_seconds
  - snap_loop:   triggers cam.snap_picture() every snap_interval_seconds, or
                 immediately when motion_loop sets the event

Auth uses blinkpy 0.25.5 OAuth-v2 (BlinkTwoFARequiredError flow). Persisted
creds in blink_credentials.json are reused across runs.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiohttp import ClientSession
from blinkpy.auth import Auth, BlinkTwoFARequiredError
from blinkpy.blinkpy import Blink, BlinkSetupError
from blinkpy.camera import BlinkCamera
from blinkpy.helpers.util import json_load

from cardinal_nest_monitor.config import get_settings

log = logging.getLogger(__name__)

# Where the 2FA PIN can be dropped when running non-interactively. The PIN
# poller checks this path every 2 seconds for up to 5 minutes.
PIN_FILE_PATH = Path("/tmp/cardinal_nest_blink_pin")


class _SnapCycleSkipped(Exception):
    """Internal control-flow sentinel used by snap_loop to short-circuit a
    single snap cycle after a Blink-network timeout. Never propagates out
    of snap_loop.
    """


async def _read_2fa_pin() -> str:
    """Get a 2FA PIN from one of three sources, in order:
      1. BLINK_PIN environment variable (instant)
      2. Real interactive stdin if attached to a TTY
      3. File at /tmp/cardinal_nest_blink_pin (polled for up to 5 minutes)

    The file-polling path is the one that works when the script runs in the
    background — drop the PIN into the file and the script picks it up.
    """
    env_pin = os.environ.get("BLINK_PIN", "").strip()
    if env_pin:
        log.info("Using PIN from BLINK_PIN env var")
        return env_pin

    if sys.stdin.isatty():
        return input("Enter 2FA PIN from email: ").strip()

    # Non-interactive: poll the file.
    if PIN_FILE_PATH.exists():
        # Stale file from a previous run — clear it so we don't re-use a stale PIN.
        try:
            PIN_FILE_PATH.unlink()
        except OSError:
            pass

    print(
        f"\n⏳ 2FA PIN required. Check email at {get_settings().blink_username}.\n"
        f"   Drop the PIN into {PIN_FILE_PATH} when you have it:\n"
        f"     echo 'YOUR_PIN' > {PIN_FILE_PATH}\n"
        f"   (waiting up to 5 minutes)\n",
        flush=True,
    )

    deadline = time.time() + 300
    while time.time() < deadline:
        if PIN_FILE_PATH.exists():
            pin = PIN_FILE_PATH.read_text().strip()
            try:
                PIN_FILE_PATH.unlink()
            except OSError:
                pass
            if pin:
                log.info("Got PIN from file (%d digits)", len(pin))
                return pin
        await asyncio.sleep(2)
    raise RuntimeError(f"Timed out waiting for PIN at {PIN_FILE_PATH}")


async def connect(prompt_2fa: bool = False) -> Blink:
    """Connect to Blink. Reuses persisted creds if present; otherwise does
    interactive 2FA (only if prompt_2fa=True). Persists creds after success.
    """
    settings = get_settings()
    session = ClientSession()
    blink = Blink(session=session, refresh_rate=settings.motion_poll_seconds)

    if settings.blink_creds_path.exists():
        creds = await json_load(str(settings.blink_creds_path))
        blink.auth = Auth(creds, no_prompt=True, session=session)
    elif prompt_2fa:
        if not settings.blink_username or not settings.blink_password:
            raise RuntimeError(
                "BLINK_USERNAME / BLINK_PASSWORD must be set in .env for first-run 2FA"
            )
        blink.auth = Auth(
            {"username": settings.blink_username, "password": settings.blink_password},
            no_prompt=False,
            session=session,
        )
    else:
        raise RuntimeError(
            "No persisted creds and prompt_2fa=False; run --auth-only first"
        )

    try:
        await blink.start()
    except BlinkTwoFARequiredError:
        if not prompt_2fa:
            raise
        pin = await _read_2fa_pin()
        success = await blink.auth.complete_2fa_login(pin)
        if not success:
            raise RuntimeError("2FA verification failed — check the PIN")
        # Mirror what blink.start() does after auth.startup() succeeds:
        blink.setup_urls()
        await blink.get_homescreen()
        await blink.setup_post_verify()
    except BlinkSetupError:
        log.exception("blink setup error")
        raise
    except Exception as e:
        if "unexpected mimetype" in str(e):
            log.warning("blink auth invalid (mimetype error); persisted creds may be stale")
        raise

    await blink.save(str(settings.blink_creds_path))
    log.info("Blink connected; %d cameras: %s", len(blink.cameras), list(blink.cameras))
    return blink


async def _reauth(blink: Blink) -> None:
    """Force-refresh auth tokens. Used when blinkpy raises 'unexpected mimetype'."""
    await blink.auth.login()
    await blink.setup_post_verify()


async def motion_loop(
    blink: Blink,
    snap_now: asyncio.Event,
    on_clip: Callable[[BlinkCamera, dict[str, Any]], Awaitable[None]],
) -> None:
    """Poll for motion events; on new clip, signal snap_now and call on_clip."""
    settings = get_settings()
    last_clip_time: str | None = None

    while True:
        try:
            # 30s hard bound: blink.refresh() has been observed to hang
            # indefinitely under API flakiness. Normal latency is <5s.
            await asyncio.wait_for(blink.refresh(force=True), timeout=30)
        except asyncio.TimeoutError:
            log.warning("motion_loop: blink.refresh() timed out after 30s; retrying")
            await asyncio.sleep(settings.motion_poll_seconds)
            continue
        except Exception as e:
            if "unexpected mimetype" in str(e):
                log.warning("motion_loop auth expired (mimetype); attempting reauth")
                try:
                    await _reauth(blink)
                except Exception:
                    log.exception("reauth failed")
                    await asyncio.sleep(30)
                continue
            log.exception("refresh failed")
            await asyncio.sleep(settings.motion_poll_seconds)
            continue

        cam = blink.cameras.get(settings.blink_camera_name)
        if cam is None:
            log.warning(
                "camera %r not found; available: %s",
                settings.blink_camera_name,
                list(blink.cameras),
            )
            await asyncio.sleep(settings.motion_poll_seconds)
            continue

        clips = getattr(cam, "recent_clips", None) or []
        if clips:
            newest = clips[-1]
            ts = newest.get("time")
            if ts and ts != last_clip_time:
                last_clip_time = ts
                log.info("new motion clip detected at %s", ts)
                snap_now.set()
                asyncio.create_task(on_clip(cam, newest))

        await asyncio.sleep(settings.motion_poll_seconds)


async def snap_loop(
    blink: Blink,
    snap_now: asyncio.Event,
    on_snap: Callable[[bytes, dict[str, Any], asyncio.Event | None], Awaitable[None]],
    get_interval: Callable[[], int] | None = None,
) -> None:
    """Snap every snap_interval_seconds OR immediately when snap_now is set.

    Skips when outside active hours OR when pause.lock exists.

    If `get_interval` is provided, it's called each cycle to determine the next
    wait time (enables dynamic cadence — e.g. Pattern A absence-aware cadence).
    If None, falls back to settings.current_snap_interval() static behavior.

    The `on_snap` callback is invoked per snap with (jpeg_bytes, meta,
    state_updated_event). It may be the full analyze+alert pipeline (combined
    role) or a spool-write shim (downloader role). The callback is dispatched
    via ``asyncio.create_task`` so a slow/hung callback cannot block subsequent
    snaps — see §17 of CLAUDE.md.
    """
    settings = get_settings()
    paused_logged = False

    while True:
        # ── active hours gate ────────────────────────────────────────────
        if not settings.in_active_hours(datetime.now().time()):
            await asyncio.sleep(60)
            continue

        # ── pause lock gate ──────────────────────────────────────────────
        if settings.pause_lock_path.exists():
            if not paused_logged:
                log.info(
                    "pause lock present at %s; snap_loop idling",
                    settings.pause_lock_path,
                )
                paused_logged = True
            await asyncio.sleep(30)
            continue
        paused_logged = False

        cam = blink.cameras.get(settings.blink_camera_name)
        if cam is None:
            log.warning(
                "camera %r not available; retrying in 30s",
                settings.blink_camera_name,
            )
            await asyncio.sleep(30)
            continue

        motion_triggered = snap_now.is_set()
        jpeg: bytes | None = None
        try:
            # Each Blink network call gets its own hard timeout. During the
            # 2026-04-13 outage, one of these hung indefinitely and stalled
            # the whole pipeline. Normal latencies: snap_picture 3–8s,
            # refresh <5s, get_media <3s.
            try:
                await asyncio.wait_for(cam.snap_picture(), timeout=30)
            except asyncio.TimeoutError:
                log.warning("snap_loop: cam.snap_picture() timed out after 30s; skipping cycle")
                jpeg = None
                raise _SnapCycleSkipped()
            await asyncio.sleep(6)
            try:
                await asyncio.wait_for(blink.refresh(), timeout=30)
            except asyncio.TimeoutError:
                log.warning("snap_loop: blink.refresh() timed out after 30s; skipping cycle")
                jpeg = None
                raise _SnapCycleSkipped()
            try:
                await asyncio.wait_for(cam.get_media(), timeout=30)
            except asyncio.TimeoutError:
                log.warning("snap_loop: cam.get_media() timed out after 30s; skipping cycle")
                jpeg = None
                raise _SnapCycleSkipped()
            cached = cam.image_from_cache
            if cached is None:
                jpeg = None
            elif isinstance(cached, (bytes, bytearray)):
                jpeg = bytes(cached)
            else:
                # blinkpy historically returned a BytesIO-like object; handle that.
                read = getattr(cached, "read", None)
                jpeg = read() if callable(read) else None
        except _SnapCycleSkipped:
            # A timeout already logged above; just move on.
            pass
        except Exception as e:
            if "unexpected mimetype" in str(e):
                log.warning("snap_loop auth expired; attempting reauth")
                try:
                    await _reauth(blink)
                except Exception:
                    log.exception("reauth failed")
                await asyncio.sleep(10)
                continue
            log.exception("snap cycle failed")

        if not jpeg:
            log.warning("snap returned no image; skipping")
        else:
            meta: dict[str, Any] = {
                "motion_triggered": motion_triggered,
                "ts": datetime.now().timestamp(),
                "battery_voltage": getattr(cam, "battery_voltage", None),
                "battery_state": getattr(cam, "battery", None),
                "wifi_strength": getattr(cam, "wifi_strength", None),
            }
            # Per-snap task isolation: spawn on_snap as a task so a slow
            # analyzer / Discord call can never block the next snap. To
            # keep the dynamic cadence (get_interval below) correct, we
            # wait up to 10s for the task to signal that state has been
            # updated. Typical case: state lands in 3–6s; max 10s wait.
            # Hang case: we lose one cadence transition cycle (bounded lag
            # ≤ 5 min), never multi-hour silence.
            # Rationale: 2026-04-13 outage — `await on_snap` serialised
            # the pipeline and one hung network call blocked everything
            # for 3+ hours. See plan reactive-tickling-rose.md Part 1A/B.
            state_updated = asyncio.Event()
            asyncio.create_task(on_snap(jpeg, meta, state_updated))
            try:
                await asyncio.wait_for(state_updated.wait(), timeout=10)
            except asyncio.TimeoutError:
                log.warning("on_snap slow; cadence may use stale state this cycle")

        # ── wait for next tick or motion nudge ──────────────────────────
        snap_now.clear()
        # Use the get_interval callback if provided (dynamic cadence — e.g.
        # Pattern A absence-aware); otherwise static quiet-hours / default.
        # Motion events still bypass via snap_now regardless of interval.
        if get_interval is not None:
            try:
                interval = get_interval()
            except Exception:
                log.exception("get_interval callback raised; using static fallback")
                interval = settings.current_snap_interval(datetime.now().time())
        else:
            interval = settings.current_snap_interval(datetime.now().time())
        timeout = max(1, interval - 6)
        try:
            await asyncio.wait_for(snap_now.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass


async def download_clip(
    blink: Blink,
    cam: BlinkCamera,
    clip: dict[str, Any],
    dest: Path,
) -> bool:
    """Download the MP4 referenced by clip['clip'] to dest. Returns True on success."""
    url = f"{blink.urls.base_url}{clip['clip']}"
    headers = {"TOKEN_AUTH": blink.auth.token}
    try:
        # 60s hard bound on the entire GET + read: video bytes can be big
        # but this is not the hot path, so bound it generously.
        async def _do_download() -> bool:
            async with blink.auth.session.get(url, headers=headers) as r:
                if r.status != 200:
                    log.warning("clip download failed: status=%s url=%s", r.status, url)
                    return False
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(await r.read())
                log.info("clip downloaded: %s (%d bytes)", dest, dest.stat().st_size)
                return True

        return await asyncio.wait_for(_do_download(), timeout=60)
    except asyncio.TimeoutError:
        log.warning("clip download timed out after 60s: url=%s", url)
        return False
    except Exception:
        log.exception("clip download error")
        return False

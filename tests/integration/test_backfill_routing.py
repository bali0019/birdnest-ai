"""Live-vs-backfill routing + WAL concurrency regression tests.

Locks in the two guarantees the decoupled downloader/analyzer architecture
depends on:

  1. Live snaps (fresh from the spool) route to the urgent alert webhook
     exactly as they did in the single-service world — ``backfill_age_seconds``
     is either ``None`` or omitted on ``notifier.send_alert``.
  2. Backfill snaps (processed from backlog during analyzer downtime) route
     to the dedicated backfill webhook with a ``[BACKFILL +Nm]`` title
     prefix, leaving the urgent channel pristine for live alerts.
  3. SQLite WAL mode is active so the downloader service (read-only) can
     read ``in_absence`` while the analyzer service (writer) is mid-record.
     Under rollback-journal mode the two would serialize; the concurrent
     read test below would hang.

Style matches ``tests/integration/test_absence_cycle.py`` — ``_pipeline``
helper, monkeypatched ``analyzer.analyze`` per test, real (but monkeypatched
to capture) ``notifier.send_alert``.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from cardinal_nest_monitor import analyzer as analyzer_mod
from cardinal_nest_monitor import main as main_mod
from cardinal_nest_monitor.config import get_settings
from cardinal_nest_monitor.state import StateStore


def _pipeline(store, notifier, evidence):
    """Build a Pipeline wired for integration tests (no feed queue)."""
    counters = main_mod.DailyCounters()
    return main_mod.Pipeline(
        store=store,
        notifier=notifier,
        evidence=evidence,
        counters=counters,
        feed_queue=None,
    )


def _meta(ts: float | None = None, motion: bool = False) -> dict:
    return {
        "motion_triggered": motion,
        "ts": ts if ts is not None else time.time(),
    }


async def test_live_snap_routes_to_urgent_webhook(
    monkeypatch, store, evidence, notifier,
    thrasher_jpeg_bytes, obs_thrasher_near_nest,
):
    """Live snap (5s old, no backfill_age_seconds passed) must fire send_alert
    once with backfill_age_seconds=None (or unset).

    This is the guardrail for: "every live snap's alert lands on the urgent
    channel, NOT the backfill channel." If Pipeline.on_image ever starts
    stamping backfill_age_seconds on live snaps, it would poison the urgent
    feed with stale-looking alerts.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "verify_alerts_with_opus", False)
    # Double-set the backfill webhook to a known TEST URL so any accidental
    # backfill routing lands somewhere benign (never a live production URL).
    monkeypatch.setattr(
        settings, "discord_backfill_webhook_url",
        "https://discord.com/api/webhooks/TEST/BACKFILL",
    )

    monkeypatch.setattr(
        analyzer_mod, "analyze",
        AsyncMock(return_value=obs_thrasher_near_nest()),
    )
    send_alert_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(notifier, "send_alert", send_alert_mock)

    pipeline = _pipeline(store, notifier, evidence)

    # Live snap: 5 seconds old — well inside backfill_live_threshold_seconds.
    # Call on_image WITHOUT backfill_age_seconds (the live path).
    await pipeline.on_image(thrasher_jpeg_bytes, _meta(ts=time.time() - 5))

    assert send_alert_mock.await_count == 1, (
        f"expected exactly one alert, got {send_alert_mock.await_count}"
    )
    # Inspect kwargs — backfill_age_seconds must be None (or omitted entirely).
    _, kwargs = send_alert_mock.await_args
    assert kwargs.get("backfill_age_seconds") is None, (
        "live-snap alerts must pass backfill_age_seconds=None (or omit), "
        f"got {kwargs.get('backfill_age_seconds')!r}"
    )


async def test_backfill_snap_routes_to_backfill_channel(
    monkeypatch, store, evidence, notifier,
    thrasher_jpeg_bytes, obs_thrasher_near_nest,
):
    """Backfill snap (10 min old, explicit backfill_age_seconds=600) must
    fire send_alert once with the numeric age forwarded through.

    Pipeline.on_image receives backfill_age_seconds from analyzer_loop when
    it claimed a spooled snap older than backfill_live_threshold_seconds;
    it MUST forward that kwarg verbatim to notifier.send_alert.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "verify_alerts_with_opus", False)
    monkeypatch.setattr(
        settings, "discord_backfill_webhook_url",
        "https://discord.com/api/webhooks/TEST/BACKFILL",
    )

    monkeypatch.setattr(
        analyzer_mod, "analyze",
        AsyncMock(return_value=obs_thrasher_near_nest()),
    )
    send_alert_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(notifier, "send_alert", send_alert_mock)

    pipeline = _pipeline(store, notifier, evidence)

    # Backfill snap: 10 minutes old, explicit age passed through.
    backfill_age = 600.0
    await pipeline.on_image(
        thrasher_jpeg_bytes,
        _meta(ts=time.time() - backfill_age),
        backfill_age_seconds=backfill_age,
    )

    assert send_alert_mock.await_count == 1, (
        f"expected exactly one alert, got {send_alert_mock.await_count}"
    )
    _, kwargs = send_alert_mock.await_args
    assert kwargs.get("backfill_age_seconds") == backfill_age, (
        "backfill alert must forward backfill_age_seconds verbatim, "
        f"got {kwargs.get('backfill_age_seconds')!r}"
    )


async def test_backfill_alert_title_has_age_prefix(
    monkeypatch, store, evidence, notifier,
    thrasher_jpeg_bytes, obs_thrasher_near_nest,
):
    """End-to-end: backfill alert payload has `[BACKFILL +15m]` in embed
    title AND posts to the backfill webhook URL (NOT the urgent webhook).

    Monkeypatches the low-level _send_multipart (snap.jpg is attached on
    alerts with an image) to capture (payload, image_path, url_override).
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "verify_alerts_with_opus", False)

    BACKFILL_URL = "https://discord.com/api/webhooks/TEST/BACKFILL"
    URGENT_URL = "https://discord.com/api/webhooks/TEST/URGENT"
    monkeypatch.setattr(settings, "discord_backfill_webhook_url", BACKFILL_URL)
    monkeypatch.setattr(settings, "discord_webhook_url", URGENT_URL)
    # The notifier fixture captured the pre-monkeypatch webhook_url. Rewrite
    # the instance attribute too so any non-backfill routing would land on
    # the TEST URGENT url (and be distinguishable from the backfill one).
    monkeypatch.setattr(notifier, "webhook_url", URGENT_URL)

    monkeypatch.setattr(
        analyzer_mod, "analyze",
        AsyncMock(return_value=obs_thrasher_near_nest()),
    )

    captured: list[dict] = []

    async def fake_send_multipart(payload, image_path, url_override=None, **kwargs):
        captured.append({
            "payload": payload,
            "image_path": image_path,
            "url_override": url_override,
            **kwargs,
        })
        return True

    async def fake_send_json(payload, url_override=None, **kwargs):
        # In case the snap.jpg path doesn't exist on disk, the notifier
        # falls back to _send_json. Capture there too for robustness.
        captured.append({
            "payload": payload,
            "image_path": None,
            "url_override": url_override,
            **kwargs,
        })
        return True

    monkeypatch.setattr(notifier, "_send_multipart", fake_send_multipart)
    monkeypatch.setattr(notifier, "_send_json", fake_send_json)

    pipeline = _pipeline(store, notifier, evidence)

    backfill_age = 900.0  # 15 minutes
    await pipeline.on_image(
        thrasher_jpeg_bytes,
        _meta(ts=time.time() - backfill_age),
        backfill_age_seconds=backfill_age,
    )

    assert len(captured) == 1, (
        f"expected exactly one Discord POST, got {len(captured)}: {captured}"
    )
    call = captured[0]

    # Embed title must contain [BACKFILL +15m].
    embeds = call["payload"]["embeds"]
    assert len(embeds) == 1, f"expected 1 embed, got {len(embeds)}"
    title = embeds[0]["title"]
    assert "[BACKFILL +15m]" in title, (
        f"backfill alert title missing '[BACKFILL +15m]' prefix: {title!r}"
    )

    # URL override must be the BACKFILL webhook, NOT the urgent one.
    assert call["url_override"] == BACKFILL_URL, (
        f"backfill alert must post to discord_backfill_webhook_url "
        f"({BACKFILL_URL!r}), got url_override={call['url_override']!r}"
    )
    assert call["url_override"] != URGENT_URL, (
        "backfill alert MUST NOT post to the urgent webhook — that would "
        "pollute the live alert channel with stale backlog alerts"
    )


async def test_wal_allows_concurrent_read_during_write(tmp_path):
    """Two StateStore connections against the same SQLite file must allow
    a read on conn2 to complete while conn1 holds a transaction open.

    Under WAL mode (what state.py sets via PRAGMA journal_mode=WAL), readers
    see a consistent snapshot without blocking on the writer. Under the
    default rollback-journal mode, the reader would block on SQLITE_BUSY
    until the writer commits — the test would hit the timeout and fail.

    This test is the regression guard for the WAL pragma in
    ``StateStore.__init__``. If a future change removes or weakens it, this
    test times out and the multi-process downloader+analyzer architecture
    silently loses its concurrency guarantee.
    """
    db_path = tmp_path / "state.sqlite"

    # Writer — also the connection that initializes the schema + WAL pragma.
    writer = StateStore(db_path)
    # Sanity-check: verify WAL is active on the writer connection.
    mode = writer._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal", (
        f"StateStore must activate WAL journal mode; got {mode!r}. "
        "Rollback-journal mode would serialize reads against writes and "
        "break the downloader+analyzer concurrency guarantee."
    )

    # Reader — independent connection against the same file.
    reader = StateStore(db_path)
    try:
        # Begin a write transaction on the writer and hold it open. We use
        # BEGIN IMMEDIATE so the write lock is acquired now — this is the
        # scenario that would block a reader under rollback-journal mode.
        writer._conn.execute("BEGIN IMMEDIATE")
        writer._conn.execute(
            "UPDATE state SET in_absence = 1 WHERE id = 1"
        )

        # Concurrently, the reader does a get_state(). Under WAL this
        # returns immediately with the PRE-write snapshot (in_absence=False).
        # Under rollback-journal it would block until we COMMIT.
        loop = asyncio.get_running_loop()

        def do_read():
            return reader.get_state()

        read_state = await asyncio.wait_for(
            loop.run_in_executor(None, do_read),
            timeout=3.0,
        )
        # Reader saw the pre-write snapshot — in_absence stays False because
        # the writer hasn't committed yet.
        assert read_state.in_absence is False, (
            "under WAL, the reader should see the pre-commit snapshot"
        )

        # Commit the writer; a subsequent reader call now sees the new value.
        writer._conn.execute("COMMIT")
        read_state2 = await asyncio.wait_for(
            loop.run_in_executor(None, do_read),
            timeout=3.0,
        )
        assert read_state2.in_absence is True, (
            "after commit, a fresh read should see the new in_absence=True"
        )
    finally:
        # Best-effort rollback in case the test failed before COMMIT ran.
        try:
            writer._conn.execute("ROLLBACK")
        except Exception:
            pass
        reader.close()
        writer.close()

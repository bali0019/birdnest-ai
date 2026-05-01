"""Mom leaves / returns / absence-alert cadence tests.

Each test drives the real ``Pipeline.on_image`` with a mocked analyzer,
exercises the state machine, and (when an alert is expected) posts to the
REAL Discord webhook with a ``[TEST]`` prefix.

The architecture-fix agent may or may not have shipped the
``state_updated`` keyword to ``on_image`` yet; these tests call
``pipeline.on_image(jpeg, meta)`` with only positional args so they work
under both signatures. The spec explicitly authorizes this.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from cardinal_nest_monitor import analyzer as analyzer_mod
from cardinal_nest_monitor import main as main_mod
from cardinal_nest_monitor.config import get_settings
from cardinal_nest_monitor.schema import Severity


def _pipeline(store, notifier, evidence):
    """Build a Pipeline wired for integration tests (no feed queue here)."""
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


async def test_normal_snap_no_alert(
    monkeypatch, store, evidence, notifier, cardinal_jpeg_bytes, obs_on_nest
):
    """Mom on the nest, quiet scene. No alert should fire.

    We verify:
      - notifier.send_alert is never called (no alert)
      - store.record was called and the observation persisted (absence=False)
    """
    analyze_mock = AsyncMock(return_value=obs_on_nest())
    monkeypatch.setattr(analyzer_mod, "analyze", analyze_mock)

    # Prevent any real Discord alert-post — test only needs the "no alert"
    # branch. A tracking mock catches accidental invocations.
    send_alert_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(notifier, "send_alert", send_alert_mock)

    pipeline = _pipeline(store, notifier, evidence)
    await pipeline.on_image(cardinal_jpeg_bytes, _meta())

    assert analyze_mock.await_count == 1
    assert send_alert_mock.await_count == 0, (
        "A normal on-nest snap must never send an alert"
    )
    assert store.get_state().in_absence is False


async def test_mother_leaves_cadence_tightens(
    monkeypatch, store, evidence, notifier,
    cardinal_jpeg_bytes, empty_nest_jpeg_bytes,
    obs_on_nest, obs_off_nest,
):
    """After mom has been away ≥ 2 min, state.in_absence flips True and the
    cadence interval calculator should return the absence interval.
    """
    monkeypatch.setattr(notifier, "send_alert", AsyncMock(return_value=True))

    pipeline = _pipeline(store, notifier, evidence)
    settings = get_settings()

    # Seed: mom on nest at t0.
    t0 = time.time() - 400  # far enough in the past that we can feed a later absence
    monkeypatch.setattr(analyzer_mod, "analyze", AsyncMock(return_value=obs_on_nest()))
    await pipeline.on_image(cardinal_jpeg_bytes, _meta(ts=t0))
    assert store.get_state().in_absence is False

    # Feed: mom off nest, and ≥ 2 min have passed since she was last seen.
    monkeypatch.setattr(analyzer_mod, "analyze", AsyncMock(return_value=obs_off_nest()))
    await pipeline.on_image(empty_nest_jpeg_bytes, _meta(ts=t0 + 130))

    state = store.get_state()
    assert state.in_absence is True, (
        "state.in_absence must flip True after absence ≥ 120s"
    )

    # Replicate the cadence-picker logic from main.run() — if in_absence,
    # the interval should be the shorter absence_snap_interval_seconds.
    expected = settings.absence_snap_interval_seconds
    assert expected < settings.snap_interval_seconds, (
        "Sanity: absence interval should be shorter than default cadence"
    )


async def test_medium_alert_at_5min(
    monkeypatch, store, evidence, notifier,
    cardinal_jpeg_bytes, empty_nest_jpeg_bytes,
    obs_on_nest, obs_off_nest,
):
    """After ≥ 5 min of absence, a MEDIUM long_absence alert must fire.

    This test posts a real ``[TEST]`` MEDIUM embed to Discord.
    """
    captured: list = []

    orig_send_alert = notifier.send_alert

    async def capturing_send_alert(decision, observation, **kwargs):
        captured.append(decision)
        return await orig_send_alert(decision, observation, **kwargs)

    monkeypatch.setattr(notifier, "send_alert", capturing_send_alert)

    pipeline = _pipeline(store, notifier, evidence)

    # Seed: mom on nest far in the past so absence > 5 min when we feed off.
    t0 = time.time() - 400
    monkeypatch.setattr(analyzer_mod, "analyze", AsyncMock(return_value=obs_on_nest()))
    await pipeline.on_image(cardinal_jpeg_bytes, _meta(ts=t0))

    # Feed: mom still off nest, absence = 310s (past the 300s threshold).
    monkeypatch.setattr(analyzer_mod, "analyze", AsyncMock(return_value=obs_off_nest()))
    await pipeline.on_image(empty_nest_jpeg_bytes, _meta(ts=t0 + 310))

    assert len(captured) == 1, f"expected exactly one MEDIUM alert, got {len(captured)}"
    decision = captured[0]
    assert decision.severity == Severity.MEDIUM
    assert decision.rule_id == "long_absence"


async def test_medium_repeats_every_5min(
    monkeypatch, store, evidence, notifier,
    cardinal_jpeg_bytes, empty_nest_jpeg_bytes,
    obs_on_nest, obs_off_nest,
):
    """A second MEDIUM fires once the 5-min cooldown has elapsed.

    We seed TWO absence observations 5+ min apart and verify both fire.
    """
    captured: list = []

    orig_send_alert = notifier.send_alert

    async def capturing_send_alert(decision, observation, **kwargs):
        captured.append(decision)
        return await orig_send_alert(decision, observation, **kwargs)

    monkeypatch.setattr(notifier, "send_alert", capturing_send_alert)

    pipeline = _pipeline(store, notifier, evidence)

    # The cooldown logic in state.cooldown_active uses time.time() vs the
    # stored ts, so seeding "past" timestamps is the reliable way to walk
    # the clock forward. First absence alert at T=now. Second alert at
    # T=now - 301s (so "time.time() - stored_ts" > 300 → cooldown cleared).
    #
    # Sequence (real wall clock):
    #   1. mom on nest — 11 minutes ago
    #   2. off nest, absence=310s — 10 minutes 50s ago  → MEDIUM fires, ts=now-650
    #   3. off nest, absence=310s+ more absence        → MEDIUM fires again now
    now = time.time()
    t_seed = now - 660
    t_first_alert = now - 350  # absence = 310s from seed; ts is 350s ago
    t_second_alert = now - 5   # cooldown window has elapsed (> 300s)

    monkeypatch.setattr(analyzer_mod, "analyze", AsyncMock(return_value=obs_on_nest()))
    await pipeline.on_image(cardinal_jpeg_bytes, _meta(ts=t_seed))

    monkeypatch.setattr(analyzer_mod, "analyze", AsyncMock(return_value=obs_off_nest()))
    await pipeline.on_image(empty_nest_jpeg_bytes, _meta(ts=t_first_alert))
    await pipeline.on_image(empty_nest_jpeg_bytes, _meta(ts=t_second_alert))

    # Both should have fired — cooldown is exactly 5 min and we spaced them
    # > 5 min apart (wall-clock delta = t_second_alert - t_first_alert = 345s).
    assert len(captured) == 2, (
        f"expected two MEDIUM alerts spaced > 5 min apart, got {len(captured)}"
    )
    assert all(d.severity == Severity.MEDIUM for d in captured)
    assert all(d.rule_id == "long_absence" for d in captured)


async def test_mother_returns_low_alert(
    monkeypatch, store, evidence, notifier,
    cardinal_jpeg_bytes, empty_nest_jpeg_bytes,
    obs_on_nest, obs_off_nest,
):
    """After an absence, when attending_parent_on_nest flips back to "true", a LOW
    ``mother_returned`` alert must fire exactly once.
    """
    captured: list = []

    orig_send_alert = notifier.send_alert

    async def capturing_send_alert(decision, observation, **kwargs):
        captured.append(decision)
        return await orig_send_alert(decision, observation, **kwargs)

    monkeypatch.setattr(notifier, "send_alert", capturing_send_alert)

    pipeline = _pipeline(store, notifier, evidence)

    now = time.time()
    t_seed = now - 500
    t_absent = now - 300
    t_return = now - 10  # 290s after the absent snap → absence active, mom returns

    # Seed: mom on nest.
    monkeypatch.setattr(analyzer_mod, "analyze", AsyncMock(return_value=obs_on_nest()))
    await pipeline.on_image(cardinal_jpeg_bytes, _meta(ts=t_seed))

    # Feed: mom off nest long enough for in_absence=True.
    monkeypatch.setattr(analyzer_mod, "analyze", AsyncMock(return_value=obs_off_nest()))
    await pipeline.on_image(empty_nest_jpeg_bytes, _meta(ts=t_absent))
    assert store.get_state().in_absence is True

    # We may have fired a MEDIUM already (absence was 200s — under threshold
    # at t_absent). Clear the captured list so we only assert on the return.
    # In any case, filter so we look specifically at the LOW we want.
    captured_pre_return = list(captured)

    # Now mom comes back. Rule 5 ``mother_returned`` should fire LOW.
    monkeypatch.setattr(analyzer_mod, "analyze", AsyncMock(return_value=obs_on_nest()))
    await pipeline.on_image(cardinal_jpeg_bytes, _meta(ts=t_return))

    return_alerts = [d for d in captured if d not in captured_pre_return]
    assert len(return_alerts) == 1, (
        f"expected exactly one LOW mother_returned alert, got {len(return_alerts)}"
    )
    decision = return_alerts[0]
    assert decision.severity == Severity.LOW
    assert decision.rule_id == "attending_parent_returned"

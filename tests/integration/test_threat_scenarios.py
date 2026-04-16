"""Predator / threat rule tests.

Exercises rules 1 (direct_attack CRITICAL) and 3 (predator_absent HIGH) plus
the mockingbird-is-not-a-threat negative case. Each alert-firing test posts
a real ``[TEST]``-prefixed embed to Discord.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest

from cardinal_nest_monitor import analyzer as analyzer_mod
from cardinal_nest_monitor import main as main_mod
from cardinal_nest_monitor.schema import Severity


def _pipeline(store, notifier, evidence):
    counters = main_mod.DailyCounters()
    return main_mod.Pipeline(
        store=store,
        notifier=notifier,
        evidence=evidence,
        counters=counters,
        feed_queue=None,
    )


def _meta(ts: float | None = None) -> dict:
    return {
        "motion_triggered": False,
        "ts": ts if ts is not None else time.time(),
    }


async def test_high_alert_thrasher_at_nest(
    monkeypatch, store, evidence, notifier, thrasher_jpeg_bytes,
    obs_thrasher_near_nest,
):
    """Thrasher near the nest but not in the cup → HIGH ``predator_absent``.

    We disable the Opus verifier so this is a single-pass HIGH — verification
    behaviour is covered separately in test_verification.py.
    """
    # Disable Opus verification to keep this a pure rule-3 test.
    settings = __import__("cardinal_nest_monitor.config", fromlist=["get_settings"]).get_settings()
    monkeypatch.setattr(settings, "verify_alerts_with_opus", False)

    captured: list = []

    orig_send_alert = notifier.send_alert

    async def capturing_send_alert(decision, observation, **kwargs):
        captured.append(decision)
        return await orig_send_alert(decision, observation, **kwargs)

    monkeypatch.setattr(notifier, "send_alert", capturing_send_alert)

    monkeypatch.setattr(
        analyzer_mod, "analyze", AsyncMock(return_value=obs_thrasher_near_nest())
    )

    pipeline = _pipeline(store, notifier, evidence)
    await pipeline.on_image(thrasher_jpeg_bytes, _meta())

    assert len(captured) == 1, f"expected a HIGH alert, got {len(captured)} alerts"
    decision = captured[0]
    assert decision.severity == Severity.HIGH
    assert decision.rule_id == "predator_absent"
    assert "brown_thrasher" in decision.species


async def test_critical_direct_interaction(
    monkeypatch, store, evidence, notifier, thrasher_jpeg_bytes,
    obs_thrasher_direct_interaction,
):
    """Thrasher beak-in-cup → CRITICAL ``direct_attack``.

    Verifier disabled so this is single-pass. Verifier disagreement cases
    are covered in test_verification.py.
    """
    settings = __import__("cardinal_nest_monitor.config", fromlist=["get_settings"]).get_settings()
    monkeypatch.setattr(settings, "verify_alerts_with_opus", False)

    captured: list = []
    orig_send_alert = notifier.send_alert

    async def capturing_send_alert(decision, observation, **kwargs):
        captured.append(decision)
        return await orig_send_alert(decision, observation, **kwargs)

    monkeypatch.setattr(notifier, "send_alert", capturing_send_alert)

    monkeypatch.setattr(
        analyzer_mod, "analyze",
        AsyncMock(return_value=obs_thrasher_direct_interaction()),
    )

    pipeline = _pipeline(store, notifier, evidence)
    await pipeline.on_image(thrasher_jpeg_bytes, _meta())

    assert len(captured) == 1, f"expected a CRITICAL alert, got {len(captured)}"
    decision = captured[0]
    assert decision.severity == Severity.CRITICAL
    assert decision.rule_id == "direct_attack"
    assert "brown_thrasher" in decision.species


async def test_mockingbird_no_alert(
    monkeypatch, store, evidence, notifier, thrasher_jpeg_bytes,
    obs_mockingbird_near_nest,
):
    """Mockingbird near the nest but NOT in the threat enum → no alert."""
    send_alert_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(notifier, "send_alert", send_alert_mock)

    monkeypatch.setattr(
        analyzer_mod, "analyze",
        AsyncMock(return_value=obs_mockingbird_near_nest()),
    )

    pipeline = _pipeline(store, notifier, evidence)
    await pipeline.on_image(thrasher_jpeg_bytes, _meta())

    assert send_alert_mock.await_count == 0, (
        "Mockingbird should not produce any alert — it is not a threat species."
    )

"""Opus verification (blind second-opinion) integration tests.

The real ``analyzer.analyze`` function is monkeypatched with a helper that
returns DIFFERENT observations on its first vs. subsequent calls — which
simulates the two-model Sonnet → Opus handoff inside
``verifier.verify_alert``.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest

from birdnest_ai import analyzer as analyzer_mod
from birdnest_ai import main as main_mod
from birdnest_ai.config import get_settings
from birdnest_ai.schema import NestObservation, Severity


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


def _sequenced_analyzer(first: NestObservation, second: NestObservation):
    """Return an AsyncMock whose first await returns ``first`` and all
    subsequent awaits return ``second``. Matches the Sonnet→Opus call
    pattern inside the pipeline.
    """
    calls = {"n": 0}

    async def _fn(jpeg_bytes, *, model_override=None, extra_user_text=None):
        calls["n"] += 1
        return first if calls["n"] == 1 else second

    mock = AsyncMock(side_effect=_fn)
    return mock


async def test_verification_suppresses_false_critical(
    monkeypatch, store, evidence, notifier, cardinal_jpeg_bytes,
    obs_thrasher_direct_interaction, obs_on_nest,
):
    """Sonnet says CRITICAL direct-attack; Opus (second call) says "cardinal
    on nest" → the suite's disagreement rule must suppress the alert.

    Assert that ``send_alert`` is NEVER invoked. No Discord message should
    be posted for this test.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "verify_alerts_with_opus", True)

    # Sonnet sees a thrasher in the cup (false positive); Opus sees the
    # cardinal sitting normally.
    analyze_mock = _sequenced_analyzer(
        obs_thrasher_direct_interaction(),  # Sonnet
        obs_on_nest(),                      # Opus re-analysis
    )
    monkeypatch.setattr(analyzer_mod, "analyze", analyze_mock)

    send_alert_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(notifier, "send_alert", send_alert_mock)

    pipeline = _pipeline(store, notifier, evidence)
    await pipeline.on_image(cardinal_jpeg_bytes, _meta())

    # analyzer should have been called TWICE: Sonnet then Opus.
    assert analyze_mock.await_count == 2, (
        "Verification should have triggered a second analyzer call"
    )
    assert send_alert_mock.await_count == 0, (
        "Opus disagreement (no alert) must SUPPRESS the Sonnet alert"
    )


async def test_verification_downgrades(
    monkeypatch, store, evidence, notifier, thrasher_jpeg_bytes,
    obs_thrasher_direct_interaction, obs_thrasher_near_nest,
):
    """Sonnet says CRITICAL direct-attack; Opus says HIGH predator-near-nest
    → final alert fires at HIGH.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "verify_alerts_with_opus", True)

    analyze_mock = _sequenced_analyzer(
        obs_thrasher_direct_interaction(),  # Sonnet — CRITICAL
        obs_thrasher_near_nest(),           # Opus — HIGH (no cup contact)
    )
    monkeypatch.setattr(analyzer_mod, "analyze", analyze_mock)

    captured: list = []
    orig_send_alert = notifier.send_alert

    async def capturing_send_alert(decision, observation, **kwargs):
        captured.append((decision, kwargs.get("verification_obs")))
        return await orig_send_alert(decision, observation, **kwargs)

    monkeypatch.setattr(notifier, "send_alert", capturing_send_alert)

    pipeline = _pipeline(store, notifier, evidence)
    await pipeline.on_image(thrasher_jpeg_bytes, _meta())

    assert analyze_mock.await_count == 2
    assert len(captured) == 1, f"expected one downgraded alert, got {len(captured)}"
    decision, verification_obs = captured[0]
    assert decision.severity == Severity.HIGH, (
        f"Opus should have downgraded CRITICAL to HIGH, got {decision.severity}"
    )
    assert decision.rule_id == "predator_absent"
    assert verification_obs is not None, (
        "verification_obs must be passed to notifier on downgrade"
    )


async def test_verification_confirms(
    monkeypatch, store, evidence, notifier, thrasher_jpeg_bytes,
    obs_thrasher_near_nest,
):
    """Both passes say HIGH → alert fires with both Tier-2 and Verification
    fields in the embed. We assert by checking that
    ``verification_obs`` was passed into ``send_alert``.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "verify_alerts_with_opus", True)

    # Both Sonnet and Opus see the same thrasher-at-nest HIGH scenario.
    analyze_mock = _sequenced_analyzer(
        obs_thrasher_near_nest(),
        obs_thrasher_near_nest(summary="Confirmed — thrasher at nest (Opus)"),
    )
    monkeypatch.setattr(analyzer_mod, "analyze", analyze_mock)

    captured: list = []
    orig_send_alert = notifier.send_alert

    async def capturing_send_alert(decision, observation, **kwargs):
        captured.append((decision, kwargs.get("verification_obs")))
        return await orig_send_alert(decision, observation, **kwargs)

    monkeypatch.setattr(notifier, "send_alert", capturing_send_alert)

    pipeline = _pipeline(store, notifier, evidence)
    await pipeline.on_image(thrasher_jpeg_bytes, _meta())

    assert analyze_mock.await_count == 2
    assert len(captured) == 1
    decision, verification_obs = captured[0]
    assert decision.severity == Severity.HIGH
    assert decision.rule_id == "predator_absent"
    # Confirmation: both the original observation AND the verification are
    # present, which is what causes notifier to render both fields.
    assert verification_obs is not None
    assert "Opus" in verification_obs.summary

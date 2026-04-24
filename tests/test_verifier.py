"""Tests for the two-model verification disagreement logic.

The actual Opus API call is tested via integration / dryrun, not here.
This file covers the pure `compute_verification_decision()` function that
decides what to do when Sonnet's and Opus's AlertDecisions diverge.
"""

from __future__ import annotations

from cardinal_nest_monitor.schema import AlertDecision, Severity
from cardinal_nest_monitor.verifier import (
    compute_verification_decision,
    should_verify,
)


def _decision(severity: Severity, rule_id: str = "predator_absent") -> AlertDecision:
    """Build a minimal AlertDecision at the given severity."""
    return AlertDecision(
        severity=severity,
        title="test",
        summary="test",
        species=["brown_thrasher"],
        confidence=0.9,
        rule_id=rule_id,
    )


# ── should_verify ──────────────────────────────────────────────────────

def test_should_verify_true_for_critical():
    assert should_verify(_decision(Severity.CRITICAL)) is True


def test_should_verify_true_for_high():
    assert should_verify(_decision(Severity.HIGH)) is True


def test_should_verify_false_for_medium():
    assert should_verify(_decision(Severity.MEDIUM)) is False


def test_should_verify_false_for_low():
    assert should_verify(_decision(Severity.LOW)) is False


# ── compute_verification_decision ──────────────────────────────────────

def test_opus_suppresses_when_no_alert():
    """Sonnet says CRITICAL, Opus says None → suppress."""
    sonnet = _decision(Severity.CRITICAL, "direct_attack")
    result = compute_verification_decision(sonnet, None)
    assert result is None  # alert suppressed


def test_opus_downgrades_critical_to_high():
    """Sonnet says CRITICAL, Opus says HIGH → use Opus's (lower) decision."""
    sonnet = _decision(Severity.CRITICAL, "direct_attack")
    opus = _decision(Severity.HIGH, "predator_absent")
    result = compute_verification_decision(sonnet, opus)
    assert result is opus  # downgraded to HIGH


def test_opus_confirms_same_severity():
    """Sonnet says HIGH, Opus says HIGH → use Sonnet's decision (agreed)."""
    sonnet = _decision(Severity.HIGH)
    opus = _decision(Severity.HIGH)
    result = compute_verification_decision(sonnet, opus)
    assert result is sonnet  # confirm sonnet's decision


def test_opus_does_not_upgrade():
    """If Opus claims higher severity than Sonnet, we still fire Sonnet's
    decision (the verifier's job is to downgrade/suppress, not amplify)."""
    sonnet = _decision(Severity.HIGH)
    opus = _decision(Severity.CRITICAL, "direct_attack")
    result = compute_verification_decision(sonnet, opus)
    # Opus rank > sonnet rank → neither downgrade nor None → confirm sonnet
    assert result is sonnet


def test_opus_downgrades_high_to_medium():
    """Sonnet says HIGH, Opus says MEDIUM → downgrade."""
    sonnet = _decision(Severity.HIGH)
    opus = _decision(Severity.MEDIUM, "long_absence")
    result = compute_verification_decision(sonnet, opus)
    assert result is opus


def test_opus_downgrades_high_to_low():
    """Sonnet says HIGH, Opus says LOW → downgrade to LOW."""
    sonnet = _decision(Severity.HIGH)
    opus = _decision(Severity.LOW, "mother_returned")
    result = compute_verification_decision(sonnet, opus)
    assert result is opus


# ── Codex P2 round 4: verifier must forward is_backfill ──────────────

def test_verify_alert_forwards_is_backfill_to_evaluate():
    """The verifier path was dropping is_backfill, letting Opus emit
    state-relative decisions on stale snaps that could downgrade or
    suppress legitimate threat alerts. Verify the parameter is forwarded
    by stubbing both analyzer.analyze() and events.evaluate() and
    inspecting the call.
    """
    import asyncio
    from unittest.mock import patch, AsyncMock, MagicMock
    from cardinal_nest_monitor.schema import NestObservation, NestState
    from cardinal_nest_monitor import verifier as verifier_mod

    sonnet_obs = NestObservation(
        attending_parent_present="false", attending_parent_on_nest="false",
        eggs_visible="false", egg_count_estimate=None,
        nest_visible=True, nest_disturbed="false",
        species_detected=["brown_thrasher"],
        threat_species_detected=["brown_thrasher"],
        near_nest_activity=True, direct_nest_interaction=True,
        confidence=0.9, summary="Thrasher in cup.",
    )
    sonnet_decision = _decision(Severity.CRITICAL, "direct_attack")
    pre_state = NestState()
    fake_store = MagicMock()

    captured: dict[str, object] = {}

    def _fake_evaluate(obs, state, store, ts, is_backfill=False):
        captured["is_backfill"] = is_backfill
        return _decision(Severity.CRITICAL, "direct_attack")

    async def _run():
        with patch.object(verifier_mod, "analyzer_mod") as mock_analyzer, \
             patch.object(verifier_mod, "evaluate", side_effect=_fake_evaluate):
            mock_analyzer.analyze = AsyncMock(return_value=sonnet_obs)
            await verifier_mod.verify_alert(
                jpeg=b"x", sonnet_obs=sonnet_obs,
                sonnet_decision=sonnet_decision,
                pre_state=pre_state, store=fake_store, ts=1234.0,
                verification_model="claude-opus-4-7",
                is_backfill=True,
            )

    asyncio.run(_run())
    assert captured["is_backfill"] is True, (
        "verifier.verify_alert() must forward is_backfill into its "
        "internal evaluate() call (Codex P2 round 4)."
    )


def test_verify_alert_default_is_backfill_false():
    """Default is_backfill should remain False so existing live-alert
    callers (most calls) get the existing behavior."""
    import asyncio
    from unittest.mock import patch, AsyncMock, MagicMock
    from cardinal_nest_monitor.schema import NestObservation, NestState
    from cardinal_nest_monitor import verifier as verifier_mod

    sonnet_obs = NestObservation(
        attending_parent_present="false", attending_parent_on_nest="false",
        eggs_visible="false", egg_count_estimate=None,
        nest_visible=True, nest_disturbed="false",
        species_detected=["brown_thrasher"],
        threat_species_detected=["brown_thrasher"],
        near_nest_activity=True, direct_nest_interaction=True,
        confidence=0.9, summary="Thrasher in cup.",
    )
    sonnet_decision = _decision(Severity.CRITICAL, "direct_attack")
    pre_state = NestState()
    fake_store = MagicMock()

    captured: dict[str, object] = {}

    def _fake_evaluate(obs, state, store, ts, is_backfill=False):
        captured["is_backfill"] = is_backfill
        return _decision(Severity.CRITICAL, "direct_attack")

    async def _run():
        with patch.object(verifier_mod, "analyzer_mod") as mock_analyzer, \
             patch.object(verifier_mod, "evaluate", side_effect=_fake_evaluate):
            mock_analyzer.analyze = AsyncMock(return_value=sonnet_obs)
            await verifier_mod.verify_alert(
                jpeg=b"x", sonnet_obs=sonnet_obs,
                sonnet_decision=sonnet_decision,
                pre_state=pre_state, store=fake_store, ts=1234.0,
                verification_model="claude-opus-4-7",
            )  # no is_backfill arg → default

    asyncio.run(_run())
    assert captured["is_backfill"] is False


# ── Track 2: content-aware suppression (2026-04-17) ────────────────

def test_is_cardinal_positive_no_threat_helper():
    """Direct unit test of the helper."""
    from cardinal_nest_monitor.schema import NestObservation
    from cardinal_nest_monitor.verifier import is_cardinal_positive_no_threat

    def _obs(**overrides):
        base = dict(
            attending_parent_present="true", attending_parent_on_nest="true",
            eggs_visible="false", egg_count_estimate=None,
            nest_visible=True, nest_disturbed="false",
            species_detected=[], threat_species_detected=[],
            near_nest_activity=False, direct_nest_interaction=False,
            confidence=0.88, summary="",
        )
        base.update(overrides)
        return NestObservation(**base)

    # Cardinal in species, no threat → True
    assert is_cardinal_positive_no_threat(_obs(
        species_detected=["female northern cardinal"],
        threat_species_detected=[],
    )) is True
    # Case-insensitive substring match
    assert is_cardinal_positive_no_threat(_obs(
        species_detected=["Northern Cardinal"],
        threat_species_detected=[],
    )) is True
    # No cardinal mentioned → False (no override)
    assert is_cardinal_positive_no_threat(_obs(
        species_detected=["unknown bird"],
        threat_species_detected=[],
    )) is False
    # Cardinal AND threat detected → False (threats present, honor them)
    assert is_cardinal_positive_no_threat(_obs(
        species_detected=["female northern cardinal", "brown thrasher"],
        threat_species_detected=["brown_thrasher"],
    )) is False
    # Empty species list → False (can't confirm cardinal identity)
    assert is_cardinal_positive_no_threat(_obs(
        species_detected=[],
        threat_species_detected=[],
    )) is False


def test_cardinal_positive_no_threat_opus_suppresses_sonnet_high():
    """Replays today's 14:56:28 false-HIGH scenario end-to-end.

    Sonnet: unknown brownish bird at nest → HIGH predator_absent.
    Opus verification: female northern cardinal tending the nest, no
    threat, but direct_nest_interaction=true (schema violation on the
    cardinal). Opus's internal evaluate() would produce CRITICAL
    direct_attack (rank 4 ≥ rank 3 HIGH), which would pre-fix have
    "confirmed" Sonnet's HIGH. With the content-aware override, the
    alert must be suppressed.
    """
    import asyncio
    from unittest.mock import patch, AsyncMock, MagicMock
    from cardinal_nest_monitor.schema import NestObservation, NestState
    from cardinal_nest_monitor import verifier as verifier_mod

    sonnet_obs = NestObservation(
        attending_parent_present="uncertain", attending_parent_on_nest="uncertain",
        eggs_visible="false", egg_count_estimate=None,
        nest_visible=True, nest_disturbed="false",
        species_detected=["unknown brownish bird"],
        threat_species_detected=["unknown"],
        near_nest_activity=True, direct_nest_interaction=False,
        confidence=0.72, summary="Brownish bird at nest, crest not visible.",
    )
    # What Opus actually returned (from today's verification.json):
    opus_obs = NestObservation(
        attending_parent_present="true", attending_parent_on_nest="false",
        eggs_visible="uncertain", egg_count_estimate=None,
        nest_visible=True, nest_disturbed="false",
        species_detected=["female northern cardinal"],
        threat_species_detected=[],  # key: no threat
        near_nest_activity=False, direct_nest_interaction=True,  # schema violation
        confidence=0.88,
        summary="Female cardinal tending the cup.",
    )
    sonnet_decision = _decision(Severity.HIGH, "predator_absent")
    pre_state = NestState()
    fake_store = MagicMock()

    async def _run():
        with patch.object(verifier_mod, "analyzer_mod") as mock_analyzer:
            mock_analyzer.analyze = AsyncMock(return_value=opus_obs)
            final, _opus = await verifier_mod.verify_alert(
                jpeg=b"x", sonnet_obs=sonnet_obs,
                sonnet_decision=sonnet_decision,
                pre_state=pre_state, store=fake_store, ts=1234.0,
                verification_model="claude-opus-4-7",
            )
            return final

    final = asyncio.run(_run())
    assert final is None, (
        "Content-aware override must suppress the alert when Opus identifies "
        "the cardinal with no threat — even if Opus's rule-engine output "
        "would be CRITICAL-rank via a schema-violating direct_nest_interaction."
    )


def test_cardinal_positive_with_thrasher_does_not_override():
    """Negative control: if Opus names BOTH the cardinal AND a threat
    (e.g. thrasher chasing cardinal off the nest), the override must
    NOT fire — we still need to alert on the thrasher.
    """
    import asyncio
    from unittest.mock import patch, AsyncMock, MagicMock
    from cardinal_nest_monitor.schema import NestObservation, NestState
    from cardinal_nest_monitor import verifier as verifier_mod

    sonnet_obs = NestObservation(
        attending_parent_present="false", attending_parent_on_nest="false",
        eggs_visible="false", egg_count_estimate=None,
        nest_visible=True, nest_disturbed="false",
        species_detected=["brown thrasher"],
        threat_species_detected=["brown_thrasher"],
        near_nest_activity=True, direct_nest_interaction=True,
        confidence=0.9, summary="Thrasher at cup.",
    )
    opus_obs = NestObservation(
        attending_parent_present="true", attending_parent_on_nest="false",
        eggs_visible="false", egg_count_estimate=None,
        nest_visible=True, nest_disturbed="false",
        species_detected=["female northern cardinal", "brown thrasher"],
        threat_species_detected=["brown_thrasher"],  # key: still present
        near_nest_activity=True, direct_nest_interaction=True,
        confidence=0.88, summary="Cardinal nearby, thrasher at cup.",
    )
    sonnet_decision = _decision(Severity.CRITICAL, "direct_attack")
    pre_state = NestState()
    fake_store = MagicMock()

    async def _run():
        with patch.object(verifier_mod, "analyzer_mod") as mock_analyzer, \
             patch.object(verifier_mod, "evaluate",
                          return_value=_decision(Severity.CRITICAL, "direct_attack")):
            mock_analyzer.analyze = AsyncMock(return_value=opus_obs)
            final, _opus = await verifier_mod.verify_alert(
                jpeg=b"x", sonnet_obs=sonnet_obs,
                sonnet_decision=sonnet_decision,
                pre_state=pre_state, store=fake_store, ts=1234.0,
                verification_model="claude-opus-4-7",
            )
            return final

    final = asyncio.run(_run())
    assert final is not None, (
        "Override must not fire when a threat species is also present; "
        "the thrasher alert must still go through."
    )
    assert final.severity == Severity.CRITICAL


def test_cardinal_override_suppresses_critical_too():
    """Content override must work for CRITICAL just as for HIGH."""
    import asyncio
    from unittest.mock import patch, AsyncMock, MagicMock
    from cardinal_nest_monitor.schema import NestObservation, NestState
    from cardinal_nest_monitor import verifier as verifier_mod

    sonnet_obs = NestObservation(
        attending_parent_present="uncertain", attending_parent_on_nest="uncertain",
        eggs_visible="false", egg_count_estimate=None,
        nest_visible=True, nest_disturbed="false",
        species_detected=["unknown bird"],
        threat_species_detected=["unknown"],
        near_nest_activity=True, direct_nest_interaction=True,
        confidence=0.72, summary="Unknown bird with beak in cup.",
    )
    opus_obs = NestObservation(
        attending_parent_present="true", attending_parent_on_nest="true",
        eggs_visible="false", egg_count_estimate=None,
        nest_visible=True, nest_disturbed="false",
        species_detected=["female northern cardinal"],
        threat_species_detected=[],  # no threat
        near_nest_activity=False, direct_nest_interaction=False,
        confidence=0.9, summary="Female cardinal on nest.",
    )
    sonnet_decision = _decision(Severity.CRITICAL, "direct_attack")
    pre_state = NestState()
    fake_store = MagicMock()

    async def _run():
        with patch.object(verifier_mod, "analyzer_mod") as mock_analyzer:
            mock_analyzer.analyze = AsyncMock(return_value=opus_obs)
            final, _opus = await verifier_mod.verify_alert(
                jpeg=b"x", sonnet_obs=sonnet_obs,
                sonnet_decision=sonnet_decision,
                pre_state=pre_state, store=fake_store, ts=1234.0,
                verification_model="claude-opus-4-7",
            )
            return final

    final = asyncio.run(_run())
    assert final is None, (
        "Override must also suppress CRITICAL when Opus says cardinal+no threat."
    )


# ── finalize_verification pure helper direct tests ───────────────

def test_finalize_verification_suppresses_cardinal_positive_no_threat():
    """Direct test: Opus identifies cardinal + no threat → suppress."""
    from unittest.mock import MagicMock
    from cardinal_nest_monitor.schema import NestObservation, NestState
    from cardinal_nest_monitor.verifier import finalize_verification

    opus_obs = NestObservation(
        attending_parent_present="true", attending_parent_on_nest="false",
        eggs_visible="false", egg_count_estimate=None,
        nest_visible=True, nest_disturbed="false",
        species_detected=["female northern cardinal"],
        threat_species_detected=[],
        near_nest_activity=False, direct_nest_interaction=True,
        confidence=0.88, summary="cardinal tending nest",
    )
    sonnet_decision = _decision(Severity.HIGH, "predator_absent")
    final = finalize_verification(
        sonnet_decision, opus_obs, NestState(), MagicMock(), 1234.0,
    )
    assert final is None


def test_finalize_verification_forwards_is_backfill():
    """Direct test: is_backfill gets forwarded to evaluate()."""
    from unittest.mock import patch, MagicMock
    from cardinal_nest_monitor.schema import NestObservation, NestState
    from cardinal_nest_monitor import verifier as verifier_mod

    opus_obs = NestObservation(
        attending_parent_present="false", attending_parent_on_nest="false",
        eggs_visible="false", egg_count_estimate=None,
        nest_visible=True, nest_disturbed="false",
        species_detected=["brown thrasher"],
        threat_species_detected=["brown_thrasher"],
        near_nest_activity=True, direct_nest_interaction=False,
        confidence=0.9, summary="thrasher at nest",
    )
    sonnet_decision = _decision(Severity.HIGH, "predator_absent")
    captured = {}

    def _fake_evaluate(obs, state, store, ts, is_backfill=False):
        captured["is_backfill"] = is_backfill
        return _decision(Severity.HIGH, "predator_absent")

    with patch.object(verifier_mod, "evaluate", side_effect=_fake_evaluate):
        verifier_mod.finalize_verification(
            sonnet_decision, opus_obs, NestState(), MagicMock(), 1234.0,
            is_backfill=True,
        )
    assert captured["is_backfill"] is True

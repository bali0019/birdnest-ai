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

"""Cooldown semantics: suppression, breakthrough on escalation, per-species."""

from __future__ import annotations

import time

import pytest

from birdnest_ai.events import evaluate
from birdnest_ai.schema import NestObservation, Severity
from birdnest_ai.state import StateStore


def _make_obs(**kwargs) -> NestObservation:
    base = dict(
        attending_parent_present="false",
        attending_parent_on_nest="false",
        eggs_visible="false",
        egg_count_estimate=None,
        nest_visible=True,
        nest_disturbed="false",
        species_detected=["brown_thrasher"],
        threat_species_detected=["brown_thrasher"],
        near_nest_activity=True,
        direct_nest_interaction=True,
        confidence=0.9,
        summary="Thrasher at nest.",
    )
    base.update(kwargs)
    return NestObservation(**base)


@pytest.fixture
def store(tmp_path):
    s = StateStore(tmp_path / "state.sqlite")
    yield s
    s.close()


def _fire_and_record(store, obs, ts):
    state = store.record(ts, False, None, obs, None)
    decision = evaluate(obs, state, store, ts)
    if decision is not None:
        store.record_alert(decision, ts, None)
    return decision


def test_same_severity_within_window_suppressed(store):
    now = time.time()
    first = _fire_and_record(store, _make_obs(), now)
    assert first is not None and first.severity == Severity.CRITICAL

    second = _fire_and_record(store, _make_obs(), now + 10)
    assert second is None


def test_same_severity_outside_window_fires_again(store):
    now = time.time()
    first = _fire_and_record(store, _make_obs(), now)
    assert first is not None

    # Age the prior alert past the 60s direct_attack cooldown.
    store._conn.execute(
        "UPDATE alerts SET ts = ? WHERE severity = 'CRITICAL'",
        (time.time() - 120,),
    )
    second = _fire_and_record(store, _make_obs(), time.time())
    assert second is not None
    assert second.severity == Severity.CRITICAL


def test_escalation_breakthrough_higher_fires(store):
    now = time.time()
    # Seed mother-present then mother-absent so a HIGH (predator_absent) can fire.
    store.record(
        now - 300, False, None,
        _make_obs(
            attending_parent_on_nest="true",
            attending_parent_present="true",
            threat_species_detected=[],
            species_detected=["northern_cardinal"],
            direct_nest_interaction=False,
            summary="Mother.",
        ),
        None,
    )
    store.record(
        now - 150, False, None,
        _make_obs(
            attending_parent_on_nest="false",
            threat_species_detected=[],
            species_detected=[],
            direct_nest_interaction=False,
            summary="Empty.",
        ),
        None,
    )

    high_obs = _make_obs(direct_nest_interaction=False)  # → HIGH (predator_absent)
    high_decision = _fire_and_record(store, high_obs, now)
    assert high_decision is not None
    assert high_decision.severity == Severity.HIGH

    # Same species, escalates to CRITICAL → must break through.
    crit_decision = _fire_and_record(store, _make_obs(), now + 5)
    assert crit_decision is not None
    assert crit_decision.severity == Severity.CRITICAL


def test_lower_severity_within_window_suppressed(store):
    now = time.time()
    first = _fire_and_record(store, _make_obs(), now)
    assert first.severity == Severity.CRITICAL

    # Make state look like mother is absent so a HIGH could otherwise fire.
    store.record(
        now - 150, False, None,
        _make_obs(
            attending_parent_on_nest="true",
            attending_parent_present="true",
            threat_species_detected=[],
            species_detected=[],
            direct_nest_interaction=False,
            summary="Mother.",
        ),
        None,
    )
    store.record(
        now - 10, False, None,
        _make_obs(
            attending_parent_on_nest="false",
            threat_species_detected=[],
            species_detected=[],
            direct_nest_interaction=False,
            summary="Empty.",
        ),
        None,
    )

    high_obs = _make_obs(direct_nest_interaction=False)
    high_decision = _fire_and_record(store, high_obs, now + 30)
    assert high_decision is None  # CRITICAL outranks HIGH within window


def test_different_species_independent_cooldown(store):
    now = time.time()
    first = _fire_and_record(store, _make_obs(), now)
    assert first is not None

    squirrel = _make_obs(
        species_detected=["squirrel"],
        threat_species_detected=["squirrel"],
    )
    second = _fire_and_record(store, squirrel, now + 5)
    assert second is not None
    assert second.severity == Severity.CRITICAL
    assert "squirrel" in second.species


# ── Codex P2 round 4: cooldown queries must respect ts <= ref_ts ──────

def test_cooldown_active_ignores_future_alerts(store):
    """Reproduces Codex's exact case: alert recorded at ts=2000, then
    cooldown_active(... ts=1000) — must return False (the alert is in the
    future relative to ref_ts and should not be considered prior history).

    Without the fix, the SQL returned the future row and the Python
    `(ref - row_ts) < window_s` check fired True for the negative
    difference, silently suppressing legitimate older backfill alerts.
    """
    from birdnest_ai.schema import AlertDecision, Severity
    decision = AlertDecision(
        severity=Severity.CRITICAL,
        title="Future alert",
        summary="recorded at ts=2000",
        species=["brown_thrasher"],
        confidence=0.9,
        rule_id="direct_attack",
    )
    store.record_alert(decision, ts=2000.0, evidence_dir=None)

    # Now check cooldown for an OLDER reference ts.
    assert store.cooldown_active(
        Severity.CRITICAL, "brown_thrasher", window_s=300, ts=1000.0,
    ) is False, (
        "Future alert at ts=2000 must not be returned for cooldown query "
        "at ts=1000 (Codex P2 round 4)."
    )


def test_latest_alert_for_species_ignores_future_alerts(store):
    """Companion to the cooldown_active test — latest_alert_for_species()
    must also constrain to ts <= ref_ts, otherwise the breakthrough
    escalation logic could see a future higher-severity alert and refuse
    to fire a legitimate older alert during backfill drain.
    """
    from birdnest_ai.schema import AlertDecision, Severity
    decision = AlertDecision(
        severity=Severity.CRITICAL,
        title="Future alert",
        summary="recorded at ts=2000",
        species=["brown_thrasher"],
        confidence=0.9,
        rule_id="direct_attack",
    )
    store.record_alert(decision, ts=2000.0, evidence_dir=None)

    result = store.latest_alert_for_species(
        "brown_thrasher", window_s=10000, ts=1000.0,
    )
    assert result is None, (
        "Future alert at ts=2000 must not be returned for latest-alert "
        "query at ts=1000 (Codex P2 round 4)."
    )


def test_cooldown_still_works_for_historical_alerts(store):
    """Negative control: cooldown_active() must still return True when a
    LEGITIMATE prior alert (ts <= ref_ts) is within the window."""
    from birdnest_ai.schema import AlertDecision, Severity
    decision = AlertDecision(
        severity=Severity.CRITICAL,
        title="Real prior alert",
        summary="recorded at ts=900",
        species=["brown_thrasher"],
        confidence=0.9,
        rule_id="direct_attack",
    )
    store.record_alert(decision, ts=900.0, evidence_dir=None)

    # Reference ts after the alert — within 300s cooldown window.
    assert store.cooldown_active(
        Severity.CRITICAL, "brown_thrasher", window_s=300, ts=1000.0,
    ) is True


# ── Codex P2 round 5: rule-scoped cooldowns ────────────────────────

def test_lifecycle_low_does_not_silence_mother_returned(store):
    """Reproduces Codex's exact case: a recent LOW lifecycle alert
    (hatch / fledge / egg_laying_begin / incubation_begin) was silencing
    a real mother_returned alert because rule 5's cooldown was keyed to
    severity LOW with no rule-id constraint. Switching to
    rule_cooldown_active("attending_parent_returned", ...) must allow mother_returned
    to fire even if a LOW hatch alert was just recorded.
    """
    from birdnest_ai.events import evaluate
    from birdnest_ai.schema import AlertDecision, Severity, NestObservation

    now = time.time()
    # Seed state: she was here, then she left → in_absence=True.
    store.record(
        now - 600, False, None,
        _make_obs(
            attending_parent_on_nest="true",
            attending_parent_present="true",
            threat_species_detected=[],
            species_detected=["northern_cardinal"],
            direct_nest_interaction=False,
            summary="Mother on nest.",
        ),
        None,
    )
    out = _make_obs(
        attending_parent_on_nest="false",
        attending_parent_present="false",
        threat_species_detected=[],
        species_detected=[],
        direct_nest_interaction=False,
        summary="Nest empty.",
    )
    state_after = store.record(now - 300, False, None, out, None)
    assert state_after.in_absence is True

    # Insert an unrelated LOW lifecycle alert 10s before the would-be return.
    hatch_alert = AlertDecision(
        severity=Severity.LOW,
        title="🐣 Chicks hatched!",
        summary="Hatch confirmed.",
        species=[],
        confidence=0.9,
        rule_id="hatch",
    )
    store.record_alert(hatch_alert, ts=now - 10, evidence_dir=None)

    # Now a "mom is back" snap.
    return_obs = _make_obs(
        attending_parent_on_nest="true",
        attending_parent_present="true",
        threat_species_detected=[],
        species_detected=["northern_cardinal"],
        direct_nest_interaction=False,
        summary="Mom back on nest.",
    )
    pre_state = store.get_state()
    decision = evaluate(return_obs, pre_state, store, now)
    assert decision is not None, (
        "mother_returned must fire even when an unrelated LOW lifecycle "
        "alert (hatch) was recorded recently — Codex P2 round 5."
    )
    assert decision.rule_id == "attending_parent_returned"


def test_mother_returned_self_cooldown_still_works(store):
    """Negative control for the rule-scoped cooldown: a prior
    mother_returned alert within the window MUST still suppress a new
    mother_returned. We're scoping by rule_id, not removing the cooldown.
    """
    from birdnest_ai.events import evaluate
    from birdnest_ai.schema import AlertDecision, Severity

    now = time.time()
    store.record(
        now - 600, False, None,
        _make_obs(
            attending_parent_on_nest="true", attending_parent_present="true",
            threat_species_detected=[], species_detected=["northern_cardinal"],
            direct_nest_interaction=False, summary="Mother on nest.",
        ),
        None,
    )
    store.record(
        now - 300, False, None,
        _make_obs(
            attending_parent_on_nest="false", attending_parent_present="false",
            threat_species_detected=[], species_detected=[],
            direct_nest_interaction=False, summary="Empty.",
        ),
        None,
    )

    # Prior mother_returned 10s ago.
    prior = AlertDecision(
        severity=Severity.LOW,
        title="Mother returned",
        summary="prior return",
        species=[],
        confidence=0.9,
        rule_id="attending_parent_returned",
    )
    store.record_alert(prior, ts=now - 10, evidence_dir=None)

    return_obs = _make_obs(
        attending_parent_on_nest="true", attending_parent_present="true",
        threat_species_detected=[], species_detected=["northern_cardinal"],
        direct_nest_interaction=False, summary="Mom back.",
    )
    pre_state = store.get_state()
    decision = evaluate(return_obs, pre_state, store, now)
    assert decision is None, (
        "Two mother_returned alerts within 5min cooldown must be suppressed."
    )


def test_rule_cooldown_active_basic():
    """Direct test of the new helper."""
    import tempfile
    from pathlib import Path
    from birdnest_ai.schema import AlertDecision, Severity

    with tempfile.TemporaryDirectory() as td:
        s = StateStore(Path(td) / "state.sqlite")
        try:
            decision = AlertDecision(
                severity=Severity.LOW, title="t", summary="s", species=[],
                confidence=0.9, rule_id="hatch",
            )
            s.record_alert(decision, ts=1000.0, evidence_dir=None)

            # Same rule, within window → blocked
            assert s.rule_cooldown_active("hatch", window_s=300, ts=1100.0) is True
            # Different rule → not blocked
            assert s.rule_cooldown_active("fledge", window_s=300, ts=1100.0) is False
            # Same rule, outside window → not blocked
            assert s.rule_cooldown_active("hatch", window_s=50, ts=1100.0) is False
            # Future alert relative to ref_ts → not blocked (ts<=ref guard)
            assert s.rule_cooldown_active("hatch", window_s=300, ts=999.0) is False
        finally:
            s.close()

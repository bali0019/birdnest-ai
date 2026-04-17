"""Cooldown semantics: suppression, breakthrough on escalation, per-species."""

from __future__ import annotations

import time

import pytest

from cardinal_nest_monitor.events import evaluate
from cardinal_nest_monitor.schema import NestObservation, Severity
from cardinal_nest_monitor.state import StateStore


def _make_obs(**kwargs) -> NestObservation:
    base = dict(
        mother_cardinal_present="false",
        cardinal_on_nest="false",
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
            cardinal_on_nest="true",
            mother_cardinal_present="true",
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
            cardinal_on_nest="false",
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
            cardinal_on_nest="true",
            mother_cardinal_present="true",
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
            cardinal_on_nest="false",
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
    from cardinal_nest_monitor.schema import AlertDecision, Severity
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
    from cardinal_nest_monitor.schema import AlertDecision, Severity
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
    from cardinal_nest_monitor.schema import AlertDecision, Severity
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

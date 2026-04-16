"""Tests for analytics.compute_report — trip detection + aggregation."""

from __future__ import annotations

import time

import pytest

from cardinal_nest_monitor.analytics import compute_report
from cardinal_nest_monitor.schema import NestObservation
from cardinal_nest_monitor.state import StateStore


def _make_obs(cardinal_on_nest: str, **overrides) -> NestObservation:
    base = dict(
        mother_cardinal_present="true" if cardinal_on_nest == "true" else "false",
        cardinal_on_nest=cardinal_on_nest,
        eggs_visible="false",
        egg_count_estimate=None,
        nest_visible=True,
        nest_disturbed="false",
        species_detected=["northern_cardinal"] if cardinal_on_nest == "true" else [],
        threat_species_detected=[],
        near_nest_activity=False,
        direct_nest_interaction=False,
        confidence=0.9,
        summary=f"cardinal_on_nest={cardinal_on_nest}",
    )
    base.update(overrides)
    return NestObservation(**base)


@pytest.fixture
def store(tmp_path):
    s = StateStore(tmp_path / "state.sqlite")
    yield s
    s.close()


# ── Baseline ───────────────────────────────────────────────────────────

def test_no_observations_returns_zero_counts(store):
    now = time.time()
    report = compute_report(store, now, window_hours=8)
    assert report["trips"]["trip_count"] == 0
    assert report["trips"]["trip_records"] == []
    assert report["threats"]["total_events"] == 0
    assert report["alerts"]["total"] == 0
    assert report["system"]["snaps_taken"] == 0
    # Unknown time fills the whole window when there are no observations
    assert report["presence"]["unknown_s"] > 0


# ── Trip detection ─────────────────────────────────────────────────────

def test_single_foraging_trip_detected(store):
    t0 = time.time() - 3600  # 1 hour ago
    store.record(t0, False, None, _make_obs("true"), None)
    store.record(t0 + 600, False, None, _make_obs("false"), None)   # left
    store.record(t0 + 1200, False, None, _make_obs("true"), None)   # returned
    report = compute_report(store, time.time(), window_hours=2)
    assert report["trips"]["trip_count"] == 1
    assert report["trips"]["trip_records"][0]["duration_s"] == 600
    assert report["trips"]["longest"]["duration_s"] == 600
    assert not report["trips"]["currently_away"]


def test_multiple_trips_detected(store):
    t0 = time.time() - 7200  # 2 hours ago
    # Trip 1: 300s
    store.record(t0, False, None, _make_obs("true"), None)
    store.record(t0 + 100, False, None, _make_obs("false"), None)
    store.record(t0 + 400, False, None, _make_obs("true"), None)
    # Trip 2: 600s
    store.record(t0 + 1000, False, None, _make_obs("false"), None)
    store.record(t0 + 1600, False, None, _make_obs("true"), None)
    # Trip 3: 180s
    store.record(t0 + 2000, False, None, _make_obs("false"), None)
    store.record(t0 + 2180, False, None, _make_obs("true"), None)
    report = compute_report(store, time.time(), window_hours=3)
    assert report["trips"]["trip_count"] == 3
    durations = [t["duration_s"] for t in report["trips"]["trip_records"]]
    assert sorted(durations) == [180, 300, 600]
    assert report["trips"]["longest"]["duration_s"] == 600


def test_partial_trip_at_window_end_flags_currently_away(store):
    t0 = time.time() - 1800  # 30 min ago
    store.record(t0, False, None, _make_obs("true"), None)
    store.record(t0 + 600, False, None, _make_obs("false"), None)  # left, hasn't returned
    report = compute_report(store, time.time(), window_hours=1)
    assert report["trips"]["trip_count"] == 0  # incomplete trip not counted
    assert report["trips"]["currently_away"]
    assert report["trips"]["currently_away_duration_s"] > 0


def test_uncertain_observations_do_not_flip_transitions(store):
    t0 = time.time() - 1800
    store.record(t0, False, None, _make_obs("true"), None)
    # Uncertain observations scattered — should be ignored for transition detection
    store.record(t0 + 100, False, None, _make_obs("uncertain"), None)
    store.record(t0 + 200, False, None, _make_obs("uncertain"), None)
    store.record(t0 + 300, False, None, _make_obs("false"), None)   # real leave
    store.record(t0 + 400, False, None, _make_obs("uncertain"), None)
    store.record(t0 + 500, False, None, _make_obs("true"), None)    # real return
    report = compute_report(store, time.time(), window_hours=1)
    assert report["trips"]["trip_count"] == 1
    assert report["trips"]["trip_records"][0]["duration_s"] == 200


def test_low_confidence_observations_ignored(store):
    """Low-confidence observations shouldn't disturb trip detection."""
    t0 = time.time() - 1800
    store.record(t0, False, None, _make_obs("true"), None)
    # Low-confidence "false" (confidence=0.30) — should NOT count as a leave
    store.record(t0 + 100, False, None, _make_obs("false", confidence=0.30), None)
    store.record(t0 + 200, False, None, _make_obs("true"), None)
    report = compute_report(store, time.time(), window_hours=1)
    assert report["trips"]["trip_count"] == 0


# ── Presence + threats + system ────────────────────────────────────────

def test_threats_counted_by_species(store):
    t0 = time.time() - 3600
    store.record(t0, False, None, _make_obs("true"), None)
    # Thrasher sighting
    store.record(t0 + 500, False, None, _make_obs(
        "false",
        threat_species_detected=["brown_thrasher"],
        near_nest_activity=True,
        summary="Brown thrasher near nest.",
    ), None)
    # Squirrel sighting
    store.record(t0 + 1000, False, None, _make_obs(
        "false",
        threat_species_detected=["squirrel"],
        near_nest_activity=False,
        summary="Squirrel in yard.",
    ), None)
    report = compute_report(store, time.time(), window_hours=2)
    assert report["threats"]["total_events"] == 2
    assert report["threats"]["by_species"].get("brown_thrasher") == 1
    assert report["threats"]["by_species"].get("squirrel") == 1
    assert report["threats"]["near_nest_events"] == 1


def test_alerts_aggregated_by_severity(store):
    from cardinal_nest_monitor.schema import AlertDecision, Severity
    t0 = time.time() - 1800
    for sev, rule in [
        (Severity.HIGH, "predator_absent"),
        (Severity.LOW, "mother_returned"),
        (Severity.LOW, "mother_returned"),
    ]:
        decision = AlertDecision(
            severity=sev,
            title="test",
            summary="test",
            species=["brown_thrasher"] if sev == Severity.HIGH else [],
            confidence=0.9,
            rule_id=rule,
        )
        store.record_alert(decision, t0 + 100, None)
        t0 += 60
    report = compute_report(store, time.time(), window_hours=1)
    assert report["alerts"]["total"] == 3
    assert report["alerts"]["by_severity"]["HIGH"] == 1
    assert report["alerts"]["by_severity"]["LOW"] == 2
    assert report["alerts"]["by_rule"]["mother_returned"] == 2


def test_system_metrics_snap_counts(store):
    t0 = time.time() - 600
    # 3 successful snaps
    for i in range(3):
        store.record(t0 + i * 60, False, None, _make_obs("true"), None)
    # 1 "failed" snap (observation=None simulates analyzer failure)
    store.record(t0 + 300, False, None, None, None)
    report = compute_report(store, time.time(), window_hours=1)
    assert report["system"]["snaps_taken"] == 4
    assert report["system"]["analyzer_failures"] == 1
    # Cost is approximate — just verify it's non-zero
    assert report["system"]["cost_window_usd"] > 0

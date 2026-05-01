"""Tests for analytics.compute_report — trip detection + aggregation."""

from __future__ import annotations

import time

import pytest

from birdnest_ai.analytics import compute_report
from birdnest_ai.schema import NestObservation
from birdnest_ai.state import StateStore


def _make_obs(attending_parent_on_nest: str, **overrides) -> NestObservation:
    base = dict(
        attending_parent_present="true" if attending_parent_on_nest == "true" else "false",
        attending_parent_on_nest=attending_parent_on_nest,
        eggs_visible="false",
        egg_count_estimate=None,
        nest_visible=True,
        nest_disturbed="false",
        species_detected=["northern_cardinal"] if attending_parent_on_nest == "true" else [],
        threat_species_detected=[],
        near_nest_activity=False,
        direct_nest_interaction=False,
        confidence=0.9,
        summary=f"attending_parent_on_nest={attending_parent_on_nest}",
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
    from birdnest_ai.schema import AlertDecision, Severity
    t0 = time.time() - 1800
    for sev, rule in [
        (Severity.HIGH, "predator_absent"),
        (Severity.LOW, "attending_parent_returned"),
        (Severity.LOW, "attending_parent_returned"),
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
    assert report["alerts"]["by_rule"]["attending_parent_returned"] == 2


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


# ── Codex round 3: analytics IR coercion (sunset → 23:00 gap) ────────

def test_dusk_ir_false_off_does_not_invent_trip(store, monkeypatch):
    """Regression: the live alert path suppresses MEDIUM long_absence on
    IR-detected frames (events.py rule 4 + observation_indicates_ir_mode).
    Analytics must do the same coercion in _trip_detection or it would
    invent phantom foraging trips on IR false-negatives at dusk.

    Reproduces by seeding a sequence: she's on the nest pre-dusk, then
    several IR frames return attending_parent_on_nest="false" with summaries that
    mention IR mode, then she's back on the nest. Without the coercion,
    this would register as a trip with the IR window's duration. With
    the coercion, the IR frames are presumed on-nest and no trip fires.
    """
    from birdnest_ai.config import get_settings
    settings = get_settings()
    # Disable wall-clock quiet hours so ONLY the IR-text coercion can save us.
    monkeypatch.setattr(settings, "quiet_hours", "")

    t0 = time.time() - 3600
    # Pre-dusk: she's on the nest.
    store.record(t0, False, None, _make_obs("true"), None)
    # IR period: 5 frames returning "false" with IR-mode summaries.
    for i in range(5):
        store.record(
            t0 + 60 + i * 60, False, None,
            _make_obs(
                "false",
                summary=(
                    "Infrared night image — compact bird shape in cup is "
                    "consistent with the female cardinal but species cannot "
                    "be confirmed in grayscale IR."
                ),
                confidence=0.62,
            ),
            None,
        )
    # Dawn-equivalent: she's back, in daylight.
    store.record(t0 + 600, False, None, _make_obs("true"), None)

    report = compute_report(store, time.time(), window_hours=1)
    assert report["trips"]["trip_count"] == 0, (
        "Analytics must coerce IR-summary frames as on-nest, same as the "
        "live alert path — otherwise it invents phantom dusk trips."
    )


def test_dusk_ir_does_not_inflate_off_nest_seconds(store, monkeypatch):
    """Same scenario for _presence_totals: IR-detected frames must be
    presumed on-nest so the report doesn't claim multi-minute off-nest
    time during dusk windows that the live path now correctly suppresses.
    """
    from birdnest_ai.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "quiet_hours", "")

    t0 = time.time() - 3600
    store.record(t0, False, None, _make_obs("true"), None)
    for i in range(5):
        store.record(
            t0 + 60 + i * 60, False, None,
            _make_obs(
                "false",
                summary="Grayscale IR view; bird in cup, identity uncertain.",
                confidence=0.62,
            ),
            None,
        )
    store.record(t0 + 600, False, None, _make_obs("true"), None)

    report = compute_report(store, time.time(), window_hours=1)
    # IR frames should NOT contribute off-nest time.
    assert report["presence"]["off_nest_s"] == 0, (
        "IR-summary frames must NOT count as off-nest time."
    )


def test_dusk_non_ir_off_frame_still_counts_as_off(store, monkeypatch):
    """Sanity check: a real off-nest snap (no IR phrasing in summary)
    OUTSIDE quiet hours must still register. The IR coercion must not
    silence legitimate daytime absences.
    """
    from birdnest_ai.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "quiet_hours", "")

    t0 = time.time() - 1800
    store.record(t0, False, None, _make_obs("true"), None)
    store.record(
        t0 + 60, False, None,
        _make_obs("false", summary="Daylight: nest empty, mom foraging."),
        None,
    )
    store.record(
        t0 + 360, False, None,
        _make_obs("true", summary="Mom returned, on the nest."),
        None,
    )

    report = compute_report(store, time.time(), window_hours=1)
    assert report["trips"]["trip_count"] == 1, (
        "Daylight off-nest must still register as a trip."
    )

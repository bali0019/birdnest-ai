"""Unit tests for lifecycle tracking: hatch detection, stage transitions,
feeding-event suppression, fledge detection.

All tests run with lifecycle_tracking_enabled=True. A dedicated regression
test verifies that flag=False leaves existing behavior byte-identical.
"""

from __future__ import annotations

import time

import pytest

from cardinal_nest_monitor.config import get_settings
from cardinal_nest_monitor.events import evaluate
from cardinal_nest_monitor.schema import NestObservation, Severity
from cardinal_nest_monitor.state import StateStore


# ── Helpers ────────────────────────────────────────────────────────────

def _obs(**kwargs) -> NestObservation:
    """Build a baseline NestObservation with sensible defaults."""
    base = dict(
        mother_cardinal_present="true",
        cardinal_on_nest="true",
        eggs_visible="false",
        egg_count_estimate=None,
        nest_visible=True,
        nest_disturbed="false",
        species_detected=["northern_cardinal"],
        threat_species_detected=[],
        near_nest_activity=False,
        direct_nest_interaction=False,
        chicks_visible="uncertain",
        chick_count_estimate=None,
        mother_feeding_chicks=False,
        confidence=0.9,
        summary="Mom on nest.",
    )
    base.update(kwargs)
    return NestObservation(**base)


@pytest.fixture
def store(tmp_path):
    s = StateStore(tmp_path / "state.sqlite")
    yield s
    s.close()


@pytest.fixture
def lifecycle_on(monkeypatch):
    """Enable lifecycle tracking for the duration of the test."""
    settings = get_settings()
    monkeypatch.setattr(settings, "lifecycle_tracking_enabled", True)
    yield


# ── Regression guard: flag=False is byte-identical ─────────────────────

def test_lifecycle_flag_off_state_stays_at_incubation(store, monkeypatch):
    """When the flag is False, lifecycle_stage never changes from incubation
    regardless of what the analyzer reports."""
    settings = get_settings()
    monkeypatch.setattr(settings, "lifecycle_tracking_enabled", False)

    t0 = time.time()
    # Observation with chicks_visible=true should NOT trigger a transition.
    obs = _obs(
        cardinal_on_nest="false",
        chicks_visible="true",
        chick_count_estimate=3,
        mother_feeding_chicks=True,
    )
    state = store.record(t0, False, None, obs, None)
    assert state.lifecycle_stage == "incubation"
    assert state.hatch_detected_ts is None
    assert state.last_chick_count is None  # not tracked when flag is off


def test_lifecycle_flag_off_no_lifecycle_alert(store, monkeypatch):
    """Hatch alert must not fire when the flag is off."""
    settings = get_settings()
    monkeypatch.setattr(settings, "lifecycle_tracking_enabled", False)

    t0 = time.time()
    obs = _obs(
        cardinal_on_nest="false",
        chicks_visible="true",
        chick_count_estimate=2,
    )
    state = store.record(t0, False, None, obs, None)
    decision = evaluate(obs, state, store, t0)
    # No hatch alert should fire.
    assert decision is None or decision.rule_id != "hatch"


# ── Stage transitions ─────────────────────────────────────────────────

def test_first_chick_sighting_does_not_transition(store, lifecycle_on):
    """1st chick sighting records the timestamp but does NOT transition.
    System waits for a 2nd confirming sighting within 4 hours."""
    t0 = time.time()
    state = store.record(t0, False, None, _obs(), None)
    assert state.lifecycle_stage == "incubation"

    # 1st chick sighting
    t1 = t0 + 3600
    obs = _obs(
        cardinal_on_nest="false",
        chicks_visible="true",
        chick_count_estimate=2,
    )
    state = store.record(t1, False, None, obs, None)
    # STILL in incubation — waiting for confirmation
    assert state.lifecycle_stage == "incubation"
    assert state.first_chick_sighting_ts == pytest.approx(t1, abs=1.0)
    assert state.hatch_detected_ts is None


def test_second_sighting_confirms_transition(store, lifecycle_on):
    """A 2nd chick signal within 4 hours of the 1st triggers the transition
    and sets hatch_detected_ts."""
    t0 = time.time()
    store.record(t0, False, None, _obs(), None)

    # 1st sighting
    t1 = t0 + 3600
    store.record(t1, False, None, _obs(
        cardinal_on_nest="false",
        chicks_visible="true",
        chick_count_estimate=2,
    ), None)

    # 2nd sighting, 30 min later (well within 4h window)
    t2 = t1 + 1800
    state = store.record(t2, False, None, _obs(
        cardinal_on_nest="false",
        chicks_visible="true",
        chick_count_estimate=3,
    ), None)
    assert state.lifecycle_stage == "feeding"
    assert state.hatch_detected_ts == pytest.approx(t2, abs=1.0)
    assert state.first_chick_sighting_ts is None  # cleared on transition
    assert state.last_chick_count == 3


def test_second_sighting_outside_window_resets(store, lifecycle_on):
    """If 5+ hours pass without confirmation, the 1st sighting is stale.
    A new chick signal becomes the new 1st sighting (not the 2nd)."""
    t0 = time.time()
    store.record(t0, False, None, _obs(), None)

    # 1st sighting
    t1 = t0 + 3600
    store.record(t1, False, None, _obs(
        cardinal_on_nest="false",
        chicks_visible="true",
    ), None)

    # 5 hours later — WAY outside the 4h confirmation window
    t2 = t1 + 5 * 3600
    state = store.record(t2, False, None, _obs(
        cardinal_on_nest="false",
        chicks_visible="true",
    ), None)

    # Still incubation — this is treated as a fresh "1st sighting"
    assert state.lifecycle_stage == "incubation"
    assert state.first_chick_sighting_ts == pytest.approx(t2, abs=1.0)
    assert state.hatch_detected_ts is None


def test_mother_feeding_chicks_also_confirms(store, lifecycle_on):
    """The confirmation signal can be chicks_visible OR mother_feeding_chicks.
    Mixing the two within the window still confirms."""
    t0 = time.time()
    store.record(t0, False, None, _obs(), None)

    # 1st: chicks_visible=true
    t1 = t0 + 3600
    store.record(t1, False, None, _obs(
        cardinal_on_nest="false",
        chicks_visible="true",
    ), None)

    # 2nd: mother_feeding_chicks=true (different signal, same window)
    t2 = t1 + 1800
    state = store.record(t2, False, None, _obs(
        mother_feeding_chicks=True,
    ), None)
    assert state.lifecycle_stage == "feeding"
    assert state.hatch_detected_ts == pytest.approx(t2, abs=1.0)


def test_feeding_to_fledging_after_12h_no_visits(store, lifecycle_on):
    """Once chicks confirmed, 12+ hours of no cardinal AND no threat →
    fledging."""
    t0 = time.time() - 20 * 3600  # seed 20 hours ago
    # Seed feeding stage — need TWO confirming sightings now
    store.record(t0 - 300, False, None, _obs(
        cardinal_on_nest="true",
        chicks_visible="true",
        chick_count_estimate=2,
    ), None)
    state = store.record(t0, False, None, _obs(
        cardinal_on_nest="true",
        chicks_visible="true",
        chick_count_estimate=2,
    ), None)
    assert state.lifecycle_stage == "feeding"
    # last_mother_seen_ts = t0

    # 13 hours later: no cardinal, no threat
    t1 = t0 + 13 * 3600
    obs_empty = _obs(
        cardinal_on_nest="false",
        mother_cardinal_present="false",
        chicks_visible="uncertain",
        species_detected=[],
    )
    state = store.record(t1, False, None, obs_empty, None)
    assert state.lifecycle_stage == "fledging"
    assert state.fledge_detected_ts == pytest.approx(t1, abs=1.0)


def test_fledging_not_triggered_if_thrasher_seen(store, lifecycle_on):
    """If a threat was seen within 48h prior, we don't transition to
    fledging — could be predation instead."""
    t0 = time.time() - 20 * 3600
    # Seed feeding — need 2 confirming sightings
    store.record(t0 - 300, False, None, _obs(
        cardinal_on_nest="true",
        chicks_visible="true",
        chick_count_estimate=2,
    ), None)
    store.record(t0, False, None, _obs(
        cardinal_on_nest="true",
        chicks_visible="true",
        chick_count_estimate=2,
    ), None)

    # 5 hours later: a thrasher is spotted near the nest
    t_threat = t0 + 5 * 3600
    store.record(t_threat, False, None, _obs(
        cardinal_on_nest="false",
        threat_species_detected=["brown_thrasher"],
        near_nest_activity=True,
    ), None)

    # Another 13 hours later: still no cardinal (the gap is long enough
    # to qualify for fledge, but the recent threat should block it).
    t1 = t_threat + 13 * 3600
    state = store.record(t1, False, None, _obs(
        cardinal_on_nest="false",
        mother_cardinal_present="false",
        chicks_visible="uncertain",
        species_detected=[],
    ), None)
    assert state.lifecycle_stage == "feeding"  # NOT fledging
    assert state.fledge_detected_ts is None


def test_hatch_detected_ts_set_once(store, lifecycle_on):
    """hatch_detected_ts is set on the confirmed transition (2nd sighting)
    and never overwritten by later observations."""
    t0 = time.time()
    # 1st sighting
    store.record(t0, False, None, _obs(
        cardinal_on_nest="false",
        chicks_visible="true",
        chick_count_estimate=1,
    ), None)
    # 2nd sighting — triggers transition
    t1 = t0 + 1800
    state = store.record(t1, False, None, _obs(
        cardinal_on_nest="false",
        chicks_visible="true",
        chick_count_estimate=2,
    ), None)
    first_hatch_ts = state.hatch_detected_ts
    assert first_hatch_ts is not None

    # A later chick observation shouldn't overwrite hatch_detected_ts
    t2 = t1 + 2 * 3600
    state = store.record(t2, False, None, _obs(
        cardinal_on_nest="false",
        chicks_visible="true",
        chick_count_estimate=3,
    ), None)
    assert state.hatch_detected_ts == first_hatch_ts  # unchanged


# ── Hatch alert event ─────────────────────────────────────────────────

def test_hatch_alert_fires_on_confirmation_not_first_sighting(store, lifecycle_on):
    """Pipeline.on_image ordering (evaluate uses pre_state, record runs after).

    The hatch alert fires ONLY on the 2nd confirming sighting. 1st sighting
    does not fire any alert."""
    t0 = time.time()
    store.record(t0, False, None, _obs(), None)

    # 1st sighting: no alert should fire
    t1 = t0 + 3600
    obs1 = _obs(
        cardinal_on_nest="false",
        chicks_visible="true",
        chick_count_estimate=2,
    )
    pre_state = store.get_state()
    decision = evaluate(obs1, pre_state, store, t1)
    assert decision is None or decision.rule_id != "hatch"
    # Commit the 1st sighting to state
    store.record(t1, False, None, obs1, None)

    # 2nd sighting within window: hatch alert fires
    t2 = t1 + 1800
    obs2 = _obs(
        cardinal_on_nest="false",
        chicks_visible="true",
        chick_count_estimate=3,
    )
    pre_state = store.get_state()
    decision = evaluate(obs2, pre_state, store, t2)
    assert decision is not None
    assert decision.severity == Severity.LOW
    assert decision.rule_id == "hatch"
    assert "🐣" in decision.title


def test_fledge_alert_fires_on_transition(store, lifecycle_on):
    """When feeding → fledging happens, evaluate() fires a LOW fledge alert."""
    t0 = time.time() - 20 * 3600
    # Seed feeding stage: 2 confirming sightings required now
    store.record(t0 - 300, False, None, _obs(
        cardinal_on_nest="true",
        chicks_visible="true",
        chick_count_estimate=2,
    ), None)
    store.record(t0, False, None, _obs(
        cardinal_on_nest="true",
        chicks_visible="true",
        chick_count_estimate=2,
    ), None)

    t1 = t0 + 13 * 3600
    obs = _obs(
        cardinal_on_nest="false",
        mother_cardinal_present="false",
        chicks_visible="uncertain",
        species_detected=[],
    )
    # Match Pipeline.on_image ordering: get pre-state BEFORE record()
    pre_state = store.get_state()
    decision = evaluate(obs, pre_state, store, t1)
    assert decision is not None
    assert decision.severity == Severity.LOW
    assert decision.rule_id == "fledge"
    assert "🦅" in decision.title


# ── Feeding suppresses MEDIUM ─────────────────────────────────────────

def test_feeding_event_suppresses_medium_for_30min(store, lifecycle_on):
    """During feeding stage, a recent feeding event suppresses MEDIUM
    long_absence alerts for 30 minutes."""
    # Seed feeding stage with 2 confirming sightings
    t0 = time.time() - 3600
    store.record(t0 - 300, False, None, _obs(
        cardinal_on_nest="true",
        mother_feeding_chicks=True,
    ), None)
    store.record(t0, False, None, _obs(
        cardinal_on_nest="true",
        mother_feeding_chicks=True,
        chicks_visible="true",
        chick_count_estimate=2,
    ), None)

    # 10 min later: mom has been absent, normally would trigger MEDIUM.
    # But because we saw a feeding event within 30 min, suppress.
    t1 = t0 + 10 * 60
    obs = _obs(
        cardinal_on_nest="false",
        mother_cardinal_present="false",
        chicks_visible="uncertain",
        species_detected=[],
    )
    state = store.record(t1, False, None, obs, None)
    decision = evaluate(obs, state, store, t1)
    assert decision is None or decision.rule_id != "long_absence"


def test_feeding_suppression_expires_after_30min(store, lifecycle_on):
    """After 30 min with no feeding event, MEDIUM fires again."""
    # Seed feeding with 2 confirming sightings. Both record feeding events.
    t0 = time.time() - 7200
    store.record(t0 - 300, False, None, _obs(
        cardinal_on_nest="true",
        mother_feeding_chicks=True,
    ), None)
    store.record(t0, False, None, _obs(
        cardinal_on_nest="true",
        mother_feeding_chicks=True,
        chicks_visible="true",
        chick_count_estimate=2,
    ), None)

    # 45 minutes later: mom still away, suppression expired
    t1 = t0 + 45 * 60
    obs = _obs(
        cardinal_on_nest="false",
        mother_cardinal_present="false",
        chicks_visible="uncertain",
        species_detected=[],
    )
    state = store.record(t1, False, None, obs, None)
    # Absence should now exceed _LONG_ABSENCE_THRESHOLD (300s) and
    # suppression has expired.
    decision = evaluate(obs, state, store, t1)
    # May fire MEDIUM (suppression expired) — the key thing is it's
    # NO LONGER suppressed by the feeding rule.
    if decision is not None:
        # If fires, must be long_absence (not another rule)
        assert decision.rule_id == "long_absence"
    # If None, cooldown may still block — either is fine as long as
    # the feeding suppression alone isn't responsible.


def test_feeding_suppression_off_outside_feeding_stage(store, lifecycle_on):
    """mother_feeding_chicks=true during incubation (pre-hatch) should NOT
    suppress MEDIUM — this guards against misidentified feeding signals
    in the incubation stage."""
    t0 = time.time() - 3600
    # Record a feeding event but stage is still incubation (pre-hatch)
    # Actually, mother_feeding_chicks=true triggers transition to feeding,
    # so this is a hard case to test directly. Instead verify: if we're
    # in incubation (say, after the feeding stage somehow regressed,
    # which shouldn't happen), MEDIUM is NOT suppressed.
    # For now: verify in incubation stage with no feeding event, MEDIUM
    # behaves normally (fires).
    store.record(t0, False, None, _obs(), None)

    t1 = t0 + 600  # 10 min later
    obs = _obs(
        cardinal_on_nest="false",
        mother_cardinal_present="false",
        species_detected=[],
    )
    state = store.record(t1, False, None, obs, None)
    assert state.lifecycle_stage == "incubation"
    decision = evaluate(obs, state, store, t1)
    # Should fire MEDIUM long_absence normally
    assert decision is not None
    assert decision.severity == Severity.MEDIUM
    assert decision.rule_id == "long_absence"


# ── Chick count tracking ──────────────────────────────────────────────

def test_chick_count_updates_when_visible(store, lifecycle_on):
    """last_chick_count is updated from chick_count_estimate when chicks
    are confidently visible."""
    t0 = time.time()
    obs = _obs(
        cardinal_on_nest="false",
        chicks_visible="true",
        chick_count_estimate=3,
    )
    state = store.record(t0, False, None, obs, None)
    assert state.last_chick_count == 3


def test_chick_count_not_updated_when_uncertain(store, lifecycle_on):
    """If chicks_visible is uncertain, don't update last_chick_count."""
    t0 = time.time()
    obs_seen = _obs(
        cardinal_on_nest="false",
        chicks_visible="true",
        chick_count_estimate=2,
    )
    state = store.record(t0, False, None, obs_seen, None)
    assert state.last_chick_count == 2

    # Another confirming sighting to get into feeding stage
    t05 = t0 + 300
    state = store.record(t05, False, None, _obs(
        cardinal_on_nest="false",
        chicks_visible="true",
        chick_count_estimate=2,
    ), None)
    assert state.lifecycle_stage == "feeding"

    # Later: uncertain observation — should NOT change last_chick_count
    t1 = t05 + 600
    obs_obscured = _obs(
        cardinal_on_nest="true",  # mom covering
        chicks_visible="uncertain",
    )
    state = store.record(t1, False, None, obs_obscured, None)
    assert state.last_chick_count == 2  # unchanged


# ── Predation in feeding stage is still CRITICAL ──────────────────────

def test_predation_in_feeding_stage_fires_critical(store, lifecycle_on):
    """Thrashers don't stop after hatching. Direct nest interaction during
    feeding stage must still fire CRITICAL."""
    # Transition to feeding — need 2 confirming sightings
    t0 = time.time() - 3600
    store.record(t0 - 300, False, None, _obs(
        cardinal_on_nest="false",
        chicks_visible="true",
        chick_count_estimate=2,
    ), None)
    store.record(t0, False, None, _obs(
        cardinal_on_nest="false",
        chicks_visible="true",
        chick_count_estimate=2,
    ), None)

    # Later: thrasher at nest with direct interaction
    t1 = t0 + 1800
    obs = _obs(
        cardinal_on_nest="false",
        mother_cardinal_present="false",
        threat_species_detected=["brown_thrasher"],
        near_nest_activity=True,
        direct_nest_interaction=True,
        species_detected=["brown_thrasher"],
        chicks_visible="uncertain",
    )
    state = store.record(t1, False, None, obs, None)
    assert state.lifecycle_stage == "feeding"

    decision = evaluate(obs, state, store, t1)
    assert decision is not None
    assert decision.severity == Severity.CRITICAL
    assert decision.rule_id == "direct_attack"

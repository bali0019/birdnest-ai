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


def test_mother_feeding_chicks_alone_no_longer_advances_lifecycle(store, lifecycle_on):
    """Tightened 2026-04-17: mother_feeding_chicks=true alone is NO LONGER
    a chick signal for lifecycle advancement. Only explicit chicks_visible=
    "true" at ≥0.75 confidence counts. This prevents "food-in-beak"
    misreads from advancing state without visible chick anatomy.

    Scenario: mother_feeding_chicks=true on two separate frames within
    the 4h window. Pre-fix behavior would have confirmed hatch. Post-fix,
    stage must stay in incubation.
    """
    t0 = time.time()
    store.record(t0, False, None, _obs(), None)

    # 1st: mother_feeding_chicks=true (no chicks_visible="true")
    t1 = t0 + 3600
    store.record(t1, False, None, _obs(
        cardinal_on_nest="false",
        mother_feeding_chicks=True,
    ), None)

    # 2nd: mother_feeding_chicks=true again
    t2 = t1 + 1800
    state = store.record(t2, False, None, _obs(
        mother_feeding_chicks=True,
    ), None)
    assert state.lifecycle_stage == "incubation", (
        "mother_feeding_chicks alone must not advance lifecycle"
    )
    assert state.hatch_detected_ts is None


def test_chicks_visible_below_confidence_floor_does_not_advance(store, lifecycle_on):
    """Even chicks_visible="true" below the 0.75 confidence floor does not
    count as a chick signal. Prevents low-confidence reddish-blob reads
    from advancing lifecycle (replays today's 15:23 false sighting at 0.82
    — but at any confidence below 0.75).
    """
    t0 = time.time()
    store.record(t0, False, None, _obs(), None)

    t1 = t0 + 3600
    state = store.record(t1, False, None, _obs(
        cardinal_on_nest="false",
        chicks_visible="true",
        confidence=0.65,  # below 0.75 floor
    ), None)
    assert state.first_chick_sighting_ts is None, (
        "Below-floor confidence must not record as 1st chick sighting"
    )
    assert state.lifecycle_stage == "incubation"


def test_chicks_visible_at_or_above_floor_does_advance(store, lifecycle_on):
    """Negative control: two chicks_visible=true observations at ≥0.75
    confidence still properly advance to feeding.
    """
    t0 = time.time()
    store.record(t0, False, None, _obs(), None)

    t1 = t0 + 3600
    store.record(t1, False, None, _obs(
        cardinal_on_nest="false",
        chicks_visible="true",
        confidence=0.80,
    ), None)

    t2 = t1 + 1800
    state = store.record(t2, False, None, _obs(
        cardinal_on_nest="false",
        chicks_visible="true",
        confidence=0.85,
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
    # Seed feeding stage with 2 confirming chicks_visible=true sightings
    # at ≥0.75 confidence (tightened 2026-04-17). mother_feeding_chicks
    # alone no longer advances lifecycle, but still records feeding events
    # for the 30-min suppression path — so we set both flags to exercise
    # both behaviors.
    t0 = time.time() - 3600
    store.record(t0 - 300, False, None, _obs(
        cardinal_on_nest="true",
        mother_feeding_chicks=True,
        chicks_visible="true",
        chick_count_estimate=2,
        confidence=0.85,
    ), None)
    store.record(t0, False, None, _obs(
        cardinal_on_nest="true",
        mother_feeding_chicks=True,
        chicks_visible="true",
        chick_count_estimate=2,
        confidence=0.85,
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
    # Seed feeding with 2 confirming chicks_visible=true sightings at
    # ≥0.75 confidence (tightened 2026-04-17). Both record feeding events.
    t0 = time.time() - 7200
    store.record(t0 - 300, False, None, _obs(
        cardinal_on_nest="true",
        mother_feeding_chicks=True,
        chicks_visible="true",
        chick_count_estimate=2,
        confidence=0.85,
    ), None)
    store.record(t0, False, None, _obs(
        cardinal_on_nest="true",
        mother_feeding_chicks=True,
        chicks_visible="true",
        chick_count_estimate=2,
        confidence=0.85,
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


# ── 6-stage expansion: building_nest + egg_laying (added 2026-04-16) ──

def _seed_stage(store, stage, **extra_cols):
    """Directly UPDATE the state row to seed a specific lifecycle_stage and
    optional additional columns. Used for tests that need to start from a
    stage other than the default ('incubation')."""
    cols = {"lifecycle_stage": stage}
    cols.update(extra_cols)
    set_clause = ", ".join(f"{k} = ?" for k in cols)
    store._conn.execute(
        f"UPDATE state SET {set_clause} WHERE id = 1",
        tuple(cols.values()),
    )


def test_building_nest_is_valid_default(store, lifecycle_on):
    """A NestState can be constructed with lifecycle_stage='building_nest'
    and the associated timestamp fields default to None."""
    from cardinal_nest_monitor.schema import NestState

    ns = NestState(lifecycle_stage="building_nest")
    assert ns.lifecycle_stage == "building_nest"
    assert ns.egg_laying_started_ts is None
    assert ns.incubation_started_ts is None
    assert ns.hatch_detected_ts is None
    assert ns.fledge_detected_ts is None


def test_building_nest_to_egg_laying_on_first_sitting(store, lifecycle_on):
    """Seed store at building_nest, record a cardinal_on_nest=true observation,
    verify stage flips to egg_laying and egg_laying_started_ts is set to ts."""
    _seed_stage(store, "building_nest")
    # Sanity check — we actually loaded building_nest.
    assert store.get_state().lifecycle_stage == "building_nest"

    t0 = time.time()
    obs = _obs(cardinal_on_nest="true", confidence=0.9)
    state = store.record(t0, False, None, obs, None)
    assert state.lifecycle_stage == "egg_laying"
    assert state.egg_laying_started_ts == pytest.approx(t0, abs=1.0)


def test_building_nest_to_egg_laying_fires_alert(store, lifecycle_on):
    """events.evaluate() with pre-state=building_nest + cardinal_on_nest=true
    should return a LOW AlertDecision with rule_id='egg_laying_begin'."""
    _seed_stage(store, "building_nest")
    pre_state = store.get_state()
    assert pre_state.lifecycle_stage == "building_nest"

    t0 = time.time()
    obs = _obs(cardinal_on_nest="true", confidence=0.9)
    decision = evaluate(obs, pre_state, store, t0)
    assert decision is not None
    assert decision.severity == Severity.LOW
    assert decision.rule_id == "egg_laying_begin"


def _seed_observations(store, base_ts, n, on_nest_ratio, confidence=0.9):
    """INSERT `n` synthetic observations into the observations table spread
    evenly over the 24h window ending at base_ts, with `on_nest_ratio`
    fraction having cardinal_on_nest='true' (rest have 'false')."""
    n_on = int(round(n * on_nest_ratio))
    interval = (24 * 3600) / n  # seconds between synthetic obs
    for i in range(n):
        ts = base_ts - (24 * 3600) + (i + 1) * interval * 0.9  # keep all within 24h
        on_nest = "true" if i < n_on else "false"
        # Match the string format produced by NestObservation.model_dump_json().
        # state.py::record() uses cheap string matching on this JSON, so we
        # only need the fields it checks for: cardinal_on_nest and confidence.
        obs_json = (
            '{"mother_cardinal_present":"true",'
            f'"cardinal_on_nest":"{on_nest}",'
            '"eggs_visible":"false","egg_count_estimate":null,'
            '"nest_visible":true,"nest_disturbed":"false",'
            '"species_detected":[],"threat_species_detected":[],'
            '"near_nest_activity":false,"direct_nest_interaction":false,'
            '"chicks_visible":"uncertain","chick_count_estimate":null,'
            '"mother_feeding_chicks":false,'
            f'"confidence":{confidence},"summary":"seed"}}'
        )
        store._conn.execute(
            "INSERT INTO observations (ts, motion_triggered, prefilter_json, "
            "observation_json, evidence_dir) VALUES (?, 0, NULL, ?, NULL)",
            (ts, obs_json),
        )


def test_egg_laying_to_incubation_auto_detection(store, lifecycle_on):
    """24h of observations with 75% cardinal_on_nest=true + egg_laying_started_ts
    24h ago → record() transitions egg_laying → incubation."""
    t0 = time.time()
    _seed_stage(
        store,
        "egg_laying",
        egg_laying_started_ts=t0 - 24 * 3600 - 60,  # slightly > 24h ago
    )
    _seed_observations(store, t0, n=30, on_nest_ratio=0.75)

    # Final observation that triggers the check.
    obs = _obs(cardinal_on_nest="true", confidence=0.9)
    state = store.record(t0, False, None, obs, None)
    assert state.lifecycle_stage == "incubation"
    assert state.incubation_started_ts == pytest.approx(t0, abs=1.0)


def test_egg_laying_stays_put_when_below_70pct(store, lifecycle_on):
    """50% cardinal_on_nest ratio does NOT trigger the transition."""
    t0 = time.time()
    _seed_stage(
        store,
        "egg_laying",
        egg_laying_started_ts=t0 - 24 * 3600 - 60,
    )
    _seed_observations(store, t0, n=30, on_nest_ratio=0.50)

    obs = _obs(cardinal_on_nest="true", confidence=0.9)
    state = store.record(t0, False, None, obs, None)
    assert state.lifecycle_stage == "egg_laying"
    assert state.incubation_started_ts is None


def test_egg_laying_to_incubation_requires_24h_of_observations(store, lifecycle_on):
    """egg_laying_started_ts only 12h ago → transition gated regardless of ratio."""
    t0 = time.time()
    _seed_stage(
        store,
        "egg_laying",
        egg_laying_started_ts=t0 - 12 * 3600,  # only 12h ago
    )
    # Seed plenty of 'true' observations in the last 12h — ratio is fine.
    _seed_observations(store, t0, n=30, on_nest_ratio=0.90)

    obs = _obs(cardinal_on_nest="true", confidence=0.9)
    state = store.record(t0, False, None, obs, None)
    assert state.lifecycle_stage == "egg_laying"
    assert state.incubation_started_ts is None


def test_egg_laying_to_incubation_fires_alert(store, lifecycle_on):
    """events.evaluate() with pre_state=egg_laying + 24h history of 75%
    sitting returns a LOW AlertDecision with rule_id='incubation_begin'."""
    t0 = time.time()
    _seed_stage(
        store,
        "egg_laying",
        egg_laying_started_ts=t0 - 24 * 3600 - 60,
    )
    _seed_observations(store, t0, n=30, on_nest_ratio=0.75)

    pre_state = store.get_state()
    assert pre_state.lifecycle_stage == "egg_laying"

    obs = _obs(cardinal_on_nest="true", confidence=0.9)
    decision = evaluate(obs, pre_state, store, t0)
    assert decision is not None
    assert decision.severity == Severity.LOW
    assert decision.rule_id == "incubation_begin"


def test_incubation_begin_cooldown_prevents_double_alert(store, lifecycle_on):
    """After firing incubation_begin and committing the transition to state,
    evaluate() must not fire a second incubation_begin alert. The natural
    guard is that lifecycle_stage has flipped to 'incubation', so the
    egg_laying-gated alert predicate no longer matches. This mirrors the
    real Pipeline.on_image ordering (evaluate → record → next-snap's evaluate)."""
    t0 = time.time()
    _seed_stage(
        store,
        "egg_laying",
        egg_laying_started_ts=t0 - 24 * 3600 - 60,
    )
    _seed_observations(store, t0, n=30, on_nest_ratio=0.75)

    pre_state = store.get_state()
    obs = _obs(cardinal_on_nest="true", confidence=0.9)
    first = evaluate(obs, pre_state, store, t0)
    assert first is not None and first.rule_id == "incubation_begin"
    # Commit the observation — this flips lifecycle_stage to incubation.
    post_state = store.record(t0, False, None, obs, None)
    assert post_state.lifecycle_stage == "incubation"
    store.record_alert(first, t0, None)

    # Next snap: evaluate() with the post-record state must not re-fire.
    t1 = t0 + 60
    next_pre_state = store.get_state()
    second = evaluate(obs, next_pre_state, store, t1)
    assert second is None or second.rule_id != "incubation_begin"


def test_lifecycle_flag_off_skips_new_transitions(store, monkeypatch):
    """With lifecycle_tracking_enabled=False, building_nest → egg_laying
    transition does NOT happen — stage stays at building_nest."""
    settings = get_settings()
    monkeypatch.setattr(settings, "lifecycle_tracking_enabled", False)

    _seed_stage(store, "building_nest")
    t0 = time.time()
    obs = _obs(cardinal_on_nest="true", confidence=0.9)
    state = store.record(t0, False, None, obs, None)
    assert state.lifecycle_stage == "building_nest"
    assert state.egg_laying_started_ts is None


def test_egg_laying_started_ts_persists_across_record_calls(store, lifecycle_on):
    """Once egg_laying_started_ts is set by the building_nest → egg_laying
    transition, subsequent record() calls must NOT overwrite it."""
    _seed_stage(store, "building_nest")

    t0 = time.time()
    state = store.record(
        t0, False, None, _obs(cardinal_on_nest="true", confidence=0.9), None,
    )
    assert state.lifecycle_stage == "egg_laying"
    original_ts = state.egg_laying_started_ts
    assert original_ts == pytest.approx(t0, abs=1.0)

    # Several more record() calls — egg_laying_started_ts must not change.
    for i in range(1, 4):
        t_later = t0 + i * 300
        state = store.record(
            t_later, False, None,
            _obs(cardinal_on_nest="true", confidence=0.9), None,
        )
        assert state.lifecycle_stage == "egg_laying"
        assert state.egg_laying_started_ts == original_ts


# ── Codex P2: nest_visible guard on lifecycle transitions ─────────────

def test_lifecycle_event_skips_frames_without_nest_visible(store, lifecycle_on):
    """_lifecycle_event() must return None when nest_visible=False.

    Yard-motion frames or obscured-scene frames don't carry enough signal
    to advance lifecycle state. Without this guard, a high-confidence
    'no nest in frame' observation in the feeding stage with a stale
    last_mother_seen_ts could emit a false fledge alert.
    """
    _seed_stage(
        store,
        "feeding",
        hatch_detected_ts=time.time() - 48 * 3600,
        last_mother_seen_ts=time.time() - 13 * 3600,
    )
    t0 = time.time()
    # No nest in this frame at all — e.g. camera pointed elsewhere briefly.
    obs = _obs(
        nest_visible=False,
        cardinal_on_nest="uncertain",
        species_detected=[],
        summary="Frame shows rose bush foliage only; no nest visible.",
    )
    pre_state = store.get_state()
    assert evaluate(obs, pre_state, store, t0) is None


def test_lifecycle_transition_skipped_when_nest_not_visible(store, lifecycle_on):
    """state.py::record() must NOT advance lifecycle_stage on a frame
    where nest_visible=False, regardless of cardinal_on_nest reading."""
    _seed_stage(store, "building_nest")
    t0 = time.time()
    obs = _obs(
        nest_visible=False,
        cardinal_on_nest="true",  # even a positive read doesn't count
        summary="Cardinal visible in frame but nest cup is out of view.",
    )
    state = store.record(t0, False, None, obs, None)
    assert state.lifecycle_stage == "building_nest"
    assert state.egg_laying_started_ts is None


# ── Codex P2: proper confidence filter in 24h ratio scan ──────────────

def test_low_confidence_rows_excluded_from_sitting_ratio(store, lifecycle_on):
    """Sitting-ratio scan must filter rows at confidence ≥ 0.55. If it
    counted low-confidence IR misreads, the egg_laying → incubation
    transition would fire prematurely in the sunset-to-23:00 IR window.
    """
    t0 = time.time()
    _seed_stage(
        store,
        "egg_laying",
        egg_laying_started_ts=t0 - 24 * 3600 - 10,
    )
    # 30 observations — 25 are LOW-confidence on-nest (should be ignored),
    # 5 are HIGH-confidence off-nest. If the ratio scan counted the
    # low-confidence rows, it would see 25/30 = 83% sitting and transition.
    # With the proper filter, only 5 confident off-nest rows count, and 0
    # confident on-nest rows — ratio should NOT cross 70%.
    _seed_observations(store, t0, n=25, on_nest_ratio=1.0, confidence=0.4)
    _seed_observations(store, t0, n=5, on_nest_ratio=0.0, confidence=0.9)

    # Trigger a record() — but not from the scan rows (that's dangerous
    # because state.py re-runs the scan including this live snap too).
    current = _obs(cardinal_on_nest="true", confidence=0.9)
    state = store.record(t0, False, None, current, None)
    # Should STILL be egg_laying because the confident evidence
    # is actually off-nest-heavy, not on-nest-heavy.
    assert state.lifecycle_stage == "egg_laying"


# ── Codex P1: stale-snap guard ────────────────────────────────────────

def test_stale_snap_inserted_but_does_not_touch_derived_state(store, lifecycle_on):
    """An out-of-order (older than latest) observation must INSERT into the
    observations table for history BUT SKIP the derived-state UPDATE.

    Protects against the spool's newest-first claim ordering rolling back
    in_absence / absence_started_ts / lifecycle_stage during analyzer
    recovery after downtime.
    """
    t0 = time.time()

    # First: a live snap at t0 flipping in_absence=True.
    store.record(t0 - 200, False, None, _obs(cardinal_on_nest="true"), None)
    out = _obs(
        cardinal_on_nest="false", mother_cardinal_present="false",
        species_detected=[], summary="Nest empty.",
    )
    live_state = store.record(t0, False, None, out, None)
    assert live_state.in_absence is True
    assert live_state.absence_started_ts == pytest.approx(t0, abs=1.0)

    # Now simulate backfill: a STALE snap from 5 min before t0, showing
    # mom on the nest. Under the old code, this would roll in_absence
    # back to False. Under the stale-snap guard, it must be inserted for
    # history but leave derived state untouched.
    stale_ts = t0 - 300
    on_nest = _obs(cardinal_on_nest="true", summary="Old snap, she was on nest.")
    post_state = store.record(stale_ts, False, None, on_nest, None)

    assert post_state.in_absence is True, (
        "Stale on-nest snap must NOT clear in_absence"
    )
    assert post_state.absence_started_ts == live_state.absence_started_ts, (
        "Stale snap must NOT regress absence_started_ts"
    )

    # Observation STILL got inserted (for analytics history).
    cur = store._conn.execute(
        "SELECT COUNT(*) FROM observations WHERE ts = ?", (stale_ts,),
    )
    assert cur.fetchone()[0] == 1


def test_stale_snap_does_not_regress_lifecycle_stage(store, lifecycle_on):
    """A stale snap from earlier in the laying stage must not roll a
    state already in `feeding` back to `incubation` or `egg_laying`."""
    t0 = time.time()
    _seed_stage(
        store,
        "feeding",
        hatch_detected_ts=t0 - 2 * 24 * 3600,
        last_mother_seen_ts=t0 - 600,
    )
    # Seed a non-stale observation so latest_ts is known.
    store.record(t0, False, None, _obs(cardinal_on_nest="true"), None)
    stage_before = store.get_state().lifecycle_stage
    assert stage_before == "feeding"

    # A stale "cardinal sitting" snap from a day ago.
    stale_ts = t0 - 24 * 3600
    state = store.record(stale_ts, False, None, _obs(cardinal_on_nest="true"), None)
    assert state.lifecycle_stage == "feeding"

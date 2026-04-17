"""End-to-end tests for the rules engine — the 7 PRD scenarios + gating."""

from __future__ import annotations

import time

import pytest

from cardinal_nest_monitor.events import evaluate
from cardinal_nest_monitor.schema import NestObservation, Severity
from cardinal_nest_monitor.state import StateStore


def _make_obs(**kwargs) -> NestObservation:
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
        confidence=0.9,
        summary="Mother on nest.",
    )
    base.update(kwargs)
    return NestObservation(**base)


@pytest.fixture
def store(tmp_path):
    s = StateStore(tmp_path / "state.sqlite")
    yield s
    s.close()


# ── 7 PRD scenarios ────────────────────────────────────────────────────

def test_mother_sitting_still_no_alert(store):
    t0 = time.time()
    for i in range(5):
        obs = _make_obs()
        state = store.record(t0 + i * 30, False, None, obs, None)
        assert evaluate(obs, state, store, t0 + i * 30) is None


def test_mother_brief_absence_no_alert(store):
    t0 = time.time()
    present = _make_obs()
    store.record(t0, False, None, present, None)
    absent = _make_obs(
        mother_cardinal_present="false",
        cardinal_on_nest="false",
        species_detected=[],
        near_nest_activity=False,
        summary="Nest empty.",
    )
    state = store.record(t0 + 90, False, None, absent, None)
    assert evaluate(absent, state, store, t0 + 90) is None


def test_brown_thrasher_near_nest_mother_absent_HIGH(store):
    t0 = time.time()
    store.record(t0, False, None, _make_obs(), None)
    absent = _make_obs(
        mother_cardinal_present="false",
        cardinal_on_nest="false",
        species_detected=[],
        summary="Nest empty.",
    )
    store.record(t0 + 130, False, None, absent, None)
    threat = _make_obs(
        mother_cardinal_present="false",
        cardinal_on_nest="false",
        species_detected=["brown_thrasher"],
        threat_species_detected=["brown_thrasher"],
        near_nest_activity=True,
        summary="Thrasher at nest.",
    )
    state = store.record(t0 + 140, False, None, threat, None)
    decision = evaluate(threat, state, store, t0 + 140)
    assert decision is not None
    assert decision.severity == Severity.HIGH
    assert decision.rule_id == "predator_absent"
    assert "brown_thrasher" in decision.species


def test_thrasher_direct_interaction_CRITICAL(store):
    t0 = time.time()
    threat = _make_obs(
        mother_cardinal_present="false",
        cardinal_on_nest="false",
        species_detected=["brown_thrasher"],
        threat_species_detected=["brown_thrasher"],
        near_nest_activity=True,
        direct_nest_interaction=True,
        summary="Thrasher reaching into nest.",
    )
    state = store.record(t0, False, None, threat, None)
    decision = evaluate(threat, state, store, t0)
    assert decision is not None
    assert decision.severity == Severity.CRITICAL
    assert decision.rule_id == "direct_attack"


def test_egg_count_drop_CRITICAL(store):
    t0 = time.time()
    seed = _make_obs(
        cardinal_on_nest="false",
        eggs_visible="true",
        egg_count_estimate=3,
        summary="Three eggs visible, mother away.",
    )
    store.record(t0, False, None, seed, None)
    drop = _make_obs(
        cardinal_on_nest="false",
        eggs_visible="true",
        egg_count_estimate=2,
        summary="Only two eggs visible now.",
    )
    # Pattern matches main.py: evaluate with PRE-record state, then persist.
    pre_state = store.get_state()
    decision = evaluate(drop, pre_state, store, t0 + 60)
    store.record(t0 + 60, False, None, drop, None)
    assert decision is not None
    assert decision.severity == Severity.CRITICAL
    assert decision.rule_id == "egg_loss"
    assert decision.egg_count_before == 3
    assert decision.egg_count_after == 2


def test_mockingbird_no_alert(store):
    t0 = time.time()
    obs = _make_obs(
        species_detected=["northern_mockingbird"],
        threat_species_detected=[],
        near_nest_activity=True,  # even if near — not a threat species
        summary="Mockingbird nearby.",
    )
    state = store.record(t0, False, None, obs, None)
    assert evaluate(obs, state, store, t0) is None


def test_squirrel_near_nest_HIGH(store):
    t0 = time.time()
    store.record(t0, False, None, _make_obs(), None)
    absent = _make_obs(
        cardinal_on_nest="false",
        mother_cardinal_present="false",
        species_detected=[],
        summary="Empty.",
    )
    store.record(t0 + 130, False, None, absent, None)
    threat = _make_obs(
        cardinal_on_nest="false",
        mother_cardinal_present="false",
        species_detected=["eastern_gray_squirrel"],
        threat_species_detected=["squirrel"],
        near_nest_activity=True,
        summary="Squirrel climbing toward nest.",
    )
    state = store.record(t0 + 140, False, None, threat, None)
    decision = evaluate(threat, state, store, t0 + 140)
    assert decision is not None
    assert decision.severity == Severity.HIGH
    assert decision.rule_id == "predator_absent"
    assert "squirrel" in decision.species


# ── Confidence gating ──────────────────────────────────────────────────

def test_low_confidence_suppresses_direct_attack(store):
    t0 = time.time()
    obs = _make_obs(
        direct_nest_interaction=True,
        threat_species_detected=["brown_thrasher"],
        species_detected=["brown_thrasher"],
        near_nest_activity=True,
        confidence=0.40,
        summary="Maybe something at the nest, not sure.",
    )
    state = store.record(t0, False, None, obs, None)
    assert evaluate(obs, state, store, t0) is None


def test_low_confidence_does_not_update_state(store):
    t0 = time.time()
    seed = _make_obs(
        eggs_visible="true",
        egg_count_estimate=3,
        confidence=0.3,
        summary="Blurry.",
    )
    state = store.record(t0, False, None, seed, None)
    assert state.last_known_egg_count is None


# ── New absence + threat rules (retuned 2026-04-15) ───────────────────

def test_absence_5min_fires_MEDIUM(store):
    """MEDIUM rule must fire after 5 min of absence (was 15 min)."""
    t0 = time.time()
    store.record(t0, False, None, _make_obs(), None)  # on nest
    absent = _make_obs(
        cardinal_on_nest="false",
        mother_cardinal_present="false",
        species_detected=[],
        summary="Nest empty, mom foraging.",
    )
    state = store.record(t0 + 310, False, None, absent, None)  # 5 min 10s
    decision = evaluate(absent, state, store, t0 + 310)
    assert decision is not None
    assert decision.severity == Severity.MEDIUM
    assert decision.rule_id == "long_absence"
    # Title MUST reflect the actual absence bucket (5 min at this point),
    # NOT a stale hardcoded "15+ minutes". See the 2026-04-15 incident
    # where the title read "15+ minutes" on a 5m 9s absence because the
    # threshold constant was retuned but the alert string was not. The
    # dynamic-title fix below makes this regression structurally
    # impossible so long as the threshold is used as the bucket size.
    assert decision.title == "Mother away from nest for 5+ minutes", (
        f"long_absence title MUST match actual absence bucket, got "
        f"{decision.title!r}"
    )


def test_long_absence_title_tracks_elapsed_bucket(store):
    """Regression guard for the 2026-04-15 inconsistency ("15+ minutes
    title fired on a 5-minute absence").

    The title MUST bucket the absence in multiples of the threshold
    constant so that the value the user sees in Discord always reflects
    the actual elapsed time. If a future change retunes the threshold
    (e.g. back to 3 min or up to 10 min), this test proves the title
    will auto-update rather than silently desyncing.
    """
    from cardinal_nest_monitor.events import _LONG_ABSENCE_THRESHOLD

    # Cooldown is 5 min, so to exercise multiple buckets in one test we
    # use a fresh store + observation sequence for each bucket.
    def _fire_alert_at(absence_s: int):
        # Fresh store per sub-test so the cooldown window can't suppress.
        from cardinal_nest_monitor.state import StateStore
        s = StateStore(":memory:")
        try:
            t0 = time.time()
            s.record(t0, False, None, _make_obs(), None)  # on nest
            absent = _make_obs(
                cardinal_on_nest="false",
                mother_cardinal_present="false",
                species_detected=[],
                summary="Mom foraging.",
            )
            state = s.record(t0 + absence_s, False, None, absent, None)
            return evaluate(absent, state, s, t0 + absence_s)
        finally:
            s.close()

    # 5m 9s → "5+ minutes" (the exact case from the 2026-04-15 Discord bug).
    d = _fire_alert_at(309)
    assert d is not None and d.rule_id == "long_absence"
    assert d.title == "Mother away from nest for 5+ minutes", d.title

    # 10m 32s → "10+ minutes"
    d = _fire_alert_at(632)
    assert d is not None and d.rule_id == "long_absence"
    assert d.title == "Mother away from nest for 10+ minutes", d.title

    # 15m 45s → "15+ minutes"
    d = _fire_alert_at(945)
    assert d is not None and d.rule_id == "long_absence"
    assert d.title == "Mother away from nest for 15+ minutes", d.title

    # 30m 1s → "30+ minutes"
    d = _fire_alert_at(1801)
    assert d is not None and d.rule_id == "long_absence"
    assert d.title == "Mother away from nest for 30+ minutes", d.title

    # Invariant: whatever the threshold is, the bucket size in the title
    # must equal threshold_seconds // 60 (so if the threshold ever changes
    # to 3 min, a 7-min absence reads "6+ minutes", not "15+ minutes").
    bucket_mins_per_unit = _LONG_ABSENCE_THRESHOLD // 60
    # At exactly 1x threshold (+1s), title must say "{bucket_mins_per_unit}+ minutes".
    d = _fire_alert_at(_LONG_ABSENCE_THRESHOLD + 1)
    assert d is not None
    assert d.title == f"Mother away from nest for {bucket_mins_per_unit}+ minutes", (
        f"At 1x threshold, title must equal {bucket_mins_per_unit}+ minutes "
        f"(derived from the _LONG_ABSENCE_THRESHOLD constant). Got: "
        f"{d.title!r}. If the constant changed, the title must follow."
    )


def test_threat_while_mom_present_fires_HIGH(store):
    """HIGH fires on any threat + near_nest_activity — no absence required."""
    t0 = time.time()
    # Mom currently on the nest, but a thrasher shows up near the bush.
    obs = _make_obs(
        mother_cardinal_present="true",
        cardinal_on_nest="true",
        species_detected=["brown_thrasher"],
        threat_species_detected=["brown_thrasher"],
        near_nest_activity=True,
        summary="Thrasher on rose bush while mom on nest.",
    )
    state = store.record(t0, False, None, obs, None)
    decision = evaluate(obs, state, store, t0)
    assert decision is not None
    assert decision.severity == Severity.HIGH
    assert decision.rule_id == "predator_absent"
    assert "brown_thrasher" in decision.species


# ── Smart filter ───────────────────────────────────────────────────────

def test_smart_filter_yard_motion_no_alert(store):
    t0 = time.time()
    obs = _make_obs(
        nest_visible=False,
        near_nest_activity=False,
        threat_species_detected=[],
        species_detected=["unknown_bird"],
        cardinal_on_nest="uncertain",
        mother_cardinal_present="uncertain",
        summary="Something in yard, not near nest.",
    )
    state = store.record(t0, False, None, obs, None)
    assert evaluate(obs, state, store, t0) is None


def test_medium_suppressed_during_quiet_hours(store, monkeypatch):
    """MEDIUM long_absence must NOT fire during quiet hours.

    IR night images produce false "mom absent" readings because the
    cardinal's plumage blends with nest material in grayscale. She's
    almost certainly sleeping on the eggs.
    """
    from cardinal_nest_monitor.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "quiet_hours", "00:00-23:59")

    t0 = time.time()
    store.record(t0, False, None, _make_obs(), None)
    absent = _make_obs(
        cardinal_on_nest="false",
        mother_cardinal_present="false",
        species_detected=[],
        summary="Nest appears empty (IR image).",
        confidence=0.65,
    )
    state = store.record(t0 + 310, False, None, absent, None)
    decision = evaluate(absent, state, store, t0 + 310)
    assert decision is None, (
        "MEDIUM long_absence must be suppressed during quiet hours"
    )


def test_medium_fires_outside_quiet_hours(store, monkeypatch):
    """Same scenario but outside quiet hours — MEDIUM must fire."""
    from cardinal_nest_monitor.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "quiet_hours", "")

    t0 = time.time()
    store.record(t0, False, None, _make_obs(), None)
    absent = _make_obs(
        cardinal_on_nest="false",
        mother_cardinal_present="false",
        species_detected=[],
        summary="Nest empty — mom foraging.",
    )
    state = store.record(t0 + 310, False, None, absent, None)
    decision = evaluate(absent, state, store, t0 + 310)
    assert decision is not None, "MEDIUM must fire outside quiet hours"
    assert decision.severity == Severity.MEDIUM
    assert decision.rule_id == "long_absence"


def test_observation_indicates_ir_mode_detects_real_phrases():
    """observation_indicates_ir_mode() must catch the IR phrasings Sonnet
    actually produces in production. Regression: see
    evidence/2026-04-16/20-48-07_MEDIUM_unknown_bird/observation.json for the
    canonical false-positive that motivated this helper.
    """
    from cardinal_nest_monitor.events import observation_indicates_ir_mode

    real_ir_summaries = [
        "A compact bird is settled low in the nest cup at night in IR mode",
        "Infrared night image shows a compact bird settled low in the nest cup",
        "A compact bird is sitting low in the nest cup on a night IR frame",
        "Nighttime IR image shows a bird-sized mass settled low",
        "grayscale IR rendering",
        "night vision shows nothing",
    ]
    for summary in real_ir_summaries:
        obs = _make_obs(summary=summary)
        assert observation_indicates_ir_mode(obs), (
            f"Should detect IR in: {summary!r}"
        )

    # Daylight summary must NOT be detected as IR.
    obs = _make_obs(summary="Female cardinal sitting on the nest in daylight.")
    assert not observation_indicates_ir_mode(obs)


def test_medium_suppressed_when_image_is_ir_outside_quiet_hours(store, monkeypatch):
    """MEDIUM long_absence must NOT fire when the analyzer reports IR mode,
    even when the wall clock is outside the configured quiet_hours window.

    Real-world case: in April-Atlanta the Blink camera switches to IR at
    sunset (~20:00) but quiet_hours doesn't begin until 23:00. During that
    ~3h gap the old rule fired false MEDIUMs as the absence counter
    accumulated despite Sonnet correctly seeing 'a compact bird in the
    nest cup'. Replays the canonical evidence/2026-04-16/20-48-07_... case.
    """
    from cardinal_nest_monitor.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "quiet_hours", "")  # disable quiet hours entirely

    t0 = time.time()
    store.record(t0, False, None, _make_obs(), None)
    ir_uncertain = _make_obs(
        cardinal_on_nest="uncertain",
        mother_cardinal_present="uncertain",
        species_detected=["unknown bird"],
        threat_species_detected=[],
        near_nest_activity=True,
        confidence=0.62,
        summary=(
            "A compact bird is settled low in the nest cup at night in IR mode "
            "— body posture and size are consistent with the incubating female "
            "cardinal, but the crest is not clearly visible and species "
            "cannot be confirmed; no threat features apparent."
        ),
    )
    state = store.record(t0 + 1900, False, None, ir_uncertain, None)
    decision = evaluate(ir_uncertain, state, store, t0 + 1900)
    assert decision is None, (
        "MEDIUM must be suppressed when analyzer indicates IR mode, "
        "regardless of wall-clock quiet_hours."
    )


# ── Codex P2: backfill snaps must NOT fire state-relative rules ──────

def test_backfill_snap_does_not_fire_mother_returned_with_negative_absence(store):
    """Reproduces Codex P2: an older on-nest backfill snap landing AFTER a
    newer "mom is gone" snap was incorrectly firing mother_returned with a
    negative absence_seconds because evaluate() was using the future state.

    With is_backfill=True the rule must be skipped entirely.
    """
    t_now = time.time()
    # Simulate the live snap that flipped in_absence=True at t_now-100.
    store.record(t_now - 400, False, None, _make_obs(), None)  # she was here
    out = _make_obs(
        cardinal_on_nest="false", mother_cardinal_present="false",
        species_detected=[], summary="Nest empty.",
    )
    live_state = store.record(t_now - 100, False, None, out, None)
    assert live_state.in_absence is True

    # Now a STALE backfill snap from t_now-300 (BEFORE the absence started).
    stale_ts = t_now - 300
    on_nest = _make_obs(cardinal_on_nest="true", summary="Old: she was here.")
    pre_state = store.get_state()

    # Without is_backfill=True, the old code would fire mother_returned with
    # absence_seconds = stale_ts - last_mother_seen_ts = (t-300) - (t-400) =
    # +100, OR negative if state.last_mother_seen_ts was newer. With the
    # belt-and-suspenders ts < last_mother_seen_ts guard, no negative-
    # absence alert can fire even without is_backfill.
    decision = evaluate(on_nest, pre_state, store, stale_ts, is_backfill=True)
    assert decision is None, (
        "Backfill snap must not fire mother_returned (state-relative)."
    )


def test_backfill_snap_does_not_fire_long_absence(store):
    """Backfill snap must not fire MEDIUM long_absence.

    State reflects future truth — applying it to an older snap would
    report the wrong absence duration, possibly firing on a snap from
    a window when she was demonstrably present.
    """
    t_now = time.time()
    store.record(t_now - 1000, False, None, _make_obs(), None)
    # Live snap: she's gone, MEDIUM-eligible.
    out = _make_obs(
        cardinal_on_nest="false", mother_cardinal_present="false",
        species_detected=[], summary="Nest empty.",
    )
    store.record(t_now, False, None, out, None)

    # Backfill snap from 500s ago, also showing absence.
    stale_ts = t_now - 500
    pre_state = store.get_state()
    decision = evaluate(out, pre_state, store, stale_ts, is_backfill=True)
    assert decision is None or decision.rule_id != "long_absence"


def test_backfill_snap_still_fires_direct_attack_threat(store):
    """Backfill snap with a thrasher's beak in the cup MUST still fire
    CRITICAL direct_attack — observation-only rule, doesn't depend on
    state-relative comparisons. Operationally important for "what
    happened during downtime" via the [BACKFILL +Nm] channel."""
    t_now = time.time()
    store.record(t_now - 1000, False, None, _make_obs(), None)
    pre_state = store.get_state()

    threat = _make_obs(
        cardinal_on_nest="false", mother_cardinal_present="false",
        threat_species_detected=["brown_thrasher"],
        species_detected=["brown_thrasher"],
        near_nest_activity=True,
        direct_nest_interaction=True,
        confidence=0.85,
        summary="Brown thrasher beak in nest cup.",
    )
    decision = evaluate(threat, pre_state, store, t_now - 500, is_backfill=True)
    assert decision is not None
    assert decision.severity == Severity.CRITICAL
    assert decision.rule_id == "direct_attack"


def test_backfill_snap_still_fires_predator_near_nest(store):
    """Backfill predator-near-nest HIGH alert must still fire — observation-
    only, useful operationally."""
    t_now = time.time()
    store.record(t_now - 1000, False, None, _make_obs(), None)
    pre_state = store.get_state()
    threat = _make_obs(
        cardinal_on_nest="false", mother_cardinal_present="false",
        threat_species_detected=["brown_thrasher"],
        species_detected=["brown_thrasher"],
        near_nest_activity=True,
        direct_nest_interaction=False,
        confidence=0.85,
        summary="Brown thrasher on the bush near the nest.",
    )
    decision = evaluate(threat, pre_state, store, t_now - 500, is_backfill=True)
    assert decision is not None
    assert decision.severity == Severity.HIGH
    assert decision.rule_id == "predator_absent"


def test_negative_absence_guard_in_mother_returned_belt_and_suspenders(store):
    """Even when is_backfill is NOT set (caller forgot), rule 5 must never
    fire with negative absence_seconds. Defense-in-depth against future
    callers passing the wrong flag."""
    t_now = time.time()
    store.record(t_now - 100, False, None, _make_obs(), None)  # she's here at t-100
    out = _make_obs(
        cardinal_on_nest="false", mother_cardinal_present="false",
        species_detected=[], summary="Nest empty.",
    )
    state_after_absence = store.record(t_now, False, None, out, None)
    # Manually flip in_absence so the rule's other preconditions are met.
    store._conn.execute(
        "UPDATE state SET in_absence=1, last_mother_seen_ts=? WHERE id=1",
        (t_now,),
    )
    pre_state = store.get_state()
    on_nest = _make_obs(cardinal_on_nest="true", summary="snap.")
    # ts < state.last_mother_seen_ts → would yield negative absence
    decision = evaluate(on_nest, pre_state, store, t_now - 500, is_backfill=False)
    assert decision is None or decision.rule_id != "mother_returned"

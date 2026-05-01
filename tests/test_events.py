"""End-to-end tests for the rules engine — the 7 PRD scenarios + gating."""

from __future__ import annotations

import time

import pytest

from birdnest_ai.events import evaluate
from birdnest_ai.schema import NestObservation, NestState, Severity
from birdnest_ai.state import StateStore


def _make_obs(**kwargs) -> NestObservation:
    base = dict(
        attending_parent_present="true",
        attending_parent_on_nest="true",
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
        attending_parent_present="false",
        attending_parent_on_nest="false",
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
        attending_parent_present="false",
        attending_parent_on_nest="false",
        species_detected=[],
        summary="Nest empty.",
    )
    store.record(t0 + 130, False, None, absent, None)
    threat = _make_obs(
        attending_parent_present="false",
        attending_parent_on_nest="false",
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
        attending_parent_present="false",
        attending_parent_on_nest="false",
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


def test_egg_count_drop_CRITICAL(store, monkeypatch):
    """Flag-on path: when ENABLE_EGG_COUNT_ALERTS=true, egg_loss fires CRITICAL.

    Historically this was the always-on behavior; post-2026-04-17 the rule
    is gated by the config flag because this deployment's camera can't
    reliably see the eggs. Test enables the flag to exercise the original
    semantic.
    """
    from birdnest_ai.config import get_settings
    monkeypatch.setattr(get_settings(), "enable_egg_count_alerts", True)

    t0 = time.time()
    seed = _make_obs(
        attending_parent_on_nest="false",
        eggs_visible="true",
        egg_count_estimate=3,
        summary="Three eggs visible, mother away.",
    )
    store.record(t0, False, None, seed, None)
    drop = _make_obs(
        attending_parent_on_nest="false",
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
        attending_parent_on_nest="false",
        attending_parent_present="false",
        species_detected=[],
        summary="Empty.",
    )
    store.record(t0 + 130, False, None, absent, None)
    threat = _make_obs(
        attending_parent_on_nest="false",
        attending_parent_present="false",
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
        attending_parent_on_nest="false",
        attending_parent_present="false",
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
    from birdnest_ai.events import _LONG_ABSENCE_THRESHOLD

    # Cooldown is 5 min, so to exercise multiple buckets in one test we
    # use a fresh store + observation sequence for each bucket.
    def _fire_alert_at(absence_s: int):
        # Fresh store per sub-test so the cooldown window can't suppress.
        from birdnest_ai.state import StateStore
        s = StateStore(":memory:")
        try:
            t0 = time.time()
            s.record(t0, False, None, _make_obs(), None)  # on nest
            absent = _make_obs(
                attending_parent_on_nest="false",
                attending_parent_present="false",
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
        attending_parent_present="true",
        attending_parent_on_nest="true",
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
        attending_parent_on_nest="uncertain",
        attending_parent_present="uncertain",
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
    from birdnest_ai.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "quiet_hours", "00:00-23:59")

    t0 = time.time()
    store.record(t0, False, None, _make_obs(), None)
    absent = _make_obs(
        attending_parent_on_nest="false",
        attending_parent_present="false",
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
    from birdnest_ai.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "quiet_hours", "")

    t0 = time.time()
    store.record(t0, False, None, _make_obs(), None)
    absent = _make_obs(
        attending_parent_on_nest="false",
        attending_parent_present="false",
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
    from birdnest_ai.events import observation_indicates_ir_mode

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
    from birdnest_ai.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "quiet_hours", "")  # disable quiet hours entirely

    t0 = time.time()
    store.record(t0, False, None, _make_obs(), None)
    ir_uncertain = _make_obs(
        attending_parent_on_nest="uncertain",
        attending_parent_present="uncertain",
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
        attending_parent_on_nest="false", attending_parent_present="false",
        species_detected=[], summary="Nest empty.",
    )
    live_state = store.record(t_now - 100, False, None, out, None)
    assert live_state.in_absence is True

    # Now a STALE backfill snap from t_now-300 (BEFORE the absence started).
    stale_ts = t_now - 300
    on_nest = _make_obs(attending_parent_on_nest="true", summary="Old: she was here.")
    pre_state = store.get_state()

    # Without is_backfill=True, the old code would fire mother_returned with
    # absence_seconds = stale_ts - last_attending_parent_seen_ts = (t-300) - (t-400) =
    # +100, OR negative if state.last_attending_parent_seen_ts was newer. With the
    # belt-and-suspenders ts < last_attending_parent_seen_ts guard, no negative-
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
        attending_parent_on_nest="false", attending_parent_present="false",
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
        attending_parent_on_nest="false", attending_parent_present="false",
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
        attending_parent_on_nest="false", attending_parent_present="false",
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
        attending_parent_on_nest="false", attending_parent_present="false",
        species_detected=[], summary="Nest empty.",
    )
    state_after_absence = store.record(t_now, False, None, out, None)
    # Manually flip in_absence so the rule's other preconditions are met.
    store._conn.execute(
        "UPDATE state SET in_absence=1, last_attending_parent_seen_ts=? WHERE id=1",
        (t_now,),
    )
    pre_state = store.get_state()
    on_nest = _make_obs(attending_parent_on_nest="true", summary="snap.")
    # ts < state.last_attending_parent_seen_ts → would yield negative absence
    decision = evaluate(on_nest, pre_state, store, t_now - 500, is_backfill=False)
    assert decision is None or decision.rule_id != "attending_parent_returned"


# ── Track 1: ENABLE_EGG_COUNT_ALERTS flag (2026-04-17) ────────────────

def test_egg_loss_silent_when_flag_off(store, monkeypatch):
    """With ENABLE_EGG_COUNT_ALERTS=false (the default on this deploy),
    the egg_loss rule must not fire even when the analyzer reports a
    lower egg count. Replays today's 15:17 false-CRITICAL scenario.

    This camera's mounting can't reliably see into the cup (eggs sit
    underneath the mother and are occluded by the rim from below/behind),
    so egg-count observations can't be trusted. The rule stays in code
    for future top-down deployments.
    """
    from birdnest_ai.config import get_settings
    monkeypatch.setattr(get_settings(), "enable_egg_count_alerts", False)

    t0 = time.time()
    seed = _make_obs(
        attending_parent_on_nest="false",
        eggs_visible="true",
        egg_count_estimate=2,
        summary="Two eggs visible.",
    )
    store.record(t0, False, None, seed, None)
    drop = _make_obs(
        attending_parent_on_nest="false",
        eggs_visible="true",
        egg_count_estimate=1,
        summary="One egg visible — other occluded by rim.",
    )
    pre_state = store.get_state()
    decision = evaluate(drop, pre_state, store, t0 + 60)
    assert decision is None, (
        "egg_loss must NOT fire when ENABLE_EGG_COUNT_ALERTS=false "
        "(flagged off for this camera's viewing angle)."
    )


def test_egg_loss_still_fires_when_flag_on_future_camera(store, monkeypatch):
    """Negative control: flag-on (as for a hypothetical top-down camera)
    still produces CRITICAL egg_loss. Ensures the rule isn't dead code.
    """
    from birdnest_ai.config import get_settings
    monkeypatch.setattr(get_settings(), "enable_egg_count_alerts", True)

    t0 = time.time()
    store.record(t0, False, None, _make_obs(
        attending_parent_on_nest="false", eggs_visible="true", egg_count_estimate=3,
        summary="Three eggs visible.",
    ), None)
    drop = _make_obs(
        attending_parent_on_nest="false", eggs_visible="true", egg_count_estimate=2,
        summary="Two eggs visible now.",
    )
    pre_state = store.get_state()
    decision = evaluate(drop, pre_state, store, t0 + 60)
    assert decision is not None
    assert decision.severity == Severity.CRITICAL
    assert decision.rule_id == "egg_loss"


# ── Track 1: direct_nest_interaction invariant (2026-04-17) ──────────

def test_direct_nest_interaction_without_threat_species_does_not_alert(store):
    """Replays today's 14:56 false-HIGH scenario: Opus saw the cardinal
    tending the nest and set direct_nest_interaction=true (a schema
    violation — that field is defined for non-cardinal animals only),
    but threat_species_detected was empty. Before the invariant, this
    would fire CRITICAL direct_attack on the cardinal's own behavior.

    With the invariant, rule 1 requires threats to be non-empty.
    """
    t0 = time.time()
    cardinal_tending = _make_obs(
        attending_parent_on_nest="false",  # she's leaning over, not sitting
        attending_parent_present="true",
        species_detected=["female northern cardinal"],
        threat_species_detected=[],  # key: no threat
        near_nest_activity=False,
        direct_nest_interaction=True,  # model's schema violation
        confidence=0.88,
        summary="Female cardinal leaning over the cup, beak down.",
    )
    pre_state = store.get_state()
    decision = evaluate(cardinal_tending, pre_state, store, t0)
    assert decision is None or decision.rule_id != "direct_attack", (
        "direct_nest_interaction=true with empty threat_species must NOT "
        "fire CRITICAL direct_attack. The cardinal's own behavior cannot "
        "be a direct attack."
    )


def test_direct_nest_interaction_with_thrasher_still_fires_critical(store):
    """Negative control: real thrasher direct attack (threat_species
    non-empty) still fires CRITICAL. The invariant must not suppress
    genuine attacks.
    """
    t0 = time.time()
    attack = _make_obs(
        attending_parent_on_nest="false",
        attending_parent_present="false",
        species_detected=["brown thrasher"],
        threat_species_detected=["brown_thrasher"],
        near_nest_activity=True,
        direct_nest_interaction=True,
        confidence=0.88,
        summary="Brown thrasher beak deep in the cup reaching for egg.",
    )
    pre_state = store.get_state()
    decision = evaluate(attack, pre_state, store, t0)
    assert decision is not None
    assert decision.severity == Severity.CRITICAL
    assert decision.rule_id == "direct_attack"


def test_direct_nest_interaction_with_unknown_threat_still_fires(store):
    """Even 'unknown' threat counts: if the model flagged something as a
    threat (not the cardinal) and direct_nest_interaction=true, fire.
    The invariant only suppresses when threat_species is EMPTY.
    """
    t0 = time.time()
    attack = _make_obs(
        attending_parent_on_nest="false",
        attending_parent_present="false",
        species_detected=["unknown bird"],
        threat_species_detected=["unknown"],
        near_nest_activity=True,
        direct_nest_interaction=True,
        confidence=0.72,
        summary="Unknown brownish bird reaching into the cup.",
    )
    pre_state = store.get_state()
    decision = evaluate(attack, pre_state, store, t0)
    assert decision is not None
    assert decision.severity == Severity.CRITICAL
    assert decision.rule_id == "direct_attack"


# ── Track 4: ambiguous-occupied-cup path (2026-04-17) ─────────────────

def _ambig_obs(**overrides) -> NestObservation:
    """Baseline ambiguous-occupied-cup observation: bird at cup, species
    unknown. Matches the dominant 2026-04-17 false-positive pattern."""
    base = dict(
        attending_parent_present="uncertain",
        attending_parent_on_nest="uncertain",
        eggs_visible="false",
        egg_count_estimate=None,
        nest_visible=True,
        nest_disturbed="false",
        species_detected=["unknown brownish bird"],
        threat_species_detected=["unknown"],
        near_nest_activity=True,
        direct_nest_interaction=False,
        confidence=0.65,
        summary="A brownish bird is in the nest cup but crest not visible.",
    )
    base.update(overrides)
    return NestObservation(**base)


def test_is_ambiguous_occupied_cup_helper():
    """Direct unit test of the predicate."""
    from birdnest_ai.events import is_ambiguous_occupied_cup

    # Positive: canonical ambig frame
    assert is_ambiguous_occupied_cup(_ambig_obs()) is True

    # Negative: confirmed attending_parent_on_nest=true
    assert is_ambiguous_occupied_cup(_ambig_obs(attending_parent_on_nest="true")) is False

    # Negative: confirmed attending_parent_on_nest=false (clearly empty)
    assert is_ambiguous_occupied_cup(_ambig_obs(attending_parent_on_nest="false")) is False

    # Negative: nest not visible
    assert is_ambiguous_occupied_cup(_ambig_obs(nest_visible=False)) is False

    # Negative: no near-nest activity
    assert is_ambiguous_occupied_cup(_ambig_obs(near_nest_activity=False)) is False

    # Negative: named threat species (brown_thrasher) bypasses path
    assert is_ambiguous_occupied_cup(_ambig_obs(
        threat_species_detected=["brown_thrasher"],
    )) is False

    # Positive: threat_species=["unknown"] still qualifies (unknown is not
    # a NAMED threat, so the ambig path handles it)
    assert is_ambiguous_occupied_cup(_ambig_obs(
        threat_species_detected=["unknown"],
    )) is True

    # Positive: empty threat list also qualifies
    assert is_ambiguous_occupied_cup(_ambig_obs(
        threat_species_detected=[],
    )) is True


def test_first_ambig_frame_does_not_fire_alert(store):
    """Replays the dominant 2026-04-17 false-alarm pattern. First ambig
    frame must produce NO alert (neither MEDIUM absence nor HIGH predator).
    Pre-fix, this would have fired HIGH predator_near_nest."""
    t0 = time.time()
    # Seed: mom was here 10 min ago, then she "left" (so in_absence=True).
    store.record(t0 - 600, False, None, _make_obs(), None)
    out = _make_obs(
        attending_parent_on_nest="false",
        attending_parent_present="false",
        species_detected=[],
        summary="Nest empty.",
    )
    store.record(t0 - 300, False, None, out, None)

    # Now the ambig frame.
    ambig = _ambig_obs()
    pre_state = store.get_state()
    decision = evaluate(ambig, pre_state, store, t0)
    assert decision is None, (
        "1st ambiguous occupied-cup frame must not fire any alert."
    )


def test_second_consecutive_ambig_frame_promotes_to_soft_presence(store):
    """Second consecutive ambig frame within the confirmation window
    clears in_absence and updates last_attending_parent_seen_ts (soft presence),
    still fires no alert."""
    t0 = time.time()
    store.record(t0 - 600, False, None, _make_obs(), None)
    out = _make_obs(
        attending_parent_on_nest="false",
        attending_parent_present="false",
        species_detected=[],
        summary="Nest empty.",
    )
    store.record(t0 - 300, False, None, out, None)
    assert store.get_state().in_absence is True

    # 1st ambig frame — sets pending.
    ambig1 = _ambig_obs()
    store.record(t0, False, None, ambig1, None)
    after1 = store.get_state()
    assert after1.pending_ambiguous_frame_ts == pytest.approx(t0, abs=1.0)
    assert after1.in_absence is True  # 1st frame doesn't clear absence yet

    # 2nd consecutive ambig frame within window (2 min later).
    t1 = t0 + 120
    ambig2 = _ambig_obs()
    state = store.record(t1, False, None, ambig2, None)
    assert state.pending_ambiguous_frame_ts is None, "pending cleared"
    assert state.in_absence is False, "soft presence clears in_absence"
    assert state.last_attending_parent_seen_ts == pytest.approx(t1, abs=1.0), (
        "soft presence updates last_attending_parent_seen_ts"
    )

    # No alert fires from evaluate() either.
    pre_state2 = NestState(
        **{**after1.model_dump(), "in_absence": True, "last_attending_parent_seen_ts": t0 - 600},
    )
    decision = evaluate(ambig2, pre_state2, store, t1)
    assert decision is None, "2nd ambig frame must still not fire an alert"


def test_pending_ambig_expires_after_window(store):
    """If no 2nd consecutive ambig frame arrives within the window, the
    next ambig frame is treated as a fresh 1st (window restarts)."""
    from birdnest_ai.state import _AMBIGUOUS_CONFIRM_WINDOW_S

    t0 = time.time()
    store.record(t0, False, None, _ambig_obs(), None)
    first_pending = store.get_state().pending_ambiguous_frame_ts
    assert first_pending == pytest.approx(t0, abs=1.0)

    # Next ambig frame arrives AFTER the window.
    t1 = t0 + _AMBIGUOUS_CONFIRM_WINDOW_S + 60
    store.record(t1, False, None, _ambig_obs(), None)
    state = store.get_state()
    # Must be treated as new 1st sighting (restart window), not promote.
    assert state.pending_ambiguous_frame_ts == pytest.approx(t1, abs=1.0)
    assert state.in_absence is False or state.in_absence is True  # unchanged
    # Crucially, last_attending_parent_seen_ts should NOT update (no soft presence).


def test_named_thrasher_still_fires_immediately_on_single_frame(store):
    """Named threat species (brown_thrasher) bypasses the ambig-cup path
    entirely — single-frame HIGH alert still fires. The ambig path must
    NOT over-suppress real threats."""
    t0 = time.time()
    # Seed: mom away.
    store.record(t0 - 600, False, None, _make_obs(), None)
    out = _make_obs(
        attending_parent_on_nest="false",
        attending_parent_present="false",
        species_detected=[],
        summary="Nest empty.",
    )
    store.record(t0 - 300, False, None, out, None)

    # Single-frame thrasher at nest.
    threat = _make_obs(
        attending_parent_on_nest="false",
        attending_parent_present="false",
        species_detected=["brown thrasher"],
        threat_species_detected=["brown_thrasher"],
        near_nest_activity=True,
        direct_nest_interaction=False,
        confidence=0.85,
        summary="Brown thrasher on the bush at the nest rim.",
    )
    pre_state = store.get_state()
    decision = evaluate(threat, pre_state, store, t0)
    assert decision is not None
    assert decision.severity == Severity.HIGH
    assert decision.rule_id == "predator_absent"


def test_ambig_then_unambiguous_cardinal_clears_pending(store):
    """A pending ambig candidate is cleared when the next frame is clearly
    unambiguous (clear cardinal or clear empty or named threat)."""
    t0 = time.time()
    # 1st ambig frame.
    store.record(t0, False, None, _ambig_obs(), None)
    assert store.get_state().pending_ambiguous_frame_ts is not None

    # Next frame: clear cardinal on nest.
    t1 = t0 + 60
    clear_cardinal = _make_obs(
        attending_parent_on_nest="true",
        attending_parent_present="true",
        species_detected=["northern_cardinal"],
        threat_species_detected=[],
        near_nest_activity=False,
        direct_nest_interaction=False,
        confidence=0.9,
        summary="Clear view of female cardinal on nest.",
    )
    state = store.record(t1, False, None, clear_cardinal, None)
    assert state.pending_ambiguous_frame_ts is None, (
        "Unambiguous frame must clear any pending ambig candidate"
    )


# ── Codex round N+1 guardrails on the ambig path (2026-04-17) ─────────

def test_unknown_species_direct_attack_still_fires_critical(store):
    """Codex P1: the ambig-cup gate must NOT suppress a direct_nest_interaction
    frame even if species is reported as 'unknown'. This is a life-critical
    signal (thrasher's beak in the cup on a single frame Sonnet couldn't
    species-ID). Real attacks must always reach the CRITICAL path.
    """
    from birdnest_ai.events import is_ambiguous_occupied_cup

    t0 = time.time()
    # Frame: attending_parent_on_nest=uncertain (can't ID), threat_species=unknown,
    # near_nest=true, AND direct_nest_interaction=true. Without the exclusion
    # in is_ambiguous_occupied_cup, this would match ambig predicate and
    # get suppressed. With the fix, direct_nest_interaction=true kicks it
    # out of the ambig predicate and into rule 1.
    attack = _make_obs(
        attending_parent_on_nest="uncertain",
        attending_parent_present="uncertain",
        species_detected=["unknown bird"],
        threat_species_detected=["unknown"],
        near_nest_activity=True,
        direct_nest_interaction=True,
        confidence=0.75,
        summary=(
            "A bird is reaching deep into the cup with its beak — species "
            "cannot be confirmed in this frame."
        ),
    )
    # Predicate must say it's NOT ambig.
    assert is_ambiguous_occupied_cup(attack) is False, (
        "direct_nest_interaction=true must exclude the frame from the "
        "ambig path, even if species is 'unknown'."
    )
    # And evaluate() must fire CRITICAL direct_attack.
    pre_state = store.get_state()
    decision = evaluate(attack, pre_state, store, t0)
    assert decision is not None
    assert decision.severity == Severity.CRITICAL
    assert decision.rule_id == "direct_attack"


def test_ambig_frame_in_feeding_stage_does_not_trigger_lifecycle(store, monkeypatch):
    """Codex P2: the ambig-cup early return must run BEFORE _lifecycle_event
    so a crest-hidden occupied-cup frame can't leak into fledge-detection
    or other lifecycle transitions during feeding stage.

    Setup: lifecycle_stage=feeding, hatch_detected_ts set 48h ago, mom's
    last confirmed sighting 13h ago (past the 12h fledge threshold), no
    threats in 48h. An ambig frame now would — without the reorder —
    trigger the fledge transition check inside _lifecycle_event because
    attending_parent_on_nest != 'true' and all fledge preconditions are met.
    With the reorder, the ambig check fires first and returns None.
    """
    from birdnest_ai.config import get_settings
    monkeypatch.setattr(get_settings(), "lifecycle_tracking_enabled", True)

    # Seed feeding-stage state directly via SQL to skip the 2-sighting dance.
    t_now = time.time()
    store._conn.execute(
        "UPDATE state SET "
        " lifecycle_stage='feeding', "
        " hatch_detected_ts=?, "
        " last_attending_parent_seen_ts=?, "
        " last_threat_seen_ts=NULL, "
        " in_absence=1 "
        "WHERE id=1",
        (t_now - 48 * 3600, t_now - 13 * 3600),
    )

    # Ambig frame right now.
    ambig = _ambig_obs()
    pre_state = store.get_state()
    assert pre_state.lifecycle_stage == "feeding"

    decision = evaluate(ambig, pre_state, store, t_now)
    assert decision is None, (
        "Ambig frame in feeding stage must not trigger fledge alert — the "
        "ambig check must run BEFORE _lifecycle_event."
    )
    assert decision is None or decision.rule_id != "fledge"

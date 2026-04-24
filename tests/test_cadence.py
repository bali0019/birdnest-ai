"""Unit tests for burst-cadence selection.

Burst cadence tightens the snap interval to `burst_snap_interval_seconds`
(default 30s) for the first `burst_duration_seconds` (default 180s = 3 min)
after `in_absence` flips True. Peak predation-risk window — thrasher
attacks can be ~4 s events that a 60s absence cadence would miss.

These tests exercise the real ``cadence.compute_snap_interval`` helper
(shared by both downloader and combined modes). A prior revision of this
file reproduced the decision tree locally, which meant the tests could
still "pass" while the downloader closure drifted. Calling the real
function keeps tests and production in lock-step.
"""

from __future__ import annotations

import time
from datetime import datetime, time as dtime

import pytest

from cardinal_nest_monitor.cadence import compute_snap_interval
from cardinal_nest_monitor.config import get_settings
from cardinal_nest_monitor.schema import NestState
from cardinal_nest_monitor.state import StateStore


def _pick_interval(
    settings,
    state: NestState,
    now_ts: float,
    now_time: dtime | None = None,
) -> tuple[int, str]:
    """Thin wrapper around the real ``compute_snap_interval``.

    ``now_time`` is retained for backwards compatibility with older test
    callsites but ignored — ``compute_snap_interval`` derives the local
    time from ``now_ts`` internally. Callers that need a specific
    time-of-day (e.g. quiet-hours tests) should set
    ``settings.quiet_hours`` to cover the window they want rather than
    pass a detached ``dtime``.
    """
    return compute_snap_interval(settings, state, now_ts)


@pytest.fixture
def store(tmp_path):
    s = StateStore(tmp_path / "state.sqlite")
    yield s
    s.close()


@pytest.fixture
def settings(monkeypatch):
    s = get_settings()
    # Force deterministic values independent of the .env on disk.
    monkeypatch.setattr(s, "snap_interval_seconds", 300)
    monkeypatch.setattr(s, "absence_snap_interval_seconds", 60)
    monkeypatch.setattr(s, "burst_snap_interval_seconds", 30)
    monkeypatch.setattr(s, "burst_duration_seconds", 180)
    monkeypatch.setattr(s, "quiet_snap_interval_seconds", 1800)
    # disable_quiet_hours_for_unit_tests (autouse) already clears this,
    # but be explicit for readability.
    monkeypatch.setattr(s, "quiet_hours", "")
    return s


# ── absence_started_ts is persisted correctly ──────────────────────────

def test_record_sets_absence_started_ts_on_flip(store):
    """in_absence: False → True must set absence_started_ts to the flip ts."""
    from cardinal_nest_monitor.schema import NestObservation

    def _obs(**kw):
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
            summary="Mom on nest.",
        )
        base.update(kw)
        return NestObservation(**base)

    t0 = time.time()
    # Seed: mom on nest.
    store.record(t0, False, None, _obs(), None)
    # 130s later, she's gone — triggers the absence flip (threshold is 120s).
    absent = _obs(
        mother_cardinal_present="false",
        cardinal_on_nest="false",
        species_detected=[],
        summary="Empty nest.",
    )
    state = store.record(t0 + 130, False, None, absent, None)
    assert state.in_absence is True
    assert state.absence_started_ts == pytest.approx(t0 + 130, abs=1)


def test_record_clears_absence_started_ts_on_return(store):
    """in_absence: True → False must clear absence_started_ts."""
    from cardinal_nest_monitor.schema import NestObservation

    def _obs(**kw):
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
            summary="On nest.",
        )
        base.update(kw)
        return NestObservation(**base)

    t0 = time.time()
    store.record(t0, False, None, _obs(), None)
    absent = _obs(
        mother_cardinal_present="false",
        cardinal_on_nest="false",
        species_detected=[],
        summary="Nest empty.",
    )
    state = store.record(t0 + 130, False, None, absent, None)
    assert state.in_absence is True
    assert state.absence_started_ts is not None

    # Mom returns.
    state = store.record(t0 + 300, False, None, _obs(), None)
    assert state.in_absence is False
    assert state.absence_started_ts is None


# ── Burst-cadence decision tree ────────────────────────────────────────

def test_burst_cadence_fires_immediately_after_absence(settings):
    """absence just started → cadence must be burst (30s)."""
    now_ts = 1_700_000_000.0
    state = NestState(
        last_mother_seen_ts=now_ts - 130,
        in_absence=True,
        absence_started_ts=now_ts,  # flipped this very instant
    )
    interval, label = _pick_interval(
        settings, state, now_ts, dtime(12, 0)
    )
    assert label == "burst"
    assert interval == 30


def test_burst_cadence_holds_within_window(settings):
    """60s into a 180s burst window → still burst cadence."""
    now_ts = 1_700_000_000.0
    state = NestState(
        last_mother_seen_ts=now_ts - 200,
        in_absence=True,
        absence_started_ts=now_ts - 60,  # 60s into the burst
    )
    interval, label = _pick_interval(
        settings, state, now_ts, dtime(12, 0)
    )
    assert label == "burst"
    assert interval == 30


def test_burst_cadence_expires_after_duration(settings):
    """200s after absence onset (past 180s burst) → normal absence (60s)."""
    now_ts = 1_700_000_000.0
    state = NestState(
        last_mother_seen_ts=now_ts - 400,
        in_absence=True,
        absence_started_ts=now_ts - 200,  # past burst_duration_seconds (180)
    )
    interval, label = _pick_interval(
        settings, state, now_ts, dtime(12, 0)
    )
    assert label == "absence"
    assert interval == 60


def test_burst_cadence_expires_exactly_at_duration_boundary(settings):
    """At exactly burst_duration_seconds elapsed → no longer burst.

    The condition is strict `<`, so 180s elapsed is the first sample
    that falls out of the burst window.
    """
    now_ts = 1_700_000_000.0
    state = NestState(
        last_mother_seen_ts=now_ts - 300,
        in_absence=True,
        absence_started_ts=now_ts - 180,
    )
    interval, label = _pick_interval(
        settings, state, now_ts, dtime(12, 0)
    )
    assert label == "absence"
    assert interval == 60


def test_burst_cadence_respects_quiet_hours(settings, monkeypatch):
    """Quiet hours override burst — even if mom just left, we stay sparse.

    During quiet hours the cardinal is almost certainly on the nest even
    if our IR-noisy analyzer says "absent" with low confidence. We stick
    to quiet_snap_interval_seconds (30 min) to save battery overnight.
    """
    monkeypatch.setattr(settings, "quiet_hours", "00:00-23:59")
    now_ts = 1_700_000_000.0
    state = NestState(
        last_mother_seen_ts=now_ts - 130,
        in_absence=True,
        absence_started_ts=now_ts,  # burst would fire
    )
    interval, label = _pick_interval(
        settings, state, now_ts, dtime(2, 30)  # middle of quiet hours
    )
    assert label == "quiet"
    assert interval == 1800


def test_normal_cadence_when_not_absent(settings):
    """No absence at all → default 300s cadence."""
    now_ts = 1_700_000_000.0
    state = NestState(
        last_mother_seen_ts=now_ts - 30,
        in_absence=False,
        absence_started_ts=None,
    )
    interval, label = _pick_interval(
        settings, state, now_ts, dtime(12, 0)
    )
    assert label == "default"
    assert interval == 300


def test_burst_cadence_defensive_when_absence_started_ts_missing(settings):
    """Legacy DB rows may have in_absence=True but absence_started_ts=None.

    In that case the burst window is unknowable, so fall through to the
    normal absence cadence rather than crashing or defaulting to burst
    indefinitely.
    """
    now_ts = 1_700_000_000.0
    state = NestState(
        last_mother_seen_ts=now_ts - 400,
        in_absence=True,
        absence_started_ts=None,  # pre-migration row
    )
    interval, label = _pick_interval(
        settings, state, now_ts, dtime(12, 0)
    )
    assert label == "absence"
    assert interval == 60


# ── Named tests per the 2026-04-23 burst-cadence fix plan ──────────────
# These exercise ``compute_snap_interval`` directly (no ``_pick_interval``
# wrapper). They duplicate some coverage from the tests above on purpose —
# the plan calls them out by name as the load-bearing guards to keep
# around while the cadence helper is new.


def test_compute_snap_interval_quiet_wins_over_absence(settings, monkeypatch):
    """Quiet hours trump burst even when in_absence and within burst window."""
    monkeypatch.setattr(settings, "quiet_hours", "00:00-23:59")
    now_ts = 1_700_000_000.0
    state = NestState(
        in_absence=True,
        absence_started_ts=now_ts,  # would otherwise be burst
    )
    interval, label = compute_snap_interval(settings, state, now_ts)
    assert label == "quiet"
    assert interval == 1800


def test_compute_snap_interval_burst_when_in_absence_within_window(settings):
    """in_absence=True and 30s into a 180s burst window → burst cadence."""
    now_ts = 1_700_000_000.0
    state = NestState(
        in_absence=True,
        absence_started_ts=now_ts - 30,
    )
    interval, label = compute_snap_interval(settings, state, now_ts)
    assert label == "burst"
    assert interval == 30


def test_compute_snap_interval_absence_when_burst_window_expired(settings):
    """in_absence=True but 200s past absence onset (>180s burst duration)
    → fall through to normal absence cadence, not burst."""
    now_ts = 1_700_000_000.0
    state = NestState(
        in_absence=True,
        absence_started_ts=now_ts - 200,
    )
    interval, label = compute_snap_interval(settings, state, now_ts)
    assert label == "absence"
    assert interval == 60


def test_compute_snap_interval_default_when_not_in_absence(settings):
    """Baseline: mom on nest, no absence → default snap interval."""
    now_ts = 1_700_000_000.0
    state = NestState(in_absence=False)
    interval, label = compute_snap_interval(settings, state, now_ts)
    assert label == "default"
    assert interval == 300


# ── PARITY GUARD: combined mode must also use compute_snap_interval ────
# Regresses if someone edits main.py's get_interval back to the
# pre-2026-04-23 quiet/absence/default-only branching, which silently
# disabled §21 burst cadence in combined mode (the dev / rollback path).


def test_main_combined_mode_get_interval_delegates_to_compute_snap_interval():
    """PARITY GUARD.

    main.py's combined-mode cadence must route through the shared
    ``compute_snap_interval`` helper so the documented §21 precedence
    (quiet > session-burst > burst > absence > default) actually fires in
    combined mode. Prior to 2026-04-23 this path branched only on
    quiet / absence / default — burst was silently dead. Source-inspect
    rather than a new test module per plan: the check is "does main.py
    call the helper at all", not "does the helper work" (already covered
    above).
    """
    import inspect

    from cardinal_nest_monitor import main as main_mod

    src = inspect.getsource(main_mod)
    assert "compute_snap_interval(" in src, (
        "main.py must delegate combined-mode cadence to "
        "cadence.compute_snap_interval — do NOT reintroduce local "
        "branching that drops the §21 burst cadence"
    )


# ── Session-burst (restart-local, evidence-gated catch-up) ─────────────
# Precedence slot: quiet > SESSION-BURST > burst > absence > default.
# Armed by cadence.arm_session_burst_if_absent once per process lifetime,
# only when the first post-startup observation confirms in_absence=True.
# Distinct from the calendar-anchored `burst` tied to absence_started_ts —
# see CLAUDE.md §21 for the rationale.


def test_compute_snap_interval_session_burst_when_armed_and_absent(settings):
    """in_absence=True + session burst armed → session-burst label."""
    now_ts = 1_700_000_000.0
    now_monotonic = 1000.0
    state = NestState(
        in_absence=True,
        # Absence started 20 min ago — WELL past the calendar-burst window.
        # The only way this should return burst cadence is via session-burst.
        absence_started_ts=now_ts - 1200,
    )
    interval, label = compute_snap_interval(
        settings, state, now_ts,
        session_burst_until_monotonic=now_monotonic + 100,  # armed
        now_monotonic=now_monotonic,
    )
    assert label == "session-burst"
    assert interval == 30


def test_compute_snap_interval_session_burst_yields_to_quiet_hours(settings, monkeypatch):
    """Quiet hours still win — even a session-burst armed during an
    absence must defer to the overnight quiet cadence. Battery over
    reaction time during quiet hours is an explicit design call."""
    monkeypatch.setattr(settings, "quiet_hours", "00:00-23:59")
    now_ts = 1_700_000_000.0
    now_monotonic = 1000.0
    state = NestState(in_absence=True, absence_started_ts=now_ts - 1200)
    interval, label = compute_snap_interval(
        settings, state, now_ts,
        session_burst_until_monotonic=now_monotonic + 100,  # armed
        now_monotonic=now_monotonic,
    )
    assert label == "quiet"
    assert interval == 1800


def test_compute_snap_interval_session_burst_skipped_when_mom_returned(settings):
    """Session-burst must NOT fire if the state shows in_absence=False,
    even if the deadline has not expired yet. This is the evidence gate
    in reverse — if mom is back, no heightened cadence needed.
    (Callers should also clear session_burst_until_monotonic on
    mom-return, but compute_snap_interval itself must also enforce the
    gate so a race between the state read and the clear can't leak.)
    """
    now_ts = 1_700_000_000.0
    now_monotonic = 1000.0
    state = NestState(in_absence=False)
    interval, label = compute_snap_interval(
        settings, state, now_ts,
        session_burst_until_monotonic=now_monotonic + 100,  # still armed
        now_monotonic=now_monotonic,
    )
    assert label == "default"
    assert interval == 300


def test_compute_snap_interval_session_burst_expires_at_deadline(settings):
    """Past the session-burst monotonic deadline → fall through to
    calendar-anchored precedence. At this point absence_started_ts is
    also > burst_duration_seconds old (by construction for this test),
    so we should see `absence`, not `burst` or `session-burst`."""
    now_ts = 1_700_000_000.0
    now_monotonic = 1000.0
    state = NestState(in_absence=True, absence_started_ts=now_ts - 1200)
    interval, label = compute_snap_interval(
        settings, state, now_ts,
        session_burst_until_monotonic=now_monotonic - 1,  # already expired
        now_monotonic=now_monotonic,
    )
    assert label == "absence"
    assert interval == 60


def test_compute_snap_interval_session_burst_defaults_backwards_compatible(settings):
    """Existing callers that don't pass session_burst kwargs must still
    get the pre-session-burst precedence — quiet > burst > absence >
    default. Regression guard for the 9+ existing test_cadence callsites
    that pre-date session-burst."""
    now_ts = 1_700_000_000.0
    state = NestState(in_absence=True, absence_started_ts=now_ts - 30)
    interval, label = compute_snap_interval(settings, state, now_ts)
    # No session_burst kwargs → calendar burst fires normally.
    assert label == "burst"
    assert interval == 30


# ── arm_session_burst_if_absent — evidence-gated arming ─────────────────


async def test_arm_session_burst_arms_when_fresh_observation_confirms_absence(
    store, settings
):
    """Happy path: startup at T, a post-T observation lands, state shows
    in_absence=True at that moment → session_state gets a monotonic
    deadline roughly equal to now+burst_duration_seconds."""
    from cardinal_nest_monitor.cadence import arm_session_burst_if_absent
    from cardinal_nest_monitor.schema import NestObservation

    startup_wall_ts = time.time()

    # Seed a post-startup observation that implies absence. We write it
    # via the real record() so in_absence flips exactly the way
    # production does.
    def _obs(**kw):
        base = dict(
            mother_cardinal_present="false",
            cardinal_on_nest="false",
            eggs_visible="false",
            egg_count_estimate=None,
            nest_visible=True,
            nest_disturbed="false",
            species_detected=[],
            threat_species_detected=[],
            near_nest_activity=False,
            direct_nest_interaction=False,
            confidence=0.9,
            summary="Nest empty.",
        )
        base.update(kw)
        return NestObservation(**base)

    # Need two records to cross the 120s in_absence threshold.
    store.record(startup_wall_ts + 0.1, False, None, _obs(
        mother_cardinal_present="true", cardinal_on_nest="true",
        species_detected=["northern_cardinal"], summary="On nest.",
    ), None)
    state_after = store.record(
        startup_wall_ts + 130.0, False, None, _obs(), None,
    )
    assert state_after.in_absence is True, "test setup: expected absence to flip"

    session_state: dict[str, float | None] = {"until_monotonic": None}
    # poll_interval tiny so the test completes fast.
    await arm_session_burst_if_absent(
        store, settings, startup_wall_ts, session_state,
        poll_interval=0.01, max_wait_seconds=2.0,
    )
    assert session_state["until_monotonic"] is not None, (
        "arming task should have armed session-burst: fresh observation "
        "existed and state showed in_absence=True"
    )


async def test_arm_session_burst_does_not_arm_when_mom_on_nest(store, settings):
    """Fresh observation exists BUT shows mom on nest → session-burst
    stays disarmed (no catch-up needed; we have fresh evidence she's
    fine)."""
    from cardinal_nest_monitor.cadence import arm_session_burst_if_absent
    from cardinal_nest_monitor.schema import NestObservation

    startup_wall_ts = time.time()

    on_nest = NestObservation(
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
        summary="On nest.",
    )
    store.record(startup_wall_ts + 0.1, False, None, on_nest, None)

    session_state: dict[str, float | None] = {"until_monotonic": None}
    await arm_session_burst_if_absent(
        store, settings, startup_wall_ts, session_state,
        poll_interval=0.01, max_wait_seconds=2.0,
    )
    assert session_state["until_monotonic"] is None, (
        "arming task must NOT arm when fresh observation shows mom on nest"
    )


async def test_arm_session_burst_skips_when_no_fresh_observation(store, settings):
    """Analyzer is down → no post-startup observation arrives within the
    grace window → session-burst stays disarmed. Load-bearing: we don't
    want to fire restart-catch-up on stale pre-restart state."""
    from cardinal_nest_monitor.cadence import arm_session_burst_if_absent

    startup_wall_ts = time.time()
    # No observations seeded. MAX(ts) will stay None throughout.

    session_state: dict[str, float | None] = {"until_monotonic": None}
    await arm_session_burst_if_absent(
        store, settings, startup_wall_ts, session_state,
        poll_interval=0.01, max_wait_seconds=0.2,  # short window for CI speed
    )
    assert session_state["until_monotonic"] is None, (
        "arming task must NOT arm when no fresh observation lands "
        "within the grace window"
    )


# ── Role parity: both downloader and combined mode must arm session-burst ──


def test_downloader_arms_session_burst_on_startup():
    """downloader_loop.run_downloader_service must launch the
    arm_session_burst_if_absent task. Regresses if someone rewires the
    downloader startup to drop the arming task.
    """
    import inspect

    from cardinal_nest_monitor import downloader_loop as dl

    src = inspect.getsource(dl)
    assert "arm_session_burst_if_absent(" in src, (
        "downloader_loop must launch the session-burst arming task on "
        "startup — without it, restart catch-up is silently disabled"
    )


def test_combined_mode_arms_session_burst_on_startup():
    """main.py's run_combined must also launch the arming task. Role
    parity with the downloader — see CLAUDE.md §21."""
    import inspect

    from cardinal_nest_monitor import main as main_mod

    src = inspect.getsource(main_mod)
    assert "arm_session_burst_if_absent(" in src, (
        "main.py must launch the session-burst arming task so combined "
        "mode has the same restart-catch-up semantics as split mode"
    )

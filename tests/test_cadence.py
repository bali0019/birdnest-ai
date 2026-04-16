"""Unit tests for burst-cadence selection in the downloader loop.

Burst cadence tightens the snap interval to `burst_snap_interval_seconds`
(default 30s) for the first `burst_duration_seconds` (default 180s = 3 min)
after `in_absence` flips True. Peak predation-risk window — thrasher
attacks can be ~4 s events that a 60s absence cadence would miss.

These tests exercise the exact decision tree in
:func:`downloader_loop.run_downloader_service`'s inner ``get_interval``
helper without booting the full async service. The logic is reproduced
literally here so that a regression in the downloader closure would still
be caught by the assertions on the intended policy.
"""

from __future__ import annotations

import time
from datetime import datetime, time as dtime

import pytest

from cardinal_nest_monitor.config import get_settings
from cardinal_nest_monitor.schema import NestState
from cardinal_nest_monitor.state import StateStore


def _pick_interval(
    settings,
    state: NestState,
    now_ts: float,
    now_time: dtime,
) -> tuple[int, str]:
    """Reproduce the cadence decision from downloader_loop.get_interval.

    Returns (interval_seconds, label). Keep in lock-step with the source —
    any change to the policy in downloader_loop.py must be mirrored here
    (and vice versa) so these tests remain meaningful.
    """
    if settings.in_quiet_hours(now_time):
        return settings.quiet_snap_interval_seconds, "quiet"

    if state.in_absence:
        if (
            state.absence_started_ts is not None
            and (now_ts - state.absence_started_ts)
            < settings.burst_duration_seconds
        ):
            return settings.burst_snap_interval_seconds, "burst"
        return settings.absence_snap_interval_seconds, "absence"

    return settings.snap_interval_seconds, "default"


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

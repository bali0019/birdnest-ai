"""Integration tests for the lifecycle tracking feature.

These simulate a full incubation → feeding → fledging lifecycle through
the real Pipeline.on_image path, using curated reference JPEGs as the
input bytes but mocking the analyzer to return observations that match
each stage (so we don't burn Anthropic credits on every test run).

The analyzer-prompt accuracy is separately validated by
`tools/lifecycle_regression.py` which calls the real API.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from cardinal_nest_monitor import analyzer as analyzer_mod
from cardinal_nest_monitor import main as main_mod
from cardinal_nest_monitor.config import get_settings
from cardinal_nest_monitor.schema import NestObservation, Severity


_LIFECYCLE_DIR = (
    Path(__file__).resolve().parents[2] / "evidence" / "reference" / "lifecycle"
)


def _pipeline(store, notifier, evidence):
    counters = main_mod.DailyCounters()
    return main_mod.Pipeline(
        store=store,
        notifier=notifier,
        evidence=evidence,
        counters=counters,
        feed_queue=None,
    )


def _obs(**kwargs) -> NestObservation:
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
        young_visible="uncertain",
        young_count_estimate=None,
        attending_parent_feeding_young=False,
        confidence=0.9,
        summary="Mom on nest.",
    )
    base.update(kwargs)
    return NestObservation(**base)


@pytest.fixture
def lifecycle_on(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "lifecycle_tracking_enabled", True)
    # Disable verifier for simpler test paths
    monkeypatch.setattr(settings, "verify_alerts_with_opus", False)


# ── Regression guard: flag off is byte-identical ──────────────────────

async def test_lifecycle_off_by_default_behaves_like_today(
    monkeypatch, store, evidence, notifier, cardinal_jpeg_bytes, obs_on_nest,
):
    """With lifecycle_tracking_enabled=False (default), Pipeline.on_image
    behavior is identical to today — no lifecycle transitions, no hatch
    or fledge alerts."""
    settings = get_settings()
    monkeypatch.setattr(settings, "lifecycle_tracking_enabled", False)

    send_alert_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(notifier, "send_alert", send_alert_mock)

    # Even if the analyzer returned young_visible=true, no transition.
    analyze_mock = AsyncMock(return_value=_obs(
        attending_parent_on_nest="false",
        young_visible="true",
        young_count_estimate=2,
    ))
    monkeypatch.setattr(analyzer_mod, "analyze", analyze_mock)

    pipeline = _pipeline(store, notifier, evidence)
    await pipeline.on_image(
        cardinal_jpeg_bytes,
        {"motion_triggered": False, "ts": time.time()},
    )

    final_state = store.get_state()
    assert final_state.lifecycle_stage == "incubation"
    assert final_state.hatch_detected_ts is None


# ── Hatch alert end-to-end ────────────────────────────────────────────

async def test_hatch_alert_fires_through_pipeline_after_confirmation(
    monkeypatch, store, evidence, notifier, lifecycle_on,
):
    """Hatch alert requires 2 confirming chick signals through the pipeline.
    First snap records the sighting silently; second snap fires the alert."""
    # Small JPEG (< 8MB) to stay under Discord's attachment limit.
    chick_jpeg = (_LIFECYCLE_DIR / "wm_chick_hatchling_01.jpg").read_bytes()

    # Seed incubation state first
    t0 = time.time() - 7200
    store.record(t0, False, None, _obs(), None)

    # Capture send_alert calls
    captured = []
    orig = notifier.send_alert

    async def _capture(decision, observation, **kwargs):
        captured.append(decision)
        return await orig(decision, observation, **kwargs)

    monkeypatch.setattr(notifier, "send_alert", _capture)

    # Mock analyzer to return young_visible=true on every snap
    analyze_mock = AsyncMock(return_value=_obs(
        attending_parent_on_nest="false",
        attending_parent_present="false",
        young_visible="true",
        young_count_estimate=3,
        summary="Three chicks begging in nest.",
    ))
    monkeypatch.setattr(analyzer_mod, "analyze", analyze_mock)

    pipeline = _pipeline(store, notifier, evidence)

    # 1st snap — no hatch alert should fire yet
    t1 = t0 + 3600
    await pipeline.on_image(chick_jpeg, {"motion_triggered": False, "ts": t1})
    hatch_alerts = [d for d in captured if d.rule_id == "hatch"]
    assert len(hatch_alerts) == 0, (
        f"expected no hatch alert on 1st sighting, got {len(hatch_alerts)}"
    )
    assert store.get_state().lifecycle_stage == "incubation"
    assert store.get_state().first_chick_sighting_ts is not None

    # 2nd snap within confirmation window — hatch alert fires
    t2 = t1 + 1800
    await pipeline.on_image(chick_jpeg, {"motion_triggered": False, "ts": t2})
    hatch_alerts = [d for d in captured if d.rule_id == "hatch"]
    assert len(hatch_alerts) == 1, (
        f"expected one hatch alert after 2nd sighting, got {len(hatch_alerts)}"
    )
    assert hatch_alerts[0].severity == Severity.LOW
    assert "🐣" in hatch_alerts[0].title
    assert store.get_state().lifecycle_stage == "feeding"


# ── Feeding stage suppresses MEDIUM absence spam ──────────────────────

async def test_feeding_stage_suppresses_medium_absence_alerts(
    monkeypatch, store, evidence, notifier, lifecycle_on,
    empty_nest_jpeg_bytes,
):
    """Once in feeding stage, a recent feeding event should suppress
    MEDIUM long_absence alerts (she's expected to be away feeding)."""
    # Seed feeding stage with 2 confirming young_visible=true sightings at
    # ≥0.75 confidence (tightened 2026-04-17: attending_parent_feeding_young alone
    # no longer advances). attending_parent_feeding_young=true still records the
    # feeding event used by the 30-min suppression path.
    t0 = time.time() - 7200
    store.record(t0 - 300, False, None, _obs(
        attending_parent_on_nest="true",
        attending_parent_feeding_young=True,
        young_visible="true",
        young_count_estimate=2,
        confidence=0.85,
    ), None)
    store.record(t0, False, None, _obs(
        attending_parent_on_nest="true",
        attending_parent_feeding_young=True,
        young_visible="true",
        young_count_estimate=2,
        confidence=0.85,
    ), None)

    assert store.get_state().lifecycle_stage == "feeding"
    assert store.get_state().last_feeding_event_ts == pytest.approx(t0, abs=1.0)

    # 10 min later (past 5 min MEDIUM threshold but within 30 min feeding
    # suppression window)
    captured = []
    orig = notifier.send_alert

    async def _capture(decision, observation, **kwargs):
        captured.append(decision)
        return await orig(decision, observation, **kwargs)

    monkeypatch.setattr(notifier, "send_alert", _capture)

    # Mock analyzer to say mom is gone again (another foraging trip)
    analyze_mock = AsyncMock(return_value=_obs(
        attending_parent_on_nest="false",
        attending_parent_present="false",
        young_visible="uncertain",
        species_detected=[],
    ))
    monkeypatch.setattr(analyzer_mod, "analyze", analyze_mock)

    # Process a snap 10 min after the feeding event
    t1 = t0 + 600
    pipeline = _pipeline(store, notifier, evidence)
    await pipeline.on_image(
        empty_nest_jpeg_bytes,
        {"motion_triggered": False, "ts": t1},
    )

    # No MEDIUM long_absence alert should have fired
    medium_alerts = [
        d for d in captured if d.severity == Severity.MEDIUM and d.rule_id == "long_absence"
    ]
    assert len(medium_alerts) == 0, (
        f"expected no MEDIUM alerts during feeding suppression window, "
        f"got {len(medium_alerts)}: {[d.rule_id for d in medium_alerts]}"
    )


# ── Predation during feeding stage still fires CRITICAL ───────────────

async def test_predation_during_feeding_stage_still_critical(
    monkeypatch, store, evidence, notifier, lifecycle_on,
    thrasher_jpeg_bytes, obs_thrasher_direct_interaction,
):
    """A thrasher reaching into the nest during the feeding stage must
    still fire CRITICAL. Feeding-stage rules don't protect chicks from
    predators."""
    # Seed feeding stage — 2 confirming sightings required
    t0 = time.time() - 3600
    store.record(t0 - 300, False, None, _obs(
        attending_parent_on_nest="false",
        young_visible="true",
        young_count_estimate=2,
    ), None)
    store.record(t0, False, None, _obs(
        attending_parent_on_nest="false",
        young_visible="true",
        young_count_estimate=2,
    ), None)
    assert store.get_state().lifecycle_stage == "feeding"

    captured = []
    orig = notifier.send_alert

    async def _capture(decision, observation, **kwargs):
        captured.append(decision)
        return await orig(decision, observation, **kwargs)

    monkeypatch.setattr(notifier, "send_alert", _capture)

    # Thrasher with beak in the cup
    analyze_mock = AsyncMock(return_value=obs_thrasher_direct_interaction())
    monkeypatch.setattr(analyzer_mod, "analyze", analyze_mock)

    pipeline = _pipeline(store, notifier, evidence)
    await pipeline.on_image(
        thrasher_jpeg_bytes,
        {"motion_triggered": False, "ts": time.time()},
    )

    critical_alerts = [d for d in captured if d.severity == Severity.CRITICAL]
    assert len(critical_alerts) == 1, (
        f"expected one CRITICAL alert, got {len(captured)} total"
    )
    assert critical_alerts[0].rule_id == "direct_attack"


# ── egg_laying → incubation via Pipeline (24h sitting ratio ≥70%) ─────

async def test_egg_laying_to_incubation_cycle_via_pipeline(
    monkeypatch, store, evidence, notifier, lifecycle_on, cardinal_jpeg_bytes,
):
    """Starting from lifecycle_stage='egg_laying' with 25h of backfilled
    observations showing sustained sitting (80% on-nest ratio), the next
    Pipeline.on_image call should fire an `incubation_begin` LOW alert via
    the lifecycle webhook and transition state to 'incubation' with
    incubation_started_ts set.
    """
    # Simulated "now" — use a fixed, recent timestamp so the observation
    # backfill doesn't collide with real wall-clock data in tmp_path DBs.
    now_ts = time.time()
    egg_laying_start_ts = now_ts - 25 * 3600  # 25h ago

    # Seed the state row directly: stage=egg_laying, with egg_laying_started_ts
    # set to 25h ago so the (ts - egg_laying_started_ts) >= 24h gate in
    # events.py::_lifecycle_event is satisfied.
    store._conn.execute(
        "UPDATE state SET "
        " lifecycle_stage = 'egg_laying', "
        " egg_laying_started_ts = ?, "
        " last_mother_seen_ts = ? "
        "WHERE id = 1",
        (egg_laying_start_ts, now_ts - 60),
    )

    # Backfill 30 observations spanning the 25h window, 24 (80%) with
    # attending_parent_on_nest="true" at confidence 0.85 and 6 with "false".
    # events.py::_lifecycle_event does cheap string matching on the JSON
    # ('"attending_parent_on_nest":"true"' / '"confidence":') so we reuse the
    # analyzer's canonical serialization via NestObservation.model_dump_json().
    on_nest_obs = _obs(
        attending_parent_on_nest="true",
        attending_parent_present="true",
        confidence=0.85,
        summary="Female on nest.",
    ).model_dump_json()
    off_nest_obs = _obs(
        attending_parent_on_nest="false",
        attending_parent_present="false",
        confidence=0.85,
        summary="Brief absence.",
        species_detected=[],
    ).model_dump_json()

    # Evenly spread 30 snaps across 25h ending ~1 min before now_ts.
    window_seconds = 25 * 3600
    for i in range(30):
        obs_ts = egg_laying_start_ts + (i * window_seconds / 30.0)
        # First 24 snaps = on-nest, last 6 = off-nest (80% ratio).
        body = on_nest_obs if i < 24 else off_nest_obs
        store._conn.execute(
            "INSERT INTO observations (ts, motion_triggered, prefilter_json, observation_json, evidence_dir) "
            "VALUES (?, 0, NULL, ?, NULL)",
            (obs_ts, body),
        )

    # Sanity check the seeded state
    seeded = store.get_state()
    assert seeded.lifecycle_stage == "egg_laying"
    assert seeded.egg_laying_started_ts == pytest.approx(egg_laying_start_ts, abs=1.0)
    assert seeded.incubation_started_ts is None

    # Capture send_alert calls on the real notifier (which routes lifecycle
    # rule_ids to discord_lifecycle_webhook_url — redirected to the test
    # webhook by the autouse enable_test_mode fixture).
    captured = []
    orig = notifier.send_alert

    async def _capture(decision, observation, **kwargs):
        captured.append(decision)
        return await orig(decision, observation, **kwargs)

    monkeypatch.setattr(notifier, "send_alert", _capture)

    # Mock the analyzer: the "now" snap shows the cardinal still sitting.
    analyze_mock = AsyncMock(return_value=_obs(
        attending_parent_on_nest="true",
        attending_parent_present="true",
        confidence=0.9,
        summary="Cardinal sustained on nest.",
    ))
    monkeypatch.setattr(analyzer_mod, "analyze", analyze_mock)

    pipeline = _pipeline(store, notifier, evidence)
    await pipeline.on_image(
        cardinal_jpeg_bytes,
        {"motion_triggered": False, "ts": now_ts},
    )

    # Assert the state transitioned to incubation with a timestamp.
    final_state = store.get_state()
    assert final_state.lifecycle_stage == "incubation", (
        f"expected lifecycle_stage='incubation', got {final_state.lifecycle_stage!r}"
    )
    assert final_state.incubation_started_ts is not None, (
        "expected incubation_started_ts to be set after the transition"
    )
    assert final_state.incubation_started_ts == pytest.approx(now_ts, abs=1.0)

    # Assert an incubation_begin alert fired. Match EITHER by rule_id
    # (preferred) OR by title substring (permissive fallback if rule_id
    # wording drifts).
    incubation_alerts = [
        d for d in captured
        if d.rule_id == "incubation_begin" or "Incubation" in d.title
    ]
    assert len(incubation_alerts) >= 1, (
        f"expected ≥1 incubation_begin alert, got {len(captured)} total: "
        f"{[(d.rule_id, d.title) for d in captured]}"
    )
    assert incubation_alerts[0].severity == Severity.LOW

"""Analytics-report + feed-channel + state-persistence-under-discord-failure
integration tests.

These exercise the "second and third Discord channels" (feed + analytics)
that the pipeline publishes to independently of the main alert channel, plus
the critical property that Discord failures must NEVER lose state.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from birdnest_ai import analyzer as analyzer_mod
from birdnest_ai import main as main_mod
from birdnest_ai.analytics import compute_report
from birdnest_ai.config import get_settings
from birdnest_ai.schema import NestObservation, Severity


def _pipeline(store, notifier, evidence, feed_queue=None):
    counters = main_mod.DailyCounters()
    return main_mod.Pipeline(
        store=store,
        notifier=notifier,
        evidence=evidence,
        counters=counters,
        feed_queue=feed_queue,
    )


def _meta(ts: float | None = None) -> dict:
    return {
        "motion_triggered": False,
        "ts": ts if ts is not None else time.time(),
    }


async def test_analytics_report_posts_to_discord(
    store, analytics_notifier,
):
    """Seed synthetic observations + alerts in the store, compute the
    24-hour analytics report, and post it to the REAL analytics webhook
    with a ``[TEST]`` prefix (well — analytics has its own title format; we
    verify the notifier returned True and the report contains the seeded
    data).

    Note: the analytics notifier's ``send_analytics_report`` does not go
    through the ``_title_with_test_prefix`` helper — analytics titles have
    their own format. But ``_footer`` still appends the ``[TEST RUN]``
    suffix in test_mode so the user can tell it apart.
    """
    now = time.time()

    # Synthetic observations: a couple of "on nest" then a "thrasher" event.
    obs_on = {
        "attending_parent_present": "true",
        "attending_parent_on_nest": "true",
        "eggs_visible": "true",
        "egg_count_estimate": 3,
        "nest_visible": True,
        "nest_disturbed": "false",
        "species_detected": ["northern_cardinal"],
        "threat_species_detected": [],
        "near_nest_activity": False,
        "direct_nest_interaction": False,
        "confidence": 0.9,
        "summary": "Mom incubating.",
    }
    obs_off = dict(obs_on)
    obs_off.update(
        attending_parent_present="false",
        attending_parent_on_nest="false",
        species_detected=[],
        summary="Nest empty — foraging trip.",
    )
    obs_threat = dict(obs_off)
    obs_threat.update(
        species_detected=["brown_thrasher"],
        threat_species_detected=["brown_thrasher"],
        near_nest_activity=True,
        summary="Thrasher at nest.",
    )

    def _insert_obs(ts: float, payload: dict) -> None:
        store._conn.execute(
            "INSERT INTO observations (ts, motion_triggered, prefilter_json, observation_json, evidence_dir) "
            "VALUES (?, ?, ?, ?, ?)",
            (ts, 0, None, json.dumps(payload), None),
        )

    # Walk through the past 6 hours
    _insert_obs(now - 6 * 3600, obs_on)
    _insert_obs(now - 5 * 3600, obs_off)       # leaves
    _insert_obs(now - 5 * 3600 + 600, obs_on)  # returns 10 min later — 1 trip
    _insert_obs(now - 2 * 3600, obs_threat)    # thrasher event
    _insert_obs(now - 2 * 3600 + 60, obs_on)
    _insert_obs(now - 600, obs_on)

    # Synthetic alert for the thrasher event.
    store._conn.execute(
        "INSERT INTO alerts (ts, severity, rule_id, species, title, summary, evidence_dir) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            now - 2 * 3600, Severity.HIGH.value, "predator_absent",
            "brown_thrasher", "Predator near nest", "Thrasher at nest.", None,
        ),
    )

    report = compute_report(store, now, window_hours=24, analyzer_model="claude-opus-4-6")
    assert report["system"]["snaps_taken"] == 6
    # trip count includes both the explicit foraging trip (on→off→on) AND
    # any additional off→on transitions from the thrasher sequence.
    assert report["trips"]["trip_count"] >= 1
    assert report["threats"]["total_events"] == 1
    assert report["alerts"]["total"] == 1

    ok = await analytics_notifier.send_analytics_report(report)
    assert ok is True, "Analytics Discord post should have returned True"


async def test_feed_channel_single_tier_embed(
    feed_notifier,
):
    """Construct a feed event matching what Pipeline.on_image enqueues,
    and post it to the REAL feed webhook. Single-tier mode (no prefilter
    fields) so the embed should render as a plain "📷 Snap" message.

    Uses the attending_parent_on_nest reference image so the attached photo matches
    the "cardinal on nest" observation summary — avoids the confusing
    UX the user flagged on 2026-04-15 where tests posted a thrasher photo
    captioned "cardinal on nest".
    """
    from tests.integration.conftest import REFERENCE_CARDINAL

    ts = time.time()
    ok = await feed_notifier.send_snap_feed(
        ts=ts,
        motion_triggered=False,
        prefilter_text=None,
        prefilter_novel=None,
        observation_summary="Integration test: quiet scene, cardinal on nest.",
        severity=None,
        snap_path=REFERENCE_CARDINAL,
    )
    assert ok is True, "Feed Discord post should have returned True"


async def test_discord_failure_does_not_block_state_update(
    monkeypatch, store, evidence, notifier, thrasher_jpeg_bytes,
    obs_thrasher_near_nest,
):
    """Simulate a Discord outage: notifier.send_alert returns False. The
    pipeline must still call store.record() so the observation persists
    (and analytics / future alerts stay accurate).

    This is the exact failure mode that motivated isolation: a Discord 5xx
    must not cascade into "we lose the observation and the state machine
    forgets mom left".
    """
    # Disable verifier so we have a single clean send_alert call.
    settings = get_settings()
    monkeypatch.setattr(settings, "verify_alerts_with_opus", False)

    monkeypatch.setattr(
        analyzer_mod, "analyze",
        AsyncMock(return_value=obs_thrasher_near_nest()),
    )

    # Simulate Discord outage on the alert channel.
    send_alert_mock = AsyncMock(return_value=False)
    monkeypatch.setattr(notifier, "send_alert", send_alert_mock)

    # Verify store.record is still called — wrap it with a counter.
    original_record = store.record
    record_calls: list = []

    def counting_record(*args, **kwargs):
        record_calls.append((args, kwargs))
        return original_record(*args, **kwargs)

    monkeypatch.setattr(store, "record", counting_record)

    pipeline = _pipeline(store, notifier, evidence)
    await pipeline.on_image(thrasher_jpeg_bytes, _meta())

    # The send should have been attempted (and failed)…
    assert send_alert_mock.await_count == 1
    # …but the observation MUST still be recorded.
    assert len(record_calls) == 1, (
        "store.record must be called exactly once per snap, even on Discord failure"
    )
    # Verify the DB actually has the observation row.
    rows = store.get_observations_in_window(
        time.time() - 3600, time.time() + 60
    )
    assert len(rows) == 1
    obs_json = rows[0]["observation_json"]
    assert obs_json and "brown_thrasher" in obs_json

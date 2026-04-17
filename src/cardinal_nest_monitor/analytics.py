"""Behavior analytics — aggregates observations + alerts for periodic reports.

This module is **entirely read-only** against the state store and does not
call the Anthropic API. It's designed to run inside a dedicated
ThreadPoolExecutor (see main.py's analytics_scheduler) so the compute never
blocks the asyncio event loop that serves the alert hot path.

The only public entry point is `compute_report()`, which returns a dict
suitable for the Discord embed formatter in notifier.py.
"""

from __future__ import annotations

import json
import logging
import time
from collections import Counter
from typing import Any

from datetime import datetime

from cardinal_nest_monitor.config import get_settings
from cardinal_nest_monitor.state import StateStore

log = logging.getLogger(__name__)


# Only trust observations at or above this confidence for trip / presence
# bookkeeping. Matches the threshold used in state.py.
_MIN_CONFIDENCE = 0.55

# Approximate cost per analyzer call, used for window-cost estimation.
# Sonnet 4.6 with multi-image (full + center-crop + overview, default-on
# 2026-04-16) is ~$0.02/snap. Was $0.010 historically when MULTI_IMAGE_ANALYSIS
# was off — bumped 2026-04-17 after Codex flagged the analytics report
# materially understated real spend. Verifier calls (~$0.05/CRITICAL or HIGH
# alert) are estimated separately from the alerts table when computing the
# window cost.
_ANALYZER_COST_USD_PER_CALL = 0.020
_VERIFIER_COST_USD_PER_CALL = 0.050


def _parse_obs(row: Any) -> dict[str, Any] | None:
    """Parse the observation_json column into a dict; return None if missing."""
    raw = row["observation_json"]
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def _trip_detection(
    observations: list[Any],
    window_end_ts: float,
) -> dict[str, Any]:
    """Walk observations chronologically and find foraging trips.

    A trip is: cardinal_on_nest transitions `"true" → "false"` (she leaves)
    followed by `"false" → "true"` (she returns). Uncertain observations
    and low-confidence observations don't drive transitions.

    Returns a dict with `trip_count`, `trip_records` (list of
    {leave_ts, return_ts, duration_s}), `longest_s`, and "currently_away"
    info (if she's still off-nest at window_end).
    """
    trips: list[dict[str, Any]] = []
    leave_ts: float | None = None
    prior_state: str | None = None

    settings = get_settings()
    from cardinal_nest_monitor.events import summary_indicates_ir_mode

    for row in observations:
        obs = _parse_obs(row)
        if obs is None:
            continue
        if float(obs.get("confidence") or 0.0) < _MIN_CONFIDENCE:
            continue
        state = obs.get("cardinal_on_nest")
        if state not in ("true", "false"):
            continue  # uncertain / unknown → don't disturb
        # During quiet hours OR whenever the analyzer reported an IR/night
        # frame, IR can't reliably detect the cardinal — she sleeps on the
        # nest overnight, and at dusk grayscale plumage blends with the
        # cup material. Presume on-nest so trip detection doesn't invent
        # phantom leave/return transitions on IR-driven false negatives.
        # Mirrors events.py rule 4 + state.py confidence-floor logic so
        # the analytics report and the live alert path can never disagree.
        obs_time = datetime.fromtimestamp(float(row["ts"])).time()
        is_quiet = settings.in_quiet_hours(obs_time)
        is_ir = summary_indicates_ir_mode(obs.get("summary"))
        if (is_quiet or is_ir) and state != "true":
            state = "true"

        if prior_state == "true" and state == "false":
            leave_ts = float(row["ts"])
        elif prior_state == "false" and state == "true":
            if leave_ts is not None:
                trips.append({
                    "leave_ts": leave_ts,
                    "return_ts": float(row["ts"]),
                    "duration_s": int(float(row["ts"]) - leave_ts),
                })
                leave_ts = None

        prior_state = state

    currently_away = prior_state == "false"
    currently_away_duration_s = (
        int(window_end_ts - leave_ts) if currently_away and leave_ts else 0
    )

    durations = [t["duration_s"] for t in trips]
    longest = max(trips, key=lambda t: t["duration_s"]) if trips else None

    return {
        "trip_count": len(trips),
        "trip_records": trips,
        "durations_s": durations,
        "avg_duration_s": int(sum(durations) / len(durations)) if durations else 0,
        "longest": longest,
        "currently_away": currently_away,
        "currently_away_duration_s": currently_away_duration_s,
    }


def _presence_totals(
    observations: list[Any],
    window_start_ts: float,
    window_end_ts: float,
) -> dict[str, int]:
    """Compute total seconds with mom on/off/unknown in the window.

    Each observation's state covers the interval from its timestamp to the
    next observation's timestamp (or to window_end for the last observation).
    The head interval [window_start, first_obs.ts] is attributed to the
    first observation's state.
    """
    on_s = 0.0
    off_s = 0.0
    unknown_s = 0.0

    # Build (ts, state) list filtered to confident observations
    settings = get_settings()
    from cardinal_nest_monitor.events import summary_indicates_ir_mode

    points: list[tuple[float, str]] = []
    for row in observations:
        obs = _parse_obs(row)
        if obs is None:
            continue
        if float(obs.get("confidence") or 0.0) < _MIN_CONFIDENCE:
            continue
        state = obs.get("cardinal_on_nest")
        if state not in ("true", "false"):
            state = "unknown"
        # During quiet hours OR whenever the analyzer reported an IR/night
        # frame, presume on-nest. Mirrors _trip_detection above + the live
        # path's IR-mode suppression (events.py rule 4) so presence totals
        # match what the alert path believes was happening.
        obs_time = datetime.fromtimestamp(float(row["ts"])).time()
        is_quiet = settings.in_quiet_hours(obs_time)
        is_ir = summary_indicates_ir_mode(obs.get("summary"))
        if (is_quiet or is_ir) and state != "true":
            state = "true"
        points.append((float(row["ts"]), state))

    if not points:
        total = max(0, int(window_end_ts - window_start_ts))
        return {"on_nest_s": 0, "off_nest_s": 0, "unknown_s": total}

    # Head: window_start to first point — attribute to first point's state
    head = max(0.0, points[0][0] - window_start_ts)
    if points[0][1] == "true":
        on_s += head
    elif points[0][1] == "false":
        off_s += head
    else:
        unknown_s += head

    # Middle: consecutive pairs
    for i in range(len(points) - 1):
        dt = max(0.0, points[i + 1][0] - points[i][0])
        if points[i][1] == "true":
            on_s += dt
        elif points[i][1] == "false":
            off_s += dt
        else:
            unknown_s += dt

    # Tail: last point to window_end — use last point's state
    tail = max(0.0, window_end_ts - points[-1][0])
    if points[-1][1] == "true":
        on_s += tail
    elif points[-1][1] == "false":
        off_s += tail
    else:
        unknown_s += tail

    return {
        "on_nest_s": int(on_s),
        "off_nest_s": int(off_s),
        "unknown_s": int(unknown_s),
    }


def _threat_summary(observations: list[Any]) -> dict[str, Any]:
    """Count threat species sightings across observations in the window."""
    species_counter: Counter[str] = Counter()
    near_nest_count = 0
    sightings: list[dict[str, Any]] = []
    for row in observations:
        obs = _parse_obs(row)
        if obs is None:
            continue
        threats = obs.get("threat_species_detected") or []
        if not threats:
            continue
        for sp in threats:
            # Can be enum instance or string
            val = sp.value if hasattr(sp, "value") else str(sp)
            species_counter[val] += 1
        if obs.get("near_nest_activity"):
            near_nest_count += 1
        sightings.append({
            "ts": float(row["ts"]),
            "species": [sp.value if hasattr(sp, "value") else str(sp) for sp in threats],
            "near_nest": bool(obs.get("near_nest_activity")),
            "summary": obs.get("summary", "")[:200],
        })
    return {
        "total_events": len(sightings),
        "by_species": dict(species_counter),
        "near_nest_events": near_nest_count,
        "sightings": sightings,
    }


def _alert_summary(alerts: list[Any]) -> dict[str, Any]:
    """Aggregate alerts by severity and rule_id."""
    by_severity: Counter[str] = Counter()
    by_rule: Counter[str] = Counter()
    for row in alerts:
        by_severity[row["severity"]] += 1
        by_rule[row["rule_id"]] += 1
    return {
        "total": len(alerts),
        "by_severity": dict(by_severity),
        "by_rule": dict(by_rule),
    }


def _system_health(
    observations: list[Any],
    alerts: list[Any],
    analyzer_model: str,
) -> dict[str, Any]:
    """System-health metrics: snap counts, failures, estimated cost.

    Cost includes both analyzer calls (per snap) AND verifier calls
    (per CRITICAL/HIGH alert that ran the blind Opus second-opinion
    pass). Verifier calls are estimated from the alerts table by counting
    rows at severity CRITICAL or HIGH within the window. This slightly
    over-counts when verify_alerts_with_opus=False but matches reality
    when the default-on path is in use.
    """
    snaps = len(observations)
    failures = sum(
        1 for row in observations if not (row["observation_json"] or "").strip()
    )
    successful = snaps - failures
    verifier_calls = sum(
        1 for row in alerts if row["severity"] in ("CRITICAL", "HIGH")
    )
    cost_usd = (
        successful * _ANALYZER_COST_USD_PER_CALL
        + verifier_calls * _VERIFIER_COST_USD_PER_CALL
    )
    return {
        "snaps_taken": snaps,
        "analyzer_failures": failures,
        "verifier_calls": verifier_calls,
        "cost_window_usd": round(cost_usd, 2),
        "analyzer_model": analyzer_model,
    }


def compute_report(
    store: StateStore,
    window_end_ts: float,
    window_hours: int,
    analyzer_model: str = "claude-sonnet-4-6",
) -> dict[str, Any]:
    """Compute a behavior analytics report for the window ending at window_end_ts.

    Runs synchronously — wrap in asyncio.to_thread or run_in_executor when
    calling from async code so the event loop isn't blocked.
    """
    window_start_ts = window_end_ts - window_hours * 3600.0

    # Read-only queries — safe from a worker thread.
    observations = store.get_observations_in_window(window_start_ts, window_end_ts)
    alerts = store.get_alerts_in_window(window_start_ts, window_end_ts)

    presence = _presence_totals(observations, window_start_ts, window_end_ts)
    trips = _trip_detection(observations, window_end_ts)
    threats = _threat_summary(observations)
    alert_stats = _alert_summary(alerts)
    system = _system_health(observations, alerts, analyzer_model)

    report: dict[str, Any] = {
        "window_start_ts": window_start_ts,
        "window_end_ts": window_end_ts,
        "window_hours": window_hours,
        "presence": presence,
        "trips": trips,
        "threats": threats,
        "alerts": alert_stats,
        "system": system,
        "generated_at": time.time(),
    }
    log.info(
        "analytics: window=%dh snaps=%d trips=%d threats=%d alerts=%d cost=$%.2f",
        window_hours, system["snaps_taken"], trips["trip_count"],
        threats["total_events"], alert_stats["total"], system["cost_window_usd"],
    )
    return report

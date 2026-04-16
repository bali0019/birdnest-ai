"""Pure rules engine. Stateless. Takes (observation, state, store, ts) and
returns an AlertDecision or None.

Universal gates first (low confidence, smart-filter for yard motion), then
five rules in priority order. Cooldown checks use the alerts table via
StateStore; severity escalation always breaks through (a CRITICAL after a
HIGH within the same cooldown window still fires).

NOTE: Rule 5 (mother_returned) is implemented best-effort. The caller
pattern record() → evaluate() means state.in_absence may have already been
cleared by the time we look at it; the rule fires correctly only when
evaluate() is called with the *pre-record* state. The wiring in main.py
should call store.get_state() before record() if you want this rule to fire
reliably.
"""

from __future__ import annotations

import logging
from datetime import datetime

from cardinal_nest_monitor.config import get_settings
from cardinal_nest_monitor.schema import (
    AlertDecision,
    NestObservation,
    NestState,
    Severity,
)
from cardinal_nest_monitor.state import StateStore

log = logging.getLogger(__name__)


_MIN_CONFIDENCE = 0.55

# Cooldown windows (seconds)
_CD_DIRECT_ATTACK = 60
_CD_EGG_LOSS = 300
_CD_PREDATOR_AT_NEST = 300    # per-species; suppresses spam while a predator lingers
_CD_LONG_ABSENCE = 300        # was 900; repeats MEDIUM every 5 min while mom is away
_MOTHER_RETURN_COOLDOWN = 300

# Absence threshold (seconds) for the MEDIUM "long absence" rule.
# User chose 5 min (was 15) because mom's typical foraging trips are 5–15 min
# and life-or-death urgency favors aggressive alerting. Combined with the
# matching cooldown above, MEDIUM repeats every 5 min while absence persists.
_LONG_ABSENCE_THRESHOLD = 300       # 5 min

# NOTE: The HIGH rule previously required absence ≥ 120s. Removed per user
# decision: any threat species + near_nest_activity fires HIGH immediately,
# mom present or not. A thrasher on the bush is worth a ping even if she's
# actively defending — the user wants to know so they can decide to intervene.


def _species_list(obs: NestObservation) -> list[str]:
    """Materialize ThreatSpecies enum members to their string values."""
    out = []
    for t in obs.threat_species_detected:
        if hasattr(t, "value"):
            out.append(t.value)
        else:
            out.append(str(t))
    return out


def _smart_filter_drop(obs: NestObservation) -> bool:
    """Yard-motion suppression: no nest, no near-nest activity, no threats."""
    return (
        (not obs.nest_visible)
        and (not obs.near_nest_activity)
        and (not obs.threat_species_detected)
    )


def _cooldown_blocks(
    store: StateStore,
    severity: Severity,
    species: str | None,
    window_s: int,
    ts: float | None = None,
) -> bool:
    """True if a prior alert for `species` within `window_s` has severity
    >= the current severity. Lower prior severity allows the new higher-
    severity alert to break through.

    When `ts` is provided, cooldown is computed relative to that timestamp
    rather than wall clock. Critical for backfill processing where snaps
    may be minutes/hours old.
    """
    latest = store.latest_alert_for_species(species, window_s, ts=ts)
    if latest is None:
        return False
    prior_sev, _ = latest
    return prior_sev.rank >= severity.rank


def evaluate(
    observation: NestObservation,
    state: NestState,
    store: StateStore,
    ts: float,
) -> AlertDecision | None:
    # ── Universal gates ────────────────────────────────────────────────
    if observation.confidence < _MIN_CONFIDENCE:
        log.debug("low confidence %.2f → no alert", observation.confidence)
        return None

    if _smart_filter_drop(observation):
        log.debug("smart filter dropped yard motion")
        return None

    threats = _species_list(observation)
    primary_species = threats[0] if threats else None

    # ── Rule 1: Direct attack (CRITICAL, 60s per species) ─────────────
    if observation.direct_nest_interaction:
        sev = Severity.CRITICAL
        if not _cooldown_blocks(store, sev, primary_species, _CD_DIRECT_ATTACK, ts):
            return AlertDecision(
                severity=sev,
                title="Direct nest interaction",
                summary=observation.summary,
                species=threats,
                mother_present=observation.mother_cardinal_present,
                absence_seconds=state.absence_seconds(ts),
                confidence=observation.confidence,
                rule_id="direct_attack",
            )
        return None

    # ── Rule 2: Egg loss (CRITICAL, 5 min) ────────────────────────────
    if (
        observation.eggs_visible == "true"
        and observation.egg_count_estimate is not None
        and state.last_known_egg_count is not None
        and observation.egg_count_estimate < state.last_known_egg_count
    ):
        sev = Severity.CRITICAL
        if not _cooldown_blocks(store, sev, None, _CD_EGG_LOSS, ts):
            return AlertDecision(
                severity=sev,
                title="Egg count dropped",
                summary=observation.summary,
                species=threats,
                mother_present=observation.mother_cardinal_present,
                egg_count_before=state.last_known_egg_count,
                egg_count_after=observation.egg_count_estimate,
                confidence=observation.confidence,
                rule_id="egg_loss",
            )
        return None

    # ── Rule 3: Predator near nest (HIGH, 5 min per species) ──────────
    # Fires whenever a threat species is at/on the bush, regardless of
    # whether mom is present. We no longer wait for an "absence" signal —
    # see constants section for rationale. Cooldown (5 min per species)
    # prevents 1-alert-per-snap spam while a predator lingers.
    absence = state.absence_seconds(ts)

    # Cap absence at time since quiet hours ended. During quiet hours, IR
    # images can't reliably detect the cardinal (she blends with the nest
    # in grayscale), so last_mother_seen_ts doesn't update — causing the
    # absence counter to accumulate the entire overnight period. When
    # morning comes, the first MEDIUM would show "515+ minutes" when she's
    # only been absent since dawn. Fix: if last_mother_seen_ts is before
    # the most recent quiet-hours-end, treat the absence as starting at
    # that boundary (she was almost certainly on the nest overnight).
    settings_obj = get_settings()
    if (
        absence is not None
        and settings_obj.quiet_hours.strip()
        and state.last_mother_seen_ts is not None
        and not settings_obj.in_quiet_hours(datetime.fromtimestamp(ts).time())
    ):
        import re as _re
        _qm = _re.match(
            r"(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})",
            settings_obj.quiet_hours.strip(),
        )
        if _qm:
            from datetime import time as _time
            _qend = _time(int(_qm.group(3)), int(_qm.group(4)))
            _snap_date = datetime.fromtimestamp(ts).date()
            _qend_dt = datetime.combine(_snap_date, _qend)
            _qend_ts = _qend_dt.timestamp()
            if _qend_ts > ts:
                _qend_ts -= 86400
            if state.last_mother_seen_ts < _qend_ts:
                absence = ts - _qend_ts

    if threats and observation.near_nest_activity:
        sev = Severity.HIGH
        if not _cooldown_blocks(store, sev, primary_species, _CD_PREDATOR_AT_NEST, ts):
            return AlertDecision(
                severity=sev,
                title="Predator near nest",
                summary=observation.summary,
                species=threats,
                mother_present=observation.mother_cardinal_present,
                absence_seconds=absence,
                confidence=observation.confidence,
                rule_id="predator_absent",  # keep rule_id for cooldown/analytics continuity
            )
        return None

    # ── Rule 4: Long absence (MEDIUM, threshold from _LONG_ABSENCE_THRESHOLD) ─
    # Suppressed during quiet hours: IR night images produce false "mom absent"
    # readings because the cardinal's plumage blends with nest material in
    # grayscale. She's almost certainly sleeping on the eggs. HIGH/CRITICAL
    # (predator rules) remain active overnight for nocturnal threats.
    if (
        absence is not None
        and absence >= _LONG_ABSENCE_THRESHOLD
        and not threats
        and observation.cardinal_on_nest != "true"
        and not get_settings().in_quiet_hours(datetime.fromtimestamp(ts).time())
    ):
        sev = Severity.MEDIUM
        if not _cooldown_blocks(store, sev, None, _CD_LONG_ABSENCE, ts):
            # Dynamic title: bucket the actual elapsed absence into multiples
            # of the threshold (currently 5 min). A 5m 9s absence reads
            # "5+ minutes", a 10m 32s absence reads "10+ minutes", etc. This
            # prevents the title from silently desyncing if the threshold
            # constant is ever retuned (history: pre-2026-04-15 the title
            # was hardcoded "15+ minutes" but the threshold had been
            # dropped to 5 min, producing "5 min absence → 15+ min title"
            # inconsistencies in Discord).
            bucket_mins = (
                int(absence) // _LONG_ABSENCE_THRESHOLD
            ) * (_LONG_ABSENCE_THRESHOLD // 60)
            return AlertDecision(
                severity=sev,
                title=f"Mother away from nest for {bucket_mins}+ minutes",
                summary=observation.summary,
                species=[],
                mother_present=observation.mother_cardinal_present,
                absence_seconds=absence,
                confidence=observation.confidence,
                rule_id="long_absence",
            )
        return None

    # ── Rule 5: Mother returned (LOW, once per absence ≥ 5 min) ──────
    if (
        observation.cardinal_on_nest == "true"
        and state.in_absence
        and state.last_mother_seen_ts is not None
    ):
        if not store.cooldown_active(Severity.LOW, None, _MOTHER_RETURN_COOLDOWN, ts=ts):
            sev = Severity.LOW
            return AlertDecision(
                severity=sev,
                title="Mother returned to nest",
                summary=observation.summary,
                species=[],
                mother_present=observation.mother_cardinal_present,
                absence_seconds=state.absence_seconds(ts),
                confidence=observation.confidence,
                rule_id="mother_returned",
            )
        return None

    return None

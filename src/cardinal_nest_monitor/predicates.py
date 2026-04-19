"""Pure observation-level predicates and constants.

Shared home for helpers that operate only on a NestObservation (no state,
no store, no DB). Imported by both events.py and state.py to break the
sibling function-local import cycle they had after the 2026-04-17 hotfix.

Keep this module dependency-light: only schema + stdlib. Anything that
needs StateStore or SQL belongs in state.py; anything that produces an
AlertDecision belongs in events.py.
"""

from __future__ import annotations

from cardinal_nest_monitor.schema import NestObservation, ThreatSpecies


# ── Constants ───────────────────────────────────────────────────────

# Chick-sighting confidence floor (2026-04-17). A false chick sighting
# at day 3-4 of incubation (a confident 0.82 reddish-blob call) led to
# a stale first_chick_sighting_ts that would have bypassed the 2-sighting
# guard on a real later hatch. A higher floor makes the 2-sighting guard
# more meaningful.
CHICK_SIGHTING_CONFIDENCE_FLOOR = 0.75


# Named threat species (derived from the enum so a new ThreatSpecies
# member automatically shows up here). These bypass the ambiguous-
# occupied-cup path and fire threat rules on a single frame.
NAMED_THREATS = frozenset(
    s.value for s in ThreatSpecies if s is not ThreatSpecies.UNKNOWN
)


# Phrases Sonnet uses when the snap is in Blink's IR/night mode. The
# Blink Outdoor camera switches to IR at sunset (~20:00 in April-
# Atlanta), but the wall-clock quiet_hours window doesn't start until
# 23:00 — leaving a ~3h gap where IR is on, the cardinal is hard to
# ID in grayscale, and the old rules would fire false MEDIUMs as the
# absence counter accumulated. See
# evidence/2026-04-16/20-48-07_MEDIUM_unknown_bird/ for the canonical
# case. Extend this list if Sonnet starts using new IR phrasing; do
# not replace existing entries.
_IR_MODE_PHRASES = (
    "ir mode",
    "ir image",
    "ir frame",
    "infrared",
    "grayscale",
    "night vision",
    "night ir",
    "in ir",  # "settled in IR" / "in IR mode" loose match
)


# ── Helpers ─────────────────────────────────────────────────────────

def species_list(obs: NestObservation) -> list[str]:
    """Materialize ThreatSpecies enum members on `obs.threat_species_detected`
    to their string values. Shared between events.py threat handling and
    the ambiguous-occupied-cup predicate so both use the same normalization.
    """
    out = []
    for t in obs.threat_species_detected:
        if hasattr(t, "value"):
            out.append(t.value)
        else:
            out.append(str(t))
    return out


def summary_indicates_ir_mode(summary: str | None) -> bool:
    """True when the analyzer's free-form summary indicates an IR/night
    frame. String-only helper so analytics.py can call it on the raw
    observation_json["summary"] without re-parsing into a pydantic model.
    Both analytics and live evaluation must use this same matcher so the
    report and the alerts can never disagree about whether a given frame
    was IR.
    """
    text = (summary or "").lower()
    return any(phrase in text for phrase in _IR_MODE_PHRASES)


def observation_indicates_ir_mode(obs: NestObservation) -> bool:
    """True when the analyzer's own description indicates an IR/night image."""
    return summary_indicates_ir_mode(obs.summary)


def is_confirmed_chick_sighting(obs: NestObservation) -> bool:
    """True when this observation counts as a confirmed chick signal for
    lifecycle advancement. Tightened 2026-04-17: requires explicit
    chicks_visible="true" AT OR ABOVE the confidence floor. mother_feeding_
    chicks=true alone does NOT advance lifecycle; it still records
    last_feeding_event_ts separately in state.py for the 30-min MEDIUM
    suppression during feeding stage.

    Used by both state.py::record and events.py::_lifecycle_event — the
    comment in state.py used to say "must stay in sync with events.py,"
    which was a request for a shared predicate. This is it.
    """
    return (
        obs.chicks_visible == "true"
        and obs.confidence >= CHICK_SIGHTING_CONFIDENCE_FLOOR
    )


def is_ambiguous_occupied_cup(obs: NestObservation) -> bool:
    """True when a bird is visibly at the nest cup but the analyzer cannot
    confirm species (no thrasher field marks visible AND no cardinal crest
    visible). The dominant 2026-04-17 false-alarm pattern. A single such
    frame would otherwise fire BOTH MEDIUM long_absence AND HIGH
    predator_near_nest; state.py holds it as a pending candidate and
    events.py returns None, deferring judgement to the next snap.

    Criteria (all must hold):
      - nest_visible=true
      - near_nest_activity=true
      - cardinal_on_nest="uncertain"
      - direct_nest_interaction=false (explicit direct attacks are NEVER
        ambiguous — beak-in-cup must reach CRITICAL even if species is
        "unknown"; Codex P1 guardrail)
      - no NAMED threat species (unknown-only or empty qualifies; any
        named thrasher/jay/squirrel/chipmunk bypasses this path)
    """
    if not obs.nest_visible:
        return False
    if not obs.near_nest_activity:
        return False
    if obs.cardinal_on_nest != "uncertain":
        return False
    if obs.direct_nest_interaction:
        return False
    for s in species_list(obs):
        if s in NAMED_THREATS:
            return False
    return True

"""Vision-model output contracts (Pydantic) and Anthropic tool-schema
factories.

Single source of truth for what the prefilter and analyzer return. All
other modules import from here.

Generic-nest-monitor refactor notes (Phase 3, 2026-04-23):
  * ``ThreatSpecies`` enum is gone. ``threat_species_detected`` is now
    a plain ``list[str]``, validated at model-creation time against the
    loaded species profile's threat list + the reserved ``"unknown"``
    sentinel. Unknown values get bucketed as ``"unknown"`` exactly like
    the prior enum's UNKNOWN fallback.
  * ``NEST_TOOL`` / ``PREFILTER_TOOL`` static dicts are gone. Tool
    schemas are now rendered per-call from the species profile via
    ``build_nest_tool(profile)`` / ``build_prefilter_tool(profile)``.
    The model-facing ``threat_species_detected`` enum in the tool
    contract is populated from ``profile.threats.names + ["unknown"]``,
    so a different profile drives a different model contract.

schema.py still avoids heavy SDK imports. The species package import
happens inside validator/factory functions (deferred) so this module
stays cheap to import from tests.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


# ── Tristate type ───────────────────────────────────────────────────────────
# We intentionally use string literals "true"/"false"/"uncertain" instead of
# bool|None so the model can express its uncertainty without us collapsing
# "I don't know" into "no" — the rules engine treats "uncertain" as a hard
# gate against firing alerts.
Tristate = Literal["true", "false", "uncertain"]


# ── Reserved threat sentinel ───────────────────────────────────────────────
# Runtime-reserved name that the analyzer uses for "something at the nest I
# can't positively identify." Valid on every profile. Profiles MUST NOT list
# this name in [threats] — the profile-schema validator rejects it there.
UNKNOWN_THREAT = "unknown"


# ── Severity ────────────────────────────────────────────────────────────────
class Severity(str, Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"

    @property
    def rank(self) -> int:
        """Higher number = more severe. Used for escalation breakthrough."""
        return {"LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}[self.value]

    @property
    def emoji(self) -> str:
        return {"CRITICAL": "🚨", "HIGH": "⚠️", "MEDIUM": "🟡", "LOW": "✅"}[self.value]

    @property
    def color(self) -> int:
        # Discord embed colors (24-bit int)
        return {
            "CRITICAL": 0xFF0000,  # red
            "HIGH": 0xFF8C00,      # dark orange
            "MEDIUM": 0xFFD700,    # gold
            "LOW": 0x32CD32,       # lime green
        }[self.value]


# ── Tier-1 prefilter result (Haiku) ─────────────────────────────────────────
class PrefilterResult(BaseModel):
    """Cheap quick-scan: is anything novel happening at the nest?

    "Novel" means anything that's NOT one of:
      (a) empty nest with no animals visible
      (b) the attending parent of the target species sitting on the nest
      (c) leaves moving in wind / static scene
    Anything else → escalate to the full analyzer.
    """

    novel_activity: Tristate = Field(
        ..., description="true/false/uncertain — should this be analyzed in full?"
    )
    reason: str = Field(..., description="One short sentence on what was seen.")

    @property
    def should_escalate(self) -> bool:
        """Conservative bias: uncertain → escalate."""
        return self.novel_activity in ("true", "uncertain")


def build_prefilter_tool(profile) -> dict[str, Any]:
    """Render the Anthropic tool schema for :class:`PrefilterResult`
    using the given species profile.

    The profile drives the target-species reference in the
    ``novel_activity`` field description — a robin profile produces a
    robin-specific prompt; a cardinal profile produces the current
    cardinal prompt.

    Parameter type is intentionally not annotated as ``SpeciesProfile``
    to avoid importing species at module load (keeps schema.py a cheap
    import for tests). Callers pass the profile object.
    """
    attending = profile.target.attending_parent_label
    return {
        "name": "report_prefilter",
        "description": (
            f"Report whether this image of a {profile.species.common_name} "
            "nest contains any novel activity that warrants a full deep "
            "analysis. Be conservative — if unclear, return 'uncertain' so "
            "the deep analyzer takes a second look."
        ),
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["novel_activity", "reason"],
            "properties": {
                "novel_activity": {
                    "type": "string",
                    "enum": ["true", "false", "uncertain"],
                    "description": (
                        f"true if anything other than (a) empty nest, (b) the "
                        f"{attending} sitting on nest, (c) static scene with "
                        f"just leaves/wind. uncertain if unclear."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": "One short sentence describing what is visible.",
                },
            },
        },
    }


# ── Tier-2 full nest observation (Opus) ─────────────────────────────────────
class NestObservation(BaseModel):
    """Full structured observation produced by the Opus analyzer.

    This is the contract the events-engine evaluates against state to decide
    whether/what to alert. Field names match the PRD verbatim.
    """

    attending_parent_present: Tristate
    attending_parent_on_nest: Tristate
    eggs_visible: Tristate
    egg_count_estimate: int | None = Field(
        None, ge=0, le=20, description="Estimated eggs visible. null if not visible/unsure."
    )
    nest_visible: bool
    nest_disturbed: Tristate

    species_detected: list[str] = Field(default_factory=list)
    threat_species_detected: list[str] = Field(default_factory=list)

    near_nest_activity: bool
    direct_nest_interaction: bool

    # Lifecycle fields (added 2026-04-16, feature-flag gated).
    # When lifecycle_tracking_enabled=False (default), these remain at their
    # safe defaults and don't affect any existing code path.
    young_visible: Tristate = "uncertain"
    young_count_estimate: int | None = Field(
        None, ge=0, le=8,
        description="Chicks visible above cup rim. null when young_visible != 'true'.",
    )
    attending_parent_feeding_young: bool = False

    confidence: float = Field(..., ge=0.0, le=1.0)
    summary: str = Field(..., description="Short human-readable explanation.")

    @field_validator("threat_species_detected", mode="before")
    @classmethod
    def _coerce_threats(cls, v: Any) -> list[str]:
        """Normalize each item and coerce unrecognized species to the
        reserved ``"unknown"`` sentinel.

        Valid values come from the active species profile's threats
        list, plus ``"unknown"``. The tool schema already constrains
        the model's output via the enum in ``build_nest_tool``, but
        this validator is a belt-and-suspenders check for cases where:
          - the model outputs a variant name (e.g. 'brown thrasher'
            with a space; we normalize to snake_case);
          - a test constructs a NestObservation directly with a typo;
          - a stored observation JSON from an older profile is replayed.

        Unknown species get bucketed as ``"unknown"`` rather than
        rejected so downstream rules still fire on "there's a threat,
        we just can't name it."
        """
        if not isinstance(v, list):
            return []
        # Deferred import — species package imports config which may
        # pull in heavy modules. Keeping this inside the validator
        # preserves schema.py's fast-import property for tests.
        from cardinal_nest_monitor.species import get_species_profile

        profile = get_species_profile()
        valid = set(profile.threats.names) | {UNKNOWN_THREAT}
        out: list[str] = []
        for item in v:
            s = str(item).strip().lower().replace(" ", "_")
            if s in valid:
                out.append(s)
            else:
                out.append(UNKNOWN_THREAT)
        return out


def build_nest_tool(profile) -> dict[str, Any]:
    """Render the Anthropic tool schema for :class:`NestObservation`
    from the active species profile.

    The profile drives:
      - the human-readable nest description in the top-level tool
        description (target species name);
      - the label used throughout field descriptions for the attending
        parent ("female cardinal" vs "robin parent" etc.) and for the
        young ("chicks" vs "nestlings");
      - the threat_species_detected enum values — populated from
        ``profile.threats.names + ["unknown"]``. A different profile
        produces a different allowed threat list.

    Factory (not static) because a single codebase ships multiple
    profiles and the runtime choice happens at startup. Cheap to call —
    just a dict literal with interpolation. Callers that care about
    minor overhead can memoize on the profile object.
    """
    common_name = profile.species.common_name
    attending = profile.target.attending_parent_label
    young = profile.target.young_label
    # Threat enum exposed to the model: profile-declared threats + the
    # always-available "unknown" sentinel. The Pydantic validator on
    # NestObservation coerces any other value to "unknown".
    threat_enum = list(profile.threats.names) + [UNKNOWN_THREAT]

    return {
        "name": "report_nest",
        "description": (
            f"Report a structured observation of a {common_name} nest "
            f"based on the provided image. Focus on: {attending} presence "
            f"on/near the nest, eggs visible and counted, threat species "
            f"near or interacting with the nest, and whether the nest "
            f"itself appears disturbed. Be conservative with uncertainty — "
            f"return 'uncertain' if unclear; never guess."
        ),
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "attending_parent_present",
                "attending_parent_on_nest",
                "eggs_visible",
                "egg_count_estimate",
                "nest_visible",
                "nest_disturbed",
                "species_detected",
                "threat_species_detected",
                "near_nest_activity",
                "direct_nest_interaction",
                "young_visible",
                "young_count_estimate",
                "attending_parent_feeding_young",
                "confidence",
                "summary",
            ],
            "properties": {
                "attending_parent_present": {
                    "type": "string",
                    "enum": ["true", "false", "uncertain"],
                    "description": f"Is the {attending} visible anywhere in the frame?",
                },
                "attending_parent_on_nest": {
                    "type": "string",
                    "enum": ["true", "false", "uncertain"],
                    "description": f"Is the {attending} sitting on/in the nest cup?",
                },
                "eggs_visible": {
                    "type": "string",
                    "enum": ["true", "false", "uncertain"],
                    "description": (
                        "Are any eggs visible in the nest cup? "
                        f"(false if the {attending} is covering them.)"
                    ),
                },
                "egg_count_estimate": {
                    "type": ["integer", "null"],
                    "minimum": 0,
                    "maximum": 20,
                    "description": (
                        "Number of eggs visible. null if eggs_visible is false or "
                        "uncertain. Be exact when you can see them clearly."
                    ),
                },
                "nest_visible": {
                    "type": "boolean",
                    "description": "Is the nest cup itself visible in the frame?",
                },
                "nest_disturbed": {
                    "type": "string",
                    "enum": ["true", "false", "uncertain"],
                    "description": (
                        "Does the nest appear disturbed (broken, displaced, branches "
                        "torn) compared to a normal intact cup?"
                    ),
                },
                "species_detected": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "All bird/animal species you observe. Free-text common names. "
                        "Empty list if none."
                    ),
                },
                "threat_species_detected": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": threat_enum,
                    },
                    "description": (
                        "Subset of species_detected that are nest threats. Use "
                        "'unknown' for any unidentified animal that isn't clearly "
                        f"the {attending} or an ambient non-threat species."
                    ),
                },
                "near_nest_activity": {
                    "type": "boolean",
                    "description": (
                        f"Is any non-target animal physically at, on, or within "
                        f"~30cm of the nest? (False if they're elsewhere in yard.)"
                    ),
                },
                "direct_nest_interaction": {
                    "type": "boolean",
                    "description": (
                        "Is a non-target animal touching, reaching into, or pulling "
                        "from the nest cup? This is the highest-severity signal."
                    ),
                },
                "young_visible": {
                    "type": "string",
                    "enum": ["true", "false", "uncertain"],
                    "description": (
                        f"Are {young} visible in the nest? True if you see "
                        "pink or feathered nestlings (heads/beaks protruding above "
                        "the cup rim, open mouths, or lying in the cup). Uncertain "
                        "on IR/obscured images. Default 'uncertain' if unsure."
                    ),
                },
                "young_count_estimate": {
                    "type": ["integer", "null"],
                    "minimum": 0,
                    "maximum": 8,
                    "description": (
                        f"Estimated number of {young} visible (best-effort count). "
                        "null if young_visible is 'false' or 'uncertain'."
                    ),
                },
                "attending_parent_feeding_young": {
                    "type": "boolean",
                    "description": (
                        f"True when the {attending} is at the nest with a food "
                        f"item visible in the {attending}'s beak (insect, "
                        "caterpillar, berry, or a bulge suggesting food). False if "
                        f"the {attending} is present without visible food, or if no "
                        "target bird is present."
                    ),
                },
                "confidence": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "description": (
                        "Your overall confidence (0–1) in this entire observation. "
                        "Image quality, occlusion, motion blur all reduce confidence."
                    ),
                },
                "summary": {
                    "type": "string",
                    "description": "One short sentence describing what is happening.",
                },
            },
        },
    }


# ── Alert decision ──────────────────────────────────────────────────────────
class AlertDecision(BaseModel):
    """Output of the events engine. May be None (no alert)."""

    severity: Severity
    title: str
    summary: str
    species: list[str] = Field(default_factory=list)
    mother_present: Tristate | None = None
    absence_seconds: int | None = None
    egg_count_before: int | None = None
    egg_count_after: int | None = None
    confidence: float
    rule_id: str  # e.g. "direct_attack", "egg_loss", "predator_absent", ...


# ── Nest state snapshot ─────────────────────────────────────────────────────
class NestState(BaseModel):
    """In-memory view of the SQLite single-row state, returned by state.record()."""

    last_mother_seen_ts: float | None = None  # unix epoch seconds
    last_known_egg_count: int | None = None
    last_threat_seen_ts: float | None = None
    last_threat_species: str | None = None
    last_alert_severity: Severity | None = None
    last_absence_alert_ts: float | None = None
    in_absence: bool = False  # True if mother currently considered absent
    # Wall-clock ts when `in_absence` flipped False → True. Consumed by the
    # downloader's burst-cadence path: first N seconds after absence onset
    # are peak predation risk and use burst_snap_interval_seconds. None when
    # not in absence.
    absence_started_ts: float | None = None

    # Lifecycle tracking (2026-04-16).
    # Full stage progression (earliest → latest):
    #   building_nest → egg_laying → incubation → feeding → fledging → empty
    # Stages are one-way; once past a stage we don't go back. For the current
    # monitored brood the system was deployed AFTER building finished and
    # during/at the tail of egg_laying, so the backfill tool sets
    # egg_laying_started_ts + incubation_started_ts from observation history
    # rather than ever observing building_nest in production.
    lifecycle_stage: Literal[
        "building_nest",
        "egg_laying",
        "incubation",
        "feeding",
        "fledging",
        "empty",
    ] = "incubation"
    last_chick_count: int | None = None
    # When the bird transitioned INTO egg_laying (first sitting observed).
    # Cardinals lay 1 egg per day for 3-4 days before starting full incubation.
    egg_laying_started_ts: float | None = None
    # When the bird transitioned INTO incubation (sustained sitting observed).
    # ~12 day countdown to hatch begins here.
    incubation_started_ts: float | None = None
    hatch_detected_ts: float | None = None  # set when 2nd confirming chick signal arrives
    fledge_detected_ts: float | None = None  # set on fledge transition
    last_feeding_event_ts: float | None = None  # set on attending_parent_feeding_young=true
    # Timestamp of the first (unconfirmed) chick sighting. A second confirming
    # chick signal within 4 hours triggers the hatch transition. After 4 hours
    # without confirmation, this resets (stale sighting — possibly a misread).
    first_chick_sighting_ts: float | None = None

    # Ambiguous-occupied-cup pending candidate (2026-04-17). Set on the first
    # frame where a bird is visibly at the nest but the analyzer cannot
    # confirm species (attending_parent_on_nest="uncertain", no named threat species).
    # A second consecutive matching frame within the pending window
    # (_AMBIGUOUS_CONFIRM_WINDOW_S, default 10 min) promotes to soft presence
    # and clears. If no second frame arrives within the window, the candidate
    # is stale and the next matching frame restarts as a fresh 1st.
    pending_ambiguous_frame_ts: float | None = None

    def absence_seconds(self, now_ts: float) -> int | None:
        if self.last_mother_seen_ts is None:
            return None
        return int(now_ts - self.last_mother_seen_ts)

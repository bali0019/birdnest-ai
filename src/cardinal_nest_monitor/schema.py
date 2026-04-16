"""Vision-model output contracts (Pydantic) and matching Anthropic tool schemas.

Single source of truth for what the prefilter and analyzer return. All other
modules import from here. Keep this file dependency-free (just pydantic + stdlib)
so it can be imported from tests without any heavy SDKs initialised.
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


# ── Threat species enum ─────────────────────────────────────────────────────
class ThreatSpecies(str, Enum):
    BROWN_THRASHER = "brown_thrasher"
    BLUE_JAY = "blue_jay"
    SQUIRREL = "squirrel"
    CHIPMUNK = "chipmunk"
    UNKNOWN = "unknown"


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
      (b) female cardinal sitting on the nest
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


# Anthropic tool schema for PrefilterResult.
PREFILTER_TOOL: dict[str, Any] = {
    "name": "report_prefilter",
    "description": (
        "Report whether this image of a cardinal nest contains any novel activity "
        "that warrants a full deep analysis. Be conservative — if unclear, return "
        "'uncertain' so the deep analyzer takes a second look."
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
                    "true if anything other than (a) empty nest, (b) female cardinal "
                    "sitting on nest, (c) static scene with just leaves/wind. "
                    "uncertain if unclear."
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

    mother_cardinal_present: Tristate
    cardinal_on_nest: Tristate
    eggs_visible: Tristate
    egg_count_estimate: int | None = Field(
        None, ge=0, le=20, description="Estimated eggs visible. null if not visible/unsure."
    )
    nest_visible: bool
    nest_disturbed: Tristate

    species_detected: list[str] = Field(default_factory=list)
    threat_species_detected: list[ThreatSpecies] = Field(default_factory=list)

    near_nest_activity: bool
    direct_nest_interaction: bool

    confidence: float = Field(..., ge=0.0, le=1.0)
    summary: str = Field(..., description="Short human-readable explanation.")

    @field_validator("threat_species_detected", mode="before")
    @classmethod
    def _coerce_threats(cls, v: Any) -> list[str]:
        """Drop unknown enum values gracefully (model may hallucinate species)."""
        if not isinstance(v, list):
            return []
        valid = {s.value for s in ThreatSpecies}
        out = []
        for item in v:
            s = str(item).strip().lower().replace(" ", "_")
            if s in valid:
                out.append(s)
            else:
                # Unknown species name → bucket as "unknown" so we still flag a threat
                out.append(ThreatSpecies.UNKNOWN.value)
        return out


# Anthropic tool schema for NestObservation.
NEST_TOOL: dict[str, Any] = {
    "name": "report_nest",
    "description": (
        "Report a structured observation of a Northern Cardinal nest based on the "
        "provided image. Focus on: female cardinal presence on/near the nest, eggs "
        "visible and counted, threat species near or interacting with the nest, "
        "and whether the nest itself appears disturbed. Be conservative with "
        "uncertainty — return 'uncertain' if unclear; never guess."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "mother_cardinal_present",
            "cardinal_on_nest",
            "eggs_visible",
            "egg_count_estimate",
            "nest_visible",
            "nest_disturbed",
            "species_detected",
            "threat_species_detected",
            "near_nest_activity",
            "direct_nest_interaction",
            "confidence",
            "summary",
        ],
        "properties": {
            "mother_cardinal_present": {
                "type": "string",
                "enum": ["true", "false", "uncertain"],
                "description": "Is the female cardinal visible anywhere in the frame?",
            },
            "cardinal_on_nest": {
                "type": "string",
                "enum": ["true", "false", "uncertain"],
                "description": "Is the female cardinal sitting on/in the nest cup?",
            },
            "eggs_visible": {
                "type": "string",
                "enum": ["true", "false", "uncertain"],
                "description": (
                    "Are any eggs visible in the nest cup? (false if mother is "
                    "covering them.)"
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
                    "enum": [
                        "brown_thrasher",
                        "blue_jay",
                        "squirrel",
                        "chipmunk",
                        "unknown",
                    ],
                },
                "description": (
                    "Subset of species_detected that are nest threats. Use "
                    "'unknown' for any unidentified animal that isn't clearly a "
                    "cardinal or mockingbird."
                ),
            },
            "near_nest_activity": {
                "type": "boolean",
                "description": (
                    "Is any non-cardinal animal physically at, on, or within "
                    "~30cm of the nest/bush? (False if they're elsewhere in yard.)"
                ),
            },
            "direct_nest_interaction": {
                "type": "boolean",
                "description": (
                    "Is a non-cardinal animal touching, reaching into, or pulling "
                    "from the nest cup? This is the highest-severity signal."
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

    def absence_seconds(self, now_ts: float) -> int | None:
        if self.last_mother_seen_ts is None:
            return None
        return int(now_ts - self.last_mother_seen_ts)

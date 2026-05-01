"""Pydantic schema for species profiles.

A species profile is a TOML file that captures everything the runtime
needs to know about a particular target nesting species: biological
identity, the prompt-context strings the analyzer will see, target and
threat field marks used to render the analyzer prompt, the list of
canonical threat names the model is allowed to report, lifecycle timing
and detection thresholds, user-facing Discord copy, and paths to
reference assets used by the regression tools.

Scope (generic-nest-monitor refactor, 2026-04-23):
  * Open-cup nesters with visible-nest camera geometry.
  * One target species per deployment; profile is selected at startup
    via SPECIES_PROFILE_PATH.
  * No backward compatibility with the cardinal branch's stored JSON —
    this branch starts fresh.

The profile is loaded once at startup (see ``species.loader``) and
treated as immutable for the process lifetime. Invalid profiles fail
fast with a clear pydantic validation error.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


# ── Species identity ────────────────────────────────────────────────────

class SpeciesIdentity(BaseModel):
    """Basic biological identity fields — used for logging, titles,
    filesystem slugs, and human-readable labels."""

    slug: str = Field(
        ...,
        pattern=r"^[a-z][a-z0-9_]*$",
        description=(
            "Filesystem-safe identifier (e.g. 'northern_cardinal'). "
            "Lowercase, underscore-separated. Used in "
            "evidence/reference/<slug>/ and similar paths."
        ),
    )
    common_name: str = Field(..., description="e.g. 'Northern Cardinal'")
    scientific_name: str = Field(..., description="e.g. 'Cardinalis cardinalis'")
    display_name: str = Field(
        ...,
        description=(
            "Shorter friendly label for Discord embeds and log lines. "
            "e.g. 'cardinal' or 'robin'."
        ),
    )


# ── Target-species runtime handles ──────────────────────────────────────

class SpeciesTarget(BaseModel):
    """Strings the runtime uses to decide whether a freeform model output
    mentions the target species and to label attending-parent/young
    references in rendered prompts and Discord copy."""

    match_terms: list[str] = Field(
        ...,
        min_length=1,
        description=(
            "Lowercased substrings the verifier matches against "
            "observation.species_detected to decide 'this is the target "
            "species, not a threat'. E.g. ['cardinal', 'northern cardinal']."
        ),
    )
    attending_parent_label: str = Field(
        ...,
        description=(
            "How to refer to the bird sitting on/visiting the nest in "
            "rendered prompts and alert copy. e.g. 'female cardinal', "
            "'attending parent'."
        ),
    )
    young_label: str = Field(
        ...,
        description=(
            "Plural noun for young at the nest. e.g. 'chicks', 'nestlings'."
        ),
    )


# ── Prompt context ──────────────────────────────────────────────────────

class PromptContext(BaseModel):
    """Situational strings that open the analyzer system prompt — what
    the camera is looking at and any historical predation that informs
    the threat posture."""

    habitat: str = Field(
        ...,
        description=(
            "One-line description of where the nest is physically located. "
            "e.g. 'backyard rose bush in a residential garden'."
        ),
    )
    camera: str = Field(
        ...,
        description=(
            "How the camera is mounted and what it can see. e.g. 'low to "
            "the ground in dense foliage near the home'."
        ),
    )
    nest_type: str = Field(
        ...,
        description="e.g. 'open cup woven into a rose bush'.",
    )
    threat_history: str = Field(
        "",
        description=(
            "Any prior predation incidents the analyzer should know about. "
            "Empty string if none. e.g. 'A Brown Thrasher has previously "
            "attacked this nest and stolen at least one egg.'"
        ),
    )


# ── Field marks ─────────────────────────────────────────────────────────

class TargetFieldMarks(BaseModel):
    """Target species identification cues — the features that
    distinguish it from threats and other ambient species."""

    summary: str = Field(..., description="One-line introduction.")
    cues: list[str] = Field(
        ...,
        min_length=1,
        description="Bulleted list of distinguishing features.",
    )


class ThreatFieldMarks(BaseModel):
    """Per-threat species identification cues."""

    cues: list[str] = Field(
        ...,
        min_length=1,
        description="Bulleted list of features that identify this threat.",
    )
    note: str = Field(
        "",
        description=(
            "Optional extra context — e.g. 'HAS ALREADY ATTACKED THIS NEST'."
        ),
    )


class AmbientSpeciesEntry(BaseModel):
    """A species the camera might see that is NEITHER the target NOR a
    threat. The analyzer is told to not classify these as threats.

    e.g. House Finch, Northern Mockingbird — visible in the yard but
    should be ignored, not reported in threat_species_detected.
    """

    name: str = Field(..., description="e.g. 'Northern Mockingbird'.")
    cues: list[str] = Field(default_factory=list, description="Identification hints.")
    note: str = Field(
        "",
        description=(
            "e.g. 'NEUTRAL — may sing/visit nearby but does not threaten the nest.'"
        ),
    )


class FieldMarks(BaseModel):
    target: TargetFieldMarks
    threats: dict[str, ThreatFieldMarks] = Field(
        default_factory=dict,
        description=(
            "Keyed by canonical threat name (e.g. 'brown_thrasher'). "
            "Keys MUST match Threats.names exactly — enforced at the "
            "profile root."
        ),
    )
    ambient: list[AmbientSpeciesEntry] = Field(
        default_factory=list,
        description=(
            "Species to ignore: visible in the yard but not threats and "
            "not the target."
        ),
    )


# ── Threat species list ─────────────────────────────────────────────────

class Threats(BaseModel):
    """Canonical threat species names that the analyzer is allowed to
    report in ``threat_species_detected``.

    The runtime validator (Phase 3) will accept any name in this list
    PLUS the reserved ``"unknown"`` sentinel for ambiguous threats.
    """

    names: list[str] = Field(
        ...,
        min_length=1,
        description=(
            "Snake_case canonical names, e.g. ['brown_thrasher', "
            "'blue_jay', 'squirrel', 'chipmunk']. 'unknown' is reserved "
            "and must NOT appear here — the runtime adds it automatically."
        ),
    )


# ── Lifecycle timing + detection thresholds ─────────────────────────────

class LifecycleTiming(BaseModel):
    """Biological durations (species-specific) and analyzer tuning
    thresholds (mostly species-independent but adjustable per profile)."""

    egg_laying_days_min: int = Field(..., ge=1, le=14)
    egg_laying_days_max: int = Field(..., ge=1, le=14)
    incubation_days_min: int = Field(..., ge=7, le=30)
    incubation_days_max: int = Field(..., ge=7, le=30)
    fledge_days_min: int = Field(
        ...,
        ge=5,
        le=60,
        description="Days from hatch to fledge, lower bound.",
    )
    fledge_days_max: int = Field(..., ge=5, le=60)

    # Detection thresholds — profile-tunable with sensible defaults for
    # open-cup passerines.
    sitting_ratio_threshold: float = Field(
        0.70,
        ge=0.5,
        le=0.95,
        description=(
            "Fraction of confident observations over "
            "sitting_ratio_window_hours needed to auto-advance egg_laying "
            "→ incubation."
        ),
    )
    sitting_ratio_window_hours: int = Field(24, ge=6, le=72)
    young_confirmation_window_hours: int = Field(
        4,
        ge=1,
        le=12,
        description=(
            "Maximum elapsed time between two chick/young sightings for "
            "the second to confirm the first and advance "
            "incubation → feeding."
        ),
    )
    fledge_absence_hours: int = Field(
        12,
        ge=6,
        le=48,
        description=(
            "Hours with no attending-parent visit needed to infer a "
            "fledge (combined with threat-free history)."
        ),
    )
    fledge_threat_free_hours: int = Field(
        48,
        ge=12,
        le=168,
        description=(
            "How recently-threat-free the history must be before the "
            "'long absence after hatch' gets interpreted as fledge rather "
            "than predation."
        ),
    )
    young_sighting_confidence_floor: float = Field(
        0.75,
        ge=0.50,
        le=0.95,
        description=(
            "Minimum analyzer confidence required for a young_visible='true' "
            "frame to count toward the 2-sighting hatch confirmation. The "
            "default 0.75 was tuned for the cardinal nest after a false 0.82 "
            "reddish-blob sighting at day 3-4 of incubation nearly poisoned "
            "the hatch detection (Codex P3, 2026-04-17). Profiles for camera "
            "geometries that show young more clearly can lower this; "
            "occluded camera angles may need to raise it."
        ),
    )

    @model_validator(mode="after")
    def _check_ranges(self) -> "LifecycleTiming":
        if self.egg_laying_days_max < self.egg_laying_days_min:
            raise ValueError("egg_laying_days_max < egg_laying_days_min")
        if self.incubation_days_max < self.incubation_days_min:
            raise ValueError("incubation_days_max < incubation_days_min")
        if self.fledge_days_max < self.fledge_days_min:
            raise ValueError("fledge_days_max < fledge_days_min")
        return self


# ── User-facing alert copy ──────────────────────────────────────────────

class AlertCopy(BaseModel):
    """All Discord-visible strings that mention the target species go
    here. Cardinal profile ships the exact strings currently hardcoded
    in events.py; new profiles author their own copy."""

    egg_laying_begin_title: str
    egg_laying_begin_summary: str
    incubation_begin_title: str
    incubation_begin_summary: str
    hatch_title: str
    hatch_summary: str
    fledge_title: str
    fledge_summary: str
    long_absence_title: str = Field(
        ...,
        description=(
            "Title for the MEDIUM alert when the attending parent has been "
            "away from the nest for an extended period. MUST contain the "
            "literal placeholder '{bucket_mins}' which events.py renders "
            "with the bucketed elapsed minutes (5, 10, 15, ...)."
        ),
    )
    attending_parent_returned_title: str = Field(
        ...,
        description=(
            "Title for the LOW alert when the attending parent is seen "
            "on/near the nest after an absence."
        ),
    )
    attending_parent_returned_summary: str = Field(
        ...,
        description=(
            "Summary (usually overridden by analyzer text at runtime; "
            "this is the fallback)."
        ),
    )

    @model_validator(mode="after")
    def _check_long_absence_placeholder(self) -> "AlertCopy":
        if "{bucket_mins}" not in self.long_absence_title:
            raise ValueError(
                "alert_copy.long_absence_title must contain literal "
                "'{bucket_mins}' placeholder"
            )
        return self


# ── Reference assets manifest ───────────────────────────────────────────

class ReferenceAssets(BaseModel):
    """Paths (relative to repo root) to reference images used by the
    regression tools. All paths are species-scoped under
    ``evidence/reference/<slug>/``.
    """

    directory: str = Field(
        ...,
        description=(
            "Relative path to the species-scoped reference directory, "
            "e.g. 'evidence/reference/northern_cardinal'."
        ),
    )
    target_on_nest: list[str] = Field(
        default_factory=list,
        description="Images showing the target species on the nest.",
    )
    threat_examples: list[str] = Field(
        default_factory=list,
        description=(
            "Images showing one or more of the listed threat species at "
            "or near the nest."
        ),
    )
    empty_nest: list[str] = Field(
        default_factory=list,
        description="Images of an empty nest cup for negative controls.",
    )
    lifecycle_regression: list[str] = Field(
        default_factory=list,
        description=(
            "Images used by tools/lifecycle_regression.py. Each with a "
            "paired .expected.json file in the same directory."
        ),
    )


# ── Top-level profile ───────────────────────────────────────────────────

class SpeciesProfile(BaseModel):
    """A complete species profile. Loaded once at startup from TOML."""

    species: SpeciesIdentity
    target: SpeciesTarget
    prompt_context: PromptContext
    field_marks: FieldMarks
    threats: Threats
    lifecycle: LifecycleTiming
    # Named `alert_copy` (not `copy`) to avoid shadowing Pydantic's
    # BaseModel.copy() method. The corresponding TOML section is
    # [alert_copy] — profile authors see the same name they set.
    alert_copy: AlertCopy
    reference_assets: ReferenceAssets

    @model_validator(mode="after")
    def _check_threats_consistent(self) -> "SpeciesProfile":
        """The set of canonical threat names in `threats.names` MUST
        match the keys of `field_marks.threats` exactly. Otherwise a
        profile can list a threat with no field-mark guidance, or ship
        field marks for a threat the runtime won't accept."""
        declared = set(self.threats.names)
        marked = set(self.field_marks.threats.keys())
        if declared != marked:
            missing_marks = declared - marked
            missing_in_names = marked - declared
            parts = []
            if missing_marks:
                parts.append(
                    f"threats.names has {sorted(missing_marks)!r} with no "
                    "field_marks.threats entry"
                )
            if missing_in_names:
                parts.append(
                    f"field_marks.threats has {sorted(missing_in_names)!r} "
                    "not listed in threats.names"
                )
            raise ValueError(
                "profile threats list and field_marks.threats keys must match: "
                + "; ".join(parts)
            )
        if "unknown" in declared:
            raise ValueError(
                "threats.names must NOT include 'unknown' — it is reserved "
                "and added automatically by the runtime validator"
            )
        return self

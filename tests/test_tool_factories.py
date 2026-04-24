"""Golden-output snapshot tests for the build_nest_tool / build_prefilter_tool
factories. These run under both shipped species profiles (cardinal, robin)
to prove the tool contract is genuinely profile-driven.

Acceptance criteria (Phase 3, 2026-04-23):
  * Rendered tool schemas are valid Anthropic tool dicts
    (top-level name/description/input_schema + object-shape input_schema).
  * threat_species_detected enum is populated from the profile's
    threat list plus the reserved "unknown" sentinel.
  * Attending-parent and young labels come from the profile — different
    profiles produce different human-readable strings.
  * Cardinal and robin profiles produce materially different tool schemas
    (they're not accidentally aliased to the same object).

These are snapshot tests in the sense that the expected shape is spelled
out in assertions. If you intentionally change what build_nest_tool
renders, update the assertions in the same commit.
"""

from __future__ import annotations

import pytest

from cardinal_nest_monitor.schema import (
    UNKNOWN_THREAT,
    build_nest_tool,
    build_prefilter_tool,
)
from cardinal_nest_monitor.species import (
    builtin_profile_path,
    load_species_profile,
)


CARDINAL = load_species_profile(builtin_profile_path("northern_cardinal"))
ROBIN = load_species_profile(builtin_profile_path("american_robin"))


# ── build_nest_tool ────────────────────────────────────────────────────

def test_nest_tool_has_required_top_level_shape():
    """A valid Anthropic tool dict has `name`, `description`, and
    `input_schema` (JSON Schema object)."""
    tool = build_nest_tool(CARDINAL)
    assert tool["name"] == "report_nest"
    assert isinstance(tool["description"], str)
    assert "input_schema" in tool
    assert tool["input_schema"]["type"] == "object"
    assert tool["input_schema"]["additionalProperties"] is False


def test_nest_tool_description_mentions_target_species():
    """Cardinal profile → description references 'Northern Cardinal';
    robin profile → description references 'American Robin'."""
    card_tool = build_nest_tool(CARDINAL)
    robin_tool = build_nest_tool(ROBIN)
    assert "Northern Cardinal" in card_tool["description"]
    assert "American Robin" in robin_tool["description"]
    assert "Northern Cardinal" not in robin_tool["description"]


def test_nest_tool_threat_enum_comes_from_profile():
    """threat_species_detected enum values come from profile.threats.names
    + the reserved 'unknown' sentinel. A robin profile must have a
    different enum than a cardinal profile (different predator list)."""
    card_enum = build_nest_tool(CARDINAL)["input_schema"]["properties"][
        "threat_species_detected"
    ]["items"]["enum"]
    robin_enum = build_nest_tool(ROBIN)["input_schema"]["properties"][
        "threat_species_detected"
    ]["items"]["enum"]

    # Both enums must contain every profile-declared threat + "unknown".
    assert set(card_enum) == set(CARDINAL.threats.names) | {UNKNOWN_THREAT}
    assert set(robin_enum) == set(ROBIN.threats.names) | {UNKNOWN_THREAT}

    # And they must actually differ (robin doesn't fight a brown
    # thrasher; cardinal doesn't fight a cooper's hawk).
    assert "brown_thrasher" in card_enum
    assert "brown_thrasher" not in robin_enum
    assert "coopers_hawk" in robin_enum
    assert "coopers_hawk" not in card_enum

    # "unknown" always present, regardless of profile.
    assert UNKNOWN_THREAT in card_enum
    assert UNKNOWN_THREAT in robin_enum


def test_nest_tool_attending_parent_label_from_profile():
    """Per-field descriptions use the profile's attending_parent_label
    (e.g. 'female cardinal' vs 'robin parent')."""
    card_tool = build_nest_tool(CARDINAL)
    robin_tool = build_nest_tool(ROBIN)

    card_desc = card_tool["input_schema"]["properties"][
        "attending_parent_on_nest"
    ]["description"]
    robin_desc = robin_tool["input_schema"]["properties"][
        "attending_parent_on_nest"
    ]["description"]

    assert "female cardinal" in card_desc
    assert "robin parent" in robin_desc
    assert "female cardinal" not in robin_desc
    assert "robin parent" not in card_desc


def test_nest_tool_young_label_from_profile():
    """young_visible / young_count_estimate descriptions use the
    profile's young_label (e.g. 'chicks')."""
    card_tool = build_nest_tool(CARDINAL)
    desc = card_tool["input_schema"]["properties"]["young_visible"][
        "description"
    ]
    assert "chicks" in desc.lower()


def test_nest_tool_required_list_is_complete():
    """The `required` list must enumerate every runtime-observable field.
    Regression guard — if a new field is added to NestObservation but
    not to the tool schema, the model will omit it and pydantic will
    reject the response."""
    required = build_nest_tool(CARDINAL)["input_schema"]["required"]
    for field in (
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
    ):
        assert field in required, (
            f"{field!r} missing from build_nest_tool input_schema.required"
        )


def test_nest_tool_is_fresh_dict_per_call():
    """Factory returns a new dict each call. Callers that want to cache
    must do so explicitly — the factory never returns a shared reference
    (avoids accidental mutation of a shared tool object)."""
    a = build_nest_tool(CARDINAL)
    b = build_nest_tool(CARDINAL)
    assert a is not b
    assert a == b  # but same content


def test_nest_tool_cardinal_and_robin_are_different():
    """Guard that the factory actually consumes the profile."""
    assert build_nest_tool(CARDINAL) != build_nest_tool(ROBIN)


# ── build_prefilter_tool ───────────────────────────────────────────────

def test_prefilter_tool_has_required_top_level_shape():
    tool = build_prefilter_tool(CARDINAL)
    assert tool["name"] == "report_prefilter"
    assert isinstance(tool["description"], str)
    assert tool["input_schema"]["type"] == "object"


def test_prefilter_tool_description_mentions_target_species():
    card = build_prefilter_tool(CARDINAL)
    robin = build_prefilter_tool(ROBIN)
    assert "Northern Cardinal" in card["description"]
    assert "American Robin" in robin["description"]


def test_prefilter_tool_attending_parent_label_from_profile():
    card = build_prefilter_tool(CARDINAL)
    robin = build_prefilter_tool(ROBIN)
    card_desc = card["input_schema"]["properties"]["novel_activity"]["description"]
    robin_desc = robin["input_schema"]["properties"]["novel_activity"]["description"]
    assert "female cardinal" in card_desc
    assert "robin parent" in robin_desc


def test_prefilter_tool_cardinal_and_robin_are_different():
    assert build_prefilter_tool(CARDINAL) != build_prefilter_tool(ROBIN)


# ── NestObservation validator uses the profile ────────────────────────

def test_nest_observation_coerces_unknown_threat_to_sentinel(monkeypatch):
    """When the model returns a threat name not in the active profile's
    list, the validator buckets it as 'unknown' — matching the prior
    ThreatSpecies.UNKNOWN enum behavior. Important so downstream rules
    still fire on 'there's a threat, we can't name it'."""
    from cardinal_nest_monitor.config import get_settings
    from cardinal_nest_monitor.schema import NestObservation
    from cardinal_nest_monitor.species import clear_species_profile_cache

    # Pin the active profile to cardinal for this test (regardless of
    # SPECIES_PROFILE_PATH in the ambient env).
    monkeypatch.setattr(
        get_settings(),
        "species_profile_path",
        builtin_profile_path("northern_cardinal"),
    )
    clear_species_profile_cache()

    obs = NestObservation(
        attending_parent_present="false",
        attending_parent_on_nest="false",
        eggs_visible="false",
        egg_count_estimate=None,
        nest_visible=True,
        nest_disturbed="false",
        species_detected=["something weird"],
        threat_species_detected=["rogue_dragon", "brown_thrasher"],
        near_nest_activity=True,
        direct_nest_interaction=False,
        confidence=0.8,
        summary="test",
    )
    # rogue_dragon is not in the cardinal profile, so it's coerced.
    # brown_thrasher IS in the cardinal profile, so it passes through.
    assert obs.threat_species_detected == [UNKNOWN_THREAT, "brown_thrasher"]
    clear_species_profile_cache()


def test_nest_observation_accepts_profile_declared_threats(monkeypatch):
    """Every threat name declared in the active profile must validate
    through unchanged."""
    from cardinal_nest_monitor.config import get_settings
    from cardinal_nest_monitor.schema import NestObservation
    from cardinal_nest_monitor.species import clear_species_profile_cache

    monkeypatch.setattr(
        get_settings(),
        "species_profile_path",
        builtin_profile_path("northern_cardinal"),
    )
    clear_species_profile_cache()

    obs = NestObservation(
        attending_parent_present="false",
        attending_parent_on_nest="false",
        eggs_visible="false",
        egg_count_estimate=None,
        nest_visible=True,
        nest_disturbed="false",
        species_detected=[],
        threat_species_detected=[
            "brown_thrasher", "blue_jay", "squirrel", "chipmunk",
        ],
        near_nest_activity=True,
        direct_nest_interaction=False,
        confidence=0.8,
        summary="test",
    )
    assert set(obs.threat_species_detected) == {
        "brown_thrasher", "blue_jay", "squirrel", "chipmunk",
    }
    clear_species_profile_cache()

"""Tests for the species profile loader + schema.

Phase 2 acceptance criteria:
  * Cardinal profile loads as default.
  * Robin profile loads successfully.
  * Invalid profiles fail fast at startup with clear pydantic errors.

No behavior change tests here — Phase 2 is the "profile is loaded but
not yet consumed" milestone. Later phases test that the loaded profile
actually drives analyzer prompts, event copy, etc.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

from birdnest_ai.species import (
    SpeciesProfile,
    builtin_profile_path,
    clear_species_profile_cache,
    get_species_profile,
    load_species_profile,
)

# Profiles are resolved via the installed-package helper so the tests
# work regardless of cwd (editable install or wheel install — both
# supported). Hard-coded paths relative to the repo root would break the
# moment someone runs pytest from a different directory.
CARDINAL_PATH = builtin_profile_path("northern_cardinal")
ROBIN_PATH = builtin_profile_path("american_robin")


# ── Happy path: both shipped profiles load cleanly ─────────────────────

def test_cardinal_profile_loads_and_validates():
    """The cardinal reference profile must load without any pydantic
    validation errors. If this breaks, the default deployment on the
    generic branch is broken."""
    profile = load_species_profile(CARDINAL_PATH)
    assert isinstance(profile, SpeciesProfile)
    assert profile.species.slug == "northern_cardinal"
    assert profile.species.common_name == "Northern Cardinal"
    assert profile.species.scientific_name == "Cardinalis cardinalis"
    assert "cardinal" in profile.target.match_terms
    assert profile.target.attending_parent_label == "female cardinal"
    assert profile.target.young_label == "chicks"


def test_cardinal_profile_has_expected_threats():
    """Cardinal profile must ship the threat list that matched the
    pre-refactor ThreatSpecies enum (minus 'unknown' which is
    reserved for the runtime)."""
    profile = load_species_profile(CARDINAL_PATH)
    assert set(profile.threats.names) == {
        "brown_thrasher", "blue_jay", "squirrel", "chipmunk",
    }
    # Each declared threat must also have field marks.
    for name in profile.threats.names:
        assert name in profile.field_marks.threats, (
            f"threat {name!r} declared in [threats] but missing from "
            "[field_marks.threats]"
        )


def test_cardinal_profile_lifecycle_matches_cardinal_biology():
    """Cardinal-specific lifecycle durations are captured faithfully."""
    profile = load_species_profile(CARDINAL_PATH)
    lc = profile.lifecycle
    # Cardinals lay one egg/day over ~3-4 days.
    assert lc.egg_laying_days_min == 3
    assert lc.egg_laying_days_max == 4
    # Incubation is ~11-13 days.
    assert lc.incubation_days_min == 11
    assert lc.incubation_days_max == 13
    # Chicks fledge at ~9-11 days post-hatch.
    assert lc.fledge_days_min == 9
    assert lc.fledge_days_max == 11
    # Detection thresholds should match the pre-refactor constants.
    assert lc.sitting_ratio_threshold == 0.70
    assert lc.young_confirmation_window_hours == 4


def test_cardinal_profile_alert_copy_is_verbatim():
    """Cardinal alert copy must match the strings currently hardcoded
    in events.py. This is the guard that keeps Discord output
    identical to pre-refactor for the cardinal use case."""
    profile = load_species_profile(CARDINAL_PATH)
    c = profile.alert_copy
    assert c.egg_laying_begin_title == "🥚 Egg laying has begun"
    assert "Female cardinal first observed" in c.egg_laying_begin_summary
    assert c.incubation_begin_title == "🪺 Incubation has begun"
    assert "{ratio_pct}" in c.incubation_begin_summary, (
        "incubation_begin_summary must contain {ratio_pct} placeholder "
        "for runtime str.format interpolation"
    )
    assert c.hatch_title == "🐣 Chicks hatched!"
    assert c.fledge_title == "🦅 Chicks fledged!"


def test_robin_profile_loads_and_validates():
    """The robin profile must load without any validation errors. It
    is the structural validation target for the whole profile-driven
    refactor — if this doesn't load, the 'any profiled species' claim
    is false."""
    profile = load_species_profile(ROBIN_PATH)
    assert isinstance(profile, SpeciesProfile)
    assert profile.species.slug == "american_robin"
    assert profile.species.common_name == "American Robin"
    assert profile.species.scientific_name == "Turdus migratorius"
    assert "robin" in profile.target.match_terms


def test_robin_profile_has_different_threats_than_cardinal():
    """The whole point of a profile is that different species face
    different threats. Robin profile ships aerial threats (crows,
    hawks) that the cardinal profile doesn't — a sanity check that
    profiles actually parameterize the runtime differently."""
    cardinal = load_species_profile(CARDINAL_PATH)
    robin = load_species_profile(ROBIN_PATH)
    cardinal_threats = set(cardinal.threats.names)
    robin_threats = set(robin.threats.names)
    # Robin-specific threats that don't apply to a backyard cardinal.
    assert "american_crow" in robin_threats
    assert "coopers_hawk" in robin_threats
    assert "american_crow" not in cardinal_threats
    # Both share squirrel + chipmunk.
    assert "squirrel" in robin_threats & cardinal_threats


def test_robin_profile_lifecycle_matches_robin_biology():
    """American Robin specific lifecycle durations."""
    profile = load_species_profile(ROBIN_PATH)
    lc = profile.lifecycle
    # Robins lay 3-5 eggs.
    assert lc.egg_laying_days_min == 3
    assert lc.egg_laying_days_max == 5
    # Incubation ~12-14 days.
    assert lc.incubation_days_min == 12
    assert lc.incubation_days_max == 14
    # Fledge ~13-16 days.
    assert lc.fledge_days_min == 13
    assert lc.fledge_days_max == 16


# ── Failure modes: malformed profiles must fail fast ───────────────────

def test_missing_profile_file_raises_filenotfound(tmp_path):
    with pytest.raises(FileNotFoundError) as exc_info:
        load_species_profile(tmp_path / "does_not_exist.toml")
    assert "species profile not found" in str(exc_info.value)


def test_invalid_slug_format_rejected(tmp_path):
    """Slug must match ^[a-z][a-z0-9_]*$ — no uppercase, no hyphens."""
    bad = tmp_path / "bad.toml"
    _write_partial(
        bad,
        species_slug="Northern-Cardinal",  # uppercase + hyphen
    )
    with pytest.raises(ValidationError):
        load_species_profile(bad)


def test_threat_listed_without_field_marks_rejected(tmp_path):
    """If a threat name appears in [threats] but NOT in
    [field_marks.threats.<name>], the model validator must reject the
    profile — otherwise the runtime would accept a threat with no
    identification guidance in the rendered prompt."""
    bad = tmp_path / "bad.toml"
    cardinal_data = tomllib.loads(CARDINAL_PATH.read_text())
    cardinal_data["threats"]["names"].append("phantom_predator")
    # Intentionally do NOT add field_marks.threats.phantom_predator.
    _dump(bad, cardinal_data)
    with pytest.raises(ValidationError) as exc_info:
        load_species_profile(bad)
    assert "phantom_predator" in str(exc_info.value)


def test_field_marks_threat_without_listing_rejected(tmp_path):
    """Reverse of above: field marks for a threat that ISN'T in the
    threats list is also invalid — a profile that ships marks for
    something the runtime won't accept is internally inconsistent."""
    bad = tmp_path / "bad.toml"
    cardinal_data = tomllib.loads(CARDINAL_PATH.read_text())
    cardinal_data["field_marks"]["threats"]["ghost_species"] = {
        "cues": ["spectral appearance"],
    }
    # Do NOT add to threats.names.
    _dump(bad, cardinal_data)
    with pytest.raises(ValidationError) as exc_info:
        load_species_profile(bad)
    assert "ghost_species" in str(exc_info.value)


def test_unknown_in_threats_list_rejected(tmp_path):
    """'unknown' is reserved as a runtime sentinel and must not appear
    in threats.names — profiles that ship it are ambiguous."""
    bad = tmp_path / "bad.toml"
    cardinal_data = tomllib.loads(CARDINAL_PATH.read_text())
    cardinal_data["threats"]["names"].append("unknown")
    cardinal_data["field_marks"]["threats"]["unknown"] = {"cues": ["n/a"]}
    _dump(bad, cardinal_data)
    with pytest.raises(ValidationError) as exc_info:
        load_species_profile(bad)
    assert "unknown" in str(exc_info.value).lower()


def test_inverted_lifecycle_range_rejected(tmp_path):
    """egg_laying_days_max < egg_laying_days_min is invalid."""
    bad = tmp_path / "bad.toml"
    cardinal_data = tomllib.loads(CARDINAL_PATH.read_text())
    cardinal_data["lifecycle"]["egg_laying_days_min"] = 5
    cardinal_data["lifecycle"]["egg_laying_days_max"] = 3
    _dump(bad, cardinal_data)
    with pytest.raises(ValidationError):
        load_species_profile(bad)


# ── Cached accessor ────────────────────────────────────────────────────

def test_get_species_profile_uses_configured_path(monkeypatch):
    """get_species_profile() must read SPECIES_PROFILE_PATH from
    settings and load that file. Cached across calls within a single
    process."""
    clear_species_profile_cache()
    from birdnest_ai.config import get_settings

    # get_settings() is lru_cached; override the attribute directly
    # (matches how other tests monkeypatch settings).
    s = get_settings()
    monkeypatch.setattr(s, "species_profile_path", CARDINAL_PATH)
    profile = get_species_profile()
    assert profile.species.slug == "northern_cardinal"
    clear_species_profile_cache()


def test_get_species_profile_is_cached(monkeypatch):
    """Second call to get_species_profile() with the same path must
    return the exact same object (cached)."""
    clear_species_profile_cache()
    from birdnest_ai.config import get_settings

    monkeypatch.setattr(get_settings(), "species_profile_path", CARDINAL_PATH)
    a = get_species_profile()
    b = get_species_profile()
    assert a is b, "expected cached identity, not a fresh reload"
    clear_species_profile_cache()


# ── Helpers ────────────────────────────────────────────────────────────

def _write_partial(path: Path, *, species_slug: str):
    """Write a minimally-shaped TOML with an overridable slug for
    negative-path tests. Everything else copied from cardinal profile
    so only the field under test is invalid."""
    data = tomllib.loads(CARDINAL_PATH.read_text())
    data["species"]["slug"] = species_slug
    _dump(path, data)


def _dump(path: Path, data: dict) -> None:
    """Serialize a dict back to TOML for test fixtures. Uses a minimal
    roundtrip — we only need enough TOML fidelity to feed it back
    through load_species_profile.

    tomllib is read-only (stdlib), so we hand-serialize. Scope is
    small: negative-path fixtures only.
    """
    import re

    def _quote(s: str) -> str:
        # Basic-string quoting with backslash escapes. Good enough for
        # test fixture data that doesn't include wild unicode or nested
        # quotes in tricky ways.
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace(
            "\n", "\\n"
        ) + '"'

    def _fmt_value(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return str(v)
        if isinstance(v, str):
            return _quote(v)
        if isinstance(v, list):
            return "[" + ", ".join(_fmt_value(x) for x in v) + "]"
        raise TypeError(f"unsupported value type: {type(v)}")

    lines: list[str] = []

    def _emit_table(prefix: list[str], d: dict) -> None:
        # Separate scalar/list entries from nested tables.
        scalars = {k: v for k, v in d.items() if not isinstance(v, dict)}
        tables = {k: v for k, v in d.items() if isinstance(v, dict)}
        lists_of_tables = {
            k: v for k, v in scalars.items()
            if isinstance(v, list) and v and isinstance(v[0], dict)
        }
        scalars = {k: v for k, v in scalars.items() if k not in lists_of_tables}

        if prefix and (scalars or not tables):
            lines.append(f"[{'.'.join(prefix)}]")
        for k, v in scalars.items():
            lines.append(f"{k} = {_fmt_value(v)}")
        if scalars:
            lines.append("")
        for k, v in tables.items():
            _emit_table(prefix + [k], v)
        for k, arr in lists_of_tables.items():
            for item in arr:
                lines.append(f"[[{'.'.join(prefix + [k])}]]")
                for ik, iv in item.items():
                    if isinstance(iv, dict):
                        # nested struct in array-of-tables — rare; recurse
                        raise NotImplementedError(
                            "nested dict in array-of-tables not supported "
                            "by test helper"
                        )
                    lines.append(f"{ik} = {_fmt_value(iv)}")
                lines.append("")

    _emit_table([], data)
    path.write_text("\n".join(lines))

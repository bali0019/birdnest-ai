"""Schema parsing / validation tests. Pure pydantic — no DB, no event engine."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cardinal_nest_monitor.schema import (
    NestObservation,
    PrefilterResult,
    Severity,
)


def _minimal_obs_dict(**overrides):
    base = {
        "mother_cardinal_present": "true",
        "cardinal_on_nest": "true",
        "eggs_visible": "false",
        "egg_count_estimate": None,
        "nest_visible": True,
        "nest_disturbed": "false",
        "species_detected": ["northern_cardinal"],
        "threat_species_detected": [],
        "near_nest_activity": False,
        "direct_nest_interaction": False,
        "confidence": 0.9,
        "summary": "Mother on nest.",
    }
    base.update(overrides)
    return base


def test_minimal_valid_observation_parses():
    obs = NestObservation(**_minimal_obs_dict())
    assert obs.mother_cardinal_present == "true"
    assert obs.confidence == 0.9
    assert obs.threat_species_detected == []


def test_confidence_out_of_range_rejected():
    with pytest.raises(ValidationError):
        NestObservation(**_minimal_obs_dict(confidence=1.5))
    with pytest.raises(ValidationError):
        NestObservation(**_minimal_obs_dict(confidence=-0.1))


def test_bad_tristate_enum_rejected():
    with pytest.raises(ValidationError):
        NestObservation(**_minimal_obs_dict(mother_cardinal_present="maybe"))


def test_unknown_species_buckets_to_unknown():
    obs = NestObservation(
        **_minimal_obs_dict(threat_species_detected=["fox", "raccoon"])
    )
    values = [
        (t.value if hasattr(t, "value") else t) for t in obs.threat_species_detected
    ]
    assert values == ["unknown", "unknown"]


def test_known_species_preserved():
    obs = NestObservation(
        **_minimal_obs_dict(threat_species_detected=["brown_thrasher", "Blue Jay"])
    )
    values = [
        (t.value if hasattr(t, "value") else t) for t in obs.threat_species_detected
    ]
    # "Blue Jay" → lowercased + spaces→underscores → "blue_jay"
    assert values == ["brown_thrasher", "blue_jay"]


def test_egg_count_range():
    with pytest.raises(ValidationError):
        NestObservation(**_minimal_obs_dict(egg_count_estimate=25))
    obs = NestObservation(**_minimal_obs_dict(eggs_visible="true", egg_count_estimate=3))
    assert obs.egg_count_estimate == 3


def test_prefilter_result_should_escalate():
    assert PrefilterResult(novel_activity="true", reason="x").should_escalate
    assert PrefilterResult(novel_activity="uncertain", reason="x").should_escalate
    assert not PrefilterResult(novel_activity="false", reason="x").should_escalate


def test_severity_rank_and_ordering():
    assert (
        Severity.CRITICAL.rank
        > Severity.HIGH.rank
        > Severity.MEDIUM.rank
        > Severity.LOW.rank
    )

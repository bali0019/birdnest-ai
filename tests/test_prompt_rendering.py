"""Phase 5 — analyzer + prefilter system prompts rendered from profile.

These tests pin the prompt-rendering contract so a future profile or
field-mark edit can't silently strip the species-ID guidance the rules
engine depends on. The behavior snapshot tests in
test_behavior_snapshots.py guard the alert-decision side; this file
guards the model-input side.

We do NOT assert byte-identity to the legacy hardcoded cardinal prompt.
Phase 3 already changed observation field names, and the renderer
generalizes the structure — exact-text equality would require both
profiles to ship identical strings, defeating the genericization.
Instead we assert that the rendered prompt CONTAINS each profile's
distinctive cues, threat names, and labels.
"""

from __future__ import annotations

import pytest

from birdnest_ai.prompts import (
    invalidate_prompt_caches,
    render_analyzer_system_prompt,
    render_prefilter_system_prompt,
)
from birdnest_ai.verifier import is_target_positive_no_threat


_PROFILES = ["northern_cardinal", "american_robin"]


# ── Analyzer prompt — per-profile content expectations ─────────────────


@pytest.mark.parametrize("use_profile", _PROFILES, indirect=True)
def test_analyzer_prompt_mentions_target_species(use_profile):
    """The rendered analyzer prompt must name the target species and
    its attending-parent label."""
    profile = use_profile
    invalidate_prompt_caches()
    prompt = render_analyzer_system_prompt(profile)

    assert profile.species.common_name in prompt, (
        f"common_name {profile.species.common_name!r} must appear in "
        "the analyzer prompt so the model knows what species the camera "
        "is watching"
    )
    assert profile.target.attending_parent_label in prompt, (
        f"attending_parent_label {profile.target.attending_parent_label!r} "
        "must appear in the prompt"
    )


@pytest.mark.parametrize("use_profile", _PROFILES, indirect=True)
def test_analyzer_prompt_includes_target_field_marks(use_profile):
    """Every target-species cue in the profile must appear in the
    rendered prompt — otherwise the model has fewer features to work
    with than the profile author specified."""
    profile = use_profile
    invalidate_prompt_caches()
    prompt = render_analyzer_system_prompt(profile)

    for cue in profile.field_marks.target.cues:
        assert cue in prompt, (
            f"target cue missing from rendered prompt under "
            f"{profile.species.slug}: {cue!r}"
        )


@pytest.mark.parametrize("use_profile", _PROFILES, indirect=True)
def test_analyzer_prompt_includes_every_threat_species(use_profile):
    """Every canonical threat name (in display form) and every threat
    cue must appear in the rendered prompt — guards against a profile
    edit that adds a threat to threats.names but forgets to render its
    field marks."""
    profile = use_profile
    invalidate_prompt_caches()
    prompt = render_analyzer_system_prompt(profile)

    for name in profile.threats.names:
        display = name.replace("_", " ").title()
        assert display in prompt, (
            f"threat display name {display!r} missing from prompt for "
            f"{profile.species.slug}"
        )
        marks = profile.field_marks.threats[name]
        for cue in marks.cues:
            assert cue in prompt, (
                f"cue for threat {name!r} missing from rendered prompt "
                f"under {profile.species.slug}: {cue!r}"
            )


@pytest.mark.parametrize("use_profile", _PROFILES, indirect=True)
def test_analyzer_prompt_includes_ambient_species(use_profile):
    """Ambient (NEUTRAL) species must appear by name so the model knows
    NOT to put them in threat_species_detected."""
    profile = use_profile
    invalidate_prompt_caches()
    prompt = render_analyzer_system_prompt(profile)

    for entry in profile.field_marks.ambient:
        assert entry.name in prompt, (
            f"ambient species {entry.name!r} missing from prompt for "
            f"{profile.species.slug}"
        )


@pytest.mark.parametrize("use_profile", _PROFILES, indirect=True)
def test_analyzer_prompt_includes_nest_type(use_profile):
    """profile.prompt_context.nest_type must appear in the rendered
    prompt. Open-cup vs cavity vs platform geometry informs how the
    analyzer should interpret 'nest disturbed' and
    'direct_nest_interaction'. Codex P2 — caught the renderer silently
    dropping nest_type after the docstring claimed it was wired."""
    profile = use_profile
    invalidate_prompt_caches()
    prompt = render_analyzer_system_prompt(profile)

    assert profile.prompt_context.nest_type in prompt, (
        f"nest_type {profile.prompt_context.nest_type!r} missing from "
        f"rendered analyzer prompt under {profile.species.slug}"
    )


@pytest.mark.parametrize("use_profile", _PROFILES, indirect=True)
def test_analyzer_prompt_includes_threat_history_when_set(use_profile):
    """If the profile has a non-empty threat_history string, it must
    appear in the prompt. Cardinal profile sets the Brown Thrasher
    history; robin profile leaves it empty by default."""
    profile = use_profile
    invalidate_prompt_caches()
    prompt = render_analyzer_system_prompt(profile)

    if profile.prompt_context.threat_history:
        assert profile.prompt_context.threat_history in prompt
    # When threat_history is empty (robin), we don't enforce anything —
    # the renderer silently omits the trailing space + history clause.


@pytest.mark.parametrize("use_profile", _PROFILES, indirect=True)
def test_analyzer_prompt_uses_young_label(use_profile):
    """The CHICKS-vs-EGGS lifecycle section must use the profile's
    young_label (e.g. 'chicks', 'nestlings') so the prompt reads
    naturally for any species that doesn't call them 'chicks'."""
    profile = use_profile
    invalidate_prompt_caches()
    prompt = render_analyzer_system_prompt(profile)

    # Both shipped profiles use "chicks", but the assertion still
    # exercises the rendering path.
    assert profile.target.young_label in prompt


def test_analyzer_prompt_caches_per_profile():
    """Repeated calls with the same profile must return the same
    string instance (lru_cache hit). Validates the cache key is the
    profile slug."""
    invalidate_prompt_caches()
    from birdnest_ai.species import (
        clear_species_profile_cache,
        get_species_profile,
    )
    from birdnest_ai.species.loader import builtin_profile_path
    from birdnest_ai.config import get_settings

    settings = get_settings()
    original = settings.species_profile_path
    try:
        settings.species_profile_path = builtin_profile_path("northern_cardinal")
        clear_species_profile_cache()
        invalidate_prompt_caches()
        profile = get_species_profile()
        a = render_analyzer_system_prompt(profile)
        b = render_analyzer_system_prompt(profile)
        assert a is b, (
            "render_analyzer_system_prompt must be cached: same profile "
            "→ same string instance"
        )
    finally:
        settings.species_profile_path = original
        clear_species_profile_cache()
        invalidate_prompt_caches()


# ── Prefilter prompt — per-profile content expectations ────────────────


@pytest.mark.parametrize("use_profile", _PROFILES, indirect=True)
def test_prefilter_prompt_uses_no_female_pronouns(use_profile):
    """Prefilter prompt must NOT contain gendered pronouns ('her', 'she',
    'his', 'he') that bias the model toward a female attending parent.
    Cardinal-only camera assumed female attending; for robins (where
    either parent may attend) and any future species this leak biases
    species ID. Codex P3 — guard against re-introducing the leak."""
    profile = use_profile
    invalidate_prompt_caches()
    prompt = render_prefilter_system_prompt(profile)
    lowered = prompt.lower()

    # Word-boundary substring check is sufficient for a small allowlist
    # — surrounding spaces/punctuation prevent false positives like
    # "the bird sheds" or "where".
    for token in (" her ", " she ", " his ", " he ", "'s her ", "her plumage"):
        assert token not in lowered, (
            f"prefilter prompt contains gendered token {token!r} under "
            f"{profile.species.slug} — use the {{attending_parent_label}} "
            f"or 'the bird' instead"
        )


@pytest.mark.parametrize("use_profile", _PROFILES, indirect=True)
def test_prefilter_prompt_mentions_target_and_threats(use_profile):
    """The prefilter prompt must name the target species (so the model
    knows what to NOT confabulate) and at least one threat name (so the
    'novel — needs deep analysis' bullet is concrete)."""
    profile = use_profile
    invalidate_prompt_caches()
    prompt = render_prefilter_system_prompt(profile)

    assert profile.species.common_name in prompt
    assert profile.target.attending_parent_label in prompt
    if profile.threats.names:
        first_threat_display = (
            profile.threats.names[0].replace("_", " ").title()
        )
        assert first_threat_display in prompt


# ── Verifier — is_target_positive_no_threat is profile-aware ──────────


@pytest.mark.parametrize("use_profile", _PROFILES, indirect=True)
def test_is_target_positive_no_threat_uses_profile_match_terms(use_profile):
    """The verifier's content-aware suppression must match against the
    active profile's match_terms — NOT the hardcoded 'cardinal'
    substring (the pre-Phase-5 bug)."""
    from birdnest_ai.schema import NestObservation

    profile = use_profile
    invalidate_prompt_caches()

    # Use the FIRST match_term — guaranteed to match the rule's target.
    matching_term = profile.target.match_terms[0]
    assert is_target_positive_no_threat(
        NestObservation(
            attending_parent_present="true",
            attending_parent_on_nest="true",
            eggs_visible="false",
            egg_count_estimate=None,
            nest_visible=True,
            nest_disturbed="false",
            species_detected=[matching_term],
            threat_species_detected=[],
            near_nest_activity=False,
            direct_nest_interaction=False,
            confidence=0.9,
            summary="target on nest",
        )
    ), f"profile {profile.species.slug}: match_term {matching_term!r} must match"


@pytest.mark.parametrize("use_profile", _PROFILES, indirect=True)
def test_is_target_positive_no_threat_returns_false_with_threat_present(use_profile):
    """If threat_species_detected has any entry, the predicate is False
    regardless of species_detected — the threat takes precedence."""
    from birdnest_ai.schema import NestObservation

    profile = use_profile
    invalidate_prompt_caches()
    threat_name = profile.threats.names[0]
    matching_term = profile.target.match_terms[0]

    assert not is_target_positive_no_threat(
        NestObservation(
            attending_parent_present="false",
            attending_parent_on_nest="false",
            eggs_visible="false",
            egg_count_estimate=None,
            nest_visible=True,
            nest_disturbed="false",
            species_detected=[matching_term],
            threat_species_detected=[threat_name],
            near_nest_activity=True,
            direct_nest_interaction=False,
            confidence=0.85,
            summary="mixed scene",
        )
    )


def test_is_cardinal_positive_no_threat_alias_still_callable():
    """Backwards-compat alias — preserved so any external callers
    (tools/dryrun, third-party scripts) keep working through the
    rename. Remove once all callers are updated."""
    from birdnest_ai.verifier import is_cardinal_positive_no_threat
    assert is_cardinal_positive_no_threat is is_target_positive_no_threat


def test_verifier_log_message_is_profile_neutral():
    """The verifier's content-aware suppression log line MUST say
    'target-positive', not 'cardinal-positive'. Codex P3 — caught a
    leaked cardinal-specific phrase that would have produced misleading
    suppression logs under the robin profile.

    Source-inspect rather than runtime-capture: the log line is
    operational, not behavioral, and we only need to lock the wording
    so future edits don't reintroduce the leak.
    """
    from pathlib import Path

    src = (
        Path(__file__).parent.parent
        / "src" / "birdnest_ai" / "verifier.py"
    ).read_text()

    # Find log calls that mention positive-no-threat suppression. The
    # specific phrase that ships in the log message must say "target-",
    # never "cardinal-".
    assert "target-positive no-threat override" in src, (
        "verifier.py must log target-positive no-threat suppressions "
        "with a profile-neutral phrase"
    )
    # Strict: the cardinal-flavored phrase must not appear in any log
    # call body. (It may still appear in docstrings/comments that
    # describe the historical incident.)
    assert "cardinal-positive no-threat override" not in src, (
        "verifier.py log message must not say 'cardinal-positive' under "
        "the generic-core branch — that wording leaks into operational "
        "logs for non-cardinal profiles"
    )

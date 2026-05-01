"""Phase 8/9 behavior-level regression guards.

Pin the rules engine's externally-visible behavior under BOTH shipped
profiles (northern_cardinal, american_robin) so that:

  1. Phase 5's analyzer prompt rewrite cannot silently change the rule
     taxonomy or alert copy without breaking these tests.
  2. Adding a new species profile cannot introduce profile-driven
     regressions to the cardinal deployment, because both profiles run
     the same parametrized assertions.
  3. The rule_id taxonomy is locked: any new alert rule must be added
     here explicitly so the change is reviewed.

Per Codex's guidance the regression target is *external behavior*:
alert severity, rule_id selection, titles + summaries, species lists.
NOT raw DB JSON or serialized state — Phase 3 intentionally reshaped
the persisted observation keys, so byte-identity is impossible.
"""

from __future__ import annotations

import time

import pytest

from birdnest_ai.events import evaluate
from birdnest_ai.predicates import is_confirmed_chick_sighting
from birdnest_ai.schema import NestObservation, Severity
from birdnest_ai.state import StateStore


_PROFILES = ["northern_cardinal", "american_robin"]


def _make_obs(**kwargs) -> NestObservation:
    base = dict(
        attending_parent_present="true",
        attending_parent_on_nest="true",
        eggs_visible="false",
        egg_count_estimate=None,
        nest_visible=True,
        nest_disturbed="false",
        species_detected=[],
        threat_species_detected=[],
        near_nest_activity=False,
        direct_nest_interaction=False,
        confidence=0.9,
        summary="Test observation.",
    )
    base.update(kwargs)
    return NestObservation(**base)


@pytest.fixture
def store(tmp_path):
    s = StateStore(tmp_path / "state.sqlite")
    yield s
    s.close()


# ── Long-absence MEDIUM: title rendered from profile.alert_copy ──────

@pytest.mark.parametrize("use_profile", _PROFILES, indirect=True)
def test_long_absence_title_renders_from_profile(use_profile, store):
    """The MEDIUM long_absence title MUST come from
    profile.alert_copy.long_absence_title with {bucket_mins} substituted.
    Catches a Phase 5 prompt rewrite that accidentally hardcodes the
    title back into events.py, or a profile that drops the placeholder.
    """
    profile = use_profile
    t0 = time.time()
    # Seed: parent on nest, then absent.
    store.record(t0, False, None, _make_obs(), None)
    absent = _make_obs(
        attending_parent_present="false",
        attending_parent_on_nest="false",
        summary="Nest is empty.",
    )
    # 5 minutes 9 seconds — the canonical "5+ minutes" bucket.
    state = store.record(t0 + 309, False, None, absent, None)
    decision = evaluate(absent, state, store, t0 + 309)

    assert decision is not None
    assert decision.severity == Severity.MEDIUM
    assert decision.rule_id == "long_absence"
    expected = profile.alert_copy.long_absence_title.format(bucket_mins=5)
    assert decision.title == expected, (
        f"long_absence title must equal "
        f"profile.alert_copy.long_absence_title.format(bucket_mins=5); "
        f"profile={profile.species.slug} got={decision.title!r} "
        f"expected={expected!r}"
    )


# ── Attending-parent-returned LOW: title rendered from profile ───────

@pytest.mark.parametrize("use_profile", _PROFILES, indirect=True)
def test_attending_parent_returned_title_from_profile(use_profile, store):
    """The LOW attending_parent_returned title MUST come from
    profile.alert_copy.attending_parent_returned_title.
    """
    profile = use_profile
    t0 = time.time()
    # Seed an absence so state.in_absence flips True.
    absent = _make_obs(
        attending_parent_present="false",
        attending_parent_on_nest="false",
        summary="Nest empty.",
    )
    store.record(t0, False, None, _make_obs(), None)  # mom present
    store.record(t0 + 130, False, None, absent, None)  # gone for >2 min
    # Now mom returns.
    returned = _make_obs(
        attending_parent_present="true",
        attending_parent_on_nest="true",
        summary="Mom is back.",
    )
    state = store.record(t0 + 600, False, None, returned, None)
    # state.in_absence flips here, so we need PRE-record state for the rule.
    # Simpler: use an explicit pre-state mock matching what the prior
    # absence put in place.
    from birdnest_ai.schema import NestState
    pre_state = NestState(
        last_attending_parent_seen_ts=t0,
        in_absence=True,
    )
    decision = evaluate(returned, pre_state, store, t0 + 600)

    assert decision is not None
    assert decision.severity == Severity.LOW
    assert decision.rule_id == "attending_parent_returned"
    assert decision.title == profile.alert_copy.attending_parent_returned_title


# ── Direct-attack CRITICAL: profile-independent severity, title is
# generic (set in events.py from the analyzer summary) ───────────────

@pytest.mark.parametrize("use_profile", _PROFILES, indirect=True)
def test_direct_attack_severity_and_rule_id_profile_independent(use_profile, store):
    """A direct nest interaction by a named threat species fires CRITICAL
    direct_attack regardless of profile. Severity and rule_id are
    species-engine concerns, not species-copy concerns — they must NOT
    drift across profiles."""
    profile = use_profile
    # Pick the first non-unknown threat species in the active profile so
    # the test works for both cardinal (brown_thrasher etc.) and robin
    # (american_crow etc.).
    threat_name = profile.threats.names[0]

    t0 = time.time()
    obs = _make_obs(
        attending_parent_present="false",
        attending_parent_on_nest="false",
        threat_species_detected=[threat_name],
        near_nest_activity=True,
        direct_nest_interaction=True,
        summary=f"{threat_name} reaching into nest cup.",
    )
    state = store.record(t0, False, None, obs, None)
    decision = evaluate(obs, state, store, t0)

    assert decision is not None
    assert decision.severity == Severity.CRITICAL
    assert decision.rule_id == "direct_attack"
    assert threat_name in decision.species


# ── Predator-near-nest HIGH: same severity/rule_id under both profiles

@pytest.mark.parametrize("use_profile", _PROFILES, indirect=True)
def test_predator_near_nest_severity_profile_independent(use_profile, store):
    profile = use_profile
    threat_name = profile.threats.names[0]
    t0 = time.time()
    # Seed mom-present so the absence path is established.
    store.record(t0, False, None, _make_obs(), None)
    # Threat at nest, mother absent.
    obs = _make_obs(
        attending_parent_present="false",
        attending_parent_on_nest="false",
        threat_species_detected=[threat_name],
        near_nest_activity=True,
        direct_nest_interaction=False,
        summary=f"{threat_name} at the nest rim.",
    )
    state = store.record(t0 + 5, False, None, obs, None)
    decision = evaluate(obs, state, store, t0 + 5)

    assert decision is not None
    assert decision.severity == Severity.HIGH
    assert decision.rule_id == "predator_absent"


# ── Lifecycle alert copy: every lifecycle title from profile.alert_copy

@pytest.mark.parametrize("use_profile", _PROFILES, indirect=True)
def test_egg_laying_begin_title_from_profile(use_profile, store):
    """Egg-laying begin LOW title must come from
    profile.alert_copy.egg_laying_begin_title."""
    profile = use_profile
    t0 = time.time()
    # Seed: building_nest stage, no prior sitting.
    from birdnest_ai.schema import NestState
    pre_state = NestState(
        lifecycle_stage="building_nest",
        last_attending_parent_seen_ts=None,
    )
    obs = _make_obs(
        attending_parent_present="true",
        attending_parent_on_nest="true",
        summary="Sitting on nest.",
    )
    decision = evaluate(obs, pre_state, store, t0)
    assert decision is not None, "first sitting in building_nest must fire"
    assert decision.rule_id == "egg_laying_begin"
    assert decision.severity == Severity.LOW
    assert decision.title == profile.alert_copy.egg_laying_begin_title
    assert decision.summary == profile.alert_copy.egg_laying_begin_summary


@pytest.mark.parametrize("use_profile", _PROFILES, indirect=True)
def test_incubation_begin_title_and_summary_from_profile(use_profile, store):
    """Incubation-begin LOW title + summary must come from profile, with
    {ratio_pct} substituted into the summary. Cardinal and robin profiles
    diverge on the summary text (cardinal: '~12 day countdown'; robin:
    '~12-14 day countdown') — locking both in catches Phase 5 prompt
    rewrites that drift one profile's wording without tripping cardinal
    coverage.

    Drives the rule by:
      1. Pre-state: lifecycle_stage='egg_laying', egg_laying_started_ts
         set to one sitting-ratio window ago.
      2. Seeding the observations table directly with N confident
         attending_parent_on_nest='true' rows so the engine's SQL scan
         meets the threshold.
    """
    profile = use_profile
    lc = profile.lifecycle
    sitting_window_s = lc.sitting_ratio_window_hours * 3600
    t0 = time.time()

    # Seed N=window-hour confident "on nest" observations spread evenly
    # across the rolling window. The engine requires
    # confident_total >= sitting_ratio_window_hours, so this matches
    # whatever the profile sets.
    n_samples = lc.sitting_ratio_window_hours
    sample_obs_json = (
        '{"attending_parent_on_nest":"true",'
        '"confidence":0.85}'
    )
    for i in range(n_samples):
        sample_ts = t0 - sitting_window_s + (i + 1) * (sitting_window_s / (n_samples + 1))
        store._conn.execute(
            "INSERT INTO observations (ts, motion_triggered, "
            "prefilter_json, observation_json, evidence_dir) "
            "VALUES (?, 0, NULL, ?, NULL)",
            (sample_ts, sample_obs_json),
        )

    # Pre-state in egg_laying with the start far enough back that the
    # transition window is open.
    from birdnest_ai.schema import NestState
    pre_state = NestState(
        lifecycle_stage="egg_laying",
        egg_laying_started_ts=t0 - sitting_window_s - 60,
    )
    obs = _make_obs(
        attending_parent_present="true",
        attending_parent_on_nest="true",
        summary="Sustained sitting.",
        confidence=0.85,
    )
    decision = evaluate(obs, pre_state, store, t0)

    assert decision is not None, (
        f"incubation_begin must fire when sitting ratio >= "
        f"{lc.sitting_ratio_threshold} over {lc.sitting_ratio_window_hours}h "
        f"with n={n_samples} confident samples"
    )
    assert decision.severity == Severity.LOW
    assert decision.rule_id == "incubation_begin"
    assert decision.title == profile.alert_copy.incubation_begin_title
    # Summary must be the profile template rendered with the actual ratio.
    # All seeded samples are "on nest", so the ratio is 100%.
    expected_summary = profile.alert_copy.incubation_begin_summary.format(
        ratio_pct="100%"
    )
    assert decision.summary == expected_summary, (
        f"incubation_begin summary must equal "
        f"profile.alert_copy.incubation_begin_summary.format(ratio_pct=...); "
        f"profile={profile.species.slug} got={decision.summary!r} "
        f"expected={expected_summary!r}"
    )


@pytest.mark.parametrize("use_profile", _PROFILES, indirect=True)
def test_hatch_title_from_profile(use_profile, store, monkeypatch):
    """Hatch LOW title must come from profile.alert_copy.hatch_title.

    Drives the 2-sighting confirmation: state pre-loaded with
    first_young_sighting_ts ~30 min ago, this observation is the
    confirming 2nd sighting within the profile's young_confirmation_window.
    """
    profile = use_profile
    t0 = time.time()
    from birdnest_ai.schema import NestState
    pre_state = NestState(
        lifecycle_stage="incubation",
        first_young_sighting_ts=t0 - 30 * 60,  # 30 min ago — well inside window
    )
    obs = _make_obs(
        attending_parent_present="true",
        attending_parent_on_nest="true",
        young_visible="true",
        young_count_estimate=2,
        confidence=0.85,  # ≥ profile.lifecycle.young_sighting_confidence_floor
        summary="Chicks visible.",
    )
    # Sanity: this must register as a confirmed sighting.
    assert is_confirmed_chick_sighting(obs)

    decision = evaluate(obs, pre_state, store, t0)
    assert decision is not None
    assert decision.rule_id == "hatch"
    assert decision.severity == Severity.LOW
    assert decision.title == profile.alert_copy.hatch_title
    assert decision.summary == profile.alert_copy.hatch_summary


@pytest.mark.parametrize("use_profile", _PROFILES, indirect=True)
def test_fledge_title_from_profile(use_profile, store):
    """Fledge LOW title must come from profile.alert_copy.fledge_title.

    Uses the profile's fledge_absence_hours / fledge_threat_free_hours
    so the trigger fires under both profiles even if they tune those
    knobs differently.
    """
    profile = use_profile
    t0 = time.time()
    lc = profile.lifecycle
    from birdnest_ai.schema import NestState
    pre_state = NestState(
        lifecycle_stage="feeding",
        hatch_detected_ts=t0 - 5 * 86400,
        last_attending_parent_seen_ts=t0 - (lc.fledge_absence_hours * 3600 + 60),
        last_threat_seen_ts=t0 - (lc.fledge_threat_free_hours * 3600 + 60),
    )
    obs = _make_obs(
        attending_parent_present="false",
        attending_parent_on_nest="false",
        summary="Nest empty.",
    )
    decision = evaluate(obs, pre_state, store, t0)
    assert decision is not None
    assert decision.rule_id == "fledge"
    assert decision.severity == Severity.LOW
    assert decision.title == profile.alert_copy.fledge_title
    assert decision.summary == profile.alert_copy.fledge_summary


# ── Rule_id taxonomy lock-down ──────────────────────────────────────

# Every rule_id the engine is allowed to emit. Adding a new rule MUST
# update this set in the same commit, which forces a code review of any
# alert taxonomy extension.
_KNOWN_RULE_IDS = frozenset({
    "direct_attack",
    "egg_loss",
    "predator_absent",          # threat present + cardinal absent → HIGH
    "long_absence",
    "attending_parent_returned",
    "egg_laying_begin",
    "incubation_begin",
    "hatch",
    "fledge",
})


def test_rule_id_taxonomy_lockdown():
    """Every rule_id literal that events.py can emit must be in
    _KNOWN_RULE_IDS. If you add a new rule, update this set in the
    same commit. If a rule is removed, drop it from the set.

    Source-inspects events.py for ``rule_id="..."`` literals — the
    canonical location where AlertDecision rule_ids are constructed.
    """
    import re
    from pathlib import Path

    src = (
        Path(__file__).parent.parent
        / "src" / "birdnest_ai" / "events.py"
    ).read_text()

    # Find all `rule_id="..."` and `rule_id='...'` literals.
    found = set(re.findall(r"""rule_id\s*=\s*['"]([a-z_]+)['"]""", src))
    extra = found - _KNOWN_RULE_IDS
    missing = _KNOWN_RULE_IDS - found
    assert not extra, (
        f"events.py emits rule_id(s) not in the locked taxonomy: "
        f"{sorted(extra)}. Add them to _KNOWN_RULE_IDS with a one-line "
        "comment if intentional."
    )
    # Missing is informational, not failure — the set is the upper
    # bound. (A rule may be commented out during a refactor.) But
    # report it for visibility.
    if missing:
        # Don't fail — but log so reviewers see an unused entry.
        print(f"INFO: _KNOWN_RULE_IDS contains rules not currently in "
              f"events.py: {sorted(missing)}")

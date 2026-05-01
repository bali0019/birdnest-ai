"""Phase 5 — render analyzer + prefilter system prompts from the active
species profile.

Profile fields drive everything that used to be hardcoded in
``analyzer.py::_SYSTEM_PROMPT`` and ``prefilter.py::_SYSTEM_PROMPT``:
prompt context (habitat / camera / nest_type / threat_history), target
field marks, threat field marks + notes, ambient (NEUTRAL) species, and
the user-facing labels for the attending parent and the young.

Why a separate module:
  - keeps the analyzer hot-path (asyncio + Anthropic SDK) free of
    multi-hundred-line string templates
  - lets the prompt renderer be unit-tested in isolation
  - makes "what is this rendered prompt for cardinal vs robin" a single
    grep target

Caching: each rendered prompt is cached by profile slug via lru_cache.
The species profile is immutable for the process lifetime
(``species/loader.py``), and tests that swap profiles call
``clear_species_profile_cache()`` which is paired with prompt cache
invalidation in ``invalidate_prompt_caches()`` below.
"""

from __future__ import annotations

from functools import lru_cache

from birdnest_ai.species._schema import (
    AmbientSpeciesEntry,
    SpeciesProfile,
    ThreatFieldMarks,
)


# ── Section helpers ────────────────────────────────────────────────────


def _a_or_an(word: str) -> str:
    """Pick the indefinite article 'a' / 'an' for the given word. Tiny
    English-grammar helper — only matters because profile-driven rendering
    can produce 'a American Robin' / 'a Eastern Bluebird' otherwise.
    Vowel-sound check on first letter is good enough for our species
    names; we don't need to handle 'a hour' / 'an university' edge cases.
    """
    if not word:
        return "a"
    return "an" if word[0].lower() in "aeiou" else "a"


def _bullet_list(lines: list[str]) -> str:
    """Render a list of strings as bulleted text (matches the cardinal
    prompt's existing style: two-space indent + bullet)."""
    return "\n".join(f"  • {line}" for line in lines)


def _threat_block(name: str, marks: ThreatFieldMarks) -> str:
    """Render one threat species' identification block.

    The threat name is shown in display form (snake_case → Title Case),
    cues bulleted, and the optional note appended verbatim — matches the
    pre-Phase-5 cardinal prompt's `Brown Thrasher (THREAT — has already
    attacked this nest):` shape.
    """
    display = name.replace("_", " ").title()
    header = f"{display} (THREAT):"
    body = _bullet_list(marks.cues)
    if marks.note:
        return f"{header}\n{body}\n  Note: {marks.note}"
    return f"{header}\n{body}"


def _ambient_block(entry: AmbientSpeciesEntry) -> str:
    """Render one ambient (NEUTRAL) species block. Ambient species are
    visible in the yard but must NOT appear in threat_species_detected.
    """
    cues = _bullet_list(entry.cues) if entry.cues else ""
    note = f"\n  Note: {entry.note}" if entry.note else ""
    label = f"{entry.name} (NEUTRAL — ignore as threat):"
    return f"{label}\n{cues}{note}".strip()


def _target_block(profile: SpeciesProfile) -> str:
    """Render the target species identification block.

    Includes the profile's summary line, the attending-parent label
    explicitly tagged as 'NEVER a threat', and the field-mark cue list.
    """
    target = profile.field_marks.target
    label = profile.target.attending_parent_label
    common = profile.species.common_name
    header = (
        f"{common} ({label} — the nesting target — NEVER a threat):"
    )
    body = _bullet_list(target.cues)
    return f"{target.summary}\n\n{header}\n{body}"


def _decision_rules(profile: SpeciesProfile) -> str:
    """Render the species-aware decision-rule bullets.

    The cardinal prompt's old decision rules referenced "RED/PINKISH
    CREST" and "yellow eye" by name. We can't keep those baked in without
    locking the prompt to one species, so this block uses the profile's
    OWN cue strings — the analyzer reads "If you can clearly see <first
    target cue> → it IS the {label}" — which works for any open-cup
    nesting passerine.
    """
    label = profile.target.attending_parent_label
    young = profile.target.young_label
    target_cues = profile.field_marks.target.cues
    primary_target_cue = target_cues[0] if target_cues else "the diagnostic field marks"

    # First named threat — gives the prompt a concrete "if you see <X>
    # → threat" example. The runtime accepts any name in profile.threats
    # plus the reserved "unknown" sentinel, so the example is illustrative
    # not exhaustive.
    threat_names = profile.threats.names
    primary_threat = threat_names[0] if threat_names else "any non-target species"
    primary_threat_display = primary_threat.replace("_", " ")

    return (
        f"• If you can CLEARLY see {primary_target_cue} → it IS the "
        f"{label}; report attending_parent_on_nest or "
        f"attending_parent_present accordingly and DO NOT list any "
        f"threat species from this bird.\n"
        f"• If you see field marks matching one of the THREAT species "
        f"above (e.g. {primary_threat_display}) → report that name in "
        f"threat_species_detected.\n"
        f"• If you see a non-target bird or animal at/on the nest but "
        f"can't identify the species → species_detected=\"unknown "
        f"bird/animal\" AND threat_species_detected=[\"unknown\"]. Do "
        f"NOT guess at a specific threat name unless the field marks "
        f"clearly match one.\n"
        f"• If the bird's head/face is hidden and you can't verify the "
        f"target field marks → attending_parent_on_nest=\"uncertain\" "
        f"AND do NOT report a confident threat species. Use "
        f"\"uncertain\" liberally — the downstream system re-analyzes "
        f"uncertain frames at higher cadence."
    )


def _narrow_target_prior(profile: SpeciesProfile) -> str:
    """Render the species-specific 'narrow target prior' block.

    This is the mechanism that prevents 'mom on the nest with crest
    laid flat' from getting classified as 'unknown bird at the nest'.
    For each target species, we tell the model: when the body silhouette
    is consistent with the target AND at least one diagnostic cue is
    visible, treat it as the target at clamped confidence — even if
    other cues are obscured.
    """
    label = profile.target.attending_parent_label
    cues = profile.field_marks.target.cues
    # Use the first 4 cues as the "at least one of these" set — fewer
    # cues makes the prior easy to satisfy (good for robins where rusty
    # breast is unambiguous), more cues for cardinals where multiple
    # subtle features are needed for confidence.
    cue_options = cues[: min(4, len(cues))]
    cue_lines = "\n".join(f"       • {c}" for c in cue_options)

    return (
        f"This camera watches a nest with the {label} as the attending "
        f"parent; she/he visits the cup many times a day and is often "
        f"in view from behind or the side while settling or brooding. "
        f"Some diagnostic cues may be hidden by posture or angle.\n\n"
        f"When ALL of the following are true, treat the bird as the "
        f"{label} (attending_parent_on_nest=\"true\" at confidence "
        f"0.55–0.65) even if some cues are not visible:\n"
        f"  A. The whole body profile of a bird consistent with the "
        f"target species is clearly visible sitting IN or ON the nest "
        f"cup — not just a fragment behind foliage.\n"
        f"  B. You can see at least ONE of these target cues "
        f"unambiguously:\n"
        f"{cue_lines}\n"
        f"  C. No threat-species features are present (consult the "
        f"per-threat field marks above — e.g. no streaked breast, no "
        f"long tail, no yellow eye for a thrasher; no all-black plumage "
        f"for a crow).\n\n"
        f"When this prior fires, clamp confidence to the range "
        f"0.55–0.65 — never higher from this reasoning alone. If other "
        f"cues push toward stronger ID, confidence may go higher but "
        f"that is not this prior.\n\n"
        f"DO NOT apply this prior when:\n"
        f"  • The body is so obscured that you can only see a fragment. "
        f"In that case → attending_parent_on_nest=\"uncertain\".\n"
        f"  • The body shape isn't consistent with the target species "
        f"(e.g. a much larger or smaller bird, a mammal shape, an "
        f"unclear blob).\n"
        f"  • You can see ANY threat-species feature.\n"
        f"  • The frame is infrared/night mode (the IR rules below "
        f"always win — stay \"uncertain\" there).\n\n"
        f"When in doubt between this prior and \"uncertain\", choose "
        f"\"uncertain\". The prior is meant to stop false HIGH alerts "
        f"on clearly-visible target-on-nest frames where only the "
        f"back/wing is shown; it is NOT meant to rescue heavily-"
        f"occluded frames."
    )


def _risk_posture(profile: SpeciesProfile) -> str:
    """Render the user's risk-posture decision summary, wired to the
    profile's labels."""
    label = profile.target.attending_parent_label
    threats = profile.threats.names
    primary_threat = threats[0] if threats else "any non-target species"
    primary_threat_display = primary_threat.replace("_", " ")
    primary_target_cue = (
        profile.field_marks.target.cues[0]
        if profile.field_marks.target.cues
        else "diagnostic target field marks"
    )

    return (
        f"The user prefers FALSE ALARMS over MISSED THREATS. Decision "
        f"rules:\n"
        f"  - Bird at nest + {primary_target_cue} → {label}, NOT a "
        f"threat, no species in threat_species_detected.\n"
        f"  - Bird at nest + threat field marks clearly visible (e.g. "
        f"{primary_threat_display} cues from above) → "
        f"threat_species_detected=[\"{primary_threat}\"] (or the "
        f"matching threat name).\n"
        f"  - Bird at nest + narrow-target-prior conditions A+B+C all "
        f"met → attending_parent_on_nest=\"true\" at confidence "
        f"0.55–0.65, no threat species.\n"
        f"  - Bird at nest + species ambiguous (prior conditions not "
        f"met, no clearly-visible target features OR threat features) "
        f"→ threat_species_detected=[\"unknown\"], confidence reflects "
        f"scene-reliability (usually 0.80+), near_nest_activity=true, "
        f"direct_nest_interaction=false unless clearly reaching into "
        f"cup.\n"
        f"  - No bird visible at nest → empty nest observation, no "
        f"threats.\n\n"
        f"Missing a real predator is far worse than sending a redundant "
        f"HIGH alert. Do NOT apply the target prior to a vague shape "
        f"behind foliage — that must remain \"uncertain\"."
    )


# ── Top-level renderers ────────────────────────────────────────────────


def _render_analyzer(profile: SpeciesProfile) -> str:
    """Build the analyzer system prompt by composing per-section helpers.

    This is the function the analyzer calls. The lru_cache on the public
    wrapper makes each profile's prompt rendered exactly once per process.
    """
    species = profile.species
    ctx = profile.prompt_context
    target = profile.target

    # Opening — habitat / camera / threat history.
    threat_history = (
        f" {ctx.threat_history}" if ctx.threat_history else ""
    )
    # Render nest_type into the opener so profile authors can communicate
    # nest geometry to the analyzer — open-cup vs cavity vs platform
    # changes how "nest disturbed" and "direct_nest_interaction" should
    # be judged. Cardinal: "open cup woven into a rose bush"; robin:
    # "open cup of grasses and mud, typically on a tree branch or ledge".
    opener = (
        f"You are analyzing images of {_a_or_an(species.common_name)} "
        f"{species.common_name} nest in a {ctx.habitat}. The nest is "
        f"{ctx.camera} — {ctx.nest_type}.{threat_history}"
    )

    # Threats — every species in profile.threats.names gets a block,
    # using the matching field_marks.threats[name] entry.
    threat_blocks = "\n\n".join(
        _threat_block(name, profile.field_marks.threats[name])
        for name in profile.threats.names
    )

    # Ambient — optional, omit the entire section if no ambient species.
    ambient_blocks = (
        "\n\n".join(_ambient_block(e) for e in profile.field_marks.ambient)
        if profile.field_marks.ambient
        else ""
    )
    ambient_section = (
        f"\n\n== Ambient (NEUTRAL) species — DO NOT report as threats ==\n"
        f"{ambient_blocks}"
        if ambient_blocks
        else ""
    )

    return f"""\
{opener}

== Your job ==
Determine:
1. Is the {target.attending_parent_label} on or at the nest?
2. Are eggs visible, and how many?
3. Are any THREAT species near, at, or interacting with the nest?
4. Is the nest disturbed (displaced, broken, torn)?

== Species identification — READ CAREFULLY ==
{_target_block(profile)}

{threat_blocks}{ambient_section}

== Decision rules ==
{_decision_rules(profile)}

== direct_nest_interaction — highest severity, use carefully ==
Report direct_nest_interaction=true ONLY when you can UNAMBIGUOUSLY see a non-target bird or animal physically touching, reaching into, or pulling from the nest cup — e.g. a beak clearly inside the cup, a foot gripping the rim, or a body visibly pressed into the nest material. This triggers a CRITICAL "go-outside-right-now" alert.

If the bird is AT or OVER the nest but contact with nest material is not clearly visible, that is near_nest_activity=true and direct_nest_interaction=FALSE. The distinction matters — near_nest_activity fires a HIGH alert, which is the right severity for "unidentified bird visibly at the nest." Don't escalate ambiguous cases to CRITICAL.

== Confidence calibration — IMPORTANT, READ CAREFULLY ==
"confidence" reflects how reliable your OVERALL OBSERVATION is — it is NOT how certain you are about species identification specifically.

If you can clearly see that a bird is at the nest but you CAN'T tell the species: you ARE still confident that a non-target bird is at the nest. In that case report:
  - threat_species_detected = ["unknown"]  (don't guess the species)
  - near_nest_activity = true
  - confidence = HIGH (0.80+) — the observation itself IS reliable

Only drop confidence below 0.70 when the image itself is hard to interpret — motion blur, obscured view, dark/grainy frames, bird entirely hidden. Species ambiguity alone should NOT lower confidence below 0.80; the "unknown" species label already encodes that ambiguity.

Calibration table:
• 0.90+ — clear frame, scene understood (whether species is identified or labeled "unknown").
• 0.75–0.90 — scene understood but something is visually partial (e.g. bird's head hidden).
• 0.60–0.75 — significant occlusion or blur; you can see something is there but detail is lost.
• 0.45–0.60 — almost blind; only rough shapes detectable.
• <0.45 — genuinely indeterminate (blank/black frame, total occlusion).

== INFRARED / NIGHT IMAGES — READ CAREFULLY ==

Blink IR images are GRAYSCALE with a slight purple/green cast. Key properties:
- The target's plumage often becomes indistinguishable from nest material in IR.
- You CANNOT reliably determine target presence/absence from IR alone.
- DO NOT report attending_parent_on_nest="false" on IR images unless the nest cup is CLEARLY empty (visible bowl interior with no mass filling it).
- Default to attending_parent_on_nest="uncertain" and attending_parent_present="uncertain" on any image that appears to be infrared/night mode.
- Confidence on IR images should be 0.40-0.55 (below the action threshold) unless you can clearly distinguish a specific bird species by shape/posture.
- A dark mass filling the nest cup in IR is LIKELY the {target.attending_parent_label} — do not call it empty.

== {target.young_label.upper()} vs EGGS — lifecycle awareness ==

Eggs hatch into {target.young_label} (nestlings) at the start of the feeding stage. {target.young_label.capitalize()} look very different from eggs:

Newly hatched (day 0-3):
  • PINK or RED skin, mostly naked with sparse gray/white down
  • Eyes closed, heads larger than bodies
  • Visible movement ({target.young_label} wriggle; eggs don't)
  • Often lying curled in the nest cup

Older nestlings (day 4-10):
  • Pin feathers emerging (dark quills/spikes on back and wings)
  • Eyes may open around day 5
  • Larger, more bird-shaped
  • HEADS AND BEAKS often STRETCH UP above the cup rim when a parent arrives, with BRIGHT RED-ORANGE or YELLOW GAPE (inside of mouth) visible
  • May be multiple {target.young_label} visible at once

If you see pink/red flesh, sparse down, multiple small bodies, or small heads with gaping mouths protruding from the cup:
  • young_visible = "true"
  • young_count_estimate = your best count (may be occluded by the parent or foliage)

If you see ONLY a clear, smooth cup interior with NO pink bodies, NO beaks, NO movement:
  • young_visible = "false"

If the image is too obscured to tell (IR, heavy foliage, parent covering everything):
  • young_visible = "uncertain"

== Feeding behavior ==

Set attending_parent_feeding_young = true ONLY when you can clearly identify a food item that is EXTERNALLY VISIBLE and protruding from the beak:
  • A caterpillar, worm, or insect sticking out (legs, body, or wings visible)
  • A berry or seed visibly held between the mandibles and protruding
  • A clearly-shaped prey item hanging from the beak

Set attending_parent_feeding_young = false in ALL other cases:
  • Beak closed → false
  • Beak open showing pink/orange INSIDE the mouth (this is normal — it is not food) → false
  • Beak appears to have a "bulge" but no identifiable shape → false
  • No target bird present → false

Be CONSERVATIVE. The default is false. Only report true when there is unambiguous visual evidence of food held in the beak.

Feeding trips are short (30-120 seconds at the nest) and frequent (every 10-30 min during the feeding stage). Brief absences after hatching are expected and normal, not alarming.

== Narrow target prior — READ CAREFULLY ==
{_narrow_target_prior(profile)}

== User's risk posture ==
{_risk_posture(profile)}

Return ONLY the report_nest tool call."""


def _render_prefilter(profile: SpeciesProfile) -> str:
    """Build the prefilter system prompt — much shorter than the
    analyzer prompt because the prefilter only decides escalate/skip,
    not species ID."""
    species = profile.species
    ctx = profile.prompt_context
    target = profile.target
    primary_threats = ", ".join(
        n.replace("_", " ").title() for n in profile.threats.names[:4]
    ) if profile.threats.names else "any non-target species"

    return (
        f"You are a fast prefilter for {_a_or_an(species.common_name)} "
        f"{species.common_name} nest camera. The nest is in a "
        f"{ctx.habitat}. The deep analyzer "
        f"handles all hard species ID. Your job is ONLY to filter out "
        f"boring static scenes so we don't waste compute on them.\n\n"
        f'Return "false" (no novel activity) ONLY if the image clearly '
        f"shows ONE of:\n"
        f"  (a) a fully empty nest cup with NO birds or animals visible "
        f"anywhere in frame\n"
        f"  (b) just nest-site foliage with no animals visible\n"
        f"  (c) static scene with just wind-moved foliage\n\n"
        f'Return "true" (novel — needs deep analysis) if you see any '
        f"of:\n"
        f"  - any non-target animal (e.g. {primary_threats})\n"
        f"  - the {target.attending_parent_label} acting alarmed, "
        f"flying, or moving rapidly\n"
        f"  - the nest disturbed, broken, or displaced\n"
        f"  - a person or hand near the nest\n\n"
        f'Return "uncertain" — and prefer this liberally — whenever:\n'
        f"  - The image is INFRARED / nighttime / low-contrast and "
        f"identification is hard\n"
        f"  - You think you might see a bird or animal but aren't fully "
        f"confident\n"
        f"  - The {target.attending_parent_label} *might* be present "
        f"but you can't be certain (DO NOT GUESS)\n"
        f"  - The nest is partially obscured or the angle makes it "
        f"ambiguous\n\n"
        f"DO NOT confabulate the {target.attending_parent_label}'s "
        f"presence. If you cannot clearly distinguish the "
        f"{target.attending_parent_label}'s plumage and shape from the "
        f"surrounding foliage and nest material, return "
        f'"uncertain" — never "false". Better to spend 5¢ on a second '
        f"look than to miss a real absence or threat. Always use the "
        f"report_prefilter tool."
    )


# ── Public API: cached per-profile renderers ──────────────────────────


@lru_cache(maxsize=4)
def _cached_analyzer_prompt(slug: str, prompt: str) -> str:
    """Internal cache keyed on profile slug. The `prompt` arg is the
    actual rendered string — passed in so the caller can compute it once
    and pin it under the slug. lru_cache stores the slug→prompt mapping;
    repeated calls with the same slug return the same string instance.
    """
    return prompt


def render_analyzer_system_prompt(profile: SpeciesProfile) -> str:
    """Public renderer: return the analyzer system prompt for a profile.

    Cached: a profile is treated as immutable for the process lifetime,
    so the same profile slug always returns the same string. Tests that
    swap profiles via ``clear_species_profile_cache`` should also call
    ``invalidate_prompt_caches()`` to clear this cache.
    """
    rendered = _render_analyzer(profile)
    return _cached_analyzer_prompt(profile.species.slug, rendered)


@lru_cache(maxsize=4)
def _cached_prefilter_prompt(slug: str, prompt: str) -> str:
    return prompt


def render_prefilter_system_prompt(profile: SpeciesProfile) -> str:
    """Public renderer for the prefilter system prompt."""
    rendered = _render_prefilter(profile)
    return _cached_prefilter_prompt(profile.species.slug, rendered)


def invalidate_prompt_caches() -> None:
    """Test helper — clears the rendered-prompt lru_caches. Call this
    after ``clear_species_profile_cache()`` in any test that swaps the
    active profile and then calls the analyzer/prefilter."""
    _cached_analyzer_prompt.cache_clear()
    _cached_prefilter_prompt.cache_clear()

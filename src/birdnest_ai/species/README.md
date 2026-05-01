# Species profiles

This package is the genericization layer for the nest monitor. The
runtime started cardinal-only and grew into a profile-driven system
that any contributor can point at a different open-cup nesting
passerine without editing Python.

A species profile is a single TOML file. The runtime loads it once at
startup and treats it as immutable for the process lifetime. Every
species-specific decision the analyzer, prefilter, rules engine,
state machine, lifecycle predictor, notifier, and verifier make is
derived from the active profile. Generic Python; species-specific TOML.

## Scope

Open-cup nesting passerines with visible-nest camera geometry. That
means:

- Open-cup nests, not cavities or platforms — the analyzer prompt
  assumes the cup is the relevant scene element and the lifecycle
  state machine assumes you can see eggs and nestlings from above or
  from the side. Cavity nesters need a different prompt template
  before this system is useful for them.
- A static camera with an unobstructed (or mostly unobstructed) view
  of the nest. Motion detection is treated as supplementary; the
  scheduled-snap path is what drives the lifecycle and threat rules.
- A single target species per deployment. Multi-target monitoring is
  out of scope — pick the species that's actually nesting and run
  one profile per process.
- North American songbirds with a small ~3-7 egg clutch and a 9-16
  day fledge timeline. Larger broods or longer cycles aren't broken
  by the schema, but the lifecycle thresholds (`fledge_absence_hours`,
  `fledge_threat_free_hours`) are tuned for that range.

What this is NOT for: hawks, raptors, owls, ground-nesters, colonial
seabirds, or anything where the camera can't see the cup. The runtime
won't crash on those, but the lifecycle and threat rules won't make
sense.

## What a profile must define

```toml
[species]
slug                # filesystem-safe id, e.g. "northern_cardinal"
common_name         # "Northern Cardinal"
scientific_name     # "Cardinalis cardinalis"
display_name        # short label for Discord, e.g. "cardinal"

[target]
match_terms         # lowercased substrings the verifier matches against
                    # observation.species_detected to decide "target, not threat".
                    # e.g. ["cardinal", "northern cardinal"]
attending_parent_label  # "female cardinal", "robin parent" — used in
                        # rendered prompts, alert copy, log messages
young_label         # "chicks", "nestlings" — used in lifecycle prompts

[prompt_context]
habitat             # "backyard rose bush in a residential garden"
camera              # "low to the ground in dense foliage near the home"
nest_type           # "open cup woven into a rose bush" — informs nest-disturbed
                    # / direct_nest_interaction judgments
threat_history      # optional; appended to the analyzer opener when set

[field_marks.target]
summary             # one-line introduction the analyzer reads
cues = [...]        # bulleted distinguishing features (red crest, rusty
                    # breast, etc.). The "narrow target prior" uses the
                    # first 4 entries to clamp confidence on partial views.

[threats]
names = [...]       # snake_case canonical threat names. The runtime
                    # accepts these PLUS the reserved "unknown" sentinel
                    # in observation.threat_species_detected. Profile
                    # validator forbids "unknown" here.

[field_marks.threats.<name>]
cues = [...]        # one block per threat name; keys must match
                    # threats.names exactly (validated at load time)
note                # optional; rendered as "Note: ..." in the prompt

[[field_marks.ambient]]
name                # NEUTRAL species visible in the yard but never a threat
cues = [...]
note

[lifecycle]
egg_laying_days_min/max
incubation_days_min/max
fledge_days_min/max         # biological durations (species-specific)

sitting_ratio_threshold     # 0.50–0.95; egg_laying → incubation transition
sitting_ratio_window_hours  # 6–72; rolling window for the ratio
young_confirmation_window_hours  # 1–12; 2-sighting hatch confirmation
fledge_absence_hours        # 6–48; required no-visit window
fledge_threat_free_hours    # 12–168; required threat-free window
young_sighting_confidence_floor  # 0.50–0.95; analyzer confidence floor
                                 # for a young_visible='true' frame to
                                 # count toward hatch confirmation

[alert_copy]
egg_laying_begin_title           # 🥚 alert when building → laying
egg_laying_begin_summary
incubation_begin_title           # 🪺 alert when laying → incubation
incubation_begin_summary         # MUST contain {ratio_pct} placeholder
hatch_title                      # 🐣 alert when incubation → feeding
hatch_summary
fledge_title                     # 🦅 alert when feeding → fledging
fledge_summary
long_absence_title               # MEDIUM alert; MUST contain {bucket_mins}
attending_parent_returned_title  # LOW alert when parent visible after absence
attending_parent_returned_summary

[reference_assets]
directory                        # repo-relative path, e.g.
                                 # "evidence/reference/northern_cardinal"
target_on_nest = [...]           # filenames relative to directory
threat_examples = [...]
empty_nest = [...]
lifecycle_regression = [...]     # paths under directory; each .jpg has
                                 # a paired .expected.json
```

The full schema lives in [`_schema.py`](_schema.py). Profile
validation happens at startup via the eager bootstrap helper
(`species.loader.bootstrap_species_profile`) — a malformed profile
crashes the service immediately on any of the three entry paths
(`run_combined`, `run_downloader_service`, `run_analyzer_service`)
before any loops launch.

## Bundled profiles

| Slug | Status | Notes |
|---|---|---|
| `northern_cardinal` | tuned reference | The original cardinal deployment. Captures the exact alert phrasing and species-ID guidance the system shipped with before the genericization. Cardinal output remains byte-identical to pre-refactor for alert decisions, rule_id selection, titles, summaries, and species lists. |
| `american_robin` | structural proof | Validates that the profile-driven runtime works for a different open-cup passerine. Different threat list (American Crow, Cooper's Hawk, raccoon), different attending-parent label ("robin parent" — both sexes incubate), different lifecycle timing (~12-14 day incubation, ~13-16 day fledge). Reference assets aren't yet collected; using this profile in production requires populating `prompt_context.habitat`/`camera`, `threat_history`, and `reference_assets`. |

## Reference assets

After Phase 7 (2026-05-01), assets live under
`evidence/reference/<species_slug>/`:

```
evidence/reference/
├── northern_cardinal/
│   ├── cardinal_on_nest.jpg
│   ├── empty_nest.jpg
│   ├── historical_thrasher_1.jpg
│   ├── historical_thrasher_2.jpg
│   ├── historical_thrasher_3.jpg
│   ├── thrasher_stealing_egg_highlighted.gif
│   └── lifecycle/
│       ├── wm_chick_brood_01.jpg
│       ├── wm_chick_brood_01.expected.json
│       ├── wm_chick_brood_01.source.json
│       └── ... (13 lifecycle images total)
└── <slug>/                 (added by future profiles)
```

Each lifecycle `.jpg` has a paired `.expected.json` containing the
expected analyzer output (a partial NestObservation with the fields
the regression harness checks). `.source.json` records attribution
for Wikimedia Commons or other source images.

The flat layout (`evidence/reference/*.jpg` at the top level) is
forbidden — `tests/test_reference_assets.py::test_no_legacy_flat_reference_assets`
guards against re-introduction.

## Authoring a new profile

1. **Pick a slug.** Lowercase, underscores allowed, no spaces:
   `eastern_bluebird`, `house_wren`, `american_goldfinch`. Filesystem
   path is `species/profiles/<slug>.toml` and reference assets land
   at `evidence/reference/<slug>/`.

2. **Copy `american_robin.toml` as the starting point.** It's the
   closest thing to a "blank slate" — structurally complete but with
   placeholder habitat/camera strings and empty reference assets. The
   cardinal profile is more cardinal-specific in its alert copy and
   prompt context, so it's a worse template.

3. **Fill in the species identity** — `[species]`, `[target]`,
   `[prompt_context]`. The `attending_parent_label` should match how
   you want alerts to read in Discord ("robin parent away from nest
   for 5+ minutes"). The `match_terms` list controls the verifier's
   content-aware suppression — anything Sonnet might write in
   `species_detected` that means "this is the target species" should
   be in the list, lowercased.

4. **Populate `[field_marks.target]`** with diagnostic features. The
   `summary` line opens the species-ID block in the rendered analyzer
   prompt. The `cues` list should be ordered by reliability; the
   first 4 are used by the narrow target prior to clamp confidence
   when the bird is partially obscured.

5. **List threats and per-threat field marks.** The `threats.names`
   list and the `field_marks.threats.<name>` keys MUST match exactly;
   the schema validator enforces this. Don't include `unknown` in
   `threats.names` — the runtime adds it as a sentinel automatically.

6. **Tune `[lifecycle]`.** Biological day-ranges should match the
   species' actual nesting cycle. Detection thresholds (sitting
   ratio, confirmation window, fledge windows, confidence floor) can
   stay at defaults for most open-cup passerines; deviate only when
   you've verified the defaults misfire on this species' actual
   behavior.

7. **Write `[alert_copy]`.** Pick a voice that matches the species
   and your Discord audience. Two strings are template-validated:
   `incubation_begin_summary` MUST contain `{ratio_pct}` and
   `long_absence_title` MUST contain `{bucket_mins}`. Schema
   validators reject profiles missing those placeholders.

8. **Declare `[reference_assets]`.** Even if you don't have images
   yet, set `directory = "evidence/reference/<slug>"`. Empty arrays
   are valid for the four asset lists; the runtime tools (lifecycle
   regression, integration test fixtures) handle missing-asset
   profiles by raising clear FileNotFoundError messages rather than
   silently passing.

9. **Validate.** Drop the file at
   `src/birdnest_ai/species/profiles/<slug>.toml`, then:

   ```bash
   source venv/bin/activate
   python -c "
   from birdnest_ai.species.loader import (
       load_species_profile, builtin_profile_path,
   )
   p = load_species_profile(builtin_profile_path('<slug>'))
   print('OK:', p.species.common_name)
   "
   ```

   Any pydantic ValidationError tells you exactly which field is
   wrong.

10. **Activate.** Set `SPECIES_PROFILE_PATH` in `.env` to your
    profile path, then restart the analyzer service. The eager
    bootstrap will validate the profile at startup and log
    `loaded species profile: slug=<slug>...`.

11. **Collect reference images.** Even one good `target_on_nest`
    image and 5-10 lifecycle images make the regression harness
    useful. Add them under `evidence/reference/<slug>/` and update
    the profile's asset arrays. The
    [lifecycle regression tool](../tools/lifecycle_regression.py)
    needs at least a `lifecycle/` subdirectory with paired
    `.expected.json` files.

## Tests that pin profile contracts

| File | What it pins |
|---|---|
| `tests/test_species_profile.py` | Both shipped profiles load, validate, and have biologically reasonable values. |
| `tests/test_behavior_snapshots.py` | Alert copy, severity assignment, and rule_id taxonomy under both profiles. The chronological replay test (`tests/integration/test_replay_2026_04_17.py`) is the daily-scale invariant. |
| `tests/test_prompt_rendering.py` | Rendered analyzer + prefilter prompts include every cue, every threat, every ambient species, plus `nest_type`, `threat_history` (when set), and the target's `young_label`. Forbids gendered pronouns in the prefilter prompt. |
| `tests/test_reference_assets.py` | Cardinal asset filenames resolve on disk; lifecycle JPGs have paired `.expected.json`; flat top-level layout is forbidden. |

When adding a new profile, run `pytest tests/test_species_profile.py
tests/test_behavior_snapshots.py tests/test_prompt_rendering.py
tests/test_reference_assets.py -q` to validate the new profile against
the same contracts the shipped profiles meet.

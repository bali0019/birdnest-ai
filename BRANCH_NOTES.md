# generic-nest-monitor branch

This is the **species-profile-driven generic refactor** of the original cardinal-only monitor. It is explicitly NOT the production cardinal deployment — that lives on `main`.

## How this branch differs from `main`

| | `main` (production cardinal) | `generic-nest-monitor` (this branch) |
|---|---|---|
| Purpose | Live cardinal nest monitor in the back yard | Profile-driven framework — any open-cup nesting passerine |
| Python package | `cardinal_nest_monitor` | `birdnest_ai` |
| pip distribution name | `cardinal-nest-monitor` | `birdnest-ai` |
| Module entrypoint | `python -m cardinal_nest_monitor` | `python -m birdnest_ai` |
| State DB | `./data/state.sqlite` | `./data_generic/state.sqlite` |
| Evidence dir | `./evidence/` | `./evidence_generic/` |
| Spool dir | `./data/spool/` | `./data_generic/spool/` |
| Pause lock | `./pause.lock` | `./pause_generic.lock` |
| LaunchAgent labels | `com.cardinalnest.downloader` / `.analyzer` | `com.birdnest.downloader.generic` / `.analyzer.generic` |
| LaunchAgent plists (in repo) | `launchd/com.cardinalnest.*.plist` | `launchd/generic/com.birdnest.*.generic.plist` |
| LaunchAgent plists (loaded) | `~/Library/LaunchAgents/com.cardinalnest.*.plist` | `~/Library/LaunchAgents/com.birdnest.*.generic.plist` |
| Log files | `~/Library/Logs/cardinal-nest-monitor/*.log` | `~/Library/Logs/birdnest-ai/*.generic.log` |
| Blink credentials | `./blink_credentials.json` | `./blink_credentials_generic.json` (separate file) |
| 2FA PIN handoff path | `/tmp/cardinal_nest_blink_pin` | `/tmp/birdnest_ai_blink_pin` |

The cardinal deployment on `main` MUST remain untouched while this branch evolves. No path, DB, label, package import, OR credentials file on this branch points at the live cardinal service's state.

## Hard operational rule: never run two downloaders against the same Blink camera/account at once

This is a **runtime rule, not a filesystem rule.** The path-isolation table above prevents filesystem races, but it does NOT prevent two `snap_loop` instances from hammering the same Blink camera through the same account. That produces:

- double the Blink API request rate, which can trigger 429 throttling or account lockouts
- two separate authentication sessions consuming `client_id` slots (~10 per account — these burn easily)
- inconsistent snap cadences as both services compete for camera attention
- double the battery drain

**When the generic service is pointed at the SAME Blink account/camera as the production cardinal service, validation is limited to:**

- offline analyzer-only work (dryrun against saved JPEGs)
- parsing/rendering tests (profile loading, tool schema generation, prompt rendering)
- integration tests against the mocked analyzer

**Running a live Blink-facing downloader on this branch requires one of:**

- stopping the production `com.cardinalnest.downloader` service first (`launchctl bootout`), OR
- using a different Blink account / different camera, OR
- a dedicated test Blink account with its own camera

Do not skip this. Two downloaders against the same camera is a production incident.

## Refactor scope

Open-cup nesting passerines with visible-nest camera geometry. One nest per deployment. Species chosen at install time via a species profile loaded from `SPECIES_PROFILE_PATH`. See [`src/birdnest_ai/species/README.md`](src/birdnest_ai/species/README.md) for the profile authoring guide.

Out of scope:
- Cavity nesters (bluebirds, wrens in boxes, chickadees)
- Multi-species / multi-nest single install
- Renaming the python module on `main` (main keeps `cardinal_nest_monitor` for backwards compatibility with the running deployment)

## Status

Refactor complete (Phases 1–10). Generic core, profile-driven prompts/verifier, reorganized reference assets, species docs, and the package rename to `birdnest_ai` all shipped. Phase 11 (parallel deploy validation) deliberately deferred. See `git log` and the `generic-refactor` tag for the canonical snapshot.

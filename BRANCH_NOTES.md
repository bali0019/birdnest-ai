# generic-nest-monitor branch

This is a **separate deployment branch** for the species-profile-driven refactor. It is explicitly NOT the production cardinal monitor.

## How this branch differs from `main`

| | `main` (production) | `generic-nest-monitor` (this branch) |
|---|---|---|
| Purpose | Live cardinal nest monitor in the back yard | Framework refactor — any open-cup nesting passerine via a species profile |
| State DB | `./data/state.sqlite` | `./data_generic/state.sqlite` |
| Evidence dir | `./evidence/` | `./evidence_generic/` |
| Spool dir | `./data/spool/` | `./data_generic/spool/` |
| Pause lock | `./pause.lock` | `./pause_generic.lock` |
| LaunchAgent labels | `com.cardinalnest.downloader` / `.analyzer` | `com.cardinalnest.downloader.generic` / `.analyzer.generic` |
| LaunchAgent plists | `launchd/*.plist` | `launchd/generic/*.plist` |
| Log files | `~/Library/Logs/cardinal-nest-monitor/{downloader,analyzer}.{out,err}.log` | `~/Library/Logs/cardinal-nest-monitor/{downloader,analyzer}.generic.{out,err}.log` |
| Blink credentials | `./blink_credentials.json` | `./blink_credentials.json` — shared, same file |

The cardinal deployment on `main` MUST remain untouched while this branch evolves. No path, DB, or label on this branch points at the live cardinal service's state.

## Refactor scope

Open-cup nesting passerines with visible-nest camera geometry. One nest per deployment. Species chosen at install time via a species profile loaded from `SPECIES_PROFILE_PATH`.

Out of scope:
- Cavity nesters (bluebirds, wrens in boxes, chickadees)
- Multi-species / multi-nest single install
- Package rename (`cardinal_nest_monitor` stays)

## Status

Phase 1 complete: branch exists, paths isolated, separate LaunchAgent plists authored. Nothing else refactored yet. Phases 2 through 11 ahead.

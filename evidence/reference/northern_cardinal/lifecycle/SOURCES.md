# Lifecycle reference image sources

All images are sourced from Wikimedia Commons under Creative Commons licenses (CC BY-SA or CC BY). Each image has a companion `.source.json` file with the direct source URL and photographer credit. Attribution is preserved per license requirements.

## Purpose

These images are used by `tools/lifecycle_regression.py` to verify that the analyzer prompt correctly identifies lifecycle stages (incubation, chicks visible, chicks begging, mother caring for chicks). Before enabling `LIFECYCLE_TRACKING_ENABLED=true` in production, every image here must pass its `.expected.json` ground-truth match.

## Stage distribution (13 images)

| Stage | Count | Files |
|---|---|---|
| Incubation (mom on nest, no chicks) | 6 | wm_incubating_01-03, wm_mom_protecting_01, wm_mom_returning_01-02 |
| Hatchling (fresh chicks, pink skin) | 2 | wm_chick_hatchling_01, wm_chick_hatchling_egg_01 |
| Chicks in nest (pin feathers) | 3 | wm_chick_brood_01, wm_chick_week_old_01, wm_chick_multiple_01 |
| Chicks begging (mouths open) | 1 | wm_chicks_begging_02 |
| Chicks with mom present | 1 | wm_chicks_with_mom_01 |

## Camera angle note

All images were selected for compatibility with the project's Blink Outdoor camera perspective: side-elevated views through foliage, with the nest cup visible from above or at eye level. Top-down clinical shots of the nest interior (where eggs would be counted directly) were avoided — our camera cannot see that view.

## Why no "empty post-fledge" images?

The existing `evidence/reference/empty_nest.jpg` (outside this directory) already covers the "empty nest cup" visual. Fledge detection is time-based (no cardinal visits for 12+ hours after confirmed chick presence), not dependent on visually distinguishing a fledged empty nest from a temporarily-empty one.

## License compliance

All images fall under Wikimedia Commons Creative Commons terms. If redistributed or published, credit each photographer per their `.source.json` file. No image here uses CC BY-NC or any license that restricts commercial/derivative use.

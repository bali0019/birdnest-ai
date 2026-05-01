"""Top-level conftest for the Birdnest AI test suite.

Ensures quiet_hours is disabled by default in all unit tests so that
time-dependent rules (like the quiet-hours MEDIUM suppression) don't
produce flaky results depending on when the test suite runs.

Integration tests have their own conftest.py in tests/integration/
that additionally sets TEST_MODE=true and redirects Discord webhooks.
"""

from __future__ import annotations

import pytest

from birdnest_ai.config import get_settings


@pytest.fixture(autouse=True)
def disable_quiet_hours_for_unit_tests(monkeypatch):
    """Clear quiet_hours so unit tests are time-of-day independent.

    Tests that specifically exercise quiet-hours behavior (e.g.
    test_medium_suppressed_during_quiet_hours) override this by
    monkeypatching quiet_hours to a specific window.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "quiet_hours", "")
    yield


@pytest.fixture
def use_profile(request, monkeypatch):
    """Parametrized fixture that swaps the active species profile for a
    test body. Use as::

        @pytest.mark.parametrize(
            "use_profile",
            ["northern_cardinal", "american_robin"],
            indirect=True,
        )
        def test_something(use_profile):
            profile = use_profile  # the loaded SpeciesProfile

    Phase 8/9 — these are the behavior-level regression guards Codex
    asked for before Phase 5 starts rewriting the analyzer prompts. They
    pin alert copy and rule_id taxonomy under both profiles so any drift
    introduced by prompt rendering will surface as a test failure.
    """
    from birdnest_ai.species import (
        clear_species_profile_cache,
        get_species_profile,
    )
    from birdnest_ai.species.loader import builtin_profile_path

    slug = request.param
    monkeypatch.setattr(
        get_settings(),
        "species_profile_path",
        builtin_profile_path(slug),
    )
    clear_species_profile_cache()
    try:
        yield get_species_profile()
    finally:
        clear_species_profile_cache()

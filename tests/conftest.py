"""Top-level conftest for the Cardinal Nest Monitor test suite.

Ensures quiet_hours is disabled by default in all unit tests so that
time-dependent rules (like the quiet-hours MEDIUM suppression) don't
produce flaky results depending on when the test suite runs.

Integration tests have their own conftest.py in tests/integration/
that additionally sets TEST_MODE=true and redirects Discord webhooks.
"""

from __future__ import annotations

import pytest

from cardinal_nest_monitor.config import get_settings


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

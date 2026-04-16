"""Shared fixtures for the Cardinal Nest Monitor integration suite.

These tests post REAL Discord messages to the user's webhooks — every one
carries a ``[TEST]`` prefix via ``settings.test_mode`` so they are easy to
distinguish from genuine alerts. See
``the plan file`` Part 2 for the
design rationale.

Design decisions baked into this file:
  - ``test_mode`` is set via the ``TEST_MODE`` env var and forced on for every
    test in this package via the autouse ``enable_test_mode`` fixture.
  - Each test gets a fresh on-disk SQLite DB via ``tmp_path`` so state never
    leaks between tests.
  - The Anthropic analyzer is ALWAYS monkeypatched in each test — we never
    make real API calls from integration tests. The mocks return hand-crafted
    ``NestObservation`` fixtures that exercise every rule in ``events.py``.
  - The Discord webhook URLs come from the operator's real ``.env`` via
    ``get_settings()``. Reusing the production webhooks is deliberate (user
    approved): it means the suite exercises the FULL path, all the way to a
    real HTTP 204.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from cardinal_nest_monitor.config import get_settings
from cardinal_nest_monitor.evidence import EvidenceWriter
from cardinal_nest_monitor.notifier import Notifier
from cardinal_nest_monitor.schema import NestObservation
from cardinal_nest_monitor.state import StateStore


# Path to reference JPEGs bundled with the repo. The analyzer is mocked
# in integration tests so bytes content doesn't affect test correctness —
# but the image IS attached to the Discord post, so we match the image
# to the scenario to avoid the confusing "thrasher photo captioned as
# cardinal" UX the user flagged on 2026-04-15.
_REFERENCE_DIR: Path = (
    Path(__file__).resolve().parents[2] / "evidence" / "reference"
)
REFERENCE_THRASHER: Path = _REFERENCE_DIR / "historical_thrasher_1.jpg"
REFERENCE_CARDINAL: Path = _REFERENCE_DIR / "cardinal_on_nest.jpg"
REFERENCE_EMPTY: Path = _REFERENCE_DIR / "empty_nest.jpg"

# Back-compat name for any test that doesn't care about image content.
# Default to the thrasher (historical first attack) since it's the most
# dramatic "this is what we're protecting against" visual.
REFERENCE_JPEG_PATH: Path = REFERENCE_THRASHER


def scenario_jpeg(scenario: str) -> Path:
    """Pick the right reference image for a given test scenario so the
    Discord post's attached image VISUALLY matches the mocked analyzer
    text. scenarios: 'cardinal', 'thrasher', 'empty'. Unknown → thrasher.
    """
    mapping = {
        "cardinal": REFERENCE_CARDINAL,
        "thrasher": REFERENCE_THRASHER,
        "empty":    REFERENCE_EMPTY,
    }
    return mapping.get(scenario, REFERENCE_THRASHER)


# ────────────────────────────────────────────────────────────────────────
# Autouse test-mode toggle
# ────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def enable_test_mode(monkeypatch):
    """Force ``settings.test_mode = True`` AND redirect every Discord
    webhook to ``DISCORD_TEST_WEBHOOK_URL`` for the duration of the test.

    Clears the ``get_settings`` cache before AND after the test so the env
    var takes effect and does not leak into unrelated test runs that share
    the same interpreter (e.g. when the user runs ``pytest tests/``).

    Redirection: at the start of each test we rewrite the three Discord
    webhook settings (alerts / feed / analytics) to the single dedicated
    test webhook. This keeps the three production channels 100% clean
    during test runs — every ``[TEST]`` post lands in one place where the
    user can see them all without them bleeding into the real alert feed.

    Also: the parallel architecture-fix agent's ``Pipeline.on_image`` uses
    a bare ``settings`` name when checking ``settings.verify_alerts_with_opus``
    but never binds it locally — presumably they expected a module-level
    ``settings`` alias. We inject one here so the integration suite can
    exercise the full pipeline without modifying ``src/``. The real
    runtime calls ``get_settings()`` inside ``run()`` so production is
    unaffected by this shim.
    """
    monkeypatch.setenv("TEST_MODE", "true")
    get_settings.cache_clear()

    settings = get_settings()
    assert settings.discord_test_webhook_url, (
        "DISCORD_TEST_WEBHOOK_URL must be set in .env before running the "
        "integration suite. See CLAUDE.md §18 for the dedicated test-channel "
        "requirement."
    )
    # Redirect ALL three production webhooks to the dedicated test channel
    # so integration-test posts never reach the live alert / feed / analytics
    # channels. monkeypatch.setattr restores them on teardown.
    monkeypatch.setattr(
        settings, "discord_webhook_url", settings.discord_test_webhook_url
    )
    monkeypatch.setattr(
        settings, "discord_feed_webhook_url", settings.discord_test_webhook_url
    )
    monkeypatch.setattr(
        settings, "discord_analytics_webhook_url", settings.discord_test_webhook_url
    )
    monkeypatch.setattr(
        settings, "discord_lifecycle_webhook_url", settings.discord_test_webhook_url
    )

    # Disable quiet hours so time-dependent rules don't produce flaky
    # results when tests run overnight. Tests that specifically exercise
    # quiet-hours behavior override with their own monkeypatch.
    monkeypatch.setattr(settings, "quiet_hours", "")

    # Shim: ensure main.settings resolves even though the parallel agent's
    # on_image implementation references a bare `settings` name.
    from cardinal_nest_monitor import main as main_mod
    if not hasattr(main_mod, "settings"):
        monkeypatch.setattr(main_mod, "settings", settings, raising=False)

    yield
    get_settings.cache_clear()


# ────────────────────────────────────────────────────────────────────────
# Storage fixtures
# ────────────────────────────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path):
    """Fresh SQLite state store in a per-test temp dir."""
    s = StateStore(tmp_path / "state.sqlite")
    yield s
    s.close()


@pytest.fixture
def evidence(tmp_path):
    """Fresh evidence writer in a per-test temp dir."""
    return EvidenceWriter(tmp_path / "evidence")


# ────────────────────────────────────────────────────────────────────────
# Notifier fixtures — talk to the real Discord channels in test_mode
# ────────────────────────────────────────────────────────────────────────


@pytest.fixture
async def notifier():
    """Primary alert-channel notifier. Posts to ``discord_webhook_url``."""
    settings = get_settings()
    n = Notifier(settings.discord_webhook_url, settings.blink_camera_name)
    yield n
    await n.close()


@pytest.fixture
async def feed_notifier():
    """Feed-channel notifier. Posts to ``discord_feed_webhook_url``."""
    settings = get_settings()
    n = Notifier(settings.discord_feed_webhook_url, settings.blink_camera_name)
    yield n
    await n.close()


@pytest.fixture
async def analytics_notifier():
    """Analytics-channel notifier. Posts to ``discord_analytics_webhook_url``."""
    settings = get_settings()
    n = Notifier(settings.discord_analytics_webhook_url, settings.blink_camera_name)
    yield n
    await n.close()


# ────────────────────────────────────────────────────────────────────────
# Reference JPEG
# ────────────────────────────────────────────────────────────────────────


@pytest.fixture
def reference_jpeg_bytes() -> bytes:
    """Raw bytes of the bundled historical thrasher JPEG. Back-compat —
    new tests should prefer one of the scenario-specific fixtures below
    so the Discord post's attached image matches the mocked text."""
    assert REFERENCE_JPEG_PATH.exists(), (
        f"Reference JPEG missing at {REFERENCE_JPEG_PATH}. "
        "Integration tests need this file."
    )
    return REFERENCE_JPEG_PATH.read_bytes()


@pytest.fixture
def reference_jpeg_path() -> Path:
    """Path to the bundled historical thrasher JPEG (back-compat)."""
    return REFERENCE_JPEG_PATH


@pytest.fixture
def cardinal_jpeg_bytes() -> bytes:
    """JPEG bytes of the female cardinal on the nest (use when the mocked
    analyzer reports cardinal_on_nest=true so Discord text+image match)."""
    assert REFERENCE_CARDINAL.exists(), f"Missing {REFERENCE_CARDINAL}"
    return REFERENCE_CARDINAL.read_bytes()


@pytest.fixture
def thrasher_jpeg_bytes() -> bytes:
    """JPEG bytes of a Brown Thrasher at the nest (use for threat scenarios)."""
    assert REFERENCE_THRASHER.exists(), f"Missing {REFERENCE_THRASHER}"
    return REFERENCE_THRASHER.read_bytes()


@pytest.fixture
def empty_nest_jpeg_bytes() -> bytes:
    """JPEG bytes of an empty nest (use when analyzer says mother is absent)."""
    assert REFERENCE_EMPTY.exists(), f"Missing {REFERENCE_EMPTY}"
    return REFERENCE_EMPTY.read_bytes()


# ────────────────────────────────────────────────────────────────────────
# NestObservation builders — one per scenario we exercise
# ────────────────────────────────────────────────────────────────────────
#
# Each fixture is a zero-arg callable (factory) rather than an observation
# directly, so tests can get fresh instances and override specific fields if
# they need to (e.g. to tweak confidence or summary text).


def _base_obs_kwargs() -> dict:
    return dict(
        mother_cardinal_present="true",
        cardinal_on_nest="true",
        eggs_visible="false",
        egg_count_estimate=None,
        nest_visible=True,
        nest_disturbed="false",
        species_detected=["northern_cardinal"],
        threat_species_detected=[],
        near_nest_activity=False,
        direct_nest_interaction=False,
        confidence=0.9,
        summary="Female cardinal on the nest, quiet scene.",
    )


@pytest.fixture
def obs_on_nest():
    def _make(**overrides) -> NestObservation:
        kw = _base_obs_kwargs()
        kw.update(overrides)
        return NestObservation(**kw)

    return _make


@pytest.fixture
def obs_off_nest():
    def _make(**overrides) -> NestObservation:
        kw = _base_obs_kwargs()
        kw.update(
            mother_cardinal_present="false",
            cardinal_on_nest="false",
            species_detected=[],
            threat_species_detected=[],
            near_nest_activity=False,
            direct_nest_interaction=False,
            summary="Nest empty — mother appears to be foraging.",
        )
        kw.update(overrides)
        return NestObservation(**kw)

    return _make


@pytest.fixture
def obs_thrasher_near_nest():
    """HIGH alert scenario: thrasher at the bush, no cup contact."""
    def _make(**overrides) -> NestObservation:
        kw = _base_obs_kwargs()
        kw.update(
            mother_cardinal_present="false",
            cardinal_on_nest="false",
            species_detected=["brown_thrasher"],
            threat_species_detected=["brown_thrasher"],
            near_nest_activity=True,
            direct_nest_interaction=False,
            summary="Brown thrasher perched on the rose bush near the nest cup.",
        )
        kw.update(overrides)
        return NestObservation(**kw)

    return _make


@pytest.fixture
def obs_thrasher_direct_interaction():
    """CRITICAL alert scenario: thrasher reaching into the nest cup."""
    def _make(**overrides) -> NestObservation:
        kw = _base_obs_kwargs()
        kw.update(
            mother_cardinal_present="false",
            cardinal_on_nest="false",
            species_detected=["brown_thrasher"],
            threat_species_detected=["brown_thrasher"],
            near_nest_activity=True,
            direct_nest_interaction=True,
            summary="Brown thrasher has its beak inside the nest cup.",
        )
        kw.update(overrides)
        return NestObservation(**kw)

    return _make


@pytest.fixture
def obs_cardinal_direct_interaction():
    """Verifier-disagreement scenario: Sonnet mistakes the cardinal for a
    thrasher. Used to simulate Opus correcting a false positive.

    The "threat" fields mimic what Sonnet would say if it misidentified the
    cardinal; in practice this fixture is consumed by tests that pair it
    with an Opus mock that returns ``obs_on_nest()`` — i.e. the verifier
    correctly sees a cardinal.
    """
    def _make(**overrides) -> NestObservation:
        kw = _base_obs_kwargs()
        kw.update(
            mother_cardinal_present="false",
            cardinal_on_nest="false",
            species_detected=["brown_thrasher"],
            threat_species_detected=["brown_thrasher"],
            near_nest_activity=True,
            direct_nest_interaction=True,
            summary="Bird reaching into nest — Sonnet ID'd as thrasher (MISID).",
        )
        kw.update(overrides)
        return NestObservation(**kw)

    return _make


@pytest.fixture
def obs_mockingbird_near_nest():
    """Non-threat scenario: mockingbird near the nest. No alert expected."""
    def _make(**overrides) -> NestObservation:
        kw = _base_obs_kwargs()
        kw.update(
            mother_cardinal_present="false",
            cardinal_on_nest="false",
            species_detected=["northern_mockingbird"],
            threat_species_detected=[],
            near_nest_activity=True,
            direct_nest_interaction=False,
            summary="Mockingbird perched nearby — not a nest threat.",
        )
        kw.update(overrides)
        return NestObservation(**kw)

    return _make

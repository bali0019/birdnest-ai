"""Unit tests for config.py validators + ensure_dirs permissions.

The Discord webhook URL validator guards against typos routing alerts to
an attacker-controlled host or a silently-wrong destination. The
ensure_dirs mode check guards state.sqlite and evidence dirs against
being world-readable on a shared machine.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from pydantic import ValidationError

from birdnest_ai.config import Settings


_VALID = "https://discord.com/api/webhooks/123/abc"


def _mk_settings(**overrides) -> Settings:
    """Build a Settings without loading the project's .env."""
    return Settings(_env_file=None, **overrides)


# ── Discord webhook URL validator ──────────────────────────────────────

def test_discord_webhook_validator_accepts_valid_url() -> None:
    s = _mk_settings(discord_webhook_url=_VALID)
    assert s.discord_webhook_url == _VALID


def test_discord_webhook_validator_accepts_empty_string() -> None:
    """Empty means the feature is disabled — must still be valid."""
    s = _mk_settings(discord_webhook_url="")
    assert s.discord_webhook_url == ""


def test_discord_webhook_validator_strips_whitespace() -> None:
    s = _mk_settings(discord_webhook_url=f"  {_VALID}  ")
    assert s.discord_webhook_url == _VALID


def test_discord_webhook_validator_rejects_non_discord_host() -> None:
    """A typo or malicious override must be rejected, not silently accepted."""
    with pytest.raises(ValidationError, match="must start with"):
        _mk_settings(discord_webhook_url="https://evil.example.com/api/webhooks/123/abc")


def test_discord_webhook_validator_rejects_http_scheme() -> None:
    """Plain http is rejected — Discord only accepts https webhooks."""
    with pytest.raises(ValidationError, match="must start with"):
        _mk_settings(discord_webhook_url="http://discord.com/api/webhooks/123/abc")


def test_discord_webhook_validator_rejects_garbage() -> None:
    with pytest.raises(ValidationError, match="must start with"):
        _mk_settings(discord_webhook_url="not-a-url")


def test_discord_webhook_validator_applies_to_all_six_fields() -> None:
    """Each discord_*_webhook_url field must be validated — typo on any
    routes alerts wrong or silently drops them."""
    fields = [
        "discord_webhook_url",
        "discord_feed_webhook_url",
        "discord_analytics_webhook_url",
        "discord_lifecycle_webhook_url",
        "discord_backfill_webhook_url",
        "discord_test_webhook_url",
    ]
    for field in fields:
        with pytest.raises(ValidationError, match="must start with"):
            _mk_settings(**{field: "https://attacker.example.com/x"})
        # And each accepts a valid URL:
        s = _mk_settings(**{field: _VALID})
        assert getattr(s, field) == _VALID


# ── ensure_dirs permissions ────────────────────────────────────────────

def test_ensure_dirs_creates_with_0700_permissions(tmp_path: Path) -> None:
    """data_dir, evidence_dir, and spool_dir must be 0700 even if the
    ambient umask would produce something laxer."""
    data_dir = tmp_path / "data"
    evidence_dir = tmp_path / "evidence"
    spool_dir = tmp_path / "data" / "spool"

    s = _mk_settings(
        data_dir=data_dir,
        evidence_dir=evidence_dir,
        spool_dir=spool_dir,
    )

    # Run against a deliberately-lax umask so the chmod belt-and-suspenders
    # is doing the work (not the mkdir mode arg alone).
    old_umask = os.umask(0o022)
    try:
        s.ensure_dirs()
    finally:
        os.umask(old_umask)

    for d in (data_dir, evidence_dir, spool_dir):
        assert d.is_dir(), f"{d} was not created"
        mode = stat.S_IMODE(d.stat().st_mode)
        assert mode == 0o700, f"{d} mode is {oct(mode)}, expected 0o700"


def test_ensure_dirs_is_idempotent_and_reclamps_permissions(tmp_path: Path) -> None:
    """Second call must still leave dirs at 0700 even if something widened
    permissions between calls (e.g. a manual chmod or a lax-umask process)."""
    data_dir = tmp_path / "data"
    evidence_dir = tmp_path / "evidence"
    spool_dir = tmp_path / "data" / "spool"

    s = _mk_settings(
        data_dir=data_dir,
        evidence_dir=evidence_dir,
        spool_dir=spool_dir,
    )

    s.ensure_dirs()
    # Widen permissions to simulate drift.
    data_dir.chmod(0o755)
    evidence_dir.chmod(0o755)

    # Second call must reclamp.
    s.ensure_dirs()

    for d in (data_dir, evidence_dir, spool_dir):
        mode = stat.S_IMODE(d.stat().st_mode)
        assert mode == 0o700, f"{d} mode is {oct(mode)}, expected 0o700"

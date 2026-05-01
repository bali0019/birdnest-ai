"""Phase 7 — reference assets reorganized under
``evidence/reference/<species_slug>/``.

Tests in this file validate the on-disk asset layout against the
profile manifest:

  1. For the cardinal profile (regression gate), every filename listed
     in ``reference_assets.target_on_nest``, ``threat_examples``,
     ``empty_nest``, and ``lifecycle_regression`` resolves to a file
     that actually exists.
  2. Every lifecycle .jpg has a paired ``.expected.json``.
  3. The robin profile's reference_assets is allowed to be empty
     (assets not yet collected) — but the directory string itself must
     still be valid and the runtime must not crash when asked to
     resolve it.
  4. The lifecycle regression CLI's ``_resolve_lifecycle_dir`` returns
     the cardinal lifecycle path when the cardinal profile is active,
     and raises FileNotFoundError when the active profile's directory
     doesn't exist on disk (e.g. robin until Phase 7+).
"""

from __future__ import annotations

from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("use_profile", ["northern_cardinal"], indirect=True)
def test_cardinal_reference_assets_exist_on_disk(use_profile):
    """Every filename listed in the cardinal profile's reference_assets
    arrays must resolve to an existing file. This is the regression
    gate — Codex called out cardinal assets as the required guarantee
    while robin can stay partial."""
    profile = use_profile
    base = _REPO_ROOT / profile.reference_assets.directory

    expected = (
        list(profile.reference_assets.target_on_nest)
        + list(profile.reference_assets.threat_examples)
        + list(profile.reference_assets.empty_nest)
        + list(profile.reference_assets.lifecycle_regression)
    )
    assert expected, "cardinal profile must declare at least one reference asset"

    missing: list[str] = []
    for rel in expected:
        path = base / rel
        if not path.is_file():
            missing.append(str(path.relative_to(_REPO_ROOT)))
    assert not missing, (
        f"cardinal reference assets missing on disk:\n  "
        + "\n  ".join(missing)
    )


@pytest.mark.parametrize("use_profile", ["northern_cardinal"], indirect=True)
def test_cardinal_lifecycle_jpgs_have_paired_expected_json(use_profile):
    """Each lifecycle .jpg in the cardinal profile must have a sibling
    ``.expected.json`` next to it, so tools/lifecycle_regression.py
    has ground-truth to compare against."""
    profile = use_profile
    base = _REPO_ROOT / profile.reference_assets.directory

    missing_pairs: list[str] = []
    for rel in profile.reference_assets.lifecycle_regression:
        jpg = base / rel
        expected = jpg.with_suffix(".expected.json")
        if not expected.is_file():
            missing_pairs.append(
                f"{jpg.relative_to(_REPO_ROOT)} → "
                f"{expected.relative_to(_REPO_ROOT)} (missing)"
            )
    assert not missing_pairs, (
        "every lifecycle .jpg must have a paired .expected.json:\n  "
        + "\n  ".join(missing_pairs)
    )


@pytest.mark.parametrize("use_profile", ["american_robin"], indirect=True)
def test_robin_reference_assets_handles_empty_arrays(use_profile):
    """Robin profile ships with empty reference_assets arrays (assets
    not yet collected). The schema and runtime must accept this without
    crashing — no asset is a valid initial state for a new profile."""
    profile = use_profile
    # Empty lists are acceptable.
    assert profile.reference_assets.target_on_nest == []
    assert profile.reference_assets.threat_examples == []
    assert profile.reference_assets.empty_nest == []
    assert profile.reference_assets.lifecycle_regression == []
    # Directory string is still required.
    assert profile.reference_assets.directory


@pytest.mark.parametrize("use_profile", ["northern_cardinal"], indirect=True)
def test_lifecycle_regression_resolves_cardinal_dir(use_profile):
    """tools.lifecycle_regression._resolve_lifecycle_dir() returns the
    cardinal lifecycle path under the active cardinal profile."""
    from birdnest_ai.tools.lifecycle_regression import (
        _resolve_lifecycle_dir,
    )

    target = _resolve_lifecycle_dir()
    assert target.is_dir()
    # Must point inside the cardinal species directory, not the legacy
    # flat evidence/reference/lifecycle path.
    assert target.name == "lifecycle"
    assert target.parent.name == "northern_cardinal"


@pytest.mark.parametrize("use_profile", ["american_robin"], indirect=True)
def test_lifecycle_regression_raises_when_profile_dir_missing(use_profile):
    """When the active profile's reference directory doesn't exist on
    disk (robin's case until assets are collected), the CLI's resolver
    raises FileNotFoundError with a clear message — fails fast rather
    than silently running on zero images."""
    from birdnest_ai.tools.lifecycle_regression import (
        _resolve_lifecycle_dir,
    )

    # Robin's reference_assets.directory is "evidence/reference/
    # american_robin" which we have NOT created on disk yet.
    with pytest.raises(FileNotFoundError) as excinfo:
        _resolve_lifecycle_dir()
    msg = str(excinfo.value)
    assert "lifecycle reference directory not found" in msg
    assert "american_robin" in msg


def test_no_legacy_flat_reference_assets():
    """The Phase 7 reorg moved cardinal assets out of the flat
    ``evidence/reference/*.jpg`` layout into
    ``evidence/reference/northern_cardinal/``. Assert the flat layout is
    gone — guards against a future ADD that drops assets back at the top
    level by accident.
    """
    flat_root = _REPO_ROOT / "evidence" / "reference"
    if not flat_root.is_dir():
        # Acceptable — no reference dir at all (e.g. shallow checkout).
        return
    leaked = [
        p.name
        for p in flat_root.iterdir()
        if p.is_file()
    ]
    assert not leaked, (
        "evidence/reference/ must contain only species subdirectories, "
        f"not flat files; found: {leaked}. Move them into "
        "evidence/reference/<species_slug>/ and update the matching "
        "profile.reference_assets manifest."
    )

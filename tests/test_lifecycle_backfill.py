"""Tests for tools/lifecycle_backfill.py fixed-shape UPDATE refactor
(CLAUDE.md §30).

The backfill tool used to build its UPDATE statement by joining a list
of string fragments (one per column being written). Not exploitable
today because every fragment was a hard-coded string literal, but the
shape invited SQLi if user input ever drove column selection. The
refactor is a static SQL that always writes BOTH columns — using the
newly computed value when the column was selected for update, or the
existing value otherwise. These tests verify the COALESCE-like
behavior: writing only one column must preserve the other.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from birdnest_ai.tools import lifecycle_backfill


def _make_db(
    tmp_path: Path,
    *,
    stage: str = "incubation",
    existing_egg: float | None = None,
    existing_inc: float | None = None,
    observations: list[tuple[float, str]] | None = None,
) -> Path:
    """Build a state.sqlite that mirrors the production schema shape
    enough for the backfill tool to run against. `observations` is a
    list of (ts, observation_json) tuples."""
    db = tmp_path / "state.sqlite"
    conn = sqlite3.connect(str(db), isolation_level=None)
    try:
        conn.executescript(
            """
            CREATE TABLE observations (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts REAL NOT NULL,
              motion_triggered INTEGER NOT NULL,
              prefilter_json TEXT,
              observation_json TEXT,
              evidence_dir TEXT
            );
            CREATE TABLE state (
              id INTEGER PRIMARY KEY CHECK (id = 1),
              lifecycle_stage TEXT NOT NULL DEFAULT 'incubation',
              egg_laying_started_ts REAL,
              incubation_started_ts REAL
            );
            INSERT INTO state (id) VALUES (1);
            """
        )
        conn.execute(
            "UPDATE state SET lifecycle_stage = ?, "
            "egg_laying_started_ts = ?, incubation_started_ts = ? "
            "WHERE id = 1",
            (stage, existing_egg, existing_inc),
        )
        for ts, oj in (observations or []):
            conn.execute(
                "INSERT INTO observations (ts, motion_triggered, "
                "observation_json) VALUES (?, 0, ?)",
                (ts, oj),
            )
    finally:
        conn.close()
    return db


def _read_state(db: Path) -> sqlite3.Row:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT lifecycle_stage, egg_laying_started_ts, "
            "incubation_started_ts FROM state WHERE id = 1"
        ).fetchone()
    finally:
        conn.close()


def _run_backfill(db: Path, argv: list[str]) -> int:
    """Invoke lifecycle_backfill.main() with the given argv. Prepends
    --db so the tool targets our scratch DB rather than the real one."""
    full_argv = ["lifecycle_backfill", "--db", str(db)] + argv
    with patch.object(sys, "argv", full_argv):
        return lifecycle_backfill.main()


def _seed_min_observations(db: Path) -> None:
    """The backfill tool requires at least one observation to proceed.
    Seed a single row so we can exercise the UPDATE path without
    needing to construct real analyzer JSON."""
    conn = sqlite3.connect(str(db), isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO observations (ts, motion_triggered, observation_json) "
            "VALUES (?, 0, ?)",
            (1776131300.0, '{"confidence":0.80}'),
        )
    finally:
        conn.close()


def test_backfill_writes_only_egg_preserves_existing_incubation(tmp_path):
    """When only egg_laying_started_ts is being written (initial-write
    on an empty egg column), the incubation_started_ts column MUST be
    preserved. The static SQL achieves this by passing final_inc —
    which falls back to the existing value when inc_write is False.

    Regression guard: a naive dynamic UPDATE that only included the
    columns being written would leave incubation alone by omission.
    A single fixed-shape UPDATE that wrote both columns but passed
    None for the non-updated column would wipe the value. Neither is
    acceptable — the refactor must preserve existing values."""
    existing_inc = 1776160100.12  # 2026-04-14 05:48 EDT prod value
    db = _make_db(
        tmp_path,
        stage="incubation",
        existing_egg=None,  # egg is empty → backfill will write it
        existing_inc=existing_inc,  # already set → skip without --force
    )
    _seed_min_observations(db)

    rc = _run_backfill(
        db,
        [
            "--egg-laying-started", "2026-04-13",
        ],
    )
    assert rc == 0

    row = _read_state(db)
    # egg_laying_started_ts: written.
    assert row["egg_laying_started_ts"] is not None
    # incubation_started_ts: untouched. This is the core invariant.
    assert row["incubation_started_ts"] == pytest.approx(existing_inc), (
        "backfill must preserve incubation_started_ts when only egg_laying "
        "was being written — the fixed-shape UPDATE must pass the existing "
        "value as the inc parameter, not None"
    )


def test_backfill_writes_only_incubation_preserves_existing_egg(tmp_path):
    """Symmetric: only incubation is being written, egg must be
    preserved. Belt-and-suspenders the same invariant in the other
    direction."""
    existing_egg = 1776131300.12  # 2026-04-13 21:48 EDT prod value
    db = _make_db(
        tmp_path,
        stage="incubation",
        existing_egg=existing_egg,  # already set → skip without --force
        existing_inc=None,  # empty → backfill will write it
    )
    _seed_min_observations(db)

    rc = _run_backfill(
        db,
        [
            "--incubation-started", "2026-04-14T00:00",
        ],
    )
    assert rc == 0

    row = _read_state(db)
    assert row["incubation_started_ts"] is not None
    assert row["egg_laying_started_ts"] == pytest.approx(existing_egg), (
        "backfill must preserve egg_laying_started_ts when only "
        "incubation was being written"
    )


def test_backfill_writes_both_when_both_provided(tmp_path):
    """Sanity: supplying both overrides writes both columns."""
    db = _make_db(
        tmp_path,
        stage="incubation",
        existing_egg=None,
        existing_inc=None,
    )
    _seed_min_observations(db)

    rc = _run_backfill(
        db,
        [
            "--egg-laying-started", "2026-04-13",
            "--incubation-started", "2026-04-14T00:00",
        ],
    )
    assert rc == 0

    row = _read_state(db)
    assert row["egg_laying_started_ts"] is not None
    assert row["incubation_started_ts"] is not None


def test_backfill_dry_run_does_not_mutate_db(tmp_path):
    """--dry-run must print but never UPDATE. Preserved behavior from
    before the refactor."""
    db = _make_db(
        tmp_path,
        stage="incubation",
        existing_egg=None,
        existing_inc=None,
    )
    _seed_min_observations(db)

    rc = _run_backfill(
        db,
        [
            "--egg-laying-started", "2026-04-13",
            "--incubation-started", "2026-04-14T00:00",
            "--dry-run",
        ],
    )
    assert rc == 0

    row = _read_state(db)
    assert row["egg_laying_started_ts"] is None, "dry-run must not write"
    assert row["incubation_started_ts"] is None, "dry-run must not write"


def test_backfill_refuses_overwrite_without_force(tmp_path):
    """Both columns already set: without --force, "nothing to do" and
    no write. Preserved behavior."""
    existing_egg = 100.0
    existing_inc = 200.0
    db = _make_db(
        tmp_path,
        stage="incubation",
        existing_egg=existing_egg,
        existing_inc=existing_inc,
    )

    rc = _run_backfill(
        db,
        [
            "--egg-laying-started", "2026-04-13",
            "--incubation-started", "2026-04-14T00:00",
        ],
    )
    assert rc == 0  # graceful no-op

    row = _read_state(db)
    assert row["egg_laying_started_ts"] == pytest.approx(existing_egg)
    assert row["incubation_started_ts"] == pytest.approx(existing_inc)


def test_backfill_force_overwrites_both(tmp_path):
    """--force flips existing values when new ones are provided."""
    db = _make_db(
        tmp_path,
        stage="incubation",
        existing_egg=100.0,
        existing_inc=200.0,
    )
    _seed_min_observations(db)

    rc = _run_backfill(
        db,
        [
            "--egg-laying-started", "2026-04-13",
            "--incubation-started", "2026-04-14T00:00",
            "--force",
        ],
    )
    assert rc == 0

    row = _read_state(db)
    # Both should now differ from 100.0 / 200.0.
    assert row["egg_laying_started_ts"] != pytest.approx(100.0)
    assert row["incubation_started_ts"] != pytest.approx(200.0)

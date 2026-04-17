"""Migration tests for state.py — verify ALTER TABLE paths work on
existing-shape DBs, not just fresh scratch DBs.

Codex P2 guardrail (2026-04-17): whenever a new state column lands, the
migration must be tested against a DB that LACKS the column (simulating
the production DB before the upgrade). A fresh scratch DB passes trivially
because CREATE TABLE includes all columns; that does NOT prove the
ALTER TABLE migration path works.
"""

from __future__ import annotations

import sqlite3

import pytest

from cardinal_nest_monitor.state import StateStore


def _build_old_shape_db(db_path, columns: list[str]) -> None:
    """Create a state DB that lacks the new column(s), mimicking an
    existing production DB pre-migration."""
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        col_defs = ",\n      ".join(columns)
        conn.executescript(
            f"""
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
              {col_defs}
            );
            INSERT INTO state (id) VALUES (1);
            CREATE TABLE alerts (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts REAL NOT NULL,
              severity TEXT NOT NULL,
              rule_id TEXT NOT NULL,
              species TEXT,
              title TEXT NOT NULL,
              summary TEXT NOT NULL,
              evidence_dir TEXT
            );
            """
        )
    finally:
        conn.close()


def test_migration_on_existing_db_adds_pending_ambiguous_frame_ts_column(tmp_path):
    """Replay: a DB without pending_ambiguous_frame_ts (the production DB
    before today's deploy). Opening a StateStore on it must run the ALTER
    TABLE migration and end with the column present + queryable."""
    db = tmp_path / "existing_state.sqlite"

    # Build a DB with all COLUMNS PRIOR to this round's migration.
    # Does NOT include pending_ambiguous_frame_ts.
    old_columns = [
        "last_mother_seen_ts REAL",
        "last_known_egg_count INTEGER",
        "last_threat_seen_ts REAL",
        "last_threat_species TEXT",
        "last_alert_severity TEXT",
        "last_absence_alert_ts REAL",
        "in_absence INTEGER NOT NULL DEFAULT 0",
        "absence_started_ts REAL",
        "lifecycle_stage TEXT NOT NULL DEFAULT 'incubation'",
        "last_chick_count INTEGER",
        "hatch_detected_ts REAL",
        "fledge_detected_ts REAL",
        "last_feeding_event_ts REAL",
        "first_chick_sighting_ts REAL",
        "egg_laying_started_ts REAL",
        "incubation_started_ts REAL",
    ]
    _build_old_shape_db(db, old_columns)

    # Verify the column is NOT there before migration.
    conn = sqlite3.connect(str(db))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(state)").fetchall()}
    conn.close()
    assert "pending_ambiguous_frame_ts" not in cols, (
        "pre-migration sanity check"
    )

    # Opening a StateStore runs the idempotent migrations.
    store = StateStore(db)
    try:
        # Column must now exist.
        cur = store._conn.execute("PRAGMA table_info(state)")
        post_cols = {r[1] for r in cur.fetchall()}
        assert "pending_ambiguous_frame_ts" in post_cols, (
            "Migration must add pending_ambiguous_frame_ts to existing DB"
        )
        # NestState load must work (uses _opt so it's defensive, but exercise it).
        nest_state = store.get_state()
        assert nest_state.pending_ambiguous_frame_ts is None, (
            "New column defaults to NULL on existing rows"
        )
    finally:
        store.close()


def test_migration_idempotent_second_open(tmp_path):
    """Opening the same DB twice must not fail — ALTER TABLE errors are
    caught when the column already exists. Regression guard for the
    established migration pattern."""
    db = tmp_path / "state.sqlite"
    s1 = StateStore(db)
    s1.close()
    # Second open should not raise.
    s2 = StateStore(db)
    try:
        cur = s2._conn.execute("PRAGMA table_info(state)")
        cols = {r[1] for r in cur.fetchall()}
        assert "pending_ambiguous_frame_ts" in cols
    finally:
        s2.close()


def test_existing_db_with_real_data_survives_migration(tmp_path):
    """A production-shaped DB with real state row values (in_absence,
    last_mother_seen_ts, lifecycle_stage, etc.) must survive the migration
    intact — no data loss, no state corruption."""
    db = tmp_path / "production_state.sqlite"
    old_columns = [
        "last_mother_seen_ts REAL",
        "last_known_egg_count INTEGER",
        "last_threat_seen_ts REAL",
        "last_threat_species TEXT",
        "last_alert_severity TEXT",
        "last_absence_alert_ts REAL",
        "in_absence INTEGER NOT NULL DEFAULT 0",
        "absence_started_ts REAL",
        "lifecycle_stage TEXT NOT NULL DEFAULT 'incubation'",
        "last_chick_count INTEGER",
        "hatch_detected_ts REAL",
        "fledge_detected_ts REAL",
        "last_feeding_event_ts REAL",
        "first_chick_sighting_ts REAL",
        "egg_laying_started_ts REAL",
        "incubation_started_ts REAL",
    ]
    _build_old_shape_db(db, old_columns)

    # Seed production-like values.
    conn = sqlite3.connect(str(db), isolation_level=None)
    try:
        conn.execute(
            "UPDATE state SET "
            " last_mother_seen_ts=?, "
            " in_absence=1, "
            " lifecycle_stage=?, "
            " incubation_started_ts=?, "
            " egg_laying_started_ts=? "
            "WHERE id=1",
            (
                1776384680.95,  # ~prod last_mother_seen_ts
                "incubation",
                1776160100.12,  # 2026-04-14 05:48 EDT prod value
                1776131300.12,  # 2026-04-13 21:48 EDT prod value
            ),
        )
    finally:
        conn.close()

    store = StateStore(db)
    try:
        state = store.get_state()
        # All prior data preserved.
        assert state.in_absence is True
        assert state.lifecycle_stage == "incubation"
        assert state.last_mother_seen_ts == pytest.approx(1776384680.95)
        assert state.incubation_started_ts == pytest.approx(1776160100.12)
        assert state.egg_laying_started_ts == pytest.approx(1776131300.12)
        # New column present but NULL on existing row.
        assert state.pending_ambiguous_frame_ts is None
    finally:
        store.close()

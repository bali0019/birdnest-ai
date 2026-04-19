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


# ── RO connection for analytics-thread queries (CLAUDE.md §30) ─────────
# The analytics thread pool reads observations/alerts via a dedicated
# read-only connection. Verifies the connection exists, is distinct from
# the writer, cannot be written to, and that the public analytics methods
# route through it — so a partial-state window between the observations
# INSERT and the state UPDATE inside record() can never be observed
# across threads.


def test_analytics_ro_connection_exists_and_is_distinct(tmp_path):
    """StateStore.__init__ must open both a writer connection and a
    separate RO connection. They must not be the same object."""
    db = tmp_path / "state.sqlite"
    store = StateStore(db)
    try:
        assert hasattr(store, "_conn"), "writer connection missing"
        assert hasattr(store, "_ro_conn"), "RO connection missing"
        assert store._conn is not store._ro_conn, (
            "RO connection must be a separate sqlite3.Connection, not an alias"
        )
    finally:
        store.close()


def test_analytics_ro_connection_refuses_writes(tmp_path):
    """The RO connection must reject INSERT/UPDATE against the DB — that's
    the whole point. `mode=ro` URI yields a connection that raises
    OperationalError on any write attempt."""
    db = tmp_path / "state.sqlite"
    store = StateStore(db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            store._ro_conn.execute(
                "UPDATE state SET last_mother_seen_ts = ? WHERE id = 1",
                (12345.0,),
            )
    finally:
        store.close()


class _ExecCountingProxy:
    """Wraps a sqlite3.Connection and counts .execute() calls. sqlite3's
    Connection type refuses attribute-level monkeypatching (execute is
    a slot on the C type), so we use a proxy object instead."""

    def __init__(self, inner, bucket: dict, name: str):
        self._inner = inner
        self._bucket = bucket
        self._name = name

    def execute(self, *args, **kwargs):
        self._bucket[self._name] = self._bucket.get(self._name, 0) + 1
        return self._inner.execute(*args, **kwargs)

    def __getattr__(self, attr):
        # Fallback for anything else (close, cursor, etc.).
        return getattr(self._inner, attr)


def test_get_observations_in_window_uses_ro_connection(tmp_path):
    """get_observations_in_window must route its SELECT through the RO
    connection, not the writer. Guards against a regression where
    someone "simplifies" back to a single connection."""
    db = tmp_path / "state.sqlite"
    store = StateStore(db)
    try:
        calls: dict[str, int] = {}
        store._conn = _ExecCountingProxy(store._conn, calls, "writer")
        store._ro_conn = _ExecCountingProxy(store._ro_conn, calls, "ro")

        # Call the analytics method.
        rows = store.get_observations_in_window(0.0, 9999999999.0)
        assert rows == [], "empty DB should return empty"

        assert calls.get("ro", 0) == 1, (
            "get_observations_in_window must route exactly one SELECT "
            "through the RO connection"
        )
        assert calls.get("writer", 0) == 0, (
            "get_observations_in_window must NOT touch the writer "
            "connection (analytics thread isolation guarantee)"
        )
    finally:
        store.close()


def test_get_alerts_in_window_uses_ro_connection(tmp_path):
    """get_alerts_in_window must route its SELECT through the RO
    connection, same as get_observations_in_window."""
    db = tmp_path / "state.sqlite"
    store = StateStore(db)
    try:
        calls: dict[str, int] = {}
        store._conn = _ExecCountingProxy(store._conn, calls, "writer")
        store._ro_conn = _ExecCountingProxy(store._ro_conn, calls, "ro")

        rows = store.get_alerts_in_window(0.0, 9999999999.0)
        assert rows == [], "empty DB should return empty"
        assert calls.get("ro", 0) == 1
        assert calls.get("writer", 0) == 0
    finally:
        store.close()


def test_close_shuts_down_both_connections(tmp_path):
    """close() must close both the writer and the RO connection — leaving
    either open leaks a file handle + a sqlite3.Connection. A follow-up
    operation on either should raise ProgrammingError."""
    db = tmp_path / "state.sqlite"
    store = StateStore(db)
    store.close()
    # sqlite3 raises ProgrammingError when you use a closed connection.
    with pytest.raises(sqlite3.ProgrammingError):
        store._conn.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError):
        store._ro_conn.execute("SELECT 1")


def test_ro_connection_sees_committed_writer_state(tmp_path):
    """Belt-and-suspenders: a write on the writer connection must be
    visible through the RO connection (they must share the DB file, not
    be isolated from each other entirely). WAL snapshot semantics mean
    the RO handle may see a slightly older view than the writer within
    a transaction — in autocommit every statement commits immediately,
    so post-statement reads on the RO conn must see the committed row.
    """
    db = tmp_path / "state.sqlite"
    store = StateStore(db)
    try:
        # Write directly via the writer.
        store._conn.execute(
            "INSERT INTO observations (ts, motion_triggered, prefilter_json, "
            "observation_json, evidence_dir) VALUES (?, ?, ?, ?, ?)",
            (12345.0, 0, None, None, None),
        )
        # Read via the analytics method (routes through RO conn).
        rows = store.get_observations_in_window(0.0, 9999999999.0)
        assert len(rows) == 1
        assert rows[0]["ts"] == pytest.approx(12345.0)
    finally:
        store.close()

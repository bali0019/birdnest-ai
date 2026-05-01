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
import time

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
    TABLE migration and end with the column present + queryable.

    This fixture uses the PRE-Phase-4 cardinal-coded column names so it
    also exercises the Phase 4 RENAME COLUMN migrations (last_mother_seen_ts
    → last_attending_parent_seen_ts, etc).
    """
    db = tmp_path / "existing_state.sqlite"

    # Build a DB with all COLUMNS PRIOR to this round's migration.
    # Does NOT include pending_ambiguous_frame_ts. Uses the OLD
    # cardinal-coded names that the Phase 4 RENAMEs migrate forward.
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
    intact — no data loss, no state corruption.

    Builds the PRE-Phase-4 cardinal-coded schema and seeds values via the
    OLD column names (last_mother_seen_ts, last_chick_count,
    first_chick_sighting_ts), then asserts the values survive into the
    NEW renamed fields after StateStore opens. Without this, a self-
    rename or no-op migration silently drops absence/lifecycle continuity
    on first deploy.
    """
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

    # Seed production-like values via the PRE-Phase-4 cardinal column names.
    # The Phase 4 RENAME COLUMN migration must move these into the renamed
    # columns when StateStore opens.
    conn = sqlite3.connect(str(db), isolation_level=None)
    try:
        conn.execute(
            "UPDATE state SET "
            " last_mother_seen_ts=?, "
            " in_absence=1, "
            " lifecycle_stage=?, "
            " incubation_started_ts=?, "
            " egg_laying_started_ts=?, "
            " last_chick_count=?, "
            " first_chick_sighting_ts=? "
            "WHERE id=1",
            (
                1776384680.95,  # ~prod last_mother_seen_ts
                "incubation",
                1776160100.12,  # 2026-04-14 05:48 EDT prod value
                1776131300.12,  # 2026-04-13 21:48 EDT prod value
                3,              # last_chick_count
                1776220000.00,  # first_chick_sighting_ts
            ),
        )
    finally:
        conn.close()

    store = StateStore(db)
    try:
        state = store.get_state()
        # All prior data preserved across the Phase 4 RENAME COLUMN
        # migration — read via the NEW field names.
        assert state.in_absence is True
        assert state.lifecycle_stage == "incubation"
        assert state.last_attending_parent_seen_ts == pytest.approx(1776384680.95)
        assert state.incubation_started_ts == pytest.approx(1776160100.12)
        assert state.egg_laying_started_ts == pytest.approx(1776131300.12)
        assert state.last_young_count == 3
        assert state.first_young_sighting_ts == pytest.approx(1776220000.00)
        # New (post-Phase-4) column present but NULL on existing row.
        assert state.pending_ambiguous_frame_ts is None
        # And the OLD column names must be gone — RENAME, not ADD.
        cur = store._conn.execute("PRAGMA table_info(state)")
        cols = {r[1] for r in cur.fetchall()}
        for old in (
            "last_mother_seen_ts",
            "last_chick_count",
            "first_chick_sighting_ts",
        ):
            assert old not in cols, (
                f"old column {old!r} must be RENAMED, not duplicated alongside "
                "the new name (otherwise downstream queries silently miss "
                "live data)"
            )
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
                "UPDATE state SET last_attending_parent_seen_ts = ? WHERE id = 1",
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


# ── Transactional record() — Codex 2026-04-23 race fix ─────────────────
# record() and record_alert() must wrap their main INSERT plus the paired
# state UPDATE in a single BEGIN IMMEDIATE / COMMIT so a cross-process RO
# reader can never observe the new row without the matching derived-state
# update. Without this, the session-burst arming helper would silently
# skip arming in exactly the deploy-during-absence case it exists to
# handle (reader sees fresh observation row → also reads state.in_absence
# which is stale-False → logs "mom on nest" and never arms).


class _SqlRecordingProxy:
    """Wraps a sqlite3.Connection and records the first two tokens of
    every execute() call for transaction-ordering assertions.

    sqlite3.Connection is a C type whose ``execute`` attribute is a slot
    and cannot be monkeypatched directly — hence the proxy (same
    pattern as ``_ExecCountingProxy`` above).
    """

    def __init__(self, inner, statements: list[str]) -> None:
        self._inner = inner
        self._statements = statements

    def execute(self, sql, *args, **kwargs):
        parts = sql.strip().split()
        head = parts[0].upper() if parts else ""
        if len(parts) > 1:
            head += " " + parts[1].upper()
        self._statements.append(head)
        return self._inner.execute(sql, *args, **kwargs)

    def __getattr__(self, attr):
        return getattr(self._inner, attr)


def test_record_wraps_writes_in_transaction(tmp_path):
    """record() must issue BEGIN IMMEDIATE, then INSERT into observations,
    then UPDATE state, then COMMIT — in that order and as a single
    atomic unit. Regression guard: without this, Codex's P2 race
    (reader sees observation row before state update) silently
    re-opens.
    """
    from cardinal_nest_monitor.schema import NestObservation

    db = tmp_path / "state.sqlite"
    store = StateStore(db)

    statements: list[str] = []
    store._conn = _SqlRecordingProxy(store._conn, statements)
    try:
        obs = NestObservation(
            attending_parent_present="true",
            attending_parent_on_nest="true",
            eggs_visible="false",
            egg_count_estimate=None,
            nest_visible=True,
            nest_disturbed="false",
            species_detected=["northern_cardinal"],
            threat_species_detected=[],
            near_nest_activity=False,
            direct_nest_interaction=False,
            confidence=0.9,
            summary="On nest.",
        )
        store.record(time.time(), False, None, obs, None)
    finally:
        store.close()

    # Filter to transaction-shape statements. Reads (SELECT) and
    # PRAGMAs are fine anywhere; we only need BEGIN/INSERT/UPDATE/COMMIT
    # in the right order.
    tx = [
        s for s in statements
        if s.startswith(("BEGIN", "INSERT", "UPDATE", "COMMIT", "ROLLBACK"))
    ]
    assert any(s.startswith("BEGIN") for s in tx), (
        "record() must open an explicit transaction (BEGIN IMMEDIATE) "
        f"around its writes — saw only {tx}"
    )
    assert any(s.startswith("COMMIT") for s in tx), (
        f"record() must COMMIT the transaction on success — saw only {tx}"
    )
    begin_i = next(i for i, s in enumerate(tx) if s.startswith("BEGIN"))
    insert_i = next(
        i for i, s in enumerate(tx) if s.startswith("INSERT INTO")
    )
    update_i = next(
        i for i, s in enumerate(tx) if s.startswith("UPDATE STATE")
    )
    commit_i = next(
        i for i, s in enumerate(tx) if s.startswith("COMMIT")
    )
    assert begin_i < insert_i < update_i < commit_i, (
        f"transaction discipline violated: {tx} "
        f"(expected BEGIN < INSERT observations < UPDATE state < COMMIT)"
    )


def test_record_alert_wraps_writes_in_transaction(tmp_path):
    """Same contract for record_alert: INSERT alerts + UPDATE state must
    be atomic."""
    from cardinal_nest_monitor.schema import AlertDecision, Severity

    db = tmp_path / "state.sqlite"
    store = StateStore(db)

    statements: list[str] = []
    store._conn = _SqlRecordingProxy(store._conn, statements)
    try:
        decision = AlertDecision(
            severity=Severity.MEDIUM,
            rule_id="attending_parent_returned",
            species=[],
            title="🟢 Mom is back",
            summary="Mother cardinal returned to the nest.",
            confidence=0.9,
        )
        store.record_alert(decision, time.time(), None)
    finally:
        store.close()

    tx = [
        s for s in statements
        if s.startswith(("BEGIN", "INSERT", "UPDATE", "COMMIT", "ROLLBACK"))
    ]
    assert any(s.startswith("BEGIN") for s in tx), (
        "record_alert() must open an explicit transaction around its writes"
    )
    assert any(s.startswith("COMMIT") for s in tx), (
        "record_alert() must COMMIT on success"
    )


def test_concurrent_reader_never_sees_post_insert_pre_update_middle(tmp_path):
    """Cross-connection guarantee: while a writer is mid-record(), the RO
    reader must see EITHER the pre-record snapshot OR the post-record
    snapshot — never the inconsistent middle where the observation row
    has committed but state has not.

    This is the Codex P2 scenario. We run the writer in a background
    thread alternating mom on/off the nest, while the main thread polls
    the RO connection. For each snapshot we assert: if the newest
    observation says attending_parent_on_nest=true at high confidence, then
    state.in_absence must be False. If the writer wasn't atomic, this
    assertion would fail on some snapshots (observation committed,
    state still in_absence=True from the prior absence).
    """
    import threading

    from cardinal_nest_monitor.schema import NestObservation

    db = tmp_path / "state.sqlite"
    store = StateStore(db)

    def _obs(on_nest: bool):
        return NestObservation(
            attending_parent_present="true" if on_nest else "false",
            attending_parent_on_nest="true" if on_nest else "false",
            eggs_visible="false",
            egg_count_estimate=None,
            nest_visible=True,
            nest_disturbed="false",
            species_detected=["northern_cardinal"] if on_nest else [],
            threat_species_detected=[],
            near_nest_activity=False,
            direct_nest_interaction=False,
            confidence=0.95,
            summary="On nest." if on_nest else "Nest empty.",
        )

    baseline_ts = time.time()
    # Seed: on nest → flip to absence so state.in_absence=True.
    store.record(baseline_ts, False, None, _obs(True), None)
    store.record(baseline_ts + 200, False, None, _obs(False), None)

    stop_writer = threading.Event()
    writer_error: list[BaseException] = []

    def _writer() -> None:
        try:
            i = 0
            while not stop_writer.is_set() and i < 40:
                ts = baseline_ts + 400 + i * 200
                on_nest = (i % 2 == 0)  # alternate — stresses the race window
                store.record(ts, False, None, _obs(on_nest), None)
                i += 1
        except BaseException as e:
            writer_error.append(e)

    t = threading.Thread(target=_writer, daemon=True)
    t.start()

    inconsistencies: list[str] = []
    snapshots_seen = 0
    start = time.monotonic()
    try:
        while t.is_alive() and time.monotonic() - start < 5.0:
            cur = store._ro_conn.execute(
                "SELECT "
                " (SELECT MAX(ts) FROM observations) AS latest_obs_ts, "
                " (SELECT observation_json FROM observations "
                "  ORDER BY ts DESC LIMIT 1) AS latest_obs_json, "
                " in_absence "
                "FROM state WHERE id = 1"
            )
            row = cur.fetchone()
            if row is None:
                continue
            snapshots_seen += 1
            latest_json = row["latest_obs_json"] or ""
            in_absence_flag = bool(row["in_absence"])
            # Invariant: a confident "on nest" observation as newest
            # implies state.in_absence must NOT be True. If the writer
            # wasn't atomic, some snapshots would show latest_obs=true
            # yet in_absence=True (from the prior absence — the state
            # UPDATE hadn't committed when we peeked).
            if (
                '"attending_parent_on_nest":"true"' in latest_json
                and in_absence_flag
            ):
                inconsistencies.append(
                    f"latest_obs attending_parent_on_nest=true but state.in_absence=True"
                )
    finally:
        stop_writer.set()
        t.join(timeout=5.0)
        store.close()

    assert not writer_error, f"writer thread raised: {writer_error}"
    assert snapshots_seen >= 10, (
        f"reader didn't see enough snapshots (only {snapshots_seen}); "
        "race window may not have been exercised"
    )
    assert not inconsistencies, (
        "reader observed post-INSERT / pre-UPDATE inconsistent state — "
        f"the record() transaction was bypassed somehow. First few: "
        f"{inconsistencies[:3]}"
    )

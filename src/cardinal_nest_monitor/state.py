"""SQLite-backed temporal state for the cardinal nest monitor.

Tracks: when the mother was last seen on the nest, the last confident egg
count, the last threat sighting, and every alert sent (for cooldown lookups).
Single-row `state` table for derived fields; append-only `observations` and
`alerts` tables for full history (and for prompt-tuning later).

NOTE on the `mother_returned` rule (events.py rule 5): the caller pattern
record() → evaluate() means in_absence has already been flipped to False by
the time evaluate sees it for the returning-mother case. The events module
implements the rule best-effort using the alerts table as backstop.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

from cardinal_nest_monitor.config import get_settings
from cardinal_nest_monitor.schema import (
    AlertDecision,
    NestObservation,
    NestState,
    PrefilterResult,
    Severity,
    ThreatSpecies,
)

log = logging.getLogger(__name__)


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS observations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  motion_triggered INTEGER NOT NULL,
  prefilter_json TEXT,
  observation_json TEXT,
  evidence_dir TEXT
);
CREATE INDEX IF NOT EXISTS idx_obs_ts ON observations(ts);

CREATE TABLE IF NOT EXISTS state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  last_mother_seen_ts REAL,
  last_known_egg_count INTEGER,
  last_threat_seen_ts REAL,
  last_threat_species TEXT,
  last_alert_severity TEXT,
  last_absence_alert_ts REAL,
  in_absence INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO state (id) VALUES (1);

CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  severity TEXT NOT NULL,
  rule_id TEXT NOT NULL,
  species TEXT,
  title TEXT NOT NULL,
  summary TEXT NOT NULL,
  evidence_dir TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts);
"""


_MIN_CONFIDENCE = 0.55
_ABSENCE_ENTER_SECONDS = 120  # mother gone ≥ 2 min → considered absent


def _threat_to_str(x) -> str:
    if isinstance(x, ThreatSpecies):
        return x.value
    return str(x)


class StateStore:
    """SQLite wrapper. Synchronous; safe for use from a single asyncio task."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.db_path),
            detect_types=sqlite3.PARSE_DECLTYPES,
            check_same_thread=False,
            isolation_level=None,  # autocommit
        )
        self._conn.row_factory = sqlite3.Row
        # WAL mode: enables safe multi-process read-while-write. Required for the
        # downloader service (read-only consumer of in_absence) to run concurrently
        # with the analyzer service (writer). See plan reactive-tickling-rose.md
        # "Cadence coordination without tight coupling" + CLAUDE.md §20.
        mode = self._conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if mode.lower() != "wal":
            log.warning(
                "SQLite journal_mode is %r, not 'wal' — multi-process safety not "
                "guaranteed. Check filesystem compatibility.", mode,
            )
        self._conn.execute("PRAGMA synchronous=NORMAL")  # WAL-safe balanced durability
        self._conn.executescript(_SCHEMA_SQL)

    # ── State row helpers ──────────────────────────────────────────────
    def _load_row(self) -> sqlite3.Row:
        cur = self._conn.execute("SELECT * FROM state WHERE id = 1")
        row = cur.fetchone()
        assert row is not None, "state row should always exist"
        return row

    def _row_to_state(self, row: sqlite3.Row) -> NestState:
        sev = row["last_alert_severity"]
        return NestState(
            last_mother_seen_ts=row["last_mother_seen_ts"],
            last_known_egg_count=row["last_known_egg_count"],
            last_threat_seen_ts=row["last_threat_seen_ts"],
            last_threat_species=row["last_threat_species"],
            last_alert_severity=Severity(sev) if sev else None,
            last_absence_alert_ts=row["last_absence_alert_ts"],
            in_absence=bool(row["in_absence"]),
        )

    def get_state(self) -> NestState:
        return self._row_to_state(self._load_row())

    # ── Recording observations ─────────────────────────────────────────
    def record(
        self,
        ts: float,
        motion_triggered: bool,
        prefilter: PrefilterResult | None,
        observation: NestObservation | None,
        evidence_dir: str | None,
    ) -> NestState:
        row = self._load_row()
        last_mother_seen_ts = row["last_mother_seen_ts"]
        last_known_egg_count = row["last_known_egg_count"]
        last_threat_seen_ts = row["last_threat_seen_ts"]
        last_threat_species = row["last_threat_species"]
        in_absence = bool(row["in_absence"])

        if observation is not None and observation.confidence >= _MIN_CONFIDENCE:
            # During quiet hours, require higher confidence to flip in_absence.
            # IR night images produce unreliable 0.60-0.70 "empty nest" readings
            # that would wastefully tighten cadence to 1-min overnight.
            _quiet_now = get_settings().in_quiet_hours(
                __import__("datetime").datetime.now().time()
            )
            _conf_ok = not _quiet_now or observation.confidence >= 0.75

            if observation.cardinal_on_nest == "true" and _conf_ok:
                last_mother_seen_ts = ts
                in_absence = False
            elif observation.cardinal_on_nest == "false" and _conf_ok:
                if (
                    last_mother_seen_ts is not None
                    and (ts - last_mother_seen_ts) >= _ABSENCE_ENTER_SECONDS
                ):
                    in_absence = True
            if (
                observation.eggs_visible == "true"
                and observation.egg_count_estimate is not None
            ):
                last_known_egg_count = int(observation.egg_count_estimate)
            if observation.threat_species_detected:
                last_threat_seen_ts = ts
                last_threat_species = _threat_to_str(observation.threat_species_detected[0])

        self._conn.execute(
            "INSERT INTO observations (ts, motion_triggered, prefilter_json, observation_json, evidence_dir) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                ts,
                1 if motion_triggered else 0,
                prefilter.model_dump_json() if prefilter is not None else None,
                observation.model_dump_json() if observation is not None else None,
                evidence_dir,
            ),
        )
        self._conn.execute(
            "UPDATE state SET "
            " last_mother_seen_ts = ?, "
            " last_known_egg_count = ?, "
            " last_threat_seen_ts = ?, "
            " last_threat_species = ?, "
            " in_absence = ? "
            "WHERE id = 1",
            (
                last_mother_seen_ts,
                last_known_egg_count,
                last_threat_seen_ts,
                last_threat_species,
                1 if in_absence else 0,
            ),
        )
        return self._row_to_state(self._load_row())

    # ── Alert recording + cooldown queries ─────────────────────────────
    def record_alert(
        self,
        decision: AlertDecision,
        ts: float,
        evidence_dir: str | None,
    ) -> None:
        species_str = ",".join(decision.species) if decision.species else None
        self._conn.execute(
            "INSERT INTO alerts (ts, severity, rule_id, species, title, summary, evidence_dir) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                ts,
                decision.severity.value,
                decision.rule_id,
                species_str,
                decision.title,
                decision.summary,
                evidence_dir,
            ),
        )
        self._conn.execute(
            "UPDATE state SET last_alert_severity = ? WHERE id = 1",
            (decision.severity.value,),
        )
        if decision.rule_id == "mother_returned":
            self._conn.execute(
                "UPDATE state SET last_absence_alert_ts = ? WHERE id = 1",
                (ts,),
            )
        log.info(
            "recorded alert: %s / %s / %s",
            decision.severity.value, decision.rule_id, decision.species,
        )

    def cooldown_active(
        self,
        severity: Severity,
        species: str | None,
        window_s: int,
    ) -> bool:
        if species is None:
            cur = self._conn.execute(
                "SELECT MAX(ts) AS latest FROM alerts WHERE severity = ?",
                (severity.value,),
            )
        else:
            cur = self._conn.execute(
                "SELECT MAX(ts) AS latest FROM alerts "
                "WHERE severity = ? AND ("
                " species = ? OR species LIKE ? OR species LIKE ? OR species LIKE ?"
                ")",
                (
                    severity.value,
                    species,
                    f"{species},%",
                    f"%,{species}",
                    f"%,{species},%",
                ),
            )
        row = cur.fetchone()
        if row is None or row["latest"] is None:
            return False
        return (time.time() - float(row["latest"])) < window_s

    def latest_alert_for_species(
        self,
        species: str | None,
        window_s: int,
    ) -> tuple[Severity, float] | None:
        """Return (severity, ts) of the most recent alert for `species` within
        `window_s`, regardless of severity. Used for escalation breakthrough.
        """
        if species is None:
            cur = self._conn.execute(
                "SELECT severity, ts FROM alerts ORDER BY ts DESC LIMIT 1"
            )
        else:
            cur = self._conn.execute(
                "SELECT severity, ts FROM alerts "
                "WHERE species = ? OR species LIKE ? OR species LIKE ? OR species LIKE ? "
                "ORDER BY ts DESC LIMIT 1",
                (
                    species,
                    f"{species},%",
                    f"%,{species}",
                    f"%,{species},%",
                ),
            )
        row = cur.fetchone()
        if row is None:
            return None
        if (time.time() - float(row["ts"])) >= window_s:
            return None
        return Severity(row["severity"]), float(row["ts"])

    # ── Analytics helpers (read-only, safe for cross-thread calls) ─────
    def get_observations_in_window(
        self, start_ts: float, end_ts: float,
    ) -> list[sqlite3.Row]:
        """Return observations whose ts is in [start_ts, end_ts] ordered by ts.

        Safe to call from a worker thread — the sqlite3 connection was opened
        with check_same_thread=False. Analytics runs in a dedicated executor,
        and this is a read-only query that doesn't contend with writes.
        """
        cur = self._conn.execute(
            "SELECT id, ts, motion_triggered, prefilter_json, observation_json, evidence_dir "
            "FROM observations WHERE ts >= ? AND ts <= ? ORDER BY ts ASC",
            (start_ts, end_ts),
        )
        return cur.fetchall()

    def get_alerts_in_window(
        self, start_ts: float, end_ts: float,
    ) -> list[sqlite3.Row]:
        """Return alerts whose ts is in [start_ts, end_ts] ordered by ts."""
        cur = self._conn.execute(
            "SELECT id, ts, severity, rule_id, species, title, summary "
            "FROM alerts WHERE ts >= ? AND ts <= ? ORDER BY ts ASC",
            (start_ts, end_ts),
        )
        return cur.fetchall()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

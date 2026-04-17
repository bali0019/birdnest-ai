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
  in_absence INTEGER NOT NULL DEFAULT 0,
  absence_started_ts REAL,
  lifecycle_stage TEXT NOT NULL DEFAULT 'incubation',
  last_chick_count INTEGER,
  hatch_detected_ts REAL,
  fledge_detected_ts REAL,
  last_feeding_event_ts REAL,
  first_chick_sighting_ts REAL,
  egg_laying_started_ts REAL,
  incubation_started_ts REAL
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

        # Idempotent migration: add columns for features added after the
        # initial schema shipped. Existing DBs created before these columns
        # existed need them added in-place. ALTER TABLE throws "duplicate
        # column" when the column already exists — catch and swallow that
        # so migrations are safe to run on every startup.
        _migrations = [
            # Burst cadence (2026-04-16)
            "ALTER TABLE state ADD COLUMN absence_started_ts REAL",
            # Lifecycle tracking (2026-04-16)
            "ALTER TABLE state ADD COLUMN lifecycle_stage TEXT NOT NULL DEFAULT 'incubation'",
            "ALTER TABLE state ADD COLUMN last_chick_count INTEGER",
            "ALTER TABLE state ADD COLUMN hatch_detected_ts REAL",
            "ALTER TABLE state ADD COLUMN fledge_detected_ts REAL",
            "ALTER TABLE state ADD COLUMN last_feeding_event_ts REAL",
            # 2-sighting hatch confirmation (2026-04-16, Step 9)
            "ALTER TABLE state ADD COLUMN first_chick_sighting_ts REAL",
            # 6-stage lifecycle expansion (2026-04-16): add building_nest and
            # egg_laying stages with their own started_ts timestamps. Backfill
            # tool (tools/lifecycle_backfill.py) populates these for the
            # existing production DB from observation history.
            "ALTER TABLE state ADD COLUMN egg_laying_started_ts REAL",
            "ALTER TABLE state ADD COLUMN incubation_started_ts REAL",
        ]
        for sql in _migrations:
            try:
                self._conn.execute(sql)
            except sqlite3.OperationalError as e:
                msg = str(e).lower()
                if "duplicate column" not in msg and "already exists" not in msg:
                    raise

    # ── State row helpers ──────────────────────────────────────────────
    def _load_row(self) -> sqlite3.Row:
        cur = self._conn.execute("SELECT * FROM state WHERE id = 1")
        row = cur.fetchone()
        assert row is not None, "state row should always exist"
        return row

    def _row_to_state(self, row: sqlite3.Row) -> NestState:
        sev = row["last_alert_severity"]
        # Defensive column access — handles both the current schema and
        # older DBs that might be mid-migration.
        def _opt(col: str, default=None):
            try:
                return row[col]
            except (IndexError, KeyError):
                return default
        absence_started_ts = _opt("absence_started_ts")
        lifecycle_stage = _opt("lifecycle_stage", "incubation") or "incubation"
        last_chick_count = _opt("last_chick_count")
        hatch_detected_ts = _opt("hatch_detected_ts")
        fledge_detected_ts = _opt("fledge_detected_ts")
        last_feeding_event_ts = _opt("last_feeding_event_ts")
        first_chick_sighting_ts = _opt("first_chick_sighting_ts")
        egg_laying_started_ts = _opt("egg_laying_started_ts")
        incubation_started_ts = _opt("incubation_started_ts")
        return NestState(
            last_mother_seen_ts=row["last_mother_seen_ts"],
            last_known_egg_count=row["last_known_egg_count"],
            last_threat_seen_ts=row["last_threat_seen_ts"],
            last_threat_species=row["last_threat_species"],
            last_alert_severity=Severity(sev) if sev else None,
            last_absence_alert_ts=row["last_absence_alert_ts"],
            in_absence=bool(row["in_absence"]),
            lifecycle_stage=lifecycle_stage,
            last_chick_count=last_chick_count,
            hatch_detected_ts=hatch_detected_ts,
            fledge_detected_ts=fledge_detected_ts,
            last_feeding_event_ts=last_feeding_event_ts,
            first_chick_sighting_ts=first_chick_sighting_ts,
            egg_laying_started_ts=egg_laying_started_ts,
            incubation_started_ts=incubation_started_ts,
            absence_started_ts=absence_started_ts,
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
        prev_in_absence = in_absence

        def _opt(col: str, default=None):
            try:
                return row[col]
            except (IndexError, KeyError):
                return default

        absence_started_ts = _opt("absence_started_ts")
        lifecycle_stage = _opt("lifecycle_stage", "incubation") or "incubation"
        prev_lifecycle_stage = lifecycle_stage
        last_chick_count = _opt("last_chick_count")
        hatch_detected_ts = _opt("hatch_detected_ts")
        fledge_detected_ts = _opt("fledge_detected_ts")
        last_feeding_event_ts = _opt("last_feeding_event_ts")
        first_chick_sighting_ts = _opt("first_chick_sighting_ts")
        egg_laying_started_ts = _opt("egg_laying_started_ts")
        incubation_started_ts = _opt("incubation_started_ts")

        if observation is not None and observation.confidence >= _MIN_CONFIDENCE:
            # During quiet hours OR whenever the camera is in IR mode, require
            # higher confidence (≥0.75) to flip in_absence or update presence.
            # IR night images produce unreliable 0.60-0.70 "empty nest" readings
            # that would wastefully tighten cadence to 1-min and potentially
            # fire false MEDIUMs. The IR check covers the sunset→23:00 gap
            # when the camera has switched to IR but quiet_hours hasn't begun.
            from datetime import datetime as _dt
            from cardinal_nest_monitor.events import observation_indicates_ir_mode
            _quiet_now = get_settings().in_quiet_hours(
                _dt.fromtimestamp(ts).time()
            )
            _ir_now = observation_indicates_ir_mode(observation)
            _conf_ok = (not _quiet_now and not _ir_now) or observation.confidence >= 0.75

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

        # Track the absence-onset timestamp so the downloader's burst-cadence
        # path knows how long mom has been gone. Only mutate on transitions:
        #   False → True  → set to ts (absence started now)
        #   True  → False → clear to None (she's back)
        #   unchanged     → leave as-is (preserve original onset)
        if in_absence and not prev_in_absence:
            absence_started_ts = ts
        elif prev_in_absence and not in_absence:
            absence_started_ts = None
        # else: no change, absence_started_ts stays whatever it was on load.

        # ── Lifecycle stage transitions (flag-gated) ──────────────────────
        # When lifecycle_tracking_enabled is False (default), these
        # transitions are dormant — lifecycle_stage stays at "incubation"
        # forever, and no chick/feeding state is recorded. That keeps the
        # existing production behavior byte-identical until the flag flips.
        if (
            get_settings().lifecycle_tracking_enabled
            and observation is not None
            and observation.confidence >= _MIN_CONFIDENCE
        ):
            # Update chick count when chicks are confidently visible.
            if observation.chicks_visible == "true" and observation.chick_count_estimate is not None:
                last_chick_count = int(observation.chick_count_estimate)

            # Feeding event — latest timestamp. Used downstream to suppress
            # MEDIUM long-absence alerts for a cooldown window.
            if observation.mother_feeding_chicks:
                last_feeding_event_ts = ts

            # Transition: building_nest → egg_laying
            # Trigger: first confident cardinal_on_nest=true observation.
            # During egg laying, the female sits briefly (1/day for 3-4 days)
            # to lay. The first sustained sitting is our signal that laying
            # has begun. We only ever see this transition for future broods;
            # the current monitored brood was already past building_nest when
            # monitoring started (backfill tool sets egg_laying_started_ts).
            if (
                lifecycle_stage == "building_nest"
                and observation.cardinal_on_nest == "true"
            ):
                lifecycle_stage = "egg_laying"
                if egg_laying_started_ts is None:
                    egg_laying_started_ts = ts
                log.info(
                    "lifecycle: transitioning building_nest → egg_laying at ts=%.0f",
                    ts,
                )

            # Transition: egg_laying → incubation
            # Trigger: ≥70% cardinal_on_nest=true ratio over a 24h rolling
            # window of confident observations. During egg laying, the female
            # visits briefly to lay one egg per day; sustained sitting means
            # the final egg is down and full incubation has begun (~12 days
            # to hatch). The 70% threshold is deliberately lenient vs real
            # incubation (~95% on-nest when mom is awake) because it
            # INCLUDES quiet-hours gaps, her natural ~5% foraging time, and
            # analyzer IR misreads. See CLAUDE.md §23 for rationale.
            # Only runs when we have enough history — silent no-op otherwise.
            if (
                lifecycle_stage == "egg_laying"
                and egg_laying_started_ts is not None
                and (ts - egg_laying_started_ts) >= 24 * 3600
            ):
                cur = self._conn.execute(
                    "SELECT observation_json FROM observations "
                    "WHERE ts >= ? AND ts <= ? AND observation_json IS NOT NULL",
                    (ts - 24 * 3600, ts),
                )
                confident_total = 0
                confident_on_nest = 0
                for r in cur.fetchall():
                    oj = r["observation_json"]
                    if not oj:
                        continue
                    # Cheap string match — avoids json.loads on every snap.
                    if '"confidence":' not in oj:
                        continue
                    # Only count observations that were confident enough to
                    # trust ("confidence": 0.55+). Approximated by checking
                    # the string for any confidence >= 0.55 — we match the
                    # threshold used elsewhere in this file.
                    if '"cardinal_on_nest":"true"' in oj:
                        confident_on_nest += 1
                        confident_total += 1
                    elif '"cardinal_on_nest":"false"' in oj:
                        confident_total += 1
                    # "uncertain" doesn't count — neither in numerator nor
                    # denominator — so partial-view/IR observations neither
                    # block nor accelerate the transition.
                if confident_total >= 24:  # at least ~1 confident obs/hour
                    ratio = confident_on_nest / confident_total
                    if ratio >= 0.70:
                        lifecycle_stage = "incubation"
                        if incubation_started_ts is None:
                            incubation_started_ts = ts
                        log.info(
                            "lifecycle: transitioning egg_laying → incubation "
                            "at ts=%.0f (%.0f%% sitting over 24h, n=%d)",
                            ts, ratio * 100, confident_total,
                        )

            # Transition: incubation → feeding (with 2-sighting confirmation)
            # Requires TWO confirming chick signals within a 4-hour window
            # before transitioning. Protects against a single misread
            # triggering a false hatch alert — the analyzer sometimes sees
            # food-in-beak artifacts or misidentifies shadows.
            #
            # State machine:
            #   1st chick signal: store first_chick_sighting_ts, stay in
            #     incubation ("waiting for confirmation").
            #   2nd signal within 4h: transition to feeding, fire 🐣.
            #   No 2nd signal within 4h: reset — this sighting is stale,
            #     treat the next one as a new "1st sighting".
            _CONFIRM_WINDOW_S = 4 * 3600
            if lifecycle_stage == "incubation":
                chick_signal = (
                    observation.chicks_visible == "true"
                    or observation.mother_feeding_chicks
                )
                if chick_signal:
                    if first_chick_sighting_ts is None:
                        # 1st sighting — record and wait for confirmation.
                        first_chick_sighting_ts = ts
                        log.info(
                            "lifecycle: 1st chick sighting at ts=%.0f; "
                            "waiting for confirmation within 4h",
                            ts,
                        )
                    elif (ts - first_chick_sighting_ts) <= _CONFIRM_WINDOW_S:
                        # 2nd sighting within window — CONFIRMED, transition.
                        lifecycle_stage = "feeding"
                        if hatch_detected_ts is None:
                            hatch_detected_ts = ts
                        first_chick_sighting_ts = None  # clear (we've committed)
                        log.info(
                            "lifecycle: chick sighting CONFIRMED — "
                            "transitioning incubation → feeding at ts=%.0f",
                            ts,
                        )
                    else:
                        # 1st sighting was too long ago — treat THIS as the
                        # new 1st sighting, restart the window.
                        log.info(
                            "lifecycle: prior chick sighting stale "
                            "(%.0fs ago); restarting confirmation window",
                            ts - first_chick_sighting_ts,
                        )
                        first_chick_sighting_ts = ts

            # Transition: feeding → fledging
            # Trigger: no cardinal visits for ≥12 hours AND no threat event
            # in prior 48 hours AND chicks were previously confirmed.
            # We check this by comparing ts against last_mother_seen_ts and
            # last_threat_seen_ts.
            if (
                lifecycle_stage == "feeding"
                and last_mother_seen_ts is not None
                and (ts - last_mother_seen_ts) >= 12 * 3600
                and (
                    last_threat_seen_ts is None
                    or (ts - last_threat_seen_ts) >= 48 * 3600
                )
                and hatch_detected_ts is not None
            ):
                lifecycle_stage = "fledging"
                if fledge_detected_ts is None:
                    fledge_detected_ts = ts

            # Transition: fledging → empty
            # Trigger: fledging state + 72 hours of no activity.
            if (
                lifecycle_stage == "fledging"
                and fledge_detected_ts is not None
                and (ts - fledge_detected_ts) >= 72 * 3600
            ):
                lifecycle_stage = "empty"

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
            " in_absence = ?, "
            " absence_started_ts = ?, "
            " lifecycle_stage = ?, "
            " last_chick_count = ?, "
            " hatch_detected_ts = ?, "
            " fledge_detected_ts = ?, "
            " last_feeding_event_ts = ?, "
            " first_chick_sighting_ts = ?, "
            " egg_laying_started_ts = ?, "
            " incubation_started_ts = ? "
            "WHERE id = 1",
            (
                last_mother_seen_ts,
                last_known_egg_count,
                last_threat_seen_ts,
                last_threat_species,
                1 if in_absence else 0,
                absence_started_ts,
                lifecycle_stage,
                last_chick_count,
                hatch_detected_ts,
                fledge_detected_ts,
                last_feeding_event_ts,
                first_chick_sighting_ts,
                egg_laying_started_ts,
                incubation_started_ts,
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
        ts: float | None = None,
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
        ref = ts if ts is not None else time.time()
        return (ref - float(row["latest"])) < window_s

    def latest_alert_for_species(
        self,
        species: str | None,
        window_s: int,
        ts: float | None = None,
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
        ref = ts if ts is not None else time.time()
        if (ref - float(row["ts"])) >= window_s:
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

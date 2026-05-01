"""SQLite-backed temporal state for the cardinal nest monitor.

Tracks: when the mother was last seen on the nest, the last confident egg
count, the last threat sighting, and every alert sent (for cooldown lookups).
Single-row `state` table for derived fields; append-only `observations` and
`alerts` tables for full history (and for prompt-tuning later).

NOTE on the `attending_parent_returned` rule (events.py rule 5): the caller pattern
record() → evaluate() means in_absence has already been flipped to False by
the time evaluate sees it for the returning-mother case. The events module
implements the rule best-effort using the alerts table as backstop.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from pathlib import Path

from cardinal_nest_monitor.config import get_settings
from cardinal_nest_monitor.predicates import (
    is_ambiguous_occupied_cup,
    is_confirmed_chick_sighting,
    observation_indicates_ir_mode,
)
from cardinal_nest_monitor.schema import (
    AlertDecision,
    NestObservation,
    NestState,
    PrefilterResult,
    Severity,
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
  last_attending_parent_seen_ts REAL,
  last_known_egg_count INTEGER,
  last_threat_seen_ts REAL,
  last_threat_species TEXT,
  last_alert_severity TEXT,
  last_absence_alert_ts REAL,
  in_absence INTEGER NOT NULL DEFAULT 0,
  absence_started_ts REAL,
  lifecycle_stage TEXT NOT NULL DEFAULT 'incubation',
  last_young_count INTEGER,
  hatch_detected_ts REAL,
  fledge_detected_ts REAL,
  last_feeding_event_ts REAL,
  first_young_sighting_ts REAL,
  egg_laying_started_ts REAL,
  incubation_started_ts REAL,
  pending_ambiguous_frame_ts REAL
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

# Window for confirming an ambiguous-occupied-cup frame. First matching
# frame sets pending_ambiguous_frame_ts. Second matching frame within this
# window promotes to soft presence (clear in_absence, update last_seen).
# 10 min covers the 5-min default + 1-min absence cadence with slack.
_AMBIGUOUS_CONFIRM_WINDOW_S = 10 * 60

# Pydantic's compact model_dump_json() produces "confidence":0.62 with no
# spaces. Use a regex to pull the numeric value out instead of json.loads()
# on every row — the 24h sitting-ratio scan touches hundreds of rows and
# we only need one field. Anchored on the quote to avoid matching
# "young_count_estimate":0.62 or similar.
_CONF_RE = re.compile(r'"confidence":([0-9]*\.?[0-9]+)')


def _row_passes_confidence(observation_json: str | None, floor: float = _MIN_CONFIDENCE) -> bool:
    """True when the serialized NestObservation has confidence >= `floor`.

    Used by the 24h sitting-ratio scan to exclude low-confidence IR misreads
    from the egg_laying → incubation transition. Previous substring match
    ("`confidence`:" in row) silently accepted every row regardless of
    value, which biased the inferred incubation_started_ts earlier than
    reality (Codex P2).
    """
    if not observation_json:
        return False
    m = _CONF_RE.search(observation_json)
    if m is None:
        return False
    try:
        return float(m.group(1)) >= floor
    except ValueError:
        return False


def _threat_to_str(x) -> str:
    """Normalize a threat-species entry to its canonical string form.

    Post-Phase-3 threat_species_detected is already list[str] (the
    ThreatSpecies enum is gone), so this is mostly a belt-and-suspenders
    cast for any legacy enum-like values stored on a NestObservation
    before rehydration.
    """
    if hasattr(x, "value"):
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

        # Dedicated read-only connection for analytics-thread queries
        # (CLAUDE.md §30). The writer connection runs in autocommit
        # (isolation_level=None) with check_same_thread=False, which
        # means analytics-thread reads against the same connection could
        # observe partial state between the observations-row INSERT and
        # the state-row UPDATE within record(). Opening a separate RO
        # handle via `mode=ro` URI isolates analytics reads to WAL
        # snapshots and guarantees the analytics thread never sees
        # half-committed state. All hot-path writes still use self._conn;
        # only get_observations_in_window / get_alerts_in_window use
        # self._ro_conn.
        self._ro_conn = sqlite3.connect(
            f"file:{self.db_path}?mode=ro",
            uri=True,
            check_same_thread=False,
            isolation_level=None,
        )
        self._ro_conn.row_factory = sqlite3.Row
        # Harmless if already set by the writer — ensures the RO handle
        # respects WAL so reads don't deadlock against concurrent writes.
        try:
            self._ro_conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            # mode=ro may refuse journal-mode changes on some SQLite
            # builds; the writer has already set WAL so this is safe
            # to ignore.
            pass

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
            "ALTER TABLE state ADD COLUMN hatch_detected_ts REAL",
            "ALTER TABLE state ADD COLUMN fledge_detected_ts REAL",
            "ALTER TABLE state ADD COLUMN last_feeding_event_ts REAL",
            # 6-stage lifecycle expansion (2026-04-16): add building_nest and
            # egg_laying stages with their own started_ts timestamps. Backfill
            # tool (tools/lifecycle_backfill.py) populates these for the
            # existing production DB from observation history.
            "ALTER TABLE state ADD COLUMN egg_laying_started_ts REAL",
            "ALTER TABLE state ADD COLUMN incubation_started_ts REAL",
            # Ambiguous-occupied-cup pending candidate (2026-04-17). When
            # attending_parent_on_nest=uncertain + near_nest_activity=true + no named
            # threat species, we treat the first frame as a "pending ambiguous"
            # candidate (no alert). A second consecutive matching frame within
            # the pending window = soft presence. See events.py
            # is_ambiguous_occupied_cup() for the exact predicate.
            "ALTER TABLE state ADD COLUMN pending_ambiguous_frame_ts REAL",
            # Phase 4 (2026-05-01) — generic species refactor. Rename
            # cardinal-coded runtime columns to species-neutral names. RENAME
            # runs first so DBs created on the generic-nest-monitor branch
            # before this commit migrate forward in place; the ADD COLUMN
            # below catches DBs that never had either name (and is a harmless
            # duplicate-column on fresh DBs created via _SCHEMA_SQL).
            "ALTER TABLE state RENAME COLUMN last_mother_seen_ts TO last_attending_parent_seen_ts",
            "ALTER TABLE state RENAME COLUMN last_chick_count TO last_young_count",
            "ALTER TABLE state RENAME COLUMN first_chick_sighting_ts TO first_young_sighting_ts",
            "ALTER TABLE state ADD COLUMN last_attending_parent_seen_ts REAL",
            "ALTER TABLE state ADD COLUMN last_young_count INTEGER",
            "ALTER TABLE state ADD COLUMN first_young_sighting_ts REAL",
        ]
        for sql in _migrations:
            try:
                self._conn.execute(sql)
            except sqlite3.OperationalError as e:
                msg = str(e).lower()
                # "duplicate column" / "already exists": ADD COLUMN re-runs.
                # "no such column": RENAME re-runs after the column is gone.
                if (
                    "duplicate column" not in msg
                    and "already exists" not in msg
                    and "no such column" not in msg
                ):
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
        last_young_count = _opt("last_young_count")
        hatch_detected_ts = _opt("hatch_detected_ts")
        fledge_detected_ts = _opt("fledge_detected_ts")
        last_feeding_event_ts = _opt("last_feeding_event_ts")
        first_young_sighting_ts = _opt("first_young_sighting_ts")
        egg_laying_started_ts = _opt("egg_laying_started_ts")
        incubation_started_ts = _opt("incubation_started_ts")
        pending_ambiguous_frame_ts = _opt("pending_ambiguous_frame_ts")
        return NestState(
            last_attending_parent_seen_ts=row["last_attending_parent_seen_ts"],
            last_known_egg_count=row["last_known_egg_count"],
            last_threat_seen_ts=row["last_threat_seen_ts"],
            last_threat_species=row["last_threat_species"],
            last_alert_severity=Severity(sev) if sev else None,
            last_absence_alert_ts=row["last_absence_alert_ts"],
            in_absence=bool(row["in_absence"]),
            lifecycle_stage=lifecycle_stage,
            last_young_count=last_young_count,
            hatch_detected_ts=hatch_detected_ts,
            fledge_detected_ts=fledge_detected_ts,
            last_feeding_event_ts=last_feeding_event_ts,
            first_young_sighting_ts=first_young_sighting_ts,
            egg_laying_started_ts=egg_laying_started_ts,
            incubation_started_ts=incubation_started_ts,
            pending_ambiguous_frame_ts=pending_ambiguous_frame_ts,
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
        # Stale-snap guard (Codex P1): the spool claims newest-first, so
        # during analyzer recovery after downtime, older snaps can be
        # processed AFTER newer ones. Without this guard, an old backfilled
        # snap would overwrite the single-row derived state with stale
        # truth — rolling back in_absence / absence_started_ts / lifecycle
        # stage until the next live snap lands. Fix: if ts is older than
        # the most recent observation we've already recorded, INSERT the
        # observation for history + analytics but SKIP the derived-state
        # UPDATE. Events.py still runs and can fire backfill alerts off
        # pre-state, so no alert is lost.
        cur = self._conn.execute("SELECT MAX(ts) AS latest FROM observations")
        latest_row = cur.fetchone()
        latest_ts = latest_row["latest"] if latest_row is not None else None
        is_stale = latest_ts is not None and ts < latest_ts

        row = self._load_row()
        last_attending_parent_seen_ts = row["last_attending_parent_seen_ts"]
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
        last_young_count = _opt("last_young_count")
        hatch_detected_ts = _opt("hatch_detected_ts")
        fledge_detected_ts = _opt("fledge_detected_ts")
        last_feeding_event_ts = _opt("last_feeding_event_ts")
        first_young_sighting_ts = _opt("first_young_sighting_ts")
        egg_laying_started_ts = _opt("egg_laying_started_ts")
        incubation_started_ts = _opt("incubation_started_ts")
        pending_ambiguous_frame_ts = _opt("pending_ambiguous_frame_ts")

        if observation is not None and observation.confidence >= _MIN_CONFIDENCE:
            # During quiet hours OR whenever the camera is in IR mode, require
            # higher confidence (≥0.75) to flip in_absence or update presence.
            # IR night images produce unreliable 0.60-0.70 "empty nest" readings
            # that would wastefully tighten cadence to 1-min and potentially
            # fire false MEDIUMs. The IR check covers the sunset→23:00 gap
            # when the camera has switched to IR but quiet_hours hasn't begun.
            from datetime import datetime as _dt
            _quiet_now = get_settings().in_quiet_hours(
                _dt.fromtimestamp(ts).time()
            )
            _ir_now = observation_indicates_ir_mode(observation)
            _conf_ok = (not _quiet_now and not _ir_now) or observation.confidence >= 0.75

            if observation.attending_parent_on_nest == "true" and _conf_ok:
                last_attending_parent_seen_ts = ts
                in_absence = False
            elif observation.attending_parent_on_nest == "false" and _conf_ok:
                if (
                    last_attending_parent_seen_ts is not None
                    and (ts - last_attending_parent_seen_ts) >= _ABSENCE_ENTER_SECONDS
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

        # ── Ambiguous-occupied-cup pending-candidate path (2026-04-17) ────
        # When the analyzer sees a bird visibly at the nest cup but cannot
        # confirm species (no thrasher field marks, no cardinal crest
        # visible) it correctly returns attending_parent_on_nest="uncertain" and
        # often threat_species_detected=["unknown"]. Before this logic,
        # that single frame would fire BOTH a MEDIUM (not-true) AND a HIGH
        # predator_near_nest (unknown + near_nest_activity). Drove ~20 false
        # alerts on 2026-04-17.
        #
        # Policy (Codex): first ambiguous occupied-cup frame = no alert,
        # store as pending candidate. Second consecutive matching frame
        # within AMBIGUOUS_CONFIRM_WINDOW_S = soft presence (update
        # last_attending_parent_seen_ts, clear in_absence, clear pending). Stale
        # pending (no 2nd within window) is discarded on next frame.
        # Explicit named threats (brown_thrasher, blue_jay, squirrel,
        # chipmunk) bypass this path and fire normally.
        if observation is not None:
            _is_ambig = is_ambiguous_occupied_cup(observation)
            if _is_ambig:
                _window = _AMBIGUOUS_CONFIRM_WINDOW_S
                if (
                    pending_ambiguous_frame_ts is not None
                    and (ts - pending_ambiguous_frame_ts) <= _window
                ):
                    # 2nd consecutive match within window → soft presence.
                    last_attending_parent_seen_ts = ts
                    in_absence = False
                    absence_started_ts = None
                    pending_ambiguous_frame_ts = None
                    log.info(
                        "ambig-cup: 2nd consecutive frame within %ds → "
                        "soft-presence (cleared in_absence, updated "
                        "last_attending_parent_seen_ts=%.0f)",
                        _window, ts,
                    )
                else:
                    # 1st ambig frame OR stale prior pending → new pending.
                    if pending_ambiguous_frame_ts is not None:
                        log.info(
                            "ambig-cup: prior pending stale (%.0fs ago); "
                            "restarting window",
                            ts - pending_ambiguous_frame_ts,
                        )
                    pending_ambiguous_frame_ts = ts
                    log.info(
                        "ambig-cup: 1st ambiguous frame at ts=%.0f; no "
                        "alert, pending confirmation within %ds",
                        ts, _window,
                    )
            else:
                # Not an ambiguous frame. If we had a pending candidate
                # and this frame is unambiguous (clear cardinal or clear
                # empty or real threat), clear the pending state.
                if pending_ambiguous_frame_ts is not None:
                    pending_ambiguous_frame_ts = None

        # ── Lifecycle stage transitions (flag-gated) ──────────────────────
        # When lifecycle_tracking_enabled is False (default), these
        # transitions are dormant — lifecycle_stage stays at "incubation"
        # forever, and no chick/feeding state is recorded. That keeps the
        # existing production behavior byte-identical until the flag flips.
        if (
            get_settings().lifecycle_tracking_enabled
            and observation is not None
            and observation.confidence >= _MIN_CONFIDENCE
            # Match the events.py guardrail: never advance lifecycle state
            # based on a frame where the nest isn't visible. Yard-motion or
            # heavily-obscured frames must not regress/advance stage.
            and observation.nest_visible
        ):
            # Phase 6 — pull lifecycle thresholds from the active species
            # profile. Defaults match the cardinal values that have been in
            # production since 2026-04-16; profiles for other species can
            # override these without code changes.
            from cardinal_nest_monitor.species import get_species_profile
            _lc = get_species_profile().lifecycle
            _sitting_window_s = _lc.sitting_ratio_window_hours * 3600
            _young_confirm_window_s = _lc.young_confirmation_window_hours * 3600
            _fledge_absence_s = _lc.fledge_absence_hours * 3600
            _fledge_threat_free_s = _lc.fledge_threat_free_hours * 3600

            # Update young count when young are confidently visible.
            if observation.young_visible == "true" and observation.young_count_estimate is not None:
                last_young_count = int(observation.young_count_estimate)

            # Feeding event — latest timestamp. Used downstream to suppress
            # MEDIUM long-absence alerts for a cooldown window.
            if observation.attending_parent_feeding_young:
                last_feeding_event_ts = ts

            # Transition: building_nest → egg_laying
            # Trigger: first confident attending_parent_on_nest=true observation.
            # During egg laying, the female sits briefly (1/day for 3-4 days)
            # to lay. The first sustained sitting is our signal that laying
            # has begun. We only ever see this transition for future broods;
            # the current monitored brood was already past building_nest when
            # monitoring started (backfill tool sets egg_laying_started_ts).
            if (
                lifecycle_stage == "building_nest"
                and observation.attending_parent_on_nest == "true"
            ):
                lifecycle_stage = "egg_laying"
                if egg_laying_started_ts is None:
                    egg_laying_started_ts = ts
                log.info(
                    "lifecycle: transitioning building_nest → egg_laying at ts=%.0f",
                    ts,
                )

            # Transition: egg_laying → incubation
            # Trigger: ≥70% attending_parent_on_nest=true ratio over a 24h rolling
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
                and (ts - egg_laying_started_ts) >= _sitting_window_s
            ):
                cur = self._conn.execute(
                    "SELECT observation_json FROM observations "
                    "WHERE ts >= ? AND ts <= ? AND observation_json IS NOT NULL",
                    (ts - _sitting_window_s, ts),
                )
                confident_total = 0
                confident_on_nest = 0
                for r in cur.fetchall():
                    oj = r["observation_json"]
                    # Parse the confidence numerically and filter at 0.55+.
                    # Low-confidence IR misreads must not contribute to the
                    # sitting ratio or they bias the transition earlier
                    # than the real egg_laying → incubation boundary.
                    if not _row_passes_confidence(oj):
                        continue
                    if '"attending_parent_on_nest":"true"' in oj:
                        confident_on_nest += 1
                        confident_total += 1
                    elif '"attending_parent_on_nest":"false"' in oj:
                        confident_total += 1
                    # "uncertain" doesn't count — neither in numerator nor
                    # denominator — so partial-view/IR observations neither
                    # block nor accelerate the transition.
                if confident_total >= _lc.sitting_ratio_window_hours:
                    ratio = confident_on_nest / confident_total
                    if ratio >= _lc.sitting_ratio_threshold:
                        lifecycle_stage = "incubation"
                        if incubation_started_ts is None:
                            incubation_started_ts = ts
                        log.info(
                            "lifecycle: transitioning egg_laying → incubation "
                            "at ts=%.0f (%.0f%% sitting over %dh, n=%d)",
                            ts, ratio * 100,
                            _lc.sitting_ratio_window_hours, confident_total,
                        )

            # Transition: incubation → feeding (with 2-sighting confirmation)
            # Requires TWO confirming chick signals within a 4-hour window
            # before transitioning. Protects against a single misread
            # triggering a false hatch alert — the analyzer sometimes sees
            # food-in-beak artifacts or misidentifies shadows.
            #
            # State machine:
            #   1st chick signal: store first_young_sighting_ts, stay in
            #     incubation ("waiting for confirmation").
            #   2nd signal within 4h: transition to feeding, fire 🐣.
            #   No 2nd signal within 4h: reset — this sighting is stale,
            #     treat the next one as a new "1st sighting".
            if lifecycle_stage == "incubation":
                if is_confirmed_chick_sighting(observation):
                    if first_young_sighting_ts is None:
                        # 1st sighting — record and wait for confirmation.
                        first_young_sighting_ts = ts
                        log.info(
                            "lifecycle: 1st young sighting at ts=%.0f; "
                            "waiting for confirmation within %dh",
                            ts, _lc.young_confirmation_window_hours,
                        )
                    elif (ts - first_young_sighting_ts) <= _young_confirm_window_s:
                        # 2nd sighting within window — CONFIRMED, transition.
                        lifecycle_stage = "feeding"
                        if hatch_detected_ts is None:
                            hatch_detected_ts = ts
                        first_young_sighting_ts = None  # clear (we've committed)
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
                            ts - first_young_sighting_ts,
                        )
                        first_young_sighting_ts = ts

            # Transition: feeding → fledging
            # Trigger: no cardinal visits for ≥12 hours AND no threat event
            # in prior 48 hours AND chicks were previously confirmed.
            # We check this by comparing ts against last_attending_parent_seen_ts and
            # last_threat_seen_ts.
            if (
                lifecycle_stage == "feeding"
                and last_attending_parent_seen_ts is not None
                and (ts - last_attending_parent_seen_ts) >= _fledge_absence_s
                and (
                    last_threat_seen_ts is None
                    or (ts - last_threat_seen_ts) >= _fledge_threat_free_s
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

        # Atomicity (Codex 2026-04-23): the observations INSERT and the
        # derived-state UPDATE must commit together, not as two separate
        # autocommits. Rationale: the split-process downloader reads
        # state.sqlite via its own RO connection to make cadence decisions,
        # and the session-burst arming helper specifically checks
        # MAX(observations.ts) to detect "analyzer just processed a
        # post-restart snap" before reading state.in_absence. Without a
        # transaction, the RO reader can observe the new observation row
        # while state is still pre-update — making session-burst silently
        # skip arming in exactly the deploy-during-absence case it exists
        # to handle. BEGIN IMMEDIATE takes the write lock upfront so we
        # fail fast on contention rather than mid-transaction.
        self._conn.execute("BEGIN IMMEDIATE")
        try:
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
            if is_stale:
                # Observation inserted for history; derived state untouched.
                # Commit the INSERT so the history row is durable, but no
                # state UPDATE fires. Readers never see a state that
                # regressed because of an out-of-order backfill.
                self._conn.execute("COMMIT")
                log.info(
                    "record: stale snap ts=%.0f (latest=%.0f); skipped derived-state "
                    "update", ts, latest_ts,
                )
                return self._row_to_state(self._load_row())
            self._conn.execute(
                "UPDATE state SET "
                " last_attending_parent_seen_ts = ?, "
                " last_known_egg_count = ?, "
                " last_threat_seen_ts = ?, "
                " last_threat_species = ?, "
                " in_absence = ?, "
                " absence_started_ts = ?, "
                " lifecycle_stage = ?, "
                " last_young_count = ?, "
                " hatch_detected_ts = ?, "
                " fledge_detected_ts = ?, "
                " last_feeding_event_ts = ?, "
                " first_young_sighting_ts = ?, "
                " egg_laying_started_ts = ?, "
                " incubation_started_ts = ?, "
                " pending_ambiguous_frame_ts = ? "
                "WHERE id = 1",
                (
                    last_attending_parent_seen_ts,
                    last_known_egg_count,
                    last_threat_seen_ts,
                    last_threat_species,
                    1 if in_absence else 0,
                    absence_started_ts,
                    lifecycle_stage,
                    last_young_count,
                    hatch_detected_ts,
                    fledge_detected_ts,
                    last_feeding_event_ts,
                    first_young_sighting_ts,
                    egg_laying_started_ts,
                    incubation_started_ts,
                    pending_ambiguous_frame_ts,
                ),
            )
            self._conn.execute("COMMIT")
        except Exception:
            try:
                self._conn.execute("ROLLBACK")
            except Exception:
                log.exception("record: rollback also failed (transaction state unclear)")
            raise
        return self._row_to_state(self._load_row())

    # ── Alert recording + cooldown queries ─────────────────────────────
    def record_alert(
        self,
        decision: AlertDecision,
        ts: float,
        evidence_dir: str | None,
    ) -> None:
        species_str = ",".join(decision.species) if decision.species else None
        # Same transaction discipline as record(): the alerts INSERT and the
        # paired state UPDATE(s) must commit atomically, so a cross-process
        # RO reader querying MAX(alerts.ts) + last_alert_severity together
        # can never see the alert row without the state fields it implies.
        self._conn.execute("BEGIN IMMEDIATE")
        try:
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
            if decision.rule_id == "attending_parent_returned":
                self._conn.execute(
                    "UPDATE state SET last_absence_alert_ts = ? WHERE id = 1",
                    (ts,),
                )
            self._conn.execute("COMMIT")
        except Exception:
            try:
                self._conn.execute("ROLLBACK")
            except Exception:
                log.exception(
                    "record_alert: rollback also failed (transaction state unclear)"
                )
            raise
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
        # Constrain to alerts at or before ref_ts (Codex P2 round 4): without
        # this filter, a future alert (recorded at a later wall-clock time)
        # would be returned for an older `ts` argument — and the Python
        # `(ref - row_ts) < window_s` check would treat the negative
        # difference as an active cooldown, silently suppressing legitimate
        # historical alerts during backfill replay. We must look at history
        # AS OF the snap's timestamp, not "the latest known".
        ref = ts if ts is not None else time.time()
        if species is None:
            cur = self._conn.execute(
                "SELECT MAX(ts) AS latest FROM alerts "
                "WHERE severity = ? AND ts <= ?",
                (severity.value, ref),
            )
        else:
            cur = self._conn.execute(
                "SELECT MAX(ts) AS latest FROM alerts "
                "WHERE severity = ? AND ts <= ? AND ("
                " species = ? OR species LIKE ? OR species LIKE ? OR species LIKE ?"
                ")",
                (
                    severity.value,
                    ref,
                    species,
                    f"{species},%",
                    f"%,{species}",
                    f"%,{species},%",
                ),
            )
        row = cur.fetchone()
        if row is None or row["latest"] is None:
            return False
        return (ref - float(row["latest"])) < window_s

    def rule_cooldown_active(
        self,
        rule_id: str,
        window_s: int,
        ts: float | None = None,
    ) -> bool:
        """True if a prior alert with the same `rule_id` exists within
        `window_s` seconds before `ts` (defaulting to now).

        Codex P2 round 5: attending_parent_returned and lifecycle alerts (hatch,
        fledge, egg_laying_begin, incubation_begin) need rule-scoped
        cooldowns rather than severity-scoped. cooldown_active() keys
        off severity + species and would either over-suppress (a LOW
        hatch alert silencing a real attending_parent_returned) or never match
        at all (lifecycle alerts have empty species, so the species
        match always failed silently — state-machine gating was the
        only thing preventing double-fires there).

        Constrains the SQL to `ts <= ref` so during backfill drain a
        future alert can't suppress a legitimate older one.
        """
        ref = ts if ts is not None else time.time()
        cur = self._conn.execute(
            "SELECT MAX(ts) AS latest FROM alerts "
            "WHERE rule_id = ? AND ts <= ?",
            (rule_id, ref),
        )
        row = cur.fetchone()
        if row is None or row["latest"] is None:
            return False
        return (ref - float(row["latest"])) < window_s

    def latest_alert_for_species(
        self,
        species: str | None,
        window_s: int,
        ts: float | None = None,
    ) -> tuple[Severity, float] | None:
        """Return (severity, ts) of the most recent alert for `species` within
        `window_s`, regardless of severity. Used for escalation breakthrough.

        Constrains to alerts at or before `ts` (Codex P2 round 4) — without
        this, future alerts could be returned for older `ts` arguments and
        downstream code would treat them as prior history.
        """
        ref = ts if ts is not None else time.time()
        if species is None:
            cur = self._conn.execute(
                "SELECT severity, ts FROM alerts WHERE ts <= ? "
                "ORDER BY ts DESC LIMIT 1",
                (ref,),
            )
        else:
            cur = self._conn.execute(
                "SELECT severity, ts FROM alerts "
                "WHERE ts <= ? AND ("
                " species = ? OR species LIKE ? OR species LIKE ? OR species LIKE ?"
                ") "
                "ORDER BY ts DESC LIMIT 1",
                (
                    ref,
                    species,
                    f"{species},%",
                    f"%,{species}",
                    f"%,{species},%",
                ),
            )
        row = cur.fetchone()
        if row is None:
            return None
        if (ref - float(row["ts"])) >= window_s:
            return None
        return Severity(row["severity"]), float(row["ts"])

    # ── Analytics helpers (read-only, safe for cross-thread calls) ─────
    def get_observations_in_window(
        self, start_ts: float, end_ts: float,
    ) -> list[sqlite3.Row]:
        """Return observations whose ts is in [start_ts, end_ts] ordered by ts.

        Routed through the dedicated read-only connection (self._ro_conn,
        opened via `mode=ro` URI) so analytics-thread reads never observe
        partial state from an in-progress record() write on the main loop.
        See CLAUDE.md §30. Analytics runs in a dedicated executor; this
        query is read-only and cross-thread safe.
        """
        cur = self._ro_conn.execute(
            "SELECT id, ts, motion_triggered, prefilter_json, observation_json, evidence_dir "
            "FROM observations WHERE ts >= ? AND ts <= ? ORDER BY ts ASC",
            (start_ts, end_ts),
        )
        return cur.fetchall()

    def get_alerts_in_window(
        self, start_ts: float, end_ts: float,
    ) -> list[sqlite3.Row]:
        """Return alerts whose ts is in [start_ts, end_ts] ordered by ts.

        Routed through self._ro_conn for the same cross-thread isolation
        reason as get_observations_in_window. See CLAUDE.md §30.
        """
        cur = self._ro_conn.execute(
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
        try:
            self._ro_conn.close()
        except Exception:
            pass

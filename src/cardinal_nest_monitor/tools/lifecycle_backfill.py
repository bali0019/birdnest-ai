"""One-shot lifecycle timestamp backfill tool.

Populates `egg_laying_started_ts` and `incubation_started_ts` in the
`state` table for production DBs that were upgraded from the old 3-stage
lifecycle schema to the 6-stage schema (building_nest → egg_laying →
incubation → feeding → fledging → empty). These two columns are NULL on
existing rows because the ALTER TABLE migration can't infer historical
timestamps.

The tool offers three modes:

  --incubation-started YYYY-MM-DD[THH:MM]   explicit local-time override
  --egg-laying-started YYYY-MM-DD[THH:MM]   explicit local-time override
  --auto                                    infer from observation history

Auto-inference scans confident `attending_parent_on_nest` observations and looks
for the earliest 24h rolling window with ≥70% sitting ratio (the same
threshold the live state.py egg_laying→incubation transition uses).

Usage:
  python -m cardinal_nest_monitor.tools.lifecycle_backfill --dry-run --auto
  python -m cardinal_nest_monitor.tools.lifecycle_backfill --auto
  python -m cardinal_nest_monitor.tools.lifecycle_backfill \\
      --incubation-started 2026-04-14T00:00 \\
      --egg-laying-started 2026-04-13

Safety:
  - Refuses to overwrite existing non-null values unless --force.
  - Refuses to run if lifecycle_stage is feeding/fledging/empty (past
    these stages; existing data is authoritative).
  - Idempotent: re-running without --force is a no-op.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from cardinal_nest_monitor.config import get_settings


# ── Constants ──────────────────────────────────────────────────────────
# Matches state.py _MIN_CONFIDENCE and the 70% threshold used by the
# egg_laying → incubation transition rule.
_WINDOW_SECONDS = 24 * 3600
_SITTING_RATIO_THRESHOLD = 0.70
_MIN_CONFIDENT_SAMPLES = 24  # ~1 confident obs/hour over 24h
_CANDIDATE_STEP_SECONDS = 3600  # iterate candidate window starts in 1h steps

_REFUSE_STAGES = {"feeding", "fledging", "empty"}


# ── Helpers ────────────────────────────────────────────────────────────
def _parse_local_time(s: str) -> float:
    """Parse YYYY-MM-DD or YYYY-MM-DDTHH:MM as LOCAL time → unix ts."""
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.timestamp()
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(
        f"invalid timestamp {s!r}; expected YYYY-MM-DD or YYYY-MM-DDTHH:MM"
    )


def _fmt_ts(ts: float | None) -> str:
    if ts is None:
        return "NULL"
    local = datetime.fromtimestamp(ts)
    tz = datetime.now().astimezone().tzname() or "local"
    return f"{ts:.2f} ({local.strftime('%Y-%m-%d %H:%M:%S')} {tz})"


def _count_confident(
    conn: sqlite3.Connection, start_ts: float, end_ts: float
) -> tuple[int, int]:
    """Return (confident_on_nest, confident_total) in [start, end]."""
    cur = conn.execute(
        "SELECT observation_json FROM observations "
        "WHERE ts >= ? AND ts <= ? AND observation_json IS NOT NULL",
        (start_ts, end_ts),
    )
    from cardinal_nest_monitor.state import _row_passes_confidence

    on_nest = 0
    total = 0
    for row in cur.fetchall():
        oj = row[0]
        # Proper numeric confidence parse — must stay in sync with
        # state.py::record and events.py::_lifecycle_event so the tool,
        # live transitions, and predictive alerts all agree on "confident".
        if not _row_passes_confidence(oj):
            continue
        if '"attending_parent_on_nest":"true"' in oj:
            on_nest += 1
            total += 1
        elif '"attending_parent_on_nest":"false"' in oj:
            total += 1
        # "uncertain" doesn't count — same rule as state.py.
    return on_nest, total


def _find_incubation_window(
    conn: sqlite3.Connection, earliest_ts: float, latest_ts: float
) -> tuple[float, int, int] | None:
    """Return (window_start_ts, on_nest_count, total_count) for the
    earliest 24h window meeting the threshold, or None if no window
    qualifies. Candidate starts step forward in 1h increments.
    """
    if latest_ts - earliest_ts < _WINDOW_SECONDS:
        return None
    start = earliest_ts
    limit = latest_ts - _WINDOW_SECONDS
    while start <= limit:
        end = start + _WINDOW_SECONDS
        on_nest, total = _count_confident(conn, start, end)
        if total >= _MIN_CONFIDENT_SAMPLES:
            ratio = on_nest / total
            if ratio >= _SITTING_RATIO_THRESHOLD:
                return start, on_nest, total
        start += _CANDIDATE_STEP_SECONDS
    return None


# ── Main ───────────────────────────────────────────────────────────────
def _resolve_db_path(cli_path: str | None) -> Path:
    if cli_path:
        return Path(cli_path).expanduser().resolve()
    try:
        return get_settings().state_db_path.expanduser().resolve()
    except Exception:
        return Path("data/state.sqlite").resolve()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--db", type=str, default=None,
        help="Path to state.sqlite (defaults to get_settings().state_db_path)",
    )
    parser.add_argument(
        "--incubation-started", type=_parse_local_time, default=None,
        metavar="YYYY-MM-DD[THH:MM]",
        help="Explicit local-time override for incubation_started_ts",
    )
    parser.add_argument(
        "--egg-laying-started", type=_parse_local_time, default=None,
        metavar="YYYY-MM-DD[THH:MM]",
        help="Explicit local-time override for egg_laying_started_ts",
    )
    parser.add_argument(
        "--auto", action="store_true",
        help="Auto-infer from observation history (70% sitting over 24h)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would change, write nothing",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Overwrite existing non-null timestamps",
    )
    args = parser.parse_args()

    db_path = _resolve_db_path(args.db)
    print("Lifecycle backfill tool")
    print(f"DB: {db_path}")
    if not db_path.exists():
        print(f"ERROR: database does not exist at {db_path}")
        return 1

    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass

    try:
        # Ensure the columns we're going to write exist. The StateStore
        # does this on startup, but this tool might run on a DB that
        # wasn't opened by the current code yet. Idempotent ALTERs — swallow
        # "duplicate column" which means the column already exists.
        for alter_sql in (
            "ALTER TABLE state ADD COLUMN egg_laying_started_ts REAL",
            "ALTER TABLE state ADD COLUMN incubation_started_ts REAL",
            "ALTER TABLE state ADD COLUMN lifecycle_stage TEXT NOT NULL DEFAULT 'incubation'",
        ):
            try:
                conn.execute(alter_sql)
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower() and "already exists" not in str(e).lower():
                    raise

        row = conn.execute(
            "SELECT lifecycle_stage, egg_laying_started_ts, "
            "incubation_started_ts FROM state WHERE id = 1"
        ).fetchone()
        if row is None:
            print("ERROR: state row not found (id=1)")
            return 1

        stage = row["lifecycle_stage"] or "incubation"
        current_egg = row["egg_laying_started_ts"]
        current_inc = row["incubation_started_ts"]

        print(f"Current lifecycle_stage: {stage}")
        print(f"Current egg_laying_started_ts: {_fmt_ts(current_egg)}")
        print(f"Current incubation_started_ts: {_fmt_ts(current_inc)}")
        print()

        if stage in _REFUSE_STAGES:
            print(
                f"REFUSING: lifecycle_stage is {stage!r} — past the stages "
                "this tool backfills. Existing data is authoritative."
            )
            return 1

        if current_egg is not None and current_inc is not None and not args.force:
            print("Nothing to do — both timestamps already set. Use --force to overwrite.")
            return 0

        # ── Gather observation bounds ─────────────────────────────────
        bounds = conn.execute(
            "SELECT COUNT(*) AS n, MIN(ts) AS min_ts, MAX(ts) AS max_ts "
            "FROM observations"
        ).fetchone()
        n_obs = bounds["n"]
        min_ts = bounds["min_ts"]
        max_ts = bounds["max_ts"]
        if n_obs == 0:
            print("ERROR: no observations in DB; cannot infer. Use explicit overrides.")
            return 1

        local_min = datetime.fromtimestamp(min_ts).strftime("%Y-%m-%d %H:%M")
        local_max = datetime.fromtimestamp(max_ts).strftime("%Y-%m-%d %H:%M")
        print(f"Scanning {n_obs} observations ({local_min} → {local_max} local)...")
        print()

        # ── Resolve target timestamps ────────────────────────────────
        new_egg: float | None = current_egg
        new_inc: float | None = current_inc
        egg_reason: str | None = None
        inc_reason: str | None = None

        # Explicit overrides take precedence over --auto.
        if args.egg_laying_started is not None:
            new_egg = args.egg_laying_started
            egg_reason = "explicit --egg-laying-started"
        if args.incubation_started is not None:
            new_inc = args.incubation_started
            inc_reason = "explicit --incubation-started"

        inferred_first_window = False
        if args.auto:
            # Only infer values the user didn't explicitly set.
            print("Auto-inference: searching for earliest 24h window with "
                  f"≥{int(_SITTING_RATIO_THRESHOLD * 100)}% sitting...")
            result = _find_incubation_window(conn, min_ts, max_ts)
            if result is not None:
                win_start, on_nest, total = result
                ratio_pct = on_nest / total * 100
                win_end_local = datetime.fromtimestamp(
                    win_start + _WINDOW_SECONDS
                ).strftime("%Y-%m-%d %H:%M")
                win_start_local = datetime.fromtimestamp(win_start).strftime(
                    "%Y-%m-%d %H:%M"
                )
                print(
                    f"  Window {win_start_local} → {win_end_local}: "
                    f"{ratio_pct:.0f}% (n={total}) ✓ MATCH"
                )
                if args.incubation_started is None:
                    new_inc = win_start
                    inc_reason = f"inferred, {ratio_pct:.0f}% sitting (n={total})"
                # If the qualifying window starts AT the earliest observation,
                # egg-laying was before monitoring began.
                if abs(win_start - min_ts) < _CANDIDATE_STEP_SECONDS:
                    inferred_first_window = True
            else:
                print("  No 24h window met the threshold.")
                if args.incubation_started is None:
                    print(
                        "  WARNING: cannot infer incubation_started_ts. "
                        "Rerun with --incubation-started YYYY-MM-DDTHH:MM."
                    )

            if args.egg_laying_started is None:
                if inferred_first_window:
                    print(
                        "  Note: qualifying window starts at earliest "
                        "observation — egg-laying predates monitoring; "
                        "leaving egg_laying_started_ts NULL."
                    )
                    # new_egg stays current (likely NULL).
                else:
                    new_egg = min_ts
                    egg_reason = "earliest observation"

        # ── Display proposed changes ─────────────────────────────────
        print()
        print("Proposed changes:")
        changed_any = False

        def _diff_line(label: str, old: float | None, new: float | None,
                       reason: str | None) -> bool:
            if new is None:
                # Nothing to write for this column.
                if old is None:
                    print(f"  {label}: NULL → NULL [no change]")
                return False
            if old is not None and not args.force:
                print(
                    f"  {label}: {_fmt_ts(old)} → (skipped — already set; "
                    "pass --force to overwrite)"
                )
                return False
            if old is not None and old == new:
                print(f"  {label}: {_fmt_ts(old)} [unchanged]")
                return False
            reason_suffix = f" [{reason}]" if reason else ""
            print(f"  {label}: {_fmt_ts(old)} → {_fmt_ts(new)}{reason_suffix}")
            return True

        egg_write = _diff_line(
            "egg_laying_started_ts", current_egg, new_egg, egg_reason
        )
        inc_write = _diff_line(
            "incubation_started_ts", current_inc, new_inc, inc_reason
        )
        changed_any = egg_write or inc_write

        if not changed_any:
            print()
            print("Nothing to write.")
            return 0

        print()
        if args.dry_run:
            print("Write changes? [dry-run] — rerun without --dry-run to apply.")
            return 0

        # ── Apply (fixed-shape UPDATE) ───────────────────────────────
        # Use a static SQL string that always writes both columns. For
        # each column we either write the newly computed value (if
        # the diff said to write) or preserve the existing value. This
        # avoids dynamically constructing an UPDATE statement from a
        # list of column fragments — even though every fragment here
        # is a hard-coded literal, the builder pattern is the wrong
        # shape to leave in the tree because it invites SQLi if a
        # future contributor lets user input drive column selection.
        final_egg: float | None = new_egg if egg_write else current_egg
        final_inc: float | None = new_inc if inc_write else current_inc

        conn.execute(
            "UPDATE state SET "
            " egg_laying_started_ts = ?, "
            " incubation_started_ts = ? "
            "WHERE id = 1",
            (final_egg, final_inc),
        )
        print("Applied.")
        print()
        print("Final state:")
        print(f"  lifecycle_stage: {stage}")
        print(f"  egg_laying_started_ts: {_fmt_ts(final_egg)}")
        print(f"  incubation_started_ts: {_fmt_ts(final_inc)}")
        return 0
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())

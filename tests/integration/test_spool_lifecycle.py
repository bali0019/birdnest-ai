"""End-to-end tests for the on-disk spool + Pipeline lifecycle.

These tests exercise the contract between the downloader service (which
writes snaps via :func:`spool.write_snap`) and the analyzer service (which
drains via :func:`spool.claim_next` / :func:`spool.mark_complete` and
processes each snap through :meth:`Pipeline.on_image`). They run against
the interface spec only — the actual ``downloader_loop`` and
``analyzer_loop`` modules are authored by parallel agents and may still
be in flux. The primitives exercised here are the stable contract
between them.

The parity-guard test at the bottom (``test_combined_mode_*``) duplicates
the behavior asserted by ``test_absence_cycle.py::test_mother_returns_low_alert``
on purpose: it locks in the combined-mode (legacy single-process) state
transitions so any refactor that subtly changes Pipeline behavior breaks
BOTH tests visibly — a cross-test parity tripwire.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from cardinal_nest_monitor import analyzer as analyzer_mod
from cardinal_nest_monitor import main as main_mod
from cardinal_nest_monitor import spool
from cardinal_nest_monitor.config import get_settings
from cardinal_nest_monitor.schema import Severity


# ────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────


def _pipeline(store, notifier, evidence):
    """Build a Pipeline wired for integration tests (no feed queue)."""
    counters = main_mod.DailyCounters()
    return main_mod.Pipeline(
        store=store,
        notifier=notifier,
        evidence=evidence,
        counters=counters,
        feed_queue=None,
    )


def _meta_at(ts: float, motion: bool = False) -> dict:
    return {"motion_triggered": motion, "ts": ts}


def _pending_count(spool_dir) -> int:
    p = spool_dir / "pending"
    if not p.exists():
        return 0
    return sum(1 for e in p.iterdir() if e.name.endswith("_snap.jpg"))


def _processing_count(spool_dir) -> int:
    p = spool_dir / "processing"
    if not p.exists():
        return 0
    return sum(1 for e in p.iterdir() if e.name.endswith("_snap.jpg"))


# ────────────────────────────────────────────────────────────────────────
# Group 1: end-to-end spool lifecycle
# ────────────────────────────────────────────────────────────────────────


async def test_spool_survives_analyzer_restart(
    tmp_path, monkeypatch, store, evidence, notifier,
    cardinal_jpeg_bytes, obs_on_nest,
):
    """5 snaps written by the downloader survive an analyzer restart.

    Steps:
      1. Downloader writes 5 snaps (timestamps 5s apart) via spool.write_snap.
      2. Analyzer "restart" = build a fresh Pipeline, call recover_stranded,
         then drain: claim_next → await on_image → mark_complete until
         claim_next returns None.
      3. Assert: 5 analyzer calls, 5 observation rows in store, and both
         pending/ + processing/ directories are empty at the end.
    """
    spool_dir = tmp_path / "spool"
    settings = get_settings()
    monkeypatch.setattr(settings, "spool_dir", spool_dir)
    monkeypatch.setattr(settings, "verify_alerts_with_opus", False)

    analyze_mock = AsyncMock(return_value=obs_on_nest())
    monkeypatch.setattr(analyzer_mod, "analyze", analyze_mock)

    # Avoid a real Discord post on every snap — analyzer-only plumbing test.
    monkeypatch.setattr(notifier, "send_alert", AsyncMock(return_value=True))

    base_ts = time.time() - 100
    for i in range(5):
        spool.write_snap(
            cardinal_jpeg_bytes,
            _meta_at(base_ts + i * 5),
            spool_dir,
        )

    assert _pending_count(spool_dir) == 5
    assert _processing_count(spool_dir) == 0

    # --- Simulate analyzer restart: fresh Pipeline + recover_stranded ---
    recovered = spool.recover_stranded(spool_dir)
    # Nothing was ever claimed — recovery should move zero entries.
    assert recovered == 0

    pipeline = _pipeline(store, notifier, evidence)

    drained = 0
    while True:
        claimed = spool.claim_next(spool_dir)
        if claimed is None:
            break
        jpeg, meta, path = claimed
        await pipeline.on_image(jpeg, meta)
        spool.mark_complete(path)
        drained += 1
        # Safety guard: should never exceed the 5 we wrote.
        assert drained <= 5, "drained more entries than were written"

    assert drained == 5, f"expected to drain 5 snaps, drained {drained}"
    assert analyze_mock.await_count == 5, (
        f"expected 5 analyzer calls, got {analyze_mock.await_count}"
    )

    # All observations persisted.
    obs_rows = store.get_observations_in_window(0, time.time() + 1)
    assert len(obs_rows) == 5, (
        f"expected 5 observation rows, got {len(obs_rows)}"
    )

    # Spool is fully drained.
    assert _pending_count(spool_dir) == 0, "pending/ should be empty"
    assert _processing_count(spool_dir) == 0, "processing/ should be empty"


async def test_analyzer_crash_mid_processing_recovers_on_restart(
    tmp_path, monkeypatch, store, evidence, notifier,
    cardinal_jpeg_bytes, obs_on_nest,
):
    """A mid-analysis crash leaves the snap stranded in processing/.
    recover_stranded must move it back to pending/ so it will be retried.
    """
    spool_dir = tmp_path / "spool"
    settings = get_settings()
    monkeypatch.setattr(settings, "spool_dir", spool_dir)
    monkeypatch.setattr(settings, "verify_alerts_with_opus", False)

    analyze_mock = AsyncMock(return_value=obs_on_nest())
    monkeypatch.setattr(analyzer_mod, "analyze", analyze_mock)
    monkeypatch.setattr(notifier, "send_alert", AsyncMock(return_value=True))

    base_ts = time.time() - 50
    for i in range(3):
        spool.write_snap(
            cardinal_jpeg_bytes,
            _meta_at(base_ts + i * 5),
            spool_dir,
        )

    assert _pending_count(spool_dir) == 3

    # Claim one (moves to processing/) but DO NOT mark_complete — simulates
    # an analyzer crash mid-analysis.
    claimed = spool.claim_next(spool_dir)
    assert claimed is not None
    _, _, stranded_path = claimed
    assert _processing_count(spool_dir) == 1
    assert _pending_count(spool_dir) == 2

    # --- Analyzer restart: recover_stranded must un-strand the claim ---
    recovered = spool.recover_stranded(spool_dir)
    assert recovered == 1, f"expected 1 stranded snap recovered, got {recovered}"
    assert _processing_count(spool_dir) == 0
    assert _pending_count(spool_dir) == 3, (
        "all 3 snaps should now be in pending/ after recovery"
    )

    # Drain all three — no double-processing side effect is *required* by
    # the spool (idempotency is handled in downstream components); we only
    # assert the three snaps can now be claimed afresh.
    pipeline = _pipeline(store, notifier, evidence)
    drained_paths: list = []
    while True:
        c = spool.claim_next(spool_dir)
        if c is None:
            break
        jpeg, meta, path = c
        await pipeline.on_image(jpeg, meta)
        spool.mark_complete(path)
        drained_paths.append(path)
        assert len(drained_paths) <= 3

    assert len(drained_paths) == 3, (
        f"expected 3 snaps drained after recovery, got {len(drained_paths)}"
    )
    assert analyze_mock.await_count == 3
    assert _pending_count(spool_dir) == 0
    assert _processing_count(spool_dir) == 0


async def test_downloader_writes_do_not_block_on_analyzer(
    tmp_path, monkeypatch, cardinal_jpeg_bytes,
):
    """The downloader writes 20 snaps in a tight loop with no analyzer
    draining. All 20 must land in pending/ within 2 seconds; no exception.

    This is the "decoupled downloader" invariant — the spool acts as a
    pressure relief so a hung / missing analyzer does NOT slow down the
    Blink snap cadence.
    """
    spool_dir = tmp_path / "spool"
    settings = get_settings()
    monkeypatch.setattr(settings, "spool_dir", spool_dir)

    base_ts = time.time() - 1000  # in the past, out of the way of real time
    start = time.monotonic()
    for i in range(20):
        # 100ms apart so filename stems are unique (UTC ms-precision stems
        # avoid collisions even at this rate, but 100ms makes it obvious).
        spool.write_snap(
            cardinal_jpeg_bytes,
            _meta_at(base_ts + i * 0.1),
            spool_dir,
        )
    elapsed = time.monotonic() - start

    assert elapsed < 2.0, (
        f"20 spool writes took {elapsed:.2f}s — downloader-side writes must "
        f"be effectively non-blocking. If this fails, the downloader will "
        f"fall behind the Blink snap cadence when the analyzer is down."
    )
    assert _pending_count(spool_dir) == 20, (
        f"expected 20 snaps in pending/, found {_pending_count(spool_dir)}"
    )
    assert _processing_count(spool_dir) == 0


async def test_drop_stale_removes_snaps_older_than_cap(
    tmp_path, monkeypatch, cardinal_jpeg_bytes,
):
    """drop_stale must delete snaps whose filename ts is older than the
    cap and leave fresh ones intact. This is the cost-cap mechanism that
    runs at analyzer startup to prevent an unbounded backlog after a long
    analyzer outage (see config.backfill_max_age_seconds).
    """
    spool_dir = tmp_path / "spool"
    settings = get_settings()
    monkeypatch.setattr(settings, "spool_dir", spool_dir)

    now = time.time()
    old_ts = now - 3600  # 1 hour old
    fresh_ts = now - 10  # 10s old

    old_path = spool.write_snap(
        cardinal_jpeg_bytes, _meta_at(old_ts), spool_dir
    )
    fresh_path = spool.write_snap(
        cardinal_jpeg_bytes, _meta_at(fresh_ts), spool_dir
    )

    assert old_path.exists()
    assert fresh_path.exists()
    assert _pending_count(spool_dir) == 2

    # Cap: 30 min. Old (1h) is stale; fresh (10s) is not.
    dropped = spool.drop_stale(spool_dir, max_age_seconds=1800)

    assert dropped == 1, f"expected 1 stale snap dropped, got {dropped}"
    assert not old_path.exists(), (
        "1-hour-old snap should have been deleted by drop_stale"
    )
    assert fresh_path.exists(), (
        "10s-old snap should NOT have been deleted"
    )
    # Meta sibling of the old one is also gone.
    old_meta = old_path.with_name(
        old_path.name[: -len("_snap.jpg")] + "_meta.json"
    )
    assert not old_meta.exists(), (
        "stale snap's meta sidecar should have been deleted alongside it"
    )
    assert _pending_count(spool_dir) == 1


# ────────────────────────────────────────────────────────────────────────
# Group 2: combined-mode parity guard
# ────────────────────────────────────────────────────────────────────────


async def test_combined_mode_end_to_end_matches_legacy_behavior(
    monkeypatch, store, evidence, notifier, tmp_path,
    cardinal_jpeg_bytes, empty_nest_jpeg_bytes,
    obs_on_nest, obs_off_nest,
):
    """COMBINED-MODE PARITY SNAPSHOT — do not delete without thinking.

    Duplicates the behavior asserted by
    ``test_absence_cycle.py::test_mother_returns_low_alert``: drive the
    Pipeline directly through the absence cycle (mom on nest → mom leaves
    long enough to flip in_absence → mom returns) and assert exactly one
    LOW ``mother_returned`` alert fires.

    Intent: any refactor that subtly changes combined-mode state
    transitions — e.g. reordering store.record vs evaluate, tweaking the
    confidence gate on cardinal_on_nest, or changing the absence-enter
    threshold — will break BOTH this test AND the original in
    test_absence_cycle.py. The duplicated assertion is the parity contract:
    decoupling the downloader/analyzer services must NOT silently alter
    the Pipeline's externally-observable behavior on the same inputs.

    If this test fails but test_absence_cycle passes (or vice versa), you
    have a divergence bug — combined-mode and spool-mode are no longer
    producing the same state transitions. Investigate before deploying.
    """
    # Isolate the spool even though this test doesn't use it — keeps the
    # fixture hygiene uniform across the whole module.
    spool_dir = tmp_path / "spool"
    settings = get_settings()
    monkeypatch.setattr(settings, "spool_dir", spool_dir)
    monkeypatch.setattr(settings, "verify_alerts_with_opus", False)

    captured: list = []

    orig_send_alert = notifier.send_alert

    async def capturing_send_alert(decision, observation, **kwargs):
        captured.append(decision)
        return await orig_send_alert(decision, observation, **kwargs)

    monkeypatch.setattr(notifier, "send_alert", capturing_send_alert)

    pipeline = _pipeline(store, notifier, evidence)

    now = time.time()
    t_seed = now - 500
    t_absent = now - 300
    t_return = now - 10  # 290s after t_absent — in_absence is True by now

    # Seed: mom on nest.
    monkeypatch.setattr(
        analyzer_mod, "analyze", AsyncMock(return_value=obs_on_nest())
    )
    await pipeline.on_image(cardinal_jpeg_bytes, _meta_at(t_seed))

    # Mom gone long enough to flip in_absence.
    monkeypatch.setattr(
        analyzer_mod, "analyze", AsyncMock(return_value=obs_off_nest())
    )
    await pipeline.on_image(empty_nest_jpeg_bytes, _meta_at(t_absent))
    assert store.get_state().in_absence is True, (
        "PARITY VIOLATION: in_absence should be True after the absence "
        "threshold elapsed. If this fails, combined-mode absence detection "
        "has regressed."
    )

    captured_pre_return = list(captured)

    # Mom returns — rule 5 ``mother_returned`` should fire LOW exactly once.
    monkeypatch.setattr(
        analyzer_mod, "analyze", AsyncMock(return_value=obs_on_nest())
    )
    await pipeline.on_image(cardinal_jpeg_bytes, _meta_at(t_return))

    return_alerts = [d for d in captured if d not in captured_pre_return]
    assert len(return_alerts) == 1, (
        f"PARITY VIOLATION: expected exactly one LOW mother_returned alert "
        f"on mom's return, got {len(return_alerts)}. Combined-mode has "
        f"diverged from the legacy absence-cycle contract asserted in "
        f"test_absence_cycle.py::test_mother_returns_low_alert."
    )
    decision = return_alerts[0]
    assert decision.severity == Severity.LOW, (
        f"PARITY VIOLATION: return alert severity = {decision.severity}, "
        f"expected LOW."
    )
    assert decision.rule_id == "mother_returned", (
        f"PARITY VIOLATION: return alert rule_id = {decision.rule_id}, "
        f"expected 'mother_returned'."
    )

    # Post-return state sanity: in_absence should be False again.
    assert store.get_state().in_absence is False, (
        "PARITY VIOLATION: in_absence should clear to False after mom "
        "returns. If this fails, the mother-returned path has regressed."
    )

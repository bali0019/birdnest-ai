"""Hang-resilience + pipeline-isolation integration tests.

The 3-hour outage on 2026-04-15 had two distinct failure modes that MUST
be covered by regression guards — if either regresses, the cardinal's eggs
are at risk from a silent multi-hour monitoring blackout.

  1. A single hung network call (the original outage trigger) — guarded by
     ``test_analyzer_timeout_does_not_hang``: a 120s analyzer hang must be
     bounded by the analyzer's internal 60s wait_for.

  2. A hung pipeline run blocking subsequent snaps (the cascade that turned
     a single failure into 3 hours of silence) — guarded by
     ``test_hung_on_image_does_not_block_next_snap``: this test replicates
     snap_loop's exact dispatch pattern (``create_task`` + ``state_updated``
     event) and asserts that a first on_image hung indefinitely does NOT
     prevent the SECOND on_image from completing within budget. This is
     the §17 pipeline-isolation regression guard.

  3. A backed-up Discord feed channel blocking the alerts hot path —
     guarded by ``test_feed_queue_full_does_not_block_alerts_hot_path``:
     fills the feed queue to capacity and asserts that on_image's alert
     path still completes. Proves the ``put_nowait`` + QueueFull handling
     in Pipeline.on_image does not regress to an awaiting put.

Any of these regressing re-introduces the exact failure mode that caused
the 2026-04-15 outage — DO NOT WEAKEN OR SKIP.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from birdnest_ai import analyzer as analyzer_mod
from birdnest_ai import main as main_mod
from birdnest_ai.config import get_settings


def _pipeline(store, notifier, evidence):
    counters = main_mod.DailyCounters()
    return main_mod.Pipeline(
        store=store,
        notifier=notifier,
        evidence=evidence,
        counters=counters,
        feed_queue=None,
    )


def test_hard_timeout_production_default_is_60s():
    """Pin the production default for ``analyzer_mod.HARD_TIMEOUT_SECONDS``.

    The test_analyzer_timeout_does_not_hang test below monkeypatches the
    constant to 1 s for speed, which means a future change that drops
    the default (e.g. accidentally shipping a "1.0" left over from a
    debug session) wouldn't fail the hang-resilience test — the mocked
    value would still be 1 s. This guard explicitly pins the production
    value against the CLAUDE.md §19 timeout budget table.

    If you're deliberately changing the budget (Anthropic p99 shifts,
    new SDK with different timeout behaviour, etc.), update both this
    assertion AND the §19 table in the same commit.
    """
    from birdnest_ai import analyzer as _analyzer_mod

    assert _analyzer_mod.HARD_TIMEOUT_SECONDS == 60.0, (
        f"analyzer_mod.HARD_TIMEOUT_SECONDS changed to "
        f"{_analyzer_mod.HARD_TIMEOUT_SECONDS}; CLAUDE.md §19 says the "
        "analyzer.analyze() budget is 60s. Update the §19 table if "
        "this change is intentional."
    )


async def test_analyzer_timeout_does_not_hang(
    monkeypatch, store, evidence, notifier, reference_jpeg_bytes,
):
    """A hung analyzer must NOT block on_image beyond the analyzer's
    hard-bound + a few seconds of slack.

    Strategy:
      - Patch ``analyzer_mod.HARD_TIMEOUT_SECONDS`` to 1 s so BOTH the
        outer ``main.py::Pipeline.on_image`` wait_for AND the inner
        ``analyzer.py::analyze`` wait_for shrink together. (Both read
        the constant at call time, not at import time.)
      - Patch ``analyzer.analyze`` to a coroutine that sleeps 5 s — well
        past the 1 s bound but short enough that the test can't be
        catastrophically slow if the bound breaks.
      - Expect: on_image returns within 2 s and no alert is sent.

    Before 2026-04-23 this test slept 120 s to hit the real 60 s
    timeout, making it dominate the test suite at 60 s of every run.
    The shared-constant refactor let the test exercise the SAME
    contract in under 2 s. The production default (60 s, see CLAUDE.md
    §19) is still the number that ships.
    """
    # Shrink both the inner analyzer wait_for AND main.py's outer bound
    # together via the shared constant.
    monkeypatch.setattr(analyzer_mod, "HARD_TIMEOUT_SECONDS", 1.0)

    async def slow_analyze(*args, **kwargs):
        await asyncio.sleep(5)  # well past the 1 s bound; unreachable return
        from birdnest_ai.schema import NestObservation
        return NestObservation(
            attending_parent_present="uncertain",
            attending_parent_on_nest="uncertain",
            eggs_visible="uncertain",
            egg_count_estimate=None,
            nest_visible=False,
            nest_disturbed="uncertain",
            species_detected=[],
            threat_species_detected=[],
            near_nest_activity=False,
            direct_nest_interaction=False,
            confidence=0.0,
            summary="unreachable",
        )

    monkeypatch.setattr(analyzer_mod, "analyze", slow_analyze)

    # Ensure we never post an alert even if one somehow gets built.
    from unittest.mock import AsyncMock
    send_alert_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(notifier, "send_alert", send_alert_mock)

    pipeline = _pipeline(store, notifier, evidence)
    meta = {"motion_triggered": False, "ts": time.time()}

    start = time.monotonic()
    try:
        await asyncio.wait_for(
            pipeline.on_image(reference_jpeg_bytes, meta),
            timeout=3,
        )
    except asyncio.TimeoutError:
        elapsed = time.monotonic() - start
        pytest.fail(
            f"pipeline.on_image did not return within 3s (elapsed={elapsed:.1f}s). "
            "The analyzer's HARD_TIMEOUT_SECONDS bound is missing or broken; a "
            "hung analyzer WILL hang the service."
        )
    elapsed = time.monotonic() - start

    # on_image must have returned within ~1-2s (the analyzer catches
    # asyncio.TimeoutError internally and the pipeline falls through with
    # no observation → no alert).
    assert elapsed < 3, f"on_image took {elapsed:.1f}s, expected < 3s"
    assert send_alert_mock.await_count == 0, (
        "No alert should be sent when the analyzer timed out"
    )


async def test_hung_on_image_does_not_block_next_snap(
    monkeypatch, store, evidence, notifier, cardinal_jpeg_bytes, obs_on_nest,
):
    """§17 PIPELINE-ISOLATION REGRESSION GUARD.

    The 2026-04-15 outage was caused by ``await on_image(...)`` in
    ``snap_loop``: a single hung network call inside on_image froze the
    snap loop for 3+ hours. Fix: ``asyncio.create_task(on_image(...))`` +
    ``state_updated`` Event to bound the cadence-computation wait.

    This test replicates snap_loop's EXACT dispatch pattern (see
    ``blink_client.py::snap_loop`` lines 318–323) and asserts:
      1. A first on_image whose analyzer hangs FOREVER does NOT prevent
         a second on_image from running and completing.
      2. The second on_image's ``state_updated`` fires within a small
         budget (10s — same as snap_loop's wait_for timeout).

    If this test fails — e.g. someone reverts create_task back to
    ``await on_image(...)`` — the test will hit the 10s timeout, fail
    loudly, and block the deploy. This is the regression guard that
    would have prevented the 3-hour outage.
    """
    # Arm: first analyzer call hangs forever; subsequent calls return
    # a normal on-nest observation.
    call_count = {"n": 0}

    async def conditional_analyze(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # Unbounded hang — no timeout, no return. Simulates the original
            # outage's hung Anthropic call.
            await asyncio.sleep(99999)
            raise RuntimeError("unreachable")  # pragma: no cover
        return obs_on_nest()

    monkeypatch.setattr(analyzer_mod, "analyze", conditional_analyze)
    monkeypatch.setattr(notifier, "send_alert", AsyncMock(return_value=True))

    # Disable verifier so the second snap's path is minimal + fast.
    settings = get_settings()
    monkeypatch.setattr(settings, "verify_alerts_with_opus", False)

    pipeline = _pipeline(store, notifier, evidence)
    meta1 = {"motion_triggered": False, "ts": time.time()}
    meta2 = {"motion_triggered": False, "ts": time.time()}

    # ── Replicate snap_loop's dispatch pattern for snap #1 ──────────────
    # (see blink_client.py::snap_loop lines 318-323)
    state_updated_1 = asyncio.Event()
    task1 = asyncio.create_task(
        pipeline.on_image(cardinal_jpeg_bytes, meta1, state_updated_1)
    )
    # Give task1 a moment to start its hang.
    await asyncio.sleep(0.3)
    assert not task1.done(), (
        "task1 should be suspended inside the hung analyzer.analyze call"
    )
    # snap_loop would now call wait_for(state_updated.wait(), 10). That
    # will time out because the analyzer never returns → state_updated
    # never fires. snap_loop catches the TimeoutError and moves on.
    try:
        await asyncio.wait_for(state_updated_1.wait(), timeout=0.5)
    except asyncio.TimeoutError:
        pass  # expected — first snap is hung

    # ── Dispatch snap #2 while snap #1 is still hung ────────────────────
    state_updated_2 = asyncio.Event()
    dispatch_start = time.monotonic()
    task2 = asyncio.create_task(
        pipeline.on_image(cardinal_jpeg_bytes, meta2, state_updated_2)
    )

    # CRITICAL ASSERTION: task2 must complete its state-update step
    # independently of task1. snap_loop gives 10s; we use a tighter 10s
    # budget here to match the production code exactly.
    try:
        await asyncio.wait_for(state_updated_2.wait(), timeout=10)
    except asyncio.TimeoutError:
        elapsed = time.monotonic() - dispatch_start
        # Clean up dangling tasks so pytest doesn't complain.
        task1.cancel()
        task2.cancel()
        pytest.fail(
            f"PIPELINE ISOLATION REGRESSION: second snap's state_updated "
            f"did NOT fire within 10s (elapsed={elapsed:.1f}s) while the "
            f"first snap was hung. The §17 regression guard has failed — "
            f"the exact failure mode that caused the 2026-04-15 3-hour "
            f"outage is back. Check that snap_loop still uses "
            f"asyncio.create_task(on_image(...)) and has NOT been reverted "
            f"to await on_image(...)."
        )

    elapsed = time.monotonic() - dispatch_start
    # Sanity: the completed snap should have taken only a few seconds
    # (mostly the no-op analyzer mock + state.record + evidence write).
    assert elapsed < 10, (
        f"second snap completed but took {elapsed:.1f}s — unexpectedly slow"
    )
    assert task2.done(), "task2 should be complete after state_updated fired"
    assert task2.exception() is None, (
        f"task2 raised an exception: {task2.exception()!r}"
    )

    # Cleanup: task1 is still hung. Cancel it so the test doesn't leak.
    task1.cancel()
    try:
        await task1
    except (asyncio.CancelledError, Exception):
        pass


async def test_feed_queue_full_does_not_block_alerts_hot_path(
    monkeypatch, store, evidence, notifier, thrasher_jpeg_bytes,
    obs_thrasher_near_nest,
):
    """Feed-channel independence regression guard.

    Pipeline.on_image enqueues every snap to the feed channel via a
    BOUNDED ``asyncio.Queue(maxsize=100)``. The enqueue uses
    ``put_nowait()`` (NOT ``await put()``) so a hung feed_worker can
    never block the alert hot path. This test proves the invariant.

    Strategy: create a feed_queue of size 1, pre-fill it to capacity
    (simulating a hung/slow feed_worker that can't drain), then call
    ``on_image`` with a threat observation (alert WILL fire). Assert:
      1. on_image completes within a few seconds
      2. send_alert was called (alerts path not blocked by full feed)
      3. the feed event was DROPPED with a warning log (not queued)

    If this regresses — e.g. someone changes put_nowait → await put() —
    the on_image call will hang forever waiting for queue slot and the
    test will fail on the outer wait_for timeout.
    """
    # Disable verifier for a single-pass alert path.
    settings = get_settings()
    monkeypatch.setattr(settings, "verify_alerts_with_opus", False)

    monkeypatch.setattr(
        analyzer_mod, "analyze",
        AsyncMock(return_value=obs_thrasher_near_nest()),
    )

    send_alert_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(notifier, "send_alert", send_alert_mock)

    # Feed queue of size 1, pre-filled to capacity. No worker draining it.
    feed_queue: asyncio.Queue = asyncio.Queue(maxsize=1)
    feed_queue.put_nowait({"sentinel": "pre-existing blocker"})
    assert feed_queue.full(), "queue should be at capacity before on_image runs"

    counters = main_mod.DailyCounters()
    pipeline = main_mod.Pipeline(
        store=store,
        notifier=notifier,
        evidence=evidence,
        counters=counters,
        feed_queue=feed_queue,
    )

    meta = {"motion_triggered": False, "ts": time.time()}
    start = time.monotonic()
    try:
        await asyncio.wait_for(
            pipeline.on_image(thrasher_jpeg_bytes, meta),
            timeout=30,
        )
    except asyncio.TimeoutError:
        elapsed = time.monotonic() - start
        pytest.fail(
            f"FEED-INDEPENDENCE REGRESSION: on_image did not return within "
            f"30s (elapsed={elapsed:.1f}s) with a full feed queue. The "
            f"alert hot path is being blocked by the feed channel — check "
            f"that Pipeline.on_image still uses feed_queue.put_nowait() "
            f"with a QueueFull except handler, and has NOT been reverted "
            f"to await feed_queue.put(). If this fails, a slow Discord "
            f"feed post could block ALL alerts."
        )
    elapsed = time.monotonic() - start

    # Alerts path must have completed normally.
    assert elapsed < 10, (
        f"on_image took {elapsed:.1f}s with a full feed queue — should be "
        f"fast (just the analyzer mock + state.record + send_alert). "
        f"Slow completion is a smell that feed backpressure is leaking "
        f"into the alerts path."
    )
    assert send_alert_mock.await_count == 1, (
        "Alert MUST have been sent even though the feed queue was full — "
        "feed/alert channels are required to be independent."
    )
    # Feed event should have been DROPPED (not queued) — queue size is
    # still 1 (just our sentinel), not 2.
    assert feed_queue.qsize() == 1, (
        f"Expected feed event to be dropped (qsize=1 sentinel only), got "
        f"qsize={feed_queue.qsize()}. Either put_nowait somehow blocked "
        f"or the QueueFull branch isn't dropping as intended."
    )


def test_snap_loop_dispatches_on_snap_via_create_task():
    """§17 SOURCE-LEVEL REGRESSION GUARD.

    The functional test above (``test_hung_on_image_does_not_block_next_snap``)
    replicates snap_loop's dispatch pattern in the test body — it proves the
    pattern works, but it does NOT catch someone changing snap_loop's source
    to revert the fix (because the test never calls snap_loop).

    This complementary source-level guard inspects ``snap_loop``'s code and
    fails if the fix has been reverted. Between the two tests, both classes
    of regression (pattern broken / pattern removed) are caught.

    NOTE: the callback parameter was renamed from ``on_image`` to ``on_snap``
    during the downloader/analyzer split (Phase 1). The regression intent is
    unchanged — snap_loop MUST dispatch the callback via create_task and MUST
    NOT await it directly. Only the identifier changed.
    """
    import inspect
    from birdnest_ai import blink_client as bc

    src = inspect.getsource(bc.snap_loop)

    # snap_loop MUST dispatch on_snap via asyncio.create_task(...) so a
    # hung pipeline run cannot serialize the entire loop.
    assert "asyncio.create_task(on_snap(" in src, (
        "§17 REGRESSION: snap_loop no longer uses asyncio.create_task() to "
        "dispatch on_snap. A hung pipeline run will now freeze the entire "
        "snap loop for the duration of the hang — the exact failure mode "
        "that caused the 2026-04-15 3-hour outage. Restore the "
        "create_task + state_updated Event pattern (see plan "
        "reactive-tickling-rose.md Part 1A/B)."
    )

    # snap_loop MUST NOT directly `await on_snap(...)` — that call pattern
    # is what serialized the pipeline and caused the outage. Check each
    # non-comment line to avoid false positives on docstring references.
    offending = []
    for lineno, line in enumerate(src.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if stripped.startswith("await on_snap("):
            offending.append(f"line {lineno}: {stripped}")
    assert not offending, (
        f"§17 REGRESSION: snap_loop now awaits on_snap directly, which "
        f"serializes the pipeline. One hung network call will hang ALL "
        f"subsequent snaps. Offending lines:\n  "
        + "\n  ".join(offending)
    )

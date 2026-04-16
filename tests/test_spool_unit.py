"""Unit tests for cardinal_nest_monitor.spool.

The spool module implements a durable on-disk queue:
  {spool_dir}/pending/     — snaps waiting for analyzer
  {spool_dir}/processing/  — snaps claimed and being analyzed

Filenames use the pattern "{YYYY-MM-DDTHH-MM-SS.mmm}_snap.jpg" with a
sibling "{YYYY-MM-DDTHH-MM-SS.mmm}_meta.json". Timestamp is derived from
meta["ts"] (unix epoch float, UTC).

The 5 public functions tested here:
    write_snap(jpeg, meta, spool_dir) -> Path
    claim_next(spool_dir) -> tuple[bytes, dict, Path] | None
    mark_complete(processing_snap_path) -> None
    recover_stranded(spool_dir) -> int
    drop_stale(spool_dir, max_age_seconds) -> int
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cardinal_nest_monitor.spool import (
    claim_next,
    drop_stale,
    mark_complete,
    recover_stranded,
    write_snap,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_jpeg(marker: bytes = b"hello") -> bytes:
    """Return a tiny byte string that stands in for a JPEG payload."""
    return b"\xff\xd8\xff\xe0" + marker + b"\xff\xd9"


def _ts_stem(ts: float) -> str:
    """Derive the expected filename stem ('YYYY-MM-DDTHH-MM-SS.mmm') for a ts."""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H-%M-%S") + f".{dt.microsecond // 1000:03d}"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_write_then_claim_round_trip(tmp_path: Path) -> None:
    """Write a snap; claim_next returns same bytes + meta; processing/ has the pair; pending/ empty."""
    spool = tmp_path / "spool"
    jpeg = _fake_jpeg(b"round-trip")
    meta = {"ts": time.time(), "trigger": "scheduled", "camera": "TEST_CAM"}

    write_snap(jpeg, meta, spool)

    claimed = claim_next(spool)
    assert claimed is not None, "claim_next returned None after a write"
    got_jpeg, got_meta, proc_path = claimed

    assert got_jpeg == jpeg, "claimed JPEG bytes do not match what was written"
    assert got_meta == meta, "claimed meta dict does not match what was written"
    assert proc_path.exists(), "returned processing path does not exist on disk"
    assert proc_path.parent.name == "processing", (
        "claimed snap is not in processing/ subdir"
    )

    # pending/ must be empty after claim
    pending = spool / "pending"
    pending_entries = list(pending.iterdir()) if pending.exists() else []
    assert pending_entries == [], f"pending/ should be empty after claim, got {pending_entries}"

    # processing/ contains the snap + meta pair
    proc_entries = sorted(p.name for p in (spool / "processing").iterdir())
    assert len(proc_entries) == 2, f"processing/ should have 2 files, got {proc_entries}"


def test_claim_newest_first(tmp_path: Path) -> None:
    """Write 3 snaps with ascending ts; claim returns the newest; remaining 2 stay in pending/."""
    spool = tmp_path / "spool"
    base_ts = time.time()
    jpegs = [_fake_jpeg(f"snap-{i}".encode()) for i in range(3)]
    metas = [
        {"ts": base_ts + i * 0.1, "idx": i}  # 100ms apart
        for i in range(3)
    ]
    for jpeg, meta in zip(jpegs, metas):
        write_snap(jpeg, meta, spool)

    claimed = claim_next(spool)
    assert claimed is not None, "claim_next returned None after 3 writes"
    _, got_meta, _ = claimed

    assert got_meta["idx"] == 2, (
        f"claim_next should return the NEWEST (idx=2), got idx={got_meta['idx']}"
    )

    # The other 2 (idx 0 and 1) should still be in pending/
    pending_files = list((spool / "pending").iterdir())
    pending_snaps = [p for p in pending_files if p.name.endswith("_snap.jpg")]
    assert len(pending_snaps) == 2, (
        f"expected 2 snaps still in pending/, got {len(pending_snaps)}: {pending_files}"
    )


def test_claim_returns_none_on_empty_spool(tmp_path: Path) -> None:
    """Empty spool → claim_next is None."""
    spool = tmp_path / "spool"
    # write_snap has not been called; directory may not even exist yet.
    result = claim_next(spool)
    assert result is None, f"claim_next on empty spool should be None, got {result!r}"


def test_claim_skips_snap_without_meta_sibling(tmp_path: Path) -> None:
    """An orphan snap.jpg in pending/ must not crash claim_next."""
    spool = tmp_path / "spool"
    pending = spool / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    (spool / "processing").mkdir(parents=True, exist_ok=True)

    # Drop an orphan snap.jpg with no sibling meta.json
    orphan_stem = _ts_stem(time.time() - 5)
    (pending / f"{orphan_stem}_snap.jpg").write_bytes(_fake_jpeg(b"orphan"))

    # Also write a proper snap via the real API
    good_jpeg = _fake_jpeg(b"good")
    good_meta = {"ts": time.time(), "kind": "good"}
    write_snap(good_jpeg, good_meta, spool)

    # claim_next may either skip the orphan and return the good one, OR return None —
    # the spec allows both, as long as there's no exception.
    try:
        result = claim_next(spool)
    except Exception as exc:  # pragma: no cover — failure path
        pytest.fail(f"claim_next crashed on orphan snap without meta: {exc!r}")

    if result is not None:
        _, got_meta, _ = result
        assert got_meta == good_meta, (
            "when claim_next returns a snap despite an orphan present, "
            "it must be the complete (good) one"
        )


def test_mark_complete_is_idempotent(tmp_path: Path) -> None:
    """Calling mark_complete twice on the same path must not raise."""
    spool = tmp_path / "spool"
    jpeg = _fake_jpeg(b"mark-complete")
    meta = {"ts": time.time()}
    write_snap(jpeg, meta, spool)

    claimed = claim_next(spool)
    assert claimed is not None, "setup: claim_next unexpectedly returned None"
    _, _, proc_path = claimed

    # First call removes it.
    mark_complete(proc_path)
    # Second call on the same (now-missing) path must not raise.
    try:
        mark_complete(proc_path)
    except Exception as exc:  # pragma: no cover — failure path
        pytest.fail(f"mark_complete not idempotent — second call raised {exc!r}")


def test_recover_stranded_moves_processing_back_to_pending(tmp_path: Path) -> None:
    """Fake a stranded pair in processing/ → recover_stranded returns 1, files move back."""
    spool = tmp_path / "spool"
    pending = spool / "pending"
    processing = spool / "processing"
    pending.mkdir(parents=True, exist_ok=True)
    processing.mkdir(parents=True, exist_ok=True)

    # Stage a "stranded" pair directly in processing/
    ts = time.time() - 10
    stem = _ts_stem(ts)
    stranded_jpeg = _fake_jpeg(b"stranded")
    stranded_meta = {"ts": ts, "kind": "stranded"}
    (processing / f"{stem}_snap.jpg").write_bytes(stranded_jpeg)
    (processing / f"{stem}_meta.json").write_text(json.dumps(stranded_meta))

    count = recover_stranded(spool)
    assert count == 1, f"recover_stranded should return 1, got {count}"

    # Files have moved back to pending/
    pending_entries = sorted(p.name for p in pending.iterdir())
    processing_entries = sorted(p.name for p in processing.iterdir())

    assert f"{stem}_snap.jpg" in pending_entries, (
        f"snap.jpg not moved back to pending/, got {pending_entries}"
    )
    assert f"{stem}_meta.json" in pending_entries, (
        f"meta.json not moved back to pending/, got {pending_entries}"
    )
    assert processing_entries == [], (
        f"processing/ should be empty after recover, got {processing_entries}"
    )


def test_drop_stale_deletes_old_entries(tmp_path: Path) -> None:
    """A 2-hour-old snap gets deleted at max_age=1800; a 5s-old one is retained."""
    spool = tmp_path / "spool"
    now = time.time()

    old_meta = {"ts": now - 2 * 3600, "kind": "old"}  # 2 hours ago
    new_meta = {"ts": now - 5, "kind": "new"}         # 5s ago

    write_snap(_fake_jpeg(b"old"), old_meta, spool)
    write_snap(_fake_jpeg(b"new"), new_meta, spool)

    count = drop_stale(spool, max_age_seconds=1800)
    assert count == 1, f"drop_stale should have deleted 1 stale entry, got count={count}"

    # The only snap left in pending/ should be the fresh one.
    pending_snaps = sorted(
        p for p in (spool / "pending").iterdir() if p.name.endswith("_snap.jpg")
    )
    assert len(pending_snaps) == 1, (
        f"expected exactly 1 snap left in pending/, got {len(pending_snaps)}: {pending_snaps}"
    )

    # The retained meta should correspond to the fresh entry.
    remaining_meta_files = [
        p for p in (spool / "pending").iterdir() if p.name.endswith("_meta.json")
    ]
    assert len(remaining_meta_files) == 1, (
        f"expected 1 meta file remaining, got {len(remaining_meta_files)}"
    )
    kept_meta = json.loads(remaining_meta_files[0].read_text())
    assert kept_meta.get("kind") == "new", (
        f"wrong entry retained — expected kind='new', got {kept_meta}"
    )


def test_drop_stale_returns_zero_on_empty(tmp_path: Path) -> None:
    """Fresh / empty spool → drop_stale returns 0."""
    spool = tmp_path / "spool"
    # Do not write anything.
    count = drop_stale(spool, max_age_seconds=1800)
    assert count == 0, f"drop_stale on empty spool should return 0, got {count}"


def test_write_snap_creates_directories_if_missing(tmp_path: Path) -> None:
    """write_snap must create pending/ and processing/ when spool_dir doesn't exist."""
    spool = tmp_path / "does" / "not" / "exist" / "yet"
    assert not spool.exists(), "precondition: spool dir should not exist"

    write_snap(_fake_jpeg(b"bootstrap"), {"ts": time.time()}, spool)

    assert (spool / "pending").is_dir(), "pending/ should have been created"
    assert (spool / "processing").is_dir(), "processing/ should have been created"


def test_atomic_rename_no_tmp_files_visible_to_claim(tmp_path: Path) -> None:
    """After write_snap returns, pending/ contains no .tmp artefacts (atomic rename guard)."""
    spool = tmp_path / "spool"
    for i in range(5):
        write_snap(_fake_jpeg(f"snap-{i}".encode()), {"ts": time.time() + i * 0.01}, spool)

    pending_names = [p.name for p in (spool / "pending").iterdir()]
    tmp_leftovers = [n for n in pending_names if n.endswith(".tmp") or ".tmp" in n]
    assert tmp_leftovers == [], (
        f"pending/ must not contain .tmp leftovers after write_snap, found: {tmp_leftovers}"
    )


def test_meta_round_trip_preserves_dict(tmp_path: Path) -> None:
    """A complex meta dict (nested, None, floats, ints) round-trips through write + claim."""
    spool = tmp_path / "spool"
    jpeg = _fake_jpeg(b"complex")
    meta = {
        "ts": 1718000000.125,
        "trigger": "scheduled",
        "camera": "TEST_CAM",
        "battery": None,
        "retry_count": 0,
        "latency_s": 3.14159,
        "flags": {"nested_bool": True, "nested_str": "ok", "deep": {"value": 42}},
        "tags": ["a", "b", "c"],
    }

    write_snap(jpeg, meta, spool)
    claimed = claim_next(spool)
    assert claimed is not None, "claim_next returned None unexpectedly"
    _, got_meta, _ = claimed

    assert got_meta == meta, (
        f"meta dict did not round-trip cleanly.\n  wrote: {meta}\n  got:   {got_meta}"
    )

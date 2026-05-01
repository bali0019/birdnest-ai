"""Unit tests for birdnest_ai.spool.

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
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from birdnest_ai.spool import (
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


# ---------------------------------------------------------------------------
# Security guard tests (Codex C3): claim_next must reject symlinks, non-regular
# files, and files owned by a different UID. Without these checks, an untrusted
# local process with write access to pending/ could make the analyzer read and
# forward arbitrary local files (e.g. /etc/passwd) into Anthropic/Discord.
# ---------------------------------------------------------------------------


def test_claim_rejects_symlink_in_pending(tmp_path: Path) -> None:
    """A symlinked *_snap.jpg in pending/ must not be claimed — even pointing at a real file."""
    spool = tmp_path / "spool"
    pending = spool / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    (spool / "processing").mkdir(parents=True, exist_ok=True)

    # Decoy file the symlink points at. Could be anything (stand-in for /etc/passwd).
    decoy = tmp_path / "secret.txt"
    decoy.write_bytes(b"SECRET CONTENTS THE ATTACKER WANTS EXFILTRATED")

    # Attacker drops a symlink named like a valid snap + a matching meta.json.
    attacker_stem = _ts_stem(time.time() + 10)  # newer than any real snap we'll write
    attacker_snap = pending / f"{attacker_stem}_snap.jpg"
    attacker_meta = pending / f"{attacker_stem}_meta.json"
    os.symlink(decoy, attacker_snap)
    attacker_meta.write_text(json.dumps({"ts": time.time() + 10, "evil": True}))

    # Also write a legitimate newer pair via the real API (younger ts for tie-break clarity).
    good_jpeg = _fake_jpeg(b"legit")
    good_meta = {"ts": time.time(), "kind": "good"}
    write_snap(good_jpeg, good_meta, spool)

    result = claim_next(spool)

    # The attacker's symlinked snap must NOT be claimed. Either we get the good one
    # (because the attacker's was filtered out) or we get None (if only the symlink was
    # visible), but under NO circumstances should we return the decoy bytes.
    if result is not None:
        got_jpeg, got_meta, proc_path = result
        assert got_jpeg != decoy.read_bytes(), (
            "SECURITY REGRESSION: claim_next returned the symlinked target's contents"
        )
        assert got_jpeg == good_jpeg, (
            f"expected legitimate snap bytes, got unexpected {got_jpeg!r}"
        )
        assert got_meta == good_meta, (
            "expected legitimate meta, got something else"
        )

    # The symlink should still be in pending/ (we skipped it, didn't move or read it).
    assert attacker_snap.is_symlink(), (
        "symlink should still be in pending/ — claim_next must not touch it"
    )


def test_claim_rejects_symlink_when_alone_in_pending(tmp_path: Path) -> None:
    """A symlinked pair alone in pending/ → claim_next returns None."""
    spool = tmp_path / "spool"
    pending = spool / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    (spool / "processing").mkdir(parents=True, exist_ok=True)

    decoy = tmp_path / "decoy.txt"
    decoy.write_bytes(b"not for you")

    stem = _ts_stem(time.time())
    os.symlink(decoy, pending / f"{stem}_snap.jpg")
    # Meta as a real file but snap is a symlink — still unsafe.
    (pending / f"{stem}_meta.json").write_text(json.dumps({"ts": time.time()}))

    result = claim_next(spool)
    assert result is None, (
        f"claim_next on a symlink-only spool must return None, got {result!r}"
    )


def test_claim_rejects_symlinked_meta(tmp_path: Path) -> None:
    """A legit snap.jpg paired with a symlinked meta.json must also be rejected."""
    spool = tmp_path / "spool"
    pending = spool / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    (spool / "processing").mkdir(parents=True, exist_ok=True)

    decoy = tmp_path / "evil_meta.json"
    decoy.write_text(json.dumps({"ts": time.time(), "evil": "yes"}))

    stem = _ts_stem(time.time())
    (pending / f"{stem}_snap.jpg").write_bytes(_fake_jpeg(b"real-snap"))
    os.symlink(decoy, pending / f"{stem}_meta.json")

    result = claim_next(spool)
    # The pair is unsafe because the meta is a symlink — whole pair skipped.
    assert result is None, (
        f"pair with symlinked meta must not be claimed, got {result!r}"
    )


def test_claim_rejects_non_regular_file_fifo(tmp_path: Path) -> None:
    """A FIFO named like a *_snap.jpg in pending/ must be skipped (not a regular file)."""
    if not hasattr(os, "mkfifo"):  # pragma: no cover — Windows
        pytest.skip("os.mkfifo not available on this platform")

    spool = tmp_path / "spool"
    pending = spool / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    (spool / "processing").mkdir(parents=True, exist_ok=True)

    # FIFO at a valid-looking snap path.
    fifo_stem = _ts_stem(time.time() + 10)
    os.mkfifo(str(pending / f"{fifo_stem}_snap.jpg"))
    # Real meta file so the pair would look complete to naive pairing logic.
    (pending / f"{fifo_stem}_meta.json").write_text(json.dumps({"ts": time.time() + 10}))

    # Add a legitimate pair. claim_next must find the legit pair, not hang on / read the FIFO.
    good_jpeg = _fake_jpeg(b"good-alongside-fifo")
    good_meta = {"ts": time.time(), "kind": "good"}
    write_snap(good_jpeg, good_meta, spool)

    result = claim_next(spool)
    assert result is not None, "claim_next should have found the legitimate pair"
    got_jpeg, got_meta, _ = result
    assert got_jpeg == good_jpeg, "claim_next returned something other than the legit snap"
    assert got_meta == good_meta


def test_claim_rejects_directory_at_snap_path(tmp_path: Path) -> None:
    """A directory named like a *_snap.jpg must be treated as non-regular and skipped."""
    spool = tmp_path / "spool"
    pending = spool / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    (spool / "processing").mkdir(parents=True, exist_ok=True)

    # Directory masquerading as a snap file.
    dir_stem = _ts_stem(time.time() + 10)
    (pending / f"{dir_stem}_snap.jpg").mkdir()
    (pending / f"{dir_stem}_meta.json").write_text(json.dumps({"ts": time.time() + 10}))

    # Legit pair alongside.
    good_jpeg = _fake_jpeg(b"good-alongside-dir")
    good_meta = {"ts": time.time(), "kind": "good"}
    write_snap(good_jpeg, good_meta, spool)

    result = claim_next(spool)
    assert result is not None, "claim_next should have returned the legitimate pair"
    got_jpeg, got_meta, _ = result
    assert got_jpeg == good_jpeg
    assert got_meta == good_meta


def test_is_safe_regular_file_helper_direct(tmp_path: Path) -> None:
    """Direct unit test of _is_safe_regular_file covering each rejection branch."""
    from birdnest_ai.spool import _is_safe_regular_file

    # Missing file → False
    missing = tmp_path / "does_not_exist.jpg"
    assert _is_safe_regular_file(missing) is False

    # Regular file owned by current uid → True
    regular = tmp_path / "good.jpg"
    regular.write_bytes(b"ok")
    assert _is_safe_regular_file(regular) is True

    # Symlink → False (even pointing at a regular file we own)
    link = tmp_path / "link.jpg"
    os.symlink(regular, link)
    assert _is_safe_regular_file(link) is False

    # Directory → False (not a regular file)
    directory = tmp_path / "adir"
    directory.mkdir()
    assert _is_safe_regular_file(directory) is False

    # FIFO → False
    if hasattr(os, "mkfifo"):
        fifo = tmp_path / "fifo"
        os.mkfifo(str(fifo))
        assert _is_safe_regular_file(fifo) is False


def test_claim_rejects_wrong_owner_via_mocked_lstat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Monkeypatch os.getuid so a real file appears owned by someone else.

    Creating a real file owned by a different UID requires sudo and is not
    portable across test environments. Instead we flip os.getuid to a value
    that definitely won't match the file's real st_uid, forcing the check to
    reject every real file in the spool.
    """
    spool = tmp_path / "spool"

    # Set up a legit pair via the real API.
    good_jpeg = _fake_jpeg(b"owner-guarded")
    good_meta = {"ts": time.time(), "kind": "good"}
    write_snap(good_jpeg, good_meta, spool)

    # Pre-fetch the real uid then swap getuid to return a definitely-different one.
    real_uid = os.getuid()
    fake_uid = real_uid + 999_999  # guaranteed to not match any real owner
    monkeypatch.setattr("birdnest_ai.spool.os.getuid", lambda: fake_uid)

    result = claim_next(spool)
    assert result is None, (
        f"claim_next must reject files owned by a different uid, got {result!r}"
    )

    # The real pair should still be in pending/ (untouched — we didn't move or delete).
    pending_entries = sorted(p.name for p in (spool / "pending").iterdir())
    assert any(n.endswith("_snap.jpg") for n in pending_entries), (
        "legitimate snap should still be in pending/ after owner rejection"
    )
    assert any(n.endswith("_meta.json") for n in pending_entries), (
        "legitimate meta should still be in pending/ after owner rejection"
    )


def test_normal_claim_still_works_after_security_hardening(tmp_path: Path) -> None:
    """Sanity: the existing happy path keeps working with the new safety checks."""
    spool = tmp_path / "spool"
    jpeg = _fake_jpeg(b"happy-path")
    meta = {"ts": time.time(), "kind": "happy"}

    write_snap(jpeg, meta, spool)

    claimed = claim_next(spool)
    assert claimed is not None
    got_jpeg, got_meta, proc_path = claimed
    assert got_jpeg == jpeg
    assert got_meta == meta
    assert proc_path.parent.name == "processing"

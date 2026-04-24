"""Security tests for cardinal_nest_monitor.blink_client.

Covers the hardened 2FA PIN file handoff:
  * _pin_file_is_safe() rejects symlinks, non-regular files, wrong ownership,
    and permissive modes.
  * _ensure_pin_dir_secure() creates a 0o700 parent directory.
  * _sanitize_clip_timestamp() strips characters outside [A-Za-z0-9_-].

We do NOT exercise _read_2fa_pin() end-to-end (it blocks on polling with
a 5-minute deadline and needs a real Blink flow). Instead we test the
validator primitives directly — they are the load-bearing security fence.

Ownership-rejection (st_uid mismatch) is untestable without sudo in CI,
so we verify it via code-path inspection + an explicit note in the
artifact deliverable rather than a runtime test.
"""

from __future__ import annotations

import os
import stat

import pytest

from cardinal_nest_monitor.blink_client import (
    _ensure_pin_dir_secure,
    _pin_file_is_safe,
    _sanitize_clip_timestamp,
)


# ─── _pin_file_is_safe ──────────────────────────────────────────────────


def test_pin_file_nonexistent_returns_false(tmp_path):
    """A missing PIN file returns False (the caller treats this as "poll again")."""
    missing = tmp_path / "does_not_exist"
    assert _pin_file_is_safe(missing) is False


def test_pin_file_symlink_rejected(tmp_path):
    """A symlink at the PIN path is rejected to prevent hijack.

    Even if the symlink TARGET is a perfectly-owned 0600 file, the path
    itself being a symlink is enough to reject — an attacker could have
    swapped what it points at.
    """
    real = tmp_path / "real_pin"
    real.write_text("123456")
    os.chmod(real, 0o600)

    link = tmp_path / "blink_pin"
    os.symlink(real, link)

    assert _pin_file_is_safe(link) is False


def test_pin_file_dangling_symlink_rejected(tmp_path):
    """A symlink pointing at a non-existent target is still rejected.

    lstat() succeeds on the symlink itself (doesn't follow), and we
    detect S_ISLNK before the S_ISREG check — so we should reject here
    rather than fall through to FileNotFoundError.
    """
    link = tmp_path / "blink_pin"
    os.symlink(tmp_path / "nowhere", link)

    assert _pin_file_is_safe(link) is False


def test_pin_file_directory_rejected(tmp_path):
    """A directory at the PIN path is rejected (not a regular file)."""
    d = tmp_path / "blink_pin"
    d.mkdir()
    os.chmod(d, 0o700)

    assert _pin_file_is_safe(d) is False


def test_pin_file_permissive_mode_group_readable_rejected(tmp_path):
    """A file with group-readable bits set is rejected."""
    p = tmp_path / "blink_pin"
    p.write_text("123456")
    os.chmod(p, 0o640)  # group read

    assert _pin_file_is_safe(p) is False


def test_pin_file_permissive_mode_world_readable_rejected(tmp_path):
    """A file with world-readable bits set is rejected."""
    p = tmp_path / "blink_pin"
    p.write_text("123456")
    os.chmod(p, 0o644)  # world read

    assert _pin_file_is_safe(p) is False


def test_pin_file_permissive_mode_0666_rejected(tmp_path):
    """A file with world-writable bits set is rejected (belt + braces)."""
    p = tmp_path / "blink_pin"
    p.write_text("123456")
    os.chmod(p, 0o666)

    assert _pin_file_is_safe(p) is False


def test_pin_file_valid_0600_regular_file_accepted(tmp_path):
    """A regular file at mode 0600 owned by the current user is accepted."""
    p = tmp_path / "blink_pin"
    p.write_text("123456")
    os.chmod(p, 0o600)

    assert _pin_file_is_safe(p) is True


def test_pin_file_valid_0400_regular_file_accepted(tmp_path):
    """A read-only 0400 file is also acceptable (no group/other bits)."""
    p = tmp_path / "blink_pin"
    p.write_text("123456")
    os.chmod(p, 0o400)

    assert _pin_file_is_safe(p) is True


def test_pin_file_fifo_rejected(tmp_path):
    """A named pipe (FIFO) at the PIN path is rejected.

    Extra defense — FIFOs are regular-file-adjacent but not S_ISREG;
    an attacker could create one that blocks read() or delivers attacker-
    controlled bytes.
    """
    fifo = tmp_path / "blink_pin"
    try:
        os.mkfifo(fifo, mode=0o600)
    except (OSError, AttributeError):
        pytest.skip("os.mkfifo not available on this platform")

    assert _pin_file_is_safe(fifo) is False


# ─── _ensure_pin_dir_secure ─────────────────────────────────────────────


def test_ensure_pin_dir_secure_creates_parent_with_0700(tmp_path):
    """Parent directory is created with mode 0o700 (user-only)."""
    target = tmp_path / "new_subdir" / "blink_pin"
    _ensure_pin_dir_secure(target)

    parent = target.parent
    assert parent.exists()
    mode = stat.S_IMODE(os.stat(parent).st_mode)
    assert mode == 0o700, f"expected 0o700, got 0o{mode:o}"


def test_ensure_pin_dir_secure_corrects_existing_dir_mode(tmp_path):
    """If the parent exists with loose perms, the chmod tightens them."""
    parent = tmp_path / "existing_subdir"
    parent.mkdir()
    os.chmod(parent, 0o755)  # world-executable, group-readable

    target = parent / "blink_pin"
    _ensure_pin_dir_secure(target)

    mode = stat.S_IMODE(os.stat(parent).st_mode)
    assert mode == 0o700, f"expected 0o700 after fix, got 0o{mode:o}"


def test_ensure_pin_dir_secure_is_idempotent(tmp_path):
    """Calling twice in a row does not error."""
    target = tmp_path / "nested" / "deeper" / "blink_pin"
    _ensure_pin_dir_secure(target)
    _ensure_pin_dir_secure(target)  # should not raise
    assert target.parent.exists()


# ─── _sanitize_clip_timestamp ───────────────────────────────────────────


def test_sanitize_clip_timestamp_preserves_alphanum_dash_underscore():
    """[A-Za-z0-9_-] survive untouched."""
    assert _sanitize_clip_timestamp("abc_XYZ-123") == "abc_XYZ-123"


def test_sanitize_clip_timestamp_replaces_colons():
    """Colons (the common blinkpy time separator) become underscores."""
    assert _sanitize_clip_timestamp("2026:04:17T12:34:56") == "2026_04_17T12_34_56"


def test_sanitize_clip_timestamp_replaces_path_separators():
    """Path separators are neutralized — this is the core security property."""
    # "../../etc/passwd" has 6 disallowed chars before "etc" (two pairs of
    # dots separated by slashes, then one trailing slash), then 1 slash
    # after "etc".
    assert _sanitize_clip_timestamp("../../etc/passwd") == "______etc_passwd"
    assert _sanitize_clip_timestamp("a/b\\c") == "a_b_c"


def test_sanitize_clip_timestamp_replaces_spaces_and_special_chars():
    """Everything outside the allowlist is replaced."""
    assert _sanitize_clip_timestamp("a b*c?d$e") == "a_b_c_d_e"


def test_sanitize_clip_timestamp_handles_none():
    """None maps to the sentinel 'unknown' rather than raising."""
    assert _sanitize_clip_timestamp(None) == "unknown"


def test_sanitize_clip_timestamp_handles_non_string():
    """Non-string input is coerced via str()."""
    assert _sanitize_clip_timestamp(12345) == "12345"


def test_sanitize_clip_timestamp_empty_string():
    """Empty input returns empty (no crash)."""
    assert _sanitize_clip_timestamp("") == ""


def test_sanitize_clip_timestamp_unicode_replaced():
    """Non-ASCII characters are out of the allowlist."""
    # "é" is outside [A-Za-z0-9_-] in our strict ASCII allowlist.
    assert _sanitize_clip_timestamp("café") == "caf_"


def test_sanitize_clip_timestamp_traversal_payload():
    """An attacker attempting path traversal via clip['time'] is defanged."""
    payload = "../../../root/.ssh/id_rsa"
    result = _sanitize_clip_timestamp(payload)
    # No slashes, no dots in the output.
    assert "/" not in result
    assert "." not in result
    # And no ".." sequence.
    assert ".." not in result


# ─── _wait_for_next_snap_deadline (burst-cadence fix, 2026-04-23) ───────
#
# The helper is load-bearing for the §21 burst cadence: without mid-wait
# re-evaluation the downloader commits to a 300 s wait right before
# absence is detected, and the 180 s burst window elapses before it ever
# looks at state again. These tests exercise the three invariants that
# the fix has to hold (Codex-vetted):
#   1. shrink observed mid-wait → return early (burst actually engages)
#   2. motion nudge during the wait → return immediately (no cadence floor
#      penalty for real-time events)
#   3. get_interval raising → fall back, don't crash (preserves the
#      pre-existing snap_loop error contract)


import asyncio
import time
from unittest.mock import MagicMock

from cardinal_nest_monitor.blink_client import _wait_for_next_snap_deadline


def _fake_settings(default_interval: int = 300):
    """Minimal settings stub for the wait helper. Only current_snap_interval
    is consulted, and only on the exception path."""
    s = MagicMock()
    s.current_snap_interval = MagicMock(return_value=default_interval)
    return s


async def test_wait_for_next_snap_deadline_advances_when_interval_shrinks_mid_wait():
    """The whole point of the fix: a mid-wait interval shrink must take
    effect on THIS wait, not the NEXT one.

    We simulate the scenario where the downloader had committed to a long
    wait, but in the meantime the analyzer flipped ``in_absence`` True so
    burst cadence engages. Anchor ``last_snap_monotonic`` 25 s in the
    past; first ``get_interval_fn()`` call returns 300 (long wait, would
    have us sleeping at 294 s deadline → remaining 269 s). Second call
    returns 30 (burst engaged → 24 s deadline, already passed because we
    anchored 25 s ago). Helper must return on the second poll tick — not
    wait out the original 300 s.

    Pre-fix single-shot code would have computed the 300 s deadline ONCE
    and slept through the shrink.
    """
    snap_now = asyncio.Event()
    last_snap_monotonic = time.monotonic() - 25.0  # anchor in the past
    settings = _fake_settings()

    calls = {"n": 0}

    def get_interval_fn() -> int:
        calls["n"] += 1
        # First call: pre-absence. Second call: burst engaged.
        return 300 if calls["n"] == 1 else 30

    # Walk-through:
    #   Poll 1: interval=300 → deadline_offset=294; remaining=294-25=269>0
    #           → sleep min(269, 0.05)=0.05 → TimeoutError → continue.
    #   Poll 2: interval=30  → deadline_offset=24;  remaining=24-25=-1<=0
    #           → return. Total elapsed ≈ 0.05 s.
    start = time.monotonic()
    await asyncio.wait_for(
        _wait_for_next_snap_deadline(
            snap_now, last_snap_monotonic, get_interval_fn, settings,
            poll_interval=0.05,
        ),
        timeout=5.0,
    )
    elapsed = time.monotonic() - start

    assert calls["n"] >= 2, "helper did not re-evaluate mid-wait"
    assert elapsed < 1.0, (
        f"helper did not return promptly after shrink; took {elapsed:.2f}s"
    )


async def test_wait_for_next_snap_deadline_motion_nudge_returns_immediately():
    """snap_now.set() during the wait must preempt — motion events are
    peak-priority and must NOT wait for the next 15 s poll boundary."""
    snap_now = asyncio.Event()
    last_snap_monotonic = time.monotonic()
    settings = _fake_settings()

    def get_interval_fn() -> int:
        return 300  # would otherwise wait 294 s

    async def nudge_soon():
        await asyncio.sleep(0.05)
        snap_now.set()

    start = time.monotonic()
    await asyncio.gather(
        _wait_for_next_snap_deadline(
            snap_now, last_snap_monotonic, get_interval_fn, settings,
            poll_interval=15.0,  # real production value; nudge still wins
        ),
        nudge_soon(),
    )
    elapsed = time.monotonic() - start
    # Must return within roughly the nudge delay + a small scheduler
    # slop, NOT at the 15 s poll boundary.
    assert elapsed < 1.0, (
        f"motion nudge should preempt the poll wait; took {elapsed:.2f}s"
    )


async def test_wait_for_next_snap_deadline_falls_back_on_get_interval_exception():
    """get_interval raising must degrade to settings.current_snap_interval,
    not crash the loop. Preserves the pre-helper snap_loop error contract
    (Codex's point #1).
    """
    snap_now = asyncio.Event()
    # Anchor far enough in the past that the fallback-derived deadline is
    # already behind us on the first poll, so the helper returns quickly.
    last_snap_monotonic = time.monotonic() - 100.0
    # Fallback returns 10s (so deadline offset = max(1, 10 - 6) = 4s;
    # elapsed 100 s ≫ 4 s → return on first poll).
    settings = _fake_settings(default_interval=10)

    def get_interval_fn() -> int:
        raise RuntimeError("boom — transient state DB hiccup")

    start = time.monotonic()
    await asyncio.wait_for(
        _wait_for_next_snap_deadline(
            snap_now, last_snap_monotonic, get_interval_fn, settings,
            poll_interval=0.05,
        ),
        timeout=5.0,
    )
    elapsed = time.monotonic() - start
    # Fallback was consulted (MagicMock records the call).
    settings.current_snap_interval.assert_called()
    # And the loop returned cleanly instead of re-raising.
    assert elapsed < 1.0

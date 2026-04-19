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

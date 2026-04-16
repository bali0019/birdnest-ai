"""On-disk spool primitives for the decoupled downloader/analyzer architecture.

The downloader writes raw JPEGs + meta sidecars into ``{spool_dir}/pending/``.
A separate analyzer process claims entries by atomically renaming the pair into
``{spool_dir}/processing/``, runs them through the analysis pipeline, then
deletes them via :func:`mark_complete`. If the analyzer crashes mid-flight,
:func:`recover_stranded` (called at analyzer startup) moves anything stuck in
``processing/`` back to ``pending/`` so it will be retried.

Atomic-rename protocol
----------------------
Every write that matters crosses a filesystem-visible boundary via
``os.rename``, which is atomic on POSIX within a single filesystem. Steps:

1. Write ``snap.jpg.tmp`` + ``meta.json.tmp`` to their final directory (same
   filesystem as the final target so the rename is a single inode op).
2. ``os.fsync`` each temp file, then ``os.fsync`` the enclosing directory to
   ensure the temp names are durably visible.
3. ``os.rename`` the meta sidecar first is tempting, but we rename the snap
   LAST so observers that key off ``snap.jpg`` never see one without its meta
   sibling. Actually, per spec, we rename snap first then meta — readers MUST
   therefore handle the brief window where snap exists but meta does not (the
   ``claim_next`` loop skips such entries).

Crash safety guarantees
-----------------------
* Partial writes leave ``.tmp`` files that are ignored by :func:`claim_next`
  (it only scans for ``_snap.jpg`` / ``_meta.json``).
* A mid-flight rename on the downloader side leaves at worst a lone
  ``_snap.jpg`` in ``pending/`` with no ``_meta.json`` sibling; those are
  skipped until the meta lands.
* A crashed analyzer leaves its claim in ``processing/``; startup recovery
  moves it back to ``pending/`` for a fresh attempt.
* Race between two analyzer workers claiming the same newest file: the loser
  of ``os.rename`` sees ``FileNotFoundError`` and falls through to the next
  candidate.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


_SNAP_SUFFIX = "_snap.jpg"
_META_SUFFIX = "_meta.json"
_FILENAME_TS_FORMAT = "%Y-%m-%dT%H-%M-%S"


def _ts_to_filename(ts: float) -> str:
    """Render a unix epoch float as ``YYYY-MM-DDTHH-MM-SS.mmm`` in UTC.

    Filename-safe (no colons or periods mid-string besides the millisecond
    separator, which callers concatenate with the ``_snap.jpg`` suffix).
    """
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime(_FILENAME_TS_FORMAT) + f".{int(dt.microsecond / 1000):03d}"


def _filename_to_ts(name: str) -> float | None:
    """Parse the ``YYYY-MM-DDTHH-MM-SS.mmm`` prefix of ``name`` to unix epoch.

    Accepts either a bare stem or a full filename; uses the first 23 chars
    (``2026-04-15T18-00-00.000``). Returns ``None`` on any parse failure.
    """
    if len(name) < 23:
        return None
    prefix = name[:23]
    # Expected shape: 10 char date + 'T' + 8 char time + '.' + 3 char millis.
    if len(prefix) != 23 or prefix[10] != "T" or prefix[19] != ".":
        return None
    try:
        date_time = datetime.strptime(prefix[:19], _FILENAME_TS_FORMAT).replace(
            tzinfo=timezone.utc
        )
        millis = int(prefix[20:23])
    except ValueError:
        return None
    return date_time.timestamp() + (millis / 1000.0)


def _ensure_dirs(spool_dir: Path) -> tuple[Path, Path]:
    """Create ``pending/`` and ``processing/`` if missing; return both paths."""
    pending = spool_dir / "pending"
    processing = spool_dir / "processing"
    pending.mkdir(parents=True, exist_ok=True)
    processing.mkdir(parents=True, exist_ok=True)
    return pending, processing


def _fsync_file(path: Path) -> None:
    """fsync a file by path. Best-effort: logs and continues on OSError."""
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError as exc:  # pragma: no cover - unlikely on a freshly-written file
        log.warning("fsync open failed for %s: %s", path, exc)
        return
    try:
        os.fsync(fd)
    except OSError as exc:  # pragma: no cover
        log.warning("fsync failed for %s: %s", path, exc)
    finally:
        os.close(fd)


def _fsync_dir(path: Path) -> None:
    """fsync a directory so freshly-created entries are durable."""
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError as exc:  # pragma: no cover
        log.warning("fsync open failed for dir %s: %s", path, exc)
        return
    try:
        os.fsync(fd)
    except OSError as exc:  # pragma: no cover - some FS don't support dir fsync
        log.debug("dir fsync not supported for %s: %s", path, exc)
    finally:
        os.close(fd)


def write_snap(jpeg: bytes, meta: dict, spool_dir: Path) -> Path:
    """Write a snap + meta sidecar atomically into ``{spool_dir}/pending/``.

    The filename stem is derived from ``meta['ts']`` (unix epoch float)
    rendered in UTC with millisecond precision. The final pair is
    ``{stem}_snap.jpg`` and ``{stem}_meta.json``.

    Atomicity pattern:
      1. Write both ``.tmp`` files, fsync each, fsync ``pending/``.
      2. os.rename the snap ``.tmp`` to its final name.
      3. os.rename the meta ``.tmp`` to its final name.

    Returns the final snap path.
    """
    if "ts" not in meta:
        raise ValueError("meta dict must contain 'ts' (unix epoch float)")

    pending, _ = _ensure_dirs(spool_dir)

    ts_value = float(meta["ts"])
    stem = _ts_to_filename(ts_value)

    snap_final = pending / f"{stem}{_SNAP_SUFFIX}"
    meta_final = pending / f"{stem}{_META_SUFFIX}"
    snap_tmp = pending / f"{stem}{_SNAP_SUFFIX}.tmp"
    meta_tmp = pending / f"{stem}{_META_SUFFIX}.tmp"

    # Write temp files with fsync so the bytes are durable before rename.
    with open(snap_tmp, "wb") as f:
        f.write(jpeg)
        f.flush()
        os.fsync(f.fileno())

    meta_bytes = json.dumps(meta, sort_keys=True, default=str).encode("utf-8")
    with open(meta_tmp, "wb") as f:
        f.write(meta_bytes)
        f.flush()
        os.fsync(f.fileno())

    _fsync_dir(pending)

    # Rename snap first (per spec), then meta. Readers that see a snap without
    # a meta sibling must skip it and retry.
    os.rename(snap_tmp, snap_final)
    os.rename(meta_tmp, meta_final)
    _fsync_dir(pending)

    return snap_final


def _list_candidates(pending: Path) -> list[tuple[float, Path, Path]]:
    """Return eligible (ts, snap_path, meta_path) tuples sorted newest first.

    Only entries where BOTH snap.jpg AND meta.json exist and the filename
    timestamp parses are returned.
    """
    candidates: list[tuple[float, Path, Path]] = []
    try:
        entries = list(pending.iterdir())
    except FileNotFoundError:
        return candidates

    stems_with_snap: dict[str, Path] = {}
    stems_with_meta: dict[str, Path] = {}
    for entry in entries:
        name = entry.name
        if name.endswith(_SNAP_SUFFIX):
            stems_with_snap[name[: -len(_SNAP_SUFFIX)]] = entry
        elif name.endswith(_META_SUFFIX):
            stems_with_meta[name[: -len(_META_SUFFIX)]] = entry

    for stem, snap_path in stems_with_snap.items():
        meta_path = stems_with_meta.get(stem)
        if meta_path is None:
            # Snap without a meta sibling — still being written. Skip.
            continue
        ts = _filename_to_ts(stem)
        if ts is None:
            log.warning("spool: skipping unparseable filename stem %r", stem)
            continue
        candidates.append((ts, snap_path, meta_path))

    candidates.sort(key=lambda t: t[0], reverse=True)
    return candidates


def claim_next(spool_dir: Path) -> tuple[bytes, dict, Path] | None:
    """Atomically claim the newest pending snap. Returns (jpeg, meta, path) or None.

    "Newest" = highest filename timestamp. Both the snap.jpg and meta.json
    must exist in ``pending/`` for the entry to be eligible. The claim is
    performed by ``os.rename``-ing both files into ``processing/``; on a race
    with another worker (FileNotFoundError), the candidate is skipped and
    the next-newest is tried.
    """
    pending, processing = _ensure_dirs(spool_dir)

    candidates = _list_candidates(pending)
    if not candidates:
        return None

    for ts, snap_src, meta_src in candidates:
        snap_dst = processing / snap_src.name
        meta_dst = processing / meta_src.name
        try:
            os.rename(snap_src, snap_dst)
        except FileNotFoundError:
            log.warning(
                "spool: race claiming snap %s (already moved); trying next", snap_src.name
            )
            continue
        # Snap is ours. Try to move meta; if it's gone, roll the snap back to
        # pending so a later claim (once the meta reappears, or never) is
        # consistent.
        try:
            os.rename(meta_src, meta_dst)
        except FileNotFoundError:
            log.warning(
                "spool: claimed snap %s but meta vanished; rolling back", snap_src.name
            )
            try:
                os.rename(snap_dst, snap_src)
            except FileNotFoundError:  # pragma: no cover
                pass
            continue

        # Load the payload from the *processing* paths so we return exactly
        # what we claimed, not something a concurrent writer later overwrote.
        try:
            jpeg_bytes = snap_dst.read_bytes()
            with open(meta_dst, "rb") as f:
                meta_dict = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
            log.warning(
                "spool: failed to load claimed entry %s: %s; discarding",
                snap_dst.name,
                exc,
            )
            # Best-effort cleanup so the corrupt pair doesn't linger.
            for p in (snap_dst, meta_dst):
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass
            continue

        return jpeg_bytes, meta_dict, snap_dst

    return None


def mark_complete(processing_snap_path: Path) -> None:
    """Delete the claimed snap.jpg and its meta.json sibling. Idempotent."""
    snap_path = processing_snap_path
    meta_path = snap_path.with_name(
        snap_path.name[: -len(_SNAP_SUFFIX)] + _META_SUFFIX
    ) if snap_path.name.endswith(_SNAP_SUFFIX) else None

    for p in (snap_path, meta_path):
        if p is None:
            continue
        try:
            p.unlink()
        except FileNotFoundError:
            # Idempotent: already gone, nothing to do.
            pass


def recover_stranded(spool_dir: Path) -> int:
    """Move everything in processing/ back to pending/. Returns snap count recovered."""
    pending, processing = _ensure_dirs(spool_dir)

    try:
        entries = list(processing.iterdir())
    except FileNotFoundError:
        return 0

    recovered_snaps = 0
    for entry in entries:
        if not entry.is_file():
            continue
        dst = pending / entry.name
        try:
            os.rename(entry, dst)
        except FileNotFoundError:
            log.warning(
                "spool: stranded entry %s vanished before recovery", entry.name
            )
            continue
        except OSError as exc:
            log.warning(
                "spool: failed to recover stranded %s: %s", entry.name, exc
            )
            continue
        if entry.name.endswith(_SNAP_SUFFIX):
            recovered_snaps += 1

    if recovered_snaps:
        _fsync_dir(pending)
    return recovered_snaps


def drop_stale(spool_dir: Path, max_age_seconds: int) -> int:
    """Delete pending entries older than ``max_age_seconds`` (by filename ts).

    Returns the count of snap.jpg files dropped. The matching meta.json
    sibling is deleted alongside each snap.
    """
    pending, _ = _ensure_dirs(spool_dir)

    cutoff = time.time() - max_age_seconds
    try:
        entries = list(pending.iterdir())
    except FileNotFoundError:
        return 0

    # Group by stem so we can delete the snap + meta pair together.
    stems: dict[str, dict[str, Path]] = {}
    for entry in entries:
        name = entry.name
        if name.endswith(_SNAP_SUFFIX):
            stems.setdefault(name[: -len(_SNAP_SUFFIX)], {})["snap"] = entry
        elif name.endswith(_META_SUFFIX):
            stems.setdefault(name[: -len(_META_SUFFIX)], {})["meta"] = entry

    dropped = 0
    for stem, pair in stems.items():
        ts = _filename_to_ts(stem)
        if ts is None:
            log.warning("spool: drop_stale skipping unparseable stem %r", stem)
            continue
        if ts >= cutoff:
            continue
        snap_path = pair.get("snap")
        meta_path = pair.get("meta")
        if snap_path is not None:
            try:
                snap_path.unlink()
                dropped += 1
            except FileNotFoundError:
                pass
        if meta_path is not None:
            try:
                meta_path.unlink()
            except FileNotFoundError:
                pass

    return dropped


__all__ = [
    "write_snap",
    "claim_next",
    "mark_complete",
    "recover_stranded",
    "drop_stale",
]

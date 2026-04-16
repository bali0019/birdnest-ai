"""Per-event evidence writer.

For every analyzed snap (alerting or not) the main loop creates a new event
directory under `evidence/YYYY-MM-DD/HH-MM-SS_<severity>_<species>/` and drops
`snap.jpg`, `prefilter.json`, `observation.json`, optionally `clip.mp4`, and
`meta.json`. Sync API — hot path writes are fast enough to not warrant async
overhead and we want the files flushed before the alert fires.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path

from cardinal_nest_monitor.schema import NestObservation, PrefilterResult

log = logging.getLogger(__name__)


_SLUG_RE = re.compile(r"[^a-zA-Z0-9_-]+")


def _slug(s: str | None, fallback: str) -> str:
    if not s:
        return fallback
    out = _SLUG_RE.sub("_", s.strip()).strip("_")
    return out or fallback


class EvidenceWriter:
    """Creates per-event directories and writes artefacts synchronously."""

    def __init__(self, root_dir: Path) -> None:
        self.root_dir = Path(root_dir)

    def new_event_dir(
        self,
        ts: datetime,
        severity: str | None,
        species: str | None,
    ) -> Path:
        """Return a freshly-created unique directory for this event."""
        day = ts.strftime("%Y-%m-%d")
        time_part = ts.strftime("%H-%M-%S")
        sev = _slug(severity, "NONE").upper()
        sp = _slug(species, "unk")
        parent = self.root_dir / day
        parent.mkdir(parents=True, exist_ok=True)

        base = f"{time_part}_{sev}_{sp}"
        candidate = parent / base
        n = 2
        while candidate.exists():
            candidate = parent / f"{base}_{n}"
            n += 1
        candidate.mkdir(parents=True, exist_ok=False)
        log.debug("evidence: created %s", candidate)
        return candidate

    def write_snap(self, event_dir: Path, jpeg_bytes: bytes) -> Path:
        path = event_dir / "snap.jpg"
        path.write_bytes(jpeg_bytes)
        return path

    def write_prefilter(self, event_dir: Path, result: PrefilterResult) -> Path:
        path = event_dir / "prefilter.json"
        path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        return path

    def write_observation(self, event_dir: Path, obs: NestObservation) -> Path:
        path = event_dir / "observation.json"
        path.write_text(obs.model_dump_json(indent=2), encoding="utf-8")
        return path

    def write_verification(self, event_dir: Path, obs: NestObservation) -> Path:
        """Save the blind Opus verification result (if verification ran).

        Stored separately from observation.json so post-hoc review can see
        what the first-pass analyzer saw vs what the verifier saw.
        """
        path = event_dir / "verification.json"
        path.write_text(obs.model_dump_json(indent=2), encoding="utf-8")
        return path

    def write_clip(self, event_dir: Path, mp4_bytes: bytes) -> Path:
        path = event_dir / "clip.mp4"
        path.write_bytes(mp4_bytes)
        return path

    def write_metadata(self, event_dir: Path, meta: dict) -> Path:
        path = event_dir / "meta.json"
        path.write_text(
            json.dumps(meta, indent=2, default=str, ensure_ascii=False),
            encoding="utf-8",
        )
        return path

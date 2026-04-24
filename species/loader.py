"""TOML profile loader + cached runtime accessor.

Uses stdlib ``tomllib`` (Python 3.11+) — no new dependency added.

Failure mode: invalid profiles fail fast at startup with a clear
pydantic validation error. Do NOT swallow ValidationError and fall
back to a default profile; the whole point is that the runtime sees
exactly the species it was configured for.
"""

from __future__ import annotations

import logging
import tomllib
from functools import lru_cache
from pathlib import Path

from species._schema import SpeciesProfile

log = logging.getLogger(__name__)


def load_species_profile(path: str | Path) -> SpeciesProfile:
    """Parse and validate a TOML profile file. Raises if anything is
    malformed.

    Intentionally not cached — callers that want caching should use
    :func:`get_species_profile`, which keys on the configured
    SPECIES_PROFILE_PATH setting.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"species profile not found: {p!s}. Set SPECIES_PROFILE_PATH "
            "in .env to an existing species/*.toml file."
        )
    with p.open("rb") as f:
        data = tomllib.load(f)
    return SpeciesProfile.model_validate(data)


@lru_cache(maxsize=1)
def _get_species_profile_cached(path_str: str) -> SpeciesProfile:
    """lru_cache-keyed helper — one profile per process lifetime."""
    profile = load_species_profile(path_str)
    log.info(
        "loaded species profile: slug=%s common_name=%s threats=%d "
        "ambient=%d",
        profile.species.slug,
        profile.species.common_name,
        len(profile.threats.names),
        len(profile.field_marks.ambient),
    )
    return profile


def get_species_profile() -> SpeciesProfile:
    """Return the active species profile for this process.

    Reads ``SPECIES_PROFILE_PATH`` from the runtime Settings. Cached for
    the lifetime of the process — the profile is immutable once loaded.
    """
    # Deferred import avoids a circular at module-load time (config
    # imports nothing from species; species.loader imports config only
    # when called).
    from cardinal_nest_monitor.config import get_settings

    path_str = str(get_settings().species_profile_path)
    return _get_species_profile_cached(path_str)


def clear_species_profile_cache() -> None:
    """Test helper — drops the lru_cache entry so a subsequent call
    re-reads from disk. Do NOT call in production; the runtime assumes
    the profile is immutable.
    """
    _get_species_profile_cached.cache_clear()

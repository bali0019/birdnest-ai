"""TOML profile loader + cached runtime accessor.

Uses stdlib ``tomllib`` (Python 3.11+) — no new dependency added.

Failure mode: invalid profiles fail fast at startup with a clear
pydantic validation error. Do NOT swallow ValidationError and fall
back to a default profile; the whole point is that the runtime sees
exactly the species it was configured for.

Shipped profile resolution:
  Profiles bundled with the package live under
  ``cardinal_nest_monitor/species/profiles/``. They're installed as
  package data (see pyproject.toml `[tool.setuptools.package-data]`)
  and resolved at runtime via ``importlib.resources``. This means the
  default profile path works regardless of the caller's cwd — e.g.
  when launchd starts the CLI from a WorkingDirectory that doesn't
  contain a species/ directory.

  Users can still override via SPECIES_PROFILE_PATH to point at any
  filesystem path for a custom profile.
"""

from __future__ import annotations

import importlib.resources as _res
import logging
import tomllib
from functools import lru_cache
from pathlib import Path

from cardinal_nest_monitor.species._schema import SpeciesProfile

log = logging.getLogger(__name__)


def builtin_profile_path(slug: str) -> Path:
    """Return the absolute filesystem path to a profile shipped inside
    the package. Works both from the source tree (editable install) and
    from a wheel-installed distribution — uses importlib.resources so
    the location resolves however the package is installed.

    Raises FileNotFoundError if the slug doesn't match a shipped profile.
    """
    filename = f"{slug}.toml"
    try:
        ref = _res.files("cardinal_nest_monitor.species.profiles") / filename
    except ModuleNotFoundError as e:
        raise FileNotFoundError(
            f"cardinal_nest_monitor.species.profiles package not found "
            f"(did you forget `pip install -e .`?): {e}"
        ) from e
    # .files() returns a Traversable; we need a concrete filesystem path.
    # For an editable install this is the real file on disk. For a zipped
    # wheel we'd need to extract via .as_file() — but this project isn't
    # distributed as a zipapp so a direct str() cast works.
    path = Path(str(ref))
    if not path.exists():
        raise FileNotFoundError(
            f"shipped profile not found: {slug!r} "
            f"(looked in {path}). Ships with: "
            f"{[p.name for p in path.parent.iterdir() if p.suffix == '.toml']}"
        )
    return path


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
            "in .env to an existing profile TOML file, or use a shipped "
            "profile (northern_cardinal, american_robin)."
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


def bootstrap_species_profile() -> SpeciesProfile:
    """Eagerly load + validate the active species profile at service startup.

    Call this from each service bootstrap coroutine BEFORE launching any
    loops, so a malformed profile crashes the service immediately with a
    pydantic ValidationError instead of waiting for the first snap to
    surface it. Subsequent calls to get_species_profile() return the cached
    instance — the lru_cache makes the eager call free for the rest of the
    process lifetime.
    """
    return get_species_profile()

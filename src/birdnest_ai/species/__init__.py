"""Species profile package.

Public surface:
    SpeciesProfile               — the validated Pydantic profile model
    load_species_profile(path)   — parse + validate a TOML profile file
    get_species_profile()        — cached accessor for the active profile
                                   (reads SPECIES_PROFILE_PATH setting)
    builtin_profile_path(slug)   — resolve a shipped profile to a filesystem
                                   path via importlib.resources
"""

from birdnest_ai.species._schema import (
    AlertCopy,
    AmbientSpeciesEntry,
    FieldMarks,
    LifecycleTiming,
    PromptContext,
    ReferenceAssets,
    SpeciesIdentity,
    SpeciesProfile,
    SpeciesTarget,
    TargetFieldMarks,
    ThreatFieldMarks,
    Threats,
)
from birdnest_ai.species.loader import (
    builtin_profile_path,
    clear_species_profile_cache,
    get_species_profile,
    load_species_profile,
)

__all__ = [
    "AlertCopy",
    "AmbientSpeciesEntry",
    "FieldMarks",
    "LifecycleTiming",
    "PromptContext",
    "ReferenceAssets",
    "SpeciesIdentity",
    "SpeciesProfile",
    "SpeciesTarget",
    "TargetFieldMarks",
    "ThreatFieldMarks",
    "Threats",
    "builtin_profile_path",
    "clear_species_profile_cache",
    "get_species_profile",
    "load_species_profile",
]

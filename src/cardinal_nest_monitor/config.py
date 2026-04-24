"""Typed configuration loaded from .env / environment.

All other modules pull settings from `get_settings()`. Keep this file
import-light so tests can mock it without booting the world.
"""

from __future__ import annotations

import re
from datetime import time
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


_HOURS_RE = re.compile(r"^(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})$")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Anthropic ───────────────────────────────────────────────────────
    anthropic_api_key: str = Field("", description="sk-ant-...")
    prefilter_model: str = Field("claude-haiku-4-5-20251001")
    analyzer_model: str = Field("claude-sonnet-4-6")
    # Second-pass verification model. Re-analyzes the same image on
    # CRITICAL/HIGH alerts before firing (see verifier.py). Blind second
    # opinion reduces false CRITICALs from single-model misidentifications.
    verification_model: str = Field("claude-opus-4-7")
    # Toggle to disable verification entirely (fall back to one-pass alerts).
    verify_alerts_with_opus: bool = Field(True)

    # Send three crops (full / center-zoom / overview) per snap to the
    # analyzer instead of one full-frame image. Improves recall on subtle
    # thrasher features half-hidden by foliage (§§ 14, 15) at the cost of
    # roughly 2–3x the per-snap Anthropic input-token bill. Default on;
    # set MULTI_IMAGE_ANALYSIS=false to fall back to the single-image path
    # if Anthropic spend becomes an issue.
    multi_image_analysis: bool = Field(True)

    # Lifecycle tracking (2026-04-16). When True, the system detects hatch
    # events, tracks chick presence, suppresses absence alerts during
    # feeding, and fires 🐣/🦅 lifecycle events.
    # Default True as of 2026-04-16 with 2-sighting confirmation guard in
    # place. Set to False in .env to disable without a code deploy if a
    # false positive fires (an escape hatch, not the expected path).
    # Regression: `python -m cardinal_nest_monitor.tools.lifecycle_regression`
    # must pass 13/13 before any analyzer prompt change.
    lifecycle_tracking_enabled: bool = Field(True)

    # Egg-count alerting (CRITICAL egg_loss rule). This camera mounting
    # CANNOT see the eggs reliably (they sit underneath the incubating
    # mother), so egg-count observations are unreliable and must not drive
    # alerts on this deployment. Default False. The rule code remains in
    # events.py for a hypothetical future camera that can see the cup
    # directly — set to True in .env to re-enable. Added 2026-04-17 after
    # a false CRITICAL egg_loss fired on a miscount (egg count 2→1 due to
    # one egg being occluded by the nest rim from this camera angle).
    enable_egg_count_alerts: bool = Field(False)

    # ── Discord ─────────────────────────────────────────────────────────
    discord_webhook_url: str = Field("")
    # Optional second webhook for the per-snap feed channel. Empty = disabled.
    discord_feed_webhook_url: str = Field("")
    # Optional third webhook for aggregated behavior analytics. Empty = disabled.
    # Runs on a dedicated thread pool; zero impact on alert / feed hot paths.
    discord_analytics_webhook_url: str = Field("")
    # Dedicated webhook for lifecycle events (🐣 hatch, 🦅 fledge). When set,
    # hatch/fledge alerts route here instead of the urgent #alerts channel.
    # Keeps the urgent channel reserved for threat alerts and keeps
    # celebration events visually separate. Empty = route to urgent channel
    # (default/fallback behavior if lifecycle channel isn't configured).
    discord_lifecycle_webhook_url: str = Field("")

    # Hours between analytics reports (drift from service start). Default 8h.
    analytics_report_hours: int = Field(8, ge=1, le=48)
    # Wall-clock hour (0–23) to post a daily 24-hour summary report.
    # Runs in ADDITION to the drifting 8h cadence. Set to -1 to disable.
    analytics_daily_hour: int = Field(8, ge=-1, le=23)

    # ── Blink ───────────────────────────────────────────────────────────
    blink_username: str = Field("")
    blink_password: str = Field("")
    blink_camera_name: str = Field("")
    # ISOLATION NOTE (generic-nest-monitor branch, 2026-04-23):
    # Defaults to a separate creds file so the generic service never races
    # the production cardinal service for Blink auth. The cardinal branch on
    # main uses `./blink_credentials.json`; this branch uses the `_generic`
    # variant. If you intentionally want to share a single file (e.g. two
    # cameras, same Blink account), override via `.env`. See BRANCH_NOTES.md
    # for the hard rule about concurrent downloader runtime.
    blink_creds_path: Path = Field(Path("./blink_credentials_generic.json"))

    # ── Cadence ─────────────────────────────────────────────────────────
    snap_interval_seconds: int = Field(300, ge=10, le=600)
    motion_poll_seconds: int = Field(15, ge=10, le=120)
    active_hours: str = Field("00:00-23:59")

    # Quiet hours: snap less frequently to save battery + Anthropic spend.
    # Cardinals sleep on the nest at night. Empty string = disabled.
    quiet_hours: str = Field("23:00-05:00")
    quiet_snap_interval_seconds: int = Field(1800, ge=30, le=3600)

    # Pattern A: cadence used when state.in_absence=True (mom is off the nest
    # ≥ 2 min). Peak predation risk window. Default 60s. Quiet hours override.
    absence_snap_interval_seconds: int = Field(60, ge=15, le=600)

    # Burst cadence: ultra-tight interval for the first N seconds after mom
    # leaves. Peak predation risk window. Drops to this for burst_duration_seconds
    # after in_absence flips True, then relaxes to absence_snap_interval_seconds.
    burst_snap_interval_seconds: int = Field(30, ge=10, le=120)
    burst_duration_seconds: int = Field(180, ge=30, le=900)  # 3 min default

    # ── Operational ─────────────────────────────────────────────────────
    battery_report_hours: int = Field(6, ge=1, le=24)
    heartbeat_hour_local: int = Field(12, ge=0, le=23)
    log_level: str = Field("INFO")

    # ── Service role (two-process launchd deploy) ───────────────────────
    # "combined" keeps today's single-process behavior (default — safe for
    # dev + integration tests). "downloader" runs only the Blink→spool
    # producer; "analyzer" runs only the spool→Discord consumer.
    role: str = Field("combined")

    # When true, all Notifier embeds get a [TEST] prefix in the title + a
    # test-run-timestamp footer line. Used by integration tests that post
    # against a dedicated test Discord channel. Production always sets this
    # to False. Set via TEST_MODE=true env var for integration test runs.
    test_mode: bool = Field(False)

    # Dedicated Discord webhook for ALL integration-test posts. Every one of
    # the three production channels (alerts / feed / analytics) routes here
    # during tests, so the three live channels stay clean. Must be set
    # alongside TEST_MODE=true before running the integration suite.
    discord_test_webhook_url: str = Field("")

    # Force an Opus ground-truth call at least every N seconds, regardless of
    # what Haiku says. Bounds the worst-case "blind window" if Haiku
    # consistently misclassifies (e.g. hallucinates the cardinal on IR images).
    # Set to 0 to disable forced periodic escalation.
    forced_opus_interval_seconds: int = Field(300, ge=0, le=3600)

    # ── Species profile (generic-nest-monitor branch) ───────────────────
    # Path to the TOML profile that drives target/threat identity, prompt
    # rendering, lifecycle timing, and user-facing copy. Loaded once at
    # startup via species.loader.get_species_profile() and cached for the
    # process lifetime. Ships with `species/northern_cardinal.toml` (the
    # current cardinal behavior) and `species/american_robin.toml`
    # (validation target for the refactor).
    species_profile_path: Path = Field(
        Path("./species/northern_cardinal.toml"),
        description=(
            "Path to the active species profile TOML. Override via "
            ".env (SPECIES_PROFILE_PATH) to monitor a different bird."
        ),
    )

    # ── Paths ───────────────────────────────────────────────────────────
    # ISOLATION NOTE (generic-nest-monitor branch, 2026-04-23):
    # Defaults point at `*_generic` paths so a generic-branch deployment
    # NEVER touches the production cardinal service's live state.sqlite,
    # spool, or evidence directories. The cardinal branch on main uses
    # `./data`, `./evidence`, and `./pause.lock`; this branch uses the
    # `_generic` suffixed variants. A `.env` override can repoint any of
    # these if needed (e.g. for running a single-branch dev install).
    data_dir: Path = Field(Path("./data_generic"))
    evidence_dir: Path = Field(Path("./evidence_generic"))
    pause_lock_path: Path = Field(Path("./pause_generic.lock"))

    # ── Spool (two-service decoupled architecture, 2026-04-15) ──────────
    spool_dir: Path = Field(
        Path("./data_generic/spool"),
        description="Filesystem spool for raw snaps. Downloader writes here; "
                    "analyzer claims from here. pending/ and processing/ "
                    "subdirs are created lazily by spool.py.",
    )

    # ── Backfill Discord channel ────────────────────────────────────────
    # Dedicated webhook for alerts on snaps processed from backlog (during
    # analyzer downtime). Urgent channel stays pristine for live alerts.
    discord_backfill_webhook_url: str = Field(
        "",
        description="Discord webhook for [BACKFILL +Nm] embeds. Empty = backfill "
                    "alerts are suppressed entirely (logs only).",
    )
    backfill_max_age_seconds: int = Field(
        1800, ge=60, le=14400,
        description="Snaps in spool older than this are dropped on analyzer "
                    "startup (cost cap — 30 min default, 4 hr max).",
    )
    backfill_live_threshold_seconds: int = Field(
        30, ge=5, le=120,
        description="Snaps younger than this route to the urgent channel; "
                    "older (within backfill_max_age) route to backfill.",
    )

    # ── Derived ─────────────────────────────────────────────────────────
    @property
    def state_db_path(self) -> Path:
        return self.data_dir / "state.sqlite"

    @property
    def active_start(self) -> time:
        return self._parse_hours()[0]

    @property
    def active_end(self) -> time:
        return self._parse_hours()[1]

    def _parse_hours(self) -> tuple[time, time]:
        m = _HOURS_RE.match(self.active_hours.strip())
        if not m:
            raise ValueError(f"ACTIVE_HOURS must be HH:MM-HH:MM, got {self.active_hours!r}")
        h1, m1, h2, m2 = (int(x) for x in m.groups())
        return time(h1, m1), time(h2, m2)

    @field_validator("active_hours")
    @classmethod
    def _validate_hours(cls, v: str) -> str:
        if not _HOURS_RE.match(v.strip()):
            raise ValueError("ACTIVE_HOURS must be HH:MM-HH:MM")
        return v.strip()

    @field_validator("role")
    @classmethod
    def _validate_role(cls, v: str) -> str:
        allowed = {"combined", "downloader", "analyzer"}
        v_clean = v.strip().lower()
        if v_clean not in allowed:
            raise ValueError(
                f"ROLE must be one of {sorted(allowed)}, got {v!r}"
            )
        return v_clean

    @field_validator(
        "discord_webhook_url",
        "discord_feed_webhook_url",
        "discord_analytics_webhook_url",
        "discord_lifecycle_webhook_url",
        "discord_backfill_webhook_url",
        "discord_test_webhook_url",
    )
    @classmethod
    def _validate_discord_webhook(cls, v: str) -> str:
        """Reject non-Discord webhook URLs so a typo can't route alerts to an
        attacker-controlled host or silently drop to a wrong channel.

        Empty is valid (means the corresponding channel is disabled).
        """
        v = v.strip()
        if not v:
            return v  # empty is valid (means feature disabled)
        if not v.startswith("https://discord.com/api/webhooks/"):
            raise ValueError(
                f"Discord webhook URL must start with "
                f"'https://discord.com/api/webhooks/', got: {v[:50]!r}"
            )
        return v

    def in_active_hours(self, now: time) -> bool:
        start, end = self._parse_hours()
        if start <= end:
            return start <= now <= end
        # wraparound (e.g. 22:00-06:00)
        return now >= start or now <= end

    def in_quiet_hours(self, now: time) -> bool:
        """True if `now` falls inside the configured quiet-hours window.
        Returns False if quiet_hours is empty (feature disabled).
        """
        if not self.quiet_hours.strip():
            return False
        m = _HOURS_RE.match(self.quiet_hours.strip())
        if not m:
            return False  # malformed — fail-open to default cadence
        h1, m1, h2, m2 = (int(x) for x in m.groups())
        start = time(h1, m1)
        end = time(h2, m2)
        if start <= end:
            return start <= now <= end
        # wraparound (e.g. 23:00-04:00)
        return now >= start or now <= end

    def current_snap_interval(self, now: time) -> int:
        """The snap interval to use right now: quiet interval if in quiet
        hours, otherwise the default snap_interval_seconds.
        """
        if self.in_quiet_hours(now):
            return self.quiet_snap_interval_seconds
        return self.snap_interval_seconds

    def ensure_dirs(self) -> None:
        """Create runtime directories if missing. Safe to call repeatedly.

        Creates with mode 0o700 (owner-only) AND chmods to 0o700 afterwards
        because mkdir's mode argument is masked by the ambient umask — the
        explicit chmod is belt-and-suspenders so state.sqlite, spool JPEGs,
        and evidence dirs are never world-readable on a shared machine.
        """
        for d in (self.data_dir, self.evidence_dir, self.spool_dir):
            d.mkdir(parents=True, exist_ok=True, mode=0o700)
            d.chmod(0o700)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings. Tests can call get_settings.cache_clear() to reload."""
    return Settings()

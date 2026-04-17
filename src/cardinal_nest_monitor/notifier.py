"""Discord webhook notifier for Cardinal Nest Monitor.

One `Notifier` class. All public send methods return bool (True on Discord's
HTTP 204 success, False otherwise). Transport errors are logged, never raised.
A single aiohttp.ClientSession is created lazily and reused; callers must
`await notifier.close()` on shutdown.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiohttp

from cardinal_nest_monitor.config import get_settings
from cardinal_nest_monitor.schema import AlertDecision, NestObservation, PrefilterResult

log = logging.getLogger(__name__)


# Discord hard limits we care about:
#   field value ≤ 1024 chars, description ≤ 4096, footer text ≤ 2048.
_FIELD_VALUE_MAX = 1024
_DESCRIPTION_MAX = 4096


def _fmt_duration(seconds: int) -> str:
    """Humanise a duration. `Xs`, `Xm Ys`, or `Xh Ym` depending on magnitude."""
    s = int(max(0, seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        m, r = divmod(s, 60)
        return f"{m}m {r}s"
    h, r = divmod(s, 3600)
    m = r // 60
    return f"{h}h {m}m"


def _truncate(s: str, limit: int) -> str:
    if len(s) <= limit:
        return s
    return s[: limit - 1] + "…"


def _footer() -> dict[str, str]:
    settings = get_settings()
    base = f"Cardinal Nest Monitor • {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    if settings.test_mode:
        base += " • [TEST RUN]"
    return {"text": base}


def _title_with_test_prefix(title: str) -> str:
    """Prefix title with [TEST] when test_mode is enabled so integration-test
    posts are clearly distinguishable from real alerts in the Discord UI.
    """
    if get_settings().test_mode:
        return f"[TEST] {title}"
    return title


def _now_iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


class Notifier:
    """Async Discord webhook poster. One instance per process."""

    def __init__(self, webhook_url: str, camera_name: str) -> None:
        self.webhook_url = webhook_url
        self.camera_name = camera_name
        self._session: aiohttp.ClientSession | None = None

    # ── session lifecycle ──────────────────────────────────────────────
    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    # ── public senders ─────────────────────────────────────────────────
    async def send_alert(
        self,
        decision: AlertDecision,
        observation: NestObservation,
        snap_path: Path | None = None,
        prefilter: PrefilterResult | None = None,
        verification_obs: NestObservation | None = None,
        backfill_age_seconds: float | None = None,
    ) -> bool:
        sev = decision.severity
        title = f"{sev.emoji} {sev.value}: {decision.title}"
        settings = get_settings()

        # Lifecycle routing: hatch/fledge events are celebrations, not
        # threats — route them to the dedicated lifecycle channel when
        # configured so the urgent #alerts channel stays focused on
        # threats. Falls back to the urgent channel if the lifecycle
        # webhook isn't configured.
        target_url: str | None = None
        backfill_prefix: str = ""
        if decision.rule_id in ("hatch", "fledge", "egg_laying_begin", "incubation_begin"):
            lifecycle_url = settings.discord_lifecycle_webhook_url
            if lifecycle_url:
                target_url = lifecycle_url

        # Backfill routing: when backfill_age_seconds is set (≥0), this is a
        # stale alert replayed from persisted state — route to the backfill
        # webhook and prefix the title. Takes precedence over lifecycle
        # routing (a stale backfilled hatch alert is more confusing without
        # the [BACKFILL] marker than it is misrouted).
        if backfill_age_seconds is not None and backfill_age_seconds >= 0:
            target_url = settings.discord_backfill_webhook_url
            if not target_url:
                log.warning(
                    "backfill alert suppressed — DISCORD_BACKFILL_WEBHOOK_URL not set"
                )
                return True
            mins = int(backfill_age_seconds // 60)
            backfill_prefix = f"[BACKFILL +{mins}m] "

        fields: list[dict[str, Any]] = [
            {
                "name": "Species",
                "value": _truncate(", ".join(decision.species) or "—", _FIELD_VALUE_MAX),
                "inline": True,
            },
            {
                "name": "Mother present",
                "value": (
                    decision.mother_present.capitalize()
                    if decision.mother_present is not None
                    else "—"
                ),
                "inline": True,
            },
            {
                "name": "Absence",
                "value": (
                    _fmt_duration(decision.absence_seconds)
                    if decision.absence_seconds is not None
                    else "—"
                ),
                "inline": True,
            },
            {
                "name": "Eggs",
                "value": self._fmt_eggs(decision.egg_count_before, decision.egg_count_after),
                "inline": True,
            },
            {
                "name": "Confidence",
                "value": f"{decision.confidence:.0%}",
                "inline": True,
            },
            {
                "name": "Camera",
                "value": self.camera_name or "—",
                "inline": True,
            },
            # Prefilter field (legacy, only shown if prefilter ran — currently disabled)
            *(
                [
                    {
                        "name": f"Prefilter ({settings.prefilter_model})",
                        "value": _truncate(
                            f"`{prefilter.novel_activity}` — {prefilter.reason}",
                            _FIELD_VALUE_MAX,
                        ),
                        "inline": False,
                    }
                ]
                if prefilter is not None
                else []
            ),
            {
                "name": f"Analyzer ({settings.analyzer_model})",
                "value": _truncate(
                    f"confidence {observation.confidence:.0%} — {observation.summary}",
                    _FIELD_VALUE_MAX,
                ),
                "inline": False,
            },
            # Verification field (only shown when Opus second-opinion ran).
            # If we're here with a verification_obs, it means Opus AGREED or
            # DOWNGRADED but still fired an alert. If Opus had fully disagreed
            # (suppressed), send_alert wouldn't have been called.
            *(
                [
                    {
                        "name": f"✓ Verification ({settings.verification_model})",
                        "value": _truncate(
                            f"confidence {verification_obs.confidence:.0%} — {verification_obs.summary}",
                            _FIELD_VALUE_MAX,
                        ),
                        "inline": False,
                    }
                ]
                if verification_obs is not None
                else []
            ),
            {
                "name": "Rule",
                "value": decision.rule_id,
                "inline": False,
            },
        ]

        # Composition order: _title_with_test_prefix first, then [BACKFILL +Nm]
        # OUTSIDE the TEST prefix. Example (both on): "[TEST] [BACKFILL +10m] MEDIUM: ..."
        composed_title = _title_with_test_prefix(title)
        if backfill_prefix:
            # Insert backfill prefix after the [TEST] prefix if present, else at the front.
            if composed_title.startswith("[TEST] "):
                composed_title = "[TEST] " + backfill_prefix + composed_title[len("[TEST] "):]
            else:
                composed_title = backfill_prefix + composed_title

        embed: dict[str, Any] = {
            "title": _truncate(composed_title, 256),
            "description": _truncate(decision.summary, _DESCRIPTION_MAX),
            "color": sev.color,
            "timestamp": _now_iso_utc(),
            "fields": fields,
            "footer": _footer(),
        }

        if snap_path is not None and Path(snap_path).exists():
            embed["image"] = {"url": "attachment://snap.jpg"}
            payload = {"embeds": [embed]}
            return await self._send_multipart(payload, Path(snap_path), url_override=target_url)

        payload = {"embeds": [embed]}
        return await self._send_json(payload, url_override=target_url)

    async def send_battery_status(
        self,
        battery_voltage: float | None,
        battery_state: str | None,
        wifi_strength: int | None,
    ) -> bool:
        state_s = (battery_state or "").lower()
        if state_s == "ok":
            color = 0x32CD32
        elif state_s in ("low", "critical", "bad"):
            color = 0xFF8C00
        else:
            color = 0x808080

        fields = [
            {
                "name": "Battery state",
                "value": battery_state.upper() if battery_state else "—",
                "inline": True,
            },
            {
                "name": "Voltage",
                "value": f"{battery_voltage:.2f} V" if battery_voltage is not None else "—",
                "inline": True,
            },
            {
                "name": "Wi-Fi",
                "value": f"{wifi_strength}" if wifi_strength is not None else "—",
                "inline": True,
            },
            {
                "name": "Camera",
                "value": self.camera_name or "—",
                "inline": True,
            },
        ]
        embed = {
            "title": "🔋 Battery health",
            "description": "Periodic battery-health report.",
            "color": color,
            "timestamp": _now_iso_utc(),
            "fields": fields,
            "footer": _footer(),
        }
        return await self._send_json({"embeds": [embed]})

    async def send_heartbeat(
        self,
        events_today: int,
        alerts_today: int,
        last_mother_seen_minutes_ago: int | None,
        analyzer_success_rate: float,
        cost_estimate_today_usd: float | None,
        lifecycle_stage: str | None = None,
        lifecycle_day_label: str | None = None,
    ) -> bool:
        last_seen = (
            f"{last_mother_seen_minutes_ago}m ago"
            if last_mother_seen_minutes_ago is not None
            else "—"
        )
        cost = (
            f"${cost_estimate_today_usd:.2f}"
            if cost_estimate_today_usd is not None
            else "—"
        )
        fields = [
            {"name": "Events analyzed", "value": str(events_today), "inline": True},
            {"name": "Alerts", "value": str(alerts_today), "inline": True},
            {"name": "Last mother seen", "value": last_seen, "inline": True},
            {
                "name": "Analyzer success rate",
                "value": f"{analyzer_success_rate:.0%}",
                "inline": True,
            },
            {"name": "Est. spend today", "value": cost, "inline": True},
            {"name": "Camera", "value": self.camera_name or "—", "inline": True},
        ]
        if lifecycle_stage:
            stage_value = lifecycle_stage.replace("_", " ").title()
            if lifecycle_day_label:
                stage_value = f"{stage_value} · {lifecycle_day_label}"
            fields.append(
                {"name": "Lifecycle", "value": stage_value, "inline": False},
            )
        embed = {
            "title": "📡 Cardinal Nest Monitor — heartbeat",
            "description": "Daily system-alive report.",
            "color": 0x1E90FF,
            "timestamp": _now_iso_utc(),
            "fields": fields,
            "footer": _footer(),
        }
        return await self._send_json({"embeds": [embed]})

    async def send_test(self) -> bool:
        embed = {
            "title": "🧪 Webhook test",
            "description": "Cardinal Nest Monitor webhook is wired up correctly.",
            "color": 0x00FF00,
            "timestamp": _now_iso_utc(),
            "fields": [
                {"name": "Camera", "value": self.camera_name or "—", "inline": True},
            ],
            "footer": _footer(),
        }
        return await self._send_json({"embeds": [embed]})

    async def send_snap_feed(
        self,
        *,
        ts: float,
        motion_triggered: bool,
        prefilter_text: str | None,
        prefilter_novel: str | None,
        observation_summary: str | None,
        severity: str | None,
        snap_path: Path,
    ) -> bool:
        """Post a single snap to the feed webhook with the JPEG attached.

        Title / color depend on what the system did with this snap:
          - severity present  → severity emoji + label, severity color
          - escalated to Opus → 🔍 Escalated, blue
          - prefilter dropped → 📷 Snap, grey
        """
        # Choose title + color. Four cases:
        #   1. An alert fired → severity color + emoji
        #   2. Single-tier mode (no prefilter): just "📷 Snap" grey
        #   3. Two-tier and the prefilter escalated to the analyzer → blue
        #   4. Two-tier and prefilter dropped (no analyzer call) → grey
        settings_local = get_settings()
        if severity:
            sev_emoji = {
                "CRITICAL": "🚨", "HIGH": "⚠️", "MEDIUM": "🟡", "LOW": "✅"
            }.get(severity, "📷")
            sev_color = {
                "CRITICAL": 0xFF0000, "HIGH": 0xFF8C00,
                "MEDIUM": 0xFFD700, "LOW": 0x32CD32,
            }.get(severity, 0x808080)
            title = f"{sev_emoji} {severity} — {self.camera_name or 'snap'}"
            color = sev_color
        elif prefilter_text is None:
            # Single-tier mode — every snap just says "Snap"
            title = f"📷 Snap — {self.camera_name or 'snap'}"
            color = 0x808080  # grey
        elif observation_summary is not None:
            # Two-tier: prefilter escalated to analyzer
            title = f"🔍 Escalated → {settings_local.analyzer_model} — {self.camera_name or 'snap'}"
            color = 0x1E90FF  # blue
        else:
            # Two-tier: prefilter dropped
            title = f"📷 Snap — {self.camera_name or 'snap'}"
            color = 0x808080  # grey

        local_time = datetime.fromtimestamp(ts).strftime("%H:%M:%S")
        trigger = "motion event" if motion_triggered else "scheduled"
        settings = get_settings()

        # Description: tier layout depends on whether prefilter ran.
        # Single-tier mode (no prefilter): just show the analyzer result.
        # Prefilter mode (legacy, currently disabled):
        desc_parts: list[str] = []
        if prefilter_text is not None and prefilter_novel is not None:
            desc_parts.append(
                f"**Prefilter ({settings.prefilter_model})** — `{prefilter_novel}`: {prefilter_text}"
            )
            if observation_summary:
                desc_parts.append(
                    f"**Analyzer ({settings.analyzer_model})** — {observation_summary}"
                )
        else:
            # Single-tier: just the analyzer
            if observation_summary:
                desc_parts.append(
                    f"**{settings.analyzer_model}** — {observation_summary}"
                )
            else:
                desc_parts.append("_(no observation)_")
        description = _truncate("\n\n".join(desc_parts), _DESCRIPTION_MAX)

        embed: dict[str, Any] = {
            "title": _truncate(_title_with_test_prefix(title), 256),
            "description": description,
            "color": color,
            "timestamp": _now_iso_utc(),
            "fields": [
                {"name": "Trigger", "value": trigger, "inline": True},
                {"name": "Time", "value": local_time, "inline": True},
            ],
            "footer": {"text": "Cardinal Nest Monitor • feed"},
            "image": {"url": "attachment://snap.jpg"},
        }

        snap_path = Path(snap_path)
        if snap_path.exists():
            return await self._send_multipart({"embeds": [embed]}, snap_path)
        # Fallback: post text-only if JPEG is missing for any reason
        embed.pop("image", None)
        return await self._send_json({"embeds": [embed]})

    async def send_analytics_report(self, report: dict[str, Any]) -> bool:
        """Format an analytics report (from analytics.compute_report) as a
        Discord embed and post it. Text-only, no image attachment.
        """
        window_hours = int(report.get("window_hours", 0))
        start_ts = float(report["window_start_ts"])
        end_ts = float(report["window_end_ts"])
        presence = report["presence"]
        trips = report["trips"]
        threats = report["threats"]
        alerts = report["alerts"]
        system = report["system"]

        total_s = presence["on_nest_s"] + presence["off_nest_s"] + presence["unknown_s"]
        pct_on = (presence["on_nest_s"] / total_s) if total_s > 0 else 0.0
        pct_off = (presence["off_nest_s"] / total_s) if total_s > 0 else 0.0

        start_local = datetime.fromtimestamp(start_ts).strftime("%Y-%m-%d %H:%M")
        end_local = datetime.fromtimestamp(end_ts).strftime("%H:%M")

        fields: list[dict[str, Any]] = []

        # ── Presence block ─────────────────────────────────────────────
        presence_lines = [
            f"On nest: **{_fmt_duration(presence['on_nest_s'])}** ({pct_on:.0%})",
            f"Off nest: **{_fmt_duration(presence['off_nest_s'])}** ({pct_off:.0%})",
        ]
        if presence["unknown_s"] > 0:
            pct_unk = presence["unknown_s"] / total_s
            presence_lines.append(
                f"Unknown (low-confidence or no data): {_fmt_duration(presence['unknown_s'])} ({pct_unk:.0%})"
            )
        fields.append({
            "name": "🏠 Presence",
            "value": "\n".join(presence_lines),
            "inline": False,
        })

        # ── Trips block ────────────────────────────────────────────────
        trip_lines = [f"Count: **{trips['trip_count']}**"]
        if trips["trip_count"] > 0:
            trip_lines.append(
                f"Average: {_fmt_duration(trips['avg_duration_s'])}"
            )
            if trips["longest"] is not None:
                longest = trips["longest"]
                longest_leave = datetime.fromtimestamp(
                    longest["leave_ts"]
                ).strftime("%H:%M")
                trip_lines.append(
                    f"Longest: {_fmt_duration(longest['duration_s'])} (left {longest_leave})"
                )
            # List individual trips, truncated
            trip_list = ", ".join(
                f"{_fmt_duration(t['duration_s'])} ({datetime.fromtimestamp(t['leave_ts']).strftime('%H:%M')})"
                for t in trips["trip_records"][:10]
            )
            if trip_list:
                trip_lines.append(f"Trips: {trip_list}")
            if len(trips["trip_records"]) > 10:
                trip_lines.append(
                    f"…and {len(trips['trip_records']) - 10} more"
                )
        if trips["currently_away"]:
            trip_lines.append(
                f"⚠️ **Currently away** for {_fmt_duration(trips['currently_away_duration_s'])}"
            )
        fields.append({
            "name": "🐦 Foraging trips",
            "value": _truncate("\n".join(trip_lines), _FIELD_VALUE_MAX),
            "inline": False,
        })

        # ── Threats block (only if there were any) ─────────────────────
        if threats["total_events"] > 0:
            species_str = ", ".join(
                f"{sp} × {cnt}" for sp, cnt in threats["by_species"].items()
            )
            threat_lines = [
                f"Events: **{threats['total_events']}**",
                f"Species: {species_str}",
                f"Near-nest events: {threats['near_nest_events']}",
            ]
            fields.append({
                "name": "⚠️ Threats detected",
                "value": _truncate("\n".join(threat_lines), _FIELD_VALUE_MAX),
                "inline": False,
            })

        # ── Alerts block ───────────────────────────────────────────────
        alert_lines = [f"Total: **{alerts['total']}**"]
        if alerts["total"] > 0:
            sev_order = ["CRITICAL", "HIGH", "MEDIUM", "LOW"]
            sev_emojis = {"CRITICAL": "🚨", "HIGH": "⚠️", "MEDIUM": "🟡", "LOW": "✅"}
            for sev in sev_order:
                cnt = alerts["by_severity"].get(sev, 0)
                if cnt > 0:
                    alert_lines.append(f"{sev_emojis[sev]} {sev} × {cnt}")
            if alerts["by_rule"]:
                rule_str = ", ".join(
                    f"{rule}×{cnt}" for rule, cnt in alerts["by_rule"].items()
                )
                alert_lines.append(f"Rules fired: {rule_str}")
        fields.append({
            "name": "🔔 Alerts",
            "value": _truncate("\n".join(alert_lines), _FIELD_VALUE_MAX),
            "inline": False,
        })

        # ── System health block ────────────────────────────────────────
        system_lines = [
            f"Snaps: **{system['snaps_taken']}** "
            f"(failures: {system['analyzer_failures']})",
            f"Analyzer: `{system['analyzer_model']}`",
            f"Estimated cost (window): **${system['cost_window_usd']:.2f}**",
        ]
        fields.append({
            "name": "🛠 System",
            "value": "\n".join(system_lines),
            "inline": False,
        })

        embed: dict[str, Any] = {
            "title": f"📊 Behavior Report — last {window_hours}h",
            "description": f"Window: {start_local} → {end_local} local time",
            "color": 0x1E90FF,  # blue
            "timestamp": _now_iso_utc(),
            "fields": fields,
            "footer": {
                "text": f"Cardinal Nest Monitor • analytics • {window_hours}h window",
            },
        }
        return await self._send_json({"embeds": [embed]})

    async def send_system_message(
        self, title: str, body: str, color: int = 0x808080
    ) -> bool:
        embed = {
            "title": _truncate(_title_with_test_prefix(title), 256),
            "description": _truncate(body, _DESCRIPTION_MAX),
            "color": color,
            "timestamp": _now_iso_utc(),
            "footer": _footer(),
        }
        return await self._send_json({"embeds": [embed]})

    async def send_lifecycle_event(
        self,
        stage: str,
        title: str,
        summary: str,
        snap_path: "Path | None" = None,
    ) -> bool:
        """Celebration-style embed for hatch (🐣) / fledge (🦅) events.

        Uses a distinct green color to visually separate from predator
        alerts (red/orange). When snap_path is provided, the image is
        attached so users can see what the system saw at the moment of
        the transition.
        """
        embed = {
            "title": _truncate(_title_with_test_prefix(title), 256),
            "description": _truncate(summary, _DESCRIPTION_MAX),
            "color": 0x32CD32,  # lime green — celebration
            "timestamp": _now_iso_utc(),
            "fields": [
                {"name": "Stage", "value": stage, "inline": True},
                {"name": "Camera", "value": self.camera_name or "—", "inline": True},
            ],
            "footer": _footer(),
        }
        if snap_path is not None and snap_path.exists():
            embed["image"] = {"url": f"attachment://{snap_path.name}"}
            return await self._send_multipart(
                payload={"embeds": [embed]}, snap_path=snap_path
            )
        return await self._send_json({"embeds": [embed]})

    # ── internals ──────────────────────────────────────────────────────
    @staticmethod
    def _fmt_eggs(before: int | None, after: int | None) -> str:
        if before is not None and after is not None:
            return f"{before} → {after}"
        if after is not None:
            return str(after)
        if before is not None:
            return str(before)
        return "—"

    async def _send_json(
        self, payload: dict[str, Any], url_override: str | None = None
    ) -> bool:
        target = url_override if url_override else self.webhook_url

        async def do_post() -> tuple[int, str]:
            session = await self._get_session()
            async with session.post(
                target,
                json=payload,
                headers={"Content-Type": "application/json"},
            ) as resp:
                body = "" if resp.status == 204 else await resp.text()
                return resp.status, body

        return await self._post_with_retry(do_post)

    async def _send_multipart(
        self,
        payload: dict[str, Any],
        image_path: Path,
        url_override: str | None = None,
    ) -> bool:
        try:
            image_bytes = image_path.read_bytes()
        except OSError as e:
            log.error("discord: cannot read snap %s: %s", image_path, e)
            return False

        target = url_override if url_override else self.webhook_url

        async def do_post() -> tuple[int, str]:
            session = await self._get_session()
            form = aiohttp.FormData()
            form.add_field("payload_json", json.dumps(payload))
            form.add_field(
                "file",
                image_bytes,
                filename="snap.jpg",
                content_type="image/jpeg",
            )
            async with session.post(target, data=form) as resp:
                body = "" if resp.status == 204 else await resp.text()
                return resp.status, body

        return await self._post_with_retry(do_post)

    async def _post_with_retry(
        self, do_post: Callable[[], Awaitable[tuple[int, str]]]
    ) -> bool:
        for attempt in (1, 2):
            try:
                # Hard 15s bound on every Discord POST. Normal p99 is <1s.
                # If the webhook stalls (which it did during the 2026-04-13
                # outage), don't block the caller forever — log and retry
                # once, then give up so the pipeline stays unblocked.
                status, body = await asyncio.wait_for(do_post(), timeout=15)
            except asyncio.TimeoutError:
                log.error(
                    "discord: POST timed out after 15s (attempt %d)", attempt,
                )
                if attempt == 1:
                    await asyncio.sleep(1.0)
                    continue
                return False
            except aiohttp.ClientError as e:
                log.error("discord: transport error (attempt %d): %s", attempt, e)
                if attempt == 1:
                    await asyncio.sleep(1.0)
                    continue
                return False

            # Discord returns 204 for JSON-only webhook posts (default) and
            # 200 (with the created message body) for multipart uploads.
            # Both indicate success.
            if status in (200, 204):
                return True
            if 500 <= status < 600 and attempt == 1:
                log.warning(
                    "discord: HTTP %d (attempt %d), retrying after 1s: %s",
                    status, attempt, body[:500],
                )
                await asyncio.sleep(1.0)
                continue
            log.error("discord: HTTP %d: %s", status, body[:2000])
            return False

        return False

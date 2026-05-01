"""Single-tier analyzer using Claude Sonnet 4.6 (configurable via ANALYZER_MODEL).

Produces a structured NestObservation that drives the rules engine. Every snap
goes through this analyzer directly (no prefilter in current single-tier mode).
"""

from __future__ import annotations

import asyncio
import logging

import anthropic
from anthropic import AsyncAnthropic

from cardinal_nest_monitor._image import downscale_jpeg_b64, prepare_multi_image
from cardinal_nest_monitor.config import get_settings
from cardinal_nest_monitor.prompts import render_analyzer_system_prompt
from cardinal_nest_monitor.schema import NestObservation, build_nest_tool
from cardinal_nest_monitor.species import get_species_profile

log = logging.getLogger(__name__)


# Hard outer bound on the Anthropic HTTP call. Load-bearing — this is
# the floor that prevented the 2026-04-15 outage from becoming an 8-hour
# one. Tuned to p99 × 3: normal analyze() is 2–6s, heavy load 15–30s,
# 60s is plenty. See CLAUDE.md §19 for the full timeout budget table.
#
# Exported (not module-private) so tests can monkeypatch it to run the
# hard-timeout regression guard in ~1s instead of ~60s. Both the inner
# wait_for below AND main.py's outer wait_for around analyze() reference
# this constant so they shrink together — a test can never leave the
# outer bound at 60s while the inner is at 1s (or vice-versa), which
# would make the race different from production.
HARD_TIMEOUT_SECONDS: float = 60.0




# Module-level client cache — lazy-initialised so tests can monkeypatch.
_client: AsyncAnthropic | None = None


def _get_client() -> AsyncAnthropic:
    global _client
    if _client is None:
        _client = AsyncAnthropic(api_key=get_settings().anthropic_api_key)
    return _client


async def analyze(
    jpeg_bytes: bytes,
    *,
    model_override: str | None = None,
    extra_user_text: str | None = None,
) -> NestObservation:
    """Run the analyzer on a single JPEG frame.

    Args:
        jpeg_bytes: raw JPEG bytes (downscaled internally).
        model_override: override the default settings.analyzer_model (e.g.
            to call Opus for verification passes).
        extra_user_text: additional text appended to the user message (e.g.
            a verification nudge). MUST NOT include any hint about what a
            prior model said — this would introduce anchoring bias and
            defeat the blind-second-opinion guarantee.

    Retries once on 5xx / timeout with 2s backoff. Raises on hard failure.
    """
    settings = get_settings()
    client = _get_client()
    model = model_override or settings.analyzer_model

    # Multi-image mode sends three deterministic crops of the same snap
    # (full frame / center zoom / ~512px overview). This roughly triples
    # the per-snap Anthropic input-token cost — ~$0.01 → ~$0.02–0.03 — but
    # materially improves recall on small thrasher-vs-cardinal features
    # half-hidden by foliage (see CLAUDE.md §§ 14, 15). Toggle off via
    # MULTI_IMAGE_ANALYSIS=false if cost becomes an issue.
    user_content: list[dict] = []
    if settings.multi_image_analysis:
        user_content.append(
            {
                "type": "text",
                "text": (
                    "Here are three views of the same snap: full frame, "
                    "center crop (zoomed), and overview."
                ),
            }
        )
        user_content.extend(prepare_multi_image(jpeg_bytes))
    else:
        b64 = downscale_jpeg_b64(jpeg_bytes, max_width=1280)
        user_content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": b64,
                },
            }
        )
    if extra_user_text:
        user_content.append({"type": "text", "text": extra_user_text})

    messages = [{"role": "user", "content": user_content}]

    profile = get_species_profile()
    system = [
        {
            "type": "text",
            "text": render_analyzer_system_prompt(profile),
            "cache_control": {"type": "ephemeral"},
        }
    ]

    last_err: Exception | None = None
    for attempt in range(2):
        try:
            # Hard outer bound on the HTTP call — the SDK's internal timeout
            # has failed us under odd network conditions in the past (hung
            # forever). HARD_TIMEOUT_SECONDS (module-level) is the shared
            # budget used by both this inner bound AND main.py's outer
            # bound around analyze(); tests patch the constant so both
            # shrink together. See CLAUDE.md §19.
            nest_tool = build_nest_tool(profile)
            response = await asyncio.wait_for(
                client.messages.create(
                    model=model,
                    max_tokens=1024,
                    system=system,
                    tools=[nest_tool],
                    tool_choice={"type": "tool", "name": "report_nest"},
                    messages=messages,
                ),
                timeout=HARD_TIMEOUT_SECONDS,
            )
            break
        except asyncio.TimeoutError:
            # Hard outer bound fired. Don't retry — let the caller decide.
            log.warning("analyzer hard timeout after 60s (attempt %d)", attempt + 1)
            raise
        except anthropic.APITimeoutError as e:
            last_err = e
            log.warning("analyzer timeout (attempt %d)", attempt + 1)
        except anthropic.APIError as e:
            status = getattr(e, "status_code", None)
            if status is not None and 500 <= status < 600:
                last_err = e
                log.warning("analyzer 5xx (status=%s, attempt %d)", status, attempt + 1)
            else:
                raise
        if attempt == 0:
            await asyncio.sleep(2.0)
    else:
        assert last_err is not None
        raise last_err

    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "report_nest":
            return NestObservation(**block.input)
    raise RuntimeError("analyzer: no report_nest tool_use block in response")

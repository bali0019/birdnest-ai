"""Tier-1 quick-scan prefilter using Claude Haiku 4.5.

Decides whether a given JPEG shows novel activity worth escalating to the
full Opus analyzer. Cheap (~$0.003/call) and fast (~1-2s).
"""

from __future__ import annotations

import asyncio
import logging

import anthropic
from anthropic import AsyncAnthropic

from birdnest_ai._image import downscale_jpeg_b64
from birdnest_ai.config import get_settings
from birdnest_ai.prompts import render_prefilter_system_prompt
from birdnest_ai.schema import PrefilterResult, build_prefilter_tool
from birdnest_ai.species import get_species_profile

log = logging.getLogger(__name__)


# Module-level client cache — lazy-initialised so tests can monkeypatch.
_client: AsyncAnthropic | None = None


def _get_client() -> AsyncAnthropic:
    global _client
    if _client is None:
        _client = AsyncAnthropic(api_key=get_settings().anthropic_api_key)
    return _client


async def prefilter(jpeg_bytes: bytes) -> PrefilterResult:
    """Run the Haiku prefilter on a single JPEG frame.

    Retries once on 5xx / timeout with 2s backoff. Raises on hard failure.
    """
    settings = get_settings()
    client = _get_client()
    b64 = downscale_jpeg_b64(jpeg_bytes, max_width=640)

    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": b64,
                    },
                },
            ],
        }
    ]

    profile = get_species_profile()
    system = [
        {
            "type": "text",
            "text": render_prefilter_system_prompt(profile),
            "cache_control": {"type": "ephemeral"},
        }
    ]

    prefilter_tool = build_prefilter_tool(profile)
    last_err: Exception | None = None
    for attempt in range(2):
        try:
            response = await client.messages.create(
                model=settings.prefilter_model,
                max_tokens=200,
                system=system,
                tools=[prefilter_tool],
                tool_choice={"type": "tool", "name": "report_prefilter"},
                messages=messages,
            )
            break
        except anthropic.APITimeoutError as e:
            last_err = e
            log.warning("prefilter timeout (attempt %d)", attempt + 1)
        except anthropic.APIError as e:
            status = getattr(e, "status_code", None)
            if status is not None and 500 <= status < 600:
                last_err = e
                log.warning("prefilter 5xx (status=%s, attempt %d)", status, attempt + 1)
            else:
                raise
        if attempt == 0:
            await asyncio.sleep(2.0)
    else:
        assert last_err is not None
        raise last_err

    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "report_prefilter":
            return PrefilterResult(**block.input)
    raise RuntimeError("prefilter: no report_prefilter tool_use block in response")

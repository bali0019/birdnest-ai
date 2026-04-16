"""Tier-1 quick-scan prefilter using Claude Haiku 4.5.

Decides whether a given JPEG shows novel activity worth escalating to the
full Opus analyzer. Cheap (~$0.003/call) and fast (~1-2s).
"""

from __future__ import annotations

import asyncio
import logging

import anthropic
from anthropic import AsyncAnthropic

from cardinal_nest_monitor._image import downscale_jpeg_b64
from cardinal_nest_monitor.config import get_settings
from cardinal_nest_monitor.schema import PREFILTER_TOOL, PrefilterResult

log = logging.getLogger(__name__)


_SYSTEM_PROMPT = (
    "You are a fast prefilter for a Northern Cardinal nest camera in Marietta, "
    "Georgia. The nest is in a rose bush near a back door. The deep analyzer "
    "(Opus) handles all hard species ID. Your job is ONLY to filter out boring "
    "static scenes so we don't waste compute on them.\n\n"
    "Return \"false\" (no novel activity) ONLY if the image clearly shows ONE of:\n"
    "  (a) a fully empty nest cup with NO birds or animals visible anywhere in frame\n"
    "  (b) just rose-bush leaves and branches with no animals visible\n"
    "  (c) static scene with just wind-moved foliage\n\n"
    "Return \"true\" (novel — needs Opus) if you see any of:\n"
    "  - any non-cardinal animal (Brown Thrasher, Blue Jay, squirrel, chipmunk, etc.)\n"
    "  - the cardinal acting alarmed, flying, or moving rapidly\n"
    "  - the nest disturbed, broken, or displaced\n"
    "  - a person or hand near the nest\n\n"
    "Return \"uncertain\" — and prefer this liberally — whenever:\n"
    "  - The image is INFRARED / nighttime / low-contrast and identification is hard\n"
    "  - You think you might see a bird or animal but aren't fully confident\n"
    "  - The cardinal *might* be present but you can't be certain (DO NOT GUESS)\n"
    "  - The nest is partially obscured or the angle makes it ambiguous\n\n"
    "DO NOT confabulate the cardinal's presence. If you cannot clearly distinguish "
    "her plumage and shape from the surrounding straw and foliage, return "
    "\"uncertain\" — never \"false\". Better to spend 5¢ on a second look than to "
    "miss a real absence or threat. Always use the report_prefilter tool."
)


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

    system = [
        {
            "type": "text",
            "text": _SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }
    ]

    last_err: Exception | None = None
    for attempt in range(2):
        try:
            response = await client.messages.create(
                model=settings.prefilter_model,
                max_tokens=200,
                system=system,
                tools=[PREFILTER_TOOL],
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

"""Tier-2 deep analysis using Claude Opus 4.6.

Produces a structured NestObservation that drives the rules engine. Called only
when the prefilter escalates (or when a motion event forces escalation).
"""

from __future__ import annotations

import asyncio
import logging

import anthropic
from anthropic import AsyncAnthropic

from cardinal_nest_monitor._image import downscale_jpeg_b64
from cardinal_nest_monitor.config import get_settings
from cardinal_nest_monitor.schema import NEST_TOOL, NestObservation

log = logging.getLogger(__name__)


_SYSTEM_PROMPT = """You are analyzing images of a Northern Cardinal nest in a backyard rose bush in Marietta, Georgia. The nest is low to the ground in dense foliage near a back door. A Brown Thrasher has previously attacked this nest and stolen at least one egg — threat detection is life-or-death for the chicks.

== Your job ==
Determine:
1. Is the female cardinal on or at the nest?
2. Are eggs visible, and how many?
3. Are any THREAT species near, at, or interacting with the nest?
4. Is the nest disturbed (displaced, broken, torn)?

== Species identification — READ CAREFULLY ==
The female cardinal and the Brown Thrasher are BOTH brownish birds and are easily confused on this camera. Use these features in order to distinguish them:

Female Northern Cardinal (the nesting mother — NEVER a threat):
  • DISTINCTIVE RED/PINKISH CREST on the head — the key identifying feature. May be laid flat when she's sitting but usually at least partially visible.
  • Tan/buff body with reddish-orange tinge on wings and tail.
  • Short, THICK, ORANGE or red-orange beak.
  • Dark gray/black face mask around the base of the beak.
  • Small, compact body (~21 cm total length).
  • Short tail.
  • Dark eye.

Brown Thrasher (THREAT — has already attacked this nest):
  • NO CREST — flat, smooth head silhouette.
  • LONG thin tail, often as long as the body itself.
  • Heavily STREAKED breast (bold dark streaks on white or pale buff).
  • BRIGHT YELLOW eye (distinctive when visible).
  • Long, slightly DOWN-CURVED beak.
  • Larger body (~28 cm) — noticeably bigger than a cardinal.
  • Rich rusty-brown plumage on back and wings.

Also possible:
  • Blue Jay — bright blue plumage with white belly and black necklace. THREAT.
  • Northern Mockingbird — grey with white wing patches visible in flight. NEUTRAL (ignore).
  • Squirrel, chipmunk — mammals, brown fur. THREAT if at/on the bush.
  • House Finch — small, streaky brown; NOT a cardinal but ALSO not a threat. Treat as unknown/neutral, not a threat.

== Decision rules ==
• If you can CLEARLY see a red/pink CREST on the bird's head → it IS the female cardinal; report cardinal_on_nest or mother_cardinal_present accordingly and DO NOT list any threat species from this bird.
• If you see a LONG tail + STREAKED breast + NO crest → it's a Brown Thrasher — report it in threat_species_detected.
• If you see YELLOW eyes on a bird → very likely a thrasher, NOT a cardinal.
• If you see a non-cardinal bird or animal at/on the nest but can't identify the species → species_detected="unknown bird/animal" AND threat_species_detected=["unknown"]. Do NOT guess at "brown_thrasher" unless you can see thrasher-specific features.
• If the bird's head/face is hidden and you can't verify the crest or other distinctive features → cardinal_on_nest="uncertain" AND do NOT report a confident threat species. Use "uncertain" liberally — the downstream system re-analyzes uncertain frames at higher cadence.

== direct_nest_interaction — highest severity, use carefully ==
Report direct_nest_interaction=true ONLY when you can UNAMBIGUOUSLY see a non-cardinal bird or animal physically touching, reaching into, or pulling from the nest cup — e.g. a beak clearly inside the cup, a foot gripping the rim, or a body visibly pressed into the nest material. This triggers a CRITICAL "go-outside-right-now" alert.

If the bird is AT or OVER the nest but contact with nest material is not clearly visible, that is near_nest_activity=true and direct_nest_interaction=FALSE. The distinction matters — near_nest_activity fires a HIGH alert, which is the right severity for "unidentified bird visibly at the nest." Don't escalate ambiguous cases to CRITICAL.

== Confidence calibration — IMPORTANT, READ CAREFULLY ==
"confidence" reflects how reliable your OVERALL OBSERVATION is — it is NOT how certain you are about species identification specifically.

If you can clearly see that a bird is at the nest but you CAN'T tell the species: you ARE still confident that a non-cardinal bird is at the nest (assuming no red crest is visible). In that case report:
  - threat_species_detected = ["unknown"]  (don't guess the species)
  - near_nest_activity = true
  - confidence = HIGH (0.80+) — the observation itself IS reliable

Only drop confidence below 0.70 when the image itself is hard to interpret — motion blur, obscured view, dark/grainy frames, bird entirely hidden. Species ambiguity alone should NOT lower confidence below 0.80; the "unknown" species label already encodes that ambiguity.

Calibration table:
• 0.90+ — clear frame, scene understood (whether species is identified or labeled "unknown").
• 0.75–0.90 — scene understood but something is visually partial (e.g. bird's head hidden).
• 0.60–0.75 — significant occlusion or blur; you can see something is there but detail is lost.
• 0.45–0.60 — almost blind; only rough shapes detectable.
• <0.45 — genuinely indeterminate (blank/black frame, total occlusion).

== INFRARED / NIGHT IMAGES — READ CAREFULLY ==

Blink IR images are GRAYSCALE with a slight purple/green cast. Key properties:
- The cardinal's brown/tan plumage becomes indistinguishable from nest straw in IR.
- You CANNOT reliably determine cardinal presence/absence from IR alone.
- DO NOT report cardinal_on_nest="false" on IR images unless the nest cup is CLEARLY empty (visible bowl interior with no mass filling it).
- Default to cardinal_on_nest="uncertain" and mother_cardinal_present="uncertain" on any image that appears to be infrared/night mode.
- Confidence on IR images should be 0.40-0.55 (below the action threshold) unless you can clearly distinguish a specific bird species by shape/posture.
- A dark mass filling the nest cup in IR is LIKELY the cardinal — do not call it empty.

== User's risk posture ==
The user prefers FALSE ALARMS over MISSED THREATS. Decision rules:
  - Bird at nest + red crest clearly visible → female cardinal, NOT a threat, no species in threat_species_detected.
  - Bird at nest + thrasher features clearly visible (no crest, streaked breast, long tail, yellow eye) → threat_species_detected=["brown_thrasher"].
  - Bird at nest + species ambiguous (can't clearly see the red crest OR thrasher features) → threat_species_detected=["unknown"], confidence reflects scene-reliability (usually 0.80+), near_nest_activity=true, direct_nest_interaction=false unless clearly reaching into cup.
  - No bird visible at nest → empty nest observation, no threats.

Missing a real predator is far worse than sending a redundant HIGH alert. Never default to "cardinal" when you can't clearly see the cardinal's crest.

Return ONLY the report_nest tool call."""


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
    b64 = downscale_jpeg_b64(jpeg_bytes, max_width=1280)

    user_content: list[dict] = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": b64,
            },
        },
    ]
    if extra_user_text:
        user_content.append({"type": "text", "text": extra_user_text})

    messages = [{"role": "user", "content": user_content}]

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
            # Hard outer bound on the HTTP call — the SDK's internal timeout
            # has failed us under odd network conditions in the past (hung
            # forever). 60s is plenty: normal latency is 2–6s, heavy load
            # 15–30s. If we hit this, raise asyncio.TimeoutError to the
            # caller, which catches and falls back gracefully.
            response = await asyncio.wait_for(
                client.messages.create(
                    model=model,
                    max_tokens=1024,
                    system=system,
                    tools=[NEST_TOOL],
                    tool_choice={"type": "tool", "name": "report_nest"},
                    messages=messages,
                ),
                timeout=60,
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

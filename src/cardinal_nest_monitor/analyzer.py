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
• If you can CLEARLY see a red/pink CREST on the bird's head → it IS the female cardinal; report attending_parent_on_nest or attending_parent_present accordingly and DO NOT list any threat species from this bird.
• If you see a LONG tail + STREAKED breast + NO crest → it's a Brown Thrasher — report it in threat_species_detected.
• If you see YELLOW eyes on a bird → very likely a thrasher, NOT a cardinal.
• If you see a non-cardinal bird or animal at/on the nest but can't identify the species → species_detected="unknown bird/animal" AND threat_species_detected=["unknown"]. Do NOT guess at "brown_thrasher" unless you can see thrasher-specific features.
• If the bird's head/face is hidden and you can't verify the crest or other distinctive features → attending_parent_on_nest="uncertain" AND do NOT report a confident threat species. Use "uncertain" liberally — the downstream system re-analyzes uncertain frames at higher cadence.

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
- DO NOT report attending_parent_on_nest="false" on IR images unless the nest cup is CLEARLY empty (visible bowl interior with no mass filling it).
- Default to attending_parent_on_nest="uncertain" and attending_parent_present="uncertain" on any image that appears to be infrared/night mode.
- Confidence on IR images should be 0.40-0.55 (below the action threshold) unless you can clearly distinguish a specific bird species by shape/posture.
- A dark mass filling the nest cup in IR is LIKELY the cardinal — do not call it empty.

== CHICKS vs EGGS — lifecycle awareness ==

Cardinal eggs hatch around 11-13 days into incubation. After hatching the nest contains CHICKS (nestlings), not eggs. Chicks look very different from eggs:

Newly hatched (day 0-3):
  • PINK or RED skin, mostly naked with sparse gray/white down
  • Eyes closed, heads larger than bodies
  • Visible movement (chicks wriggle; eggs don't)
  • Often lying curled in the nest cup

Older nestlings (day 4-10):
  • Pin feathers emerging (dark quills/spikes on back and wings)
  • Eyes may open around day 5
  • Larger, more bird-shaped
  • HEADS AND BEAKS often STRETCH UP above the cup rim when a parent arrives, with BRIGHT RED-ORANGE or YELLOW GAPE (inside of mouth) visible
  • May be multiple chicks visible at once

If you see pink/red flesh, sparse down, multiple small bodies, or small heads with gaping mouths protruding from the cup:
  • young_visible = "true"
  • young_count_estimate = your best count (may be occluded by mom or foliage)

If you see ONLY a clear, smooth cup interior with NO pink bodies, NO beaks, NO movement:
  • young_visible = "false"

If the image is too obscured to tell (IR, heavy foliage, mom covering everything):
  • young_visible = "uncertain"

== Feeding behavior ==

Set attending_parent_feeding_young = true ONLY when you can clearly identify a food item that is EXTERNALLY VISIBLE and protruding from the beak:
  • A caterpillar, worm, or insect sticking out (legs, body, or wings visible)
  • A berry or seed visibly held between the mandibles and protruding
  • A clearly-shaped prey item hanging from the beak

Set attending_parent_feeding_young = false in ALL other cases:
  • Beak closed → false
  • Beak open showing pink/orange INSIDE the mouth (this is normal — it is not food) → false
  • Beak appears to have a "bulge" but no identifiable shape → false
  • No cardinal present → false

Be CONSERVATIVE. The default is false. Only report true when there is unambiguous visual evidence of food held in the beak.

Feeding trips are short (30-120 seconds at the nest) and frequent (every 10-30 min during the feeding stage). Brief absences after hatching are expected and normal, not alarming.

== Narrow cardinal prior — READ CAREFULLY ==
This camera watches a Northern Cardinal nest; the nesting female visits the cup many times a day and her back, wing, and tail are often in view from behind or the side while she settles or broods. The crest often lies flat on the head when she's on the nest, so "no crest visible" is NOT sufficient reason to reject a cardinal ID.

When ALL of the following are true, treat the bird as the female cardinal (attending_parent_on_nest="true" at confidence 0.55–0.65) even if the crest, face, and beak are not visible:
  A. The whole body profile of a small compact songbird (~21 cm) is clearly visible sitting IN or ON the nest cup — not just a fragment behind foliage.
  B. You can see at least ONE of these cardinal plumage features unambiguously:
       • A warm REDDISH, ORANGE, or RUSTY-PINK tint on the wing, rump, or tail (the most reliable single cue).
       • The short ORANGE beak in profile.
       • The dark face mask around the beak base.
       • A visible crest silhouette (even laid flat).
  C. No thrasher features are present: no heavy dark breast streaking, no long tail exceeding the body, no bright yellow eye, no sharply down-curved beak.

When this prior fires, clamp confidence to the range 0.55–0.65 — never higher from this reasoning alone. If other cues (crest clearly visible, orange beak clearly visible) push toward stronger ID, confidence may go higher but that is not this prior.

DO NOT apply this prior when:
  • The body is so obscured by foliage/branches that you can only see a fragment (a patch of brown/tan without a recognizable body silhouette). In that case → attending_parent_on_nest="uncertain".
  • The bird's body shape or size isn't clearly consistent with a small songbird (e.g. larger bird, a mammal shape, an unclear blob).
  • You can see ANY thrasher feature.
  • The frame is infrared/night mode (the IR rules above always win — stay "uncertain" there).

When in doubt between this prior and "uncertain", choose "uncertain". The prior is meant to stop false HIGH alerts on clearly-visible mom-on-nest frames where we only see her back/wing; it is NOT meant to rescue heavily-occluded frames.

== User's risk posture ==
The user prefers FALSE ALARMS over MISSED THREATS. Decision rules:
  - Bird at nest + red crest clearly visible → female cardinal, NOT a threat, no species in threat_species_detected.
  - Bird at nest + thrasher features clearly visible (no crest, streaked breast, long tail, yellow eye) → threat_species_detected=["brown_thrasher"].
  - Bird at nest + narrow-cardinal-prior conditions A+B+C all met → attending_parent_on_nest="true" at confidence 0.55–0.65, no threat species.
  - Bird at nest + species ambiguous (prior conditions not met, no clearly-visible crest OR thrasher features) → threat_species_detected=["unknown"], confidence reflects scene-reliability (usually 0.80+), near_nest_activity=true, direct_nest_interaction=false unless clearly reaching into cup.
  - No bird visible at nest → empty nest observation, no threats.

Missing a real predator is far worse than sending a redundant HIGH alert. Do NOT apply the cardinal prior to a vague brownish shape behind foliage — that must remain "uncertain".

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
            # forever). HARD_TIMEOUT_SECONDS (module-level) is the shared
            # budget used by both this inner bound AND main.py's outer
            # bound around analyze(); tests patch the constant so both
            # shrink together. See CLAUDE.md §19.
            nest_tool = build_nest_tool(get_species_profile())
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

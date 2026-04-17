"""Two-model verification for CRITICAL/HIGH alerts.

When Sonnet (the primary analyzer) flags an alert at CRITICAL or HIGH severity,
we do a blind second-opinion call with Opus. Opus wins on disagreement:
  - Opus says "no threat" → SUPPRESS the Sonnet alert (false positive caught)
  - Opus says lower severity → DOWNGRADE
  - Opus agrees (same or higher severity) → FIRE with Sonnet's decision

Design principle: the Opus call is BLIND. It receives the same system prompt
and the same image with NO hint of what Sonnet said. Priming Opus with
"Sonnet thinks this is a thrasher, verify" would introduce anchoring bias
and collapse the two-model guarantee into one-model-with-extra-cost.

A small verification nudge in the user message reminds Opus to be conservative
about CRITICAL classifications — that is NOT priming (doesn't mention Sonnet
or its specific claims), just reinforces the existing system-prompt guidance.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from cardinal_nest_monitor import analyzer as analyzer_mod
from cardinal_nest_monitor.events import evaluate
from cardinal_nest_monitor.schema import (
    AlertDecision,
    NestObservation,
    NestState,
    Severity,
)

log = logging.getLogger(__name__)


# Verification is triggered for these severities only. MEDIUM / LOW alerts are
# timing-based (long absence, mother returned), not species-ID dependent; the
# failure modes that verification addresses don't apply to them.
_VERIFIED_SEVERITIES: frozenset[Severity] = frozenset(
    {Severity.CRITICAL, Severity.HIGH}
)


# Nudge appended to the Opus user message. Does NOT reveal Sonnet's verdict —
# this keeps the second opinion blind. Only reinforces the existing prompt's
# "be careful with CRITICAL" guidance.
_VERIFICATION_NUDGE = (
    "This image is being independently verified. Be especially careful with "
    "CRITICAL-level classifications (direct_nest_interaction=true). If species "
    "identity is not clearly confirmable from the visible features, return "
    "threat_species_detected=[\"unknown\"] with direct_nest_interaction=false "
    "rather than guessing a specific species."
)


def compute_verification_decision(
    sonnet_decision: AlertDecision,
    opus_decision: AlertDecision | None,
) -> AlertDecision | None:
    """Apply the disagreement rule. Pure function, easy to unit-test.

    - opus says no alert → None (suppress)
    - opus says lower severity → opus_decision (downgrade)
    - opus says same or higher severity → sonnet_decision (confirm;
      we don't upgrade to avoid over-escalating on minor disagreements)
    """
    if opus_decision is None:
        return None
    if opus_decision.severity.rank < sonnet_decision.severity.rank:
        return opus_decision
    return sonnet_decision


def should_verify(decision: AlertDecision) -> bool:
    """True if this alert's severity is in the verification set."""
    return decision.severity in _VERIFIED_SEVERITIES


async def verify_alert(
    jpeg: bytes,
    sonnet_obs: NestObservation,
    sonnet_decision: AlertDecision,
    pre_state: NestState,
    store: Any,  # StateStore; typed loosely to avoid circular import
    ts: float,
    verification_model: str,
    is_backfill: bool = False,
) -> tuple[AlertDecision | None, NestObservation | None]:
    """Re-analyze the image with Opus and apply the disagreement rule.

    Returns (final_decision, opus_obs):
      - final_decision: None if suppressed, otherwise the (possibly
        downgraded) decision to fire.
      - opus_obs: the Opus NestObservation, or None if the call failed
        (in which case we fall back to Sonnet's decision).

    This coroutine catches all exceptions from the Opus call internally; a
    verification infrastructure failure must NOT silently suppress a real
    Sonnet alert — we fall through with the original decision.

    is_backfill (Codex P2 round 4): forwarded to the internal evaluate()
    call so Opus's verdict uses the SAME backfill mode as Sonnet's. Without
    this, an older HIGH/CRITICAL backfill snap could be downgraded or
    suppressed by a bogus Opus state-relative result (mother_returned /
    long_absence) computed against future state — a real correctness
    gap specifically when verify_alerts_with_opus=True.
    """
    try:
        opus_obs = await analyzer_mod.analyze(
            jpeg,
            model_override=verification_model,
            extra_user_text=_VERIFICATION_NUDGE,
        )
    except asyncio.TimeoutError:
        # analyzer.analyze() hit its 60s hard outer bound. Don't block the
        # alert — fall through with the Sonnet decision so we never silently
        # suppress a real alert on infra flakiness.
        log.warning(
            "Opus verification timed out; falling back to Sonnet decision (%s / %s)",
            sonnet_decision.severity.value,
            sonnet_decision.rule_id,
        )
        return (sonnet_decision, None)
    except Exception:
        log.exception(
            "Opus verification failed; falling back to Sonnet decision (%s / %s)",
            sonnet_decision.severity.value,
            sonnet_decision.rule_id,
        )
        return (sonnet_decision, None)

    # Evaluate against the SAME pre-record state that Sonnet's decision used.
    # This keeps the verification apples-to-apples — Opus is re-running the
    # rules engine on its own observation, not a different state snapshot.
    # is_backfill must be forwarded so Opus and Sonnet make the same set of
    # rules eligible. Mismatched flags would let Opus emit a state-relative
    # result on a stale snap and incorrectly downgrade Sonnet's threat alert.
    opus_decision = evaluate(opus_obs, pre_state, store, ts, is_backfill=is_backfill)
    final = compute_verification_decision(sonnet_decision, opus_decision)

    if final is None:
        log.info(
            "Opus verification: %s %s → SUPPRESSED (Opus saw no alert). "
            "Sonnet: %r | Opus: %r",
            sonnet_decision.severity.value,
            sonnet_decision.rule_id,
            sonnet_obs.summary[:120],
            opus_obs.summary[:120],
        )
    elif final is not sonnet_decision:
        log.info(
            "Opus verification: %s → %s (downgraded). "
            "Sonnet: %r | Opus: %r",
            sonnet_decision.severity.value,
            final.severity.value,
            sonnet_obs.summary[:120],
            opus_obs.summary[:120],
        )
    else:
        log.info(
            "Opus verification: %s confirmed. Opus: %r",
            sonnet_decision.severity.value,
            opus_obs.summary[:120],
        )
    return (final, opus_obs)

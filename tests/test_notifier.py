"""Tests for the Discord notifier's security + reliability behaviors.

Covers four correctness properties:

  1. Webhook token redaction in ``aiohttp.ClientError`` log output so a
     stalled connection's full URL (which carries the auth token) never
     reaches the log stream.
  2. Response-body redaction + 500-char cap before logging.
  3. Every outbound Discord payload carries
     ``allowed_mentions={"parse": []}`` as defense-in-depth against a
     future ``content``-field addition accidentally enabling
     ``@everyone`` / role pings.
  4. Severity-aware retry policy: CRITICAL/HIGH get up to 3 retries with
     exponential backoff (1s → 3s → 10s) and honor ``Retry-After`` on
     429; MEDIUM/LOW keep the 2-try cheap behavior.

Uses the ``unittest.mock`` + ``AsyncMock`` pattern established in
``tests/test_verifier.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest

from cardinal_nest_monitor.notifier import (
    Notifier,
    _redact,
    _scrub_response_body,
    _with_allowed_mentions,
)
from cardinal_nest_monitor.schema import (
    AlertDecision,
    NestObservation,
    Severity,
)


# A fake Discord webhook URL that looks real enough for redaction tests
# to exercise the actual regex. It is NOT a live webhook.
FAKE_WEBHOOK_URL = (
    "https://discord.com/api/webhooks/1234567890123456789/"
    "abcDEF_-0123456789xyzABCDEFghijklmnopqrstuvwxyzABCDEFGH12"
)


# ── Pure helpers ───────────────────────────────────────────────────────

class TestRedaction:
    def test_redact_absolute_webhook_url(self):
        """A full https://.../webhooks/ID/TOKEN URL gets its token
        replaced with REDACTED, preserving the ID path segment."""
        text = f"Cannot connect to {FAKE_WEBHOOK_URL}: timed out"
        redacted = _redact(text)
        assert "REDACTED" in redacted
        assert "1234567890123456789" in redacted  # ID preserved
        assert "abcDEF_-0123456789" not in redacted  # token gone
        assert redacted.startswith("Cannot connect to ")

    def test_redact_bare_path_form(self):
        """Some log sources include just `/webhooks/ID/TOKEN` without
        the scheme+host — still must be redacted."""
        text = "POST /webhooks/1234567890/secrettokenhere failed"
        redacted = _redact(text)
        assert "secrettokenhere" not in redacted
        assert "/webhooks/1234567890/REDACTED" in redacted

    def test_redact_exception_via_str(self):
        """``_redact`` must accept arbitrary input (exceptions, args)
        so callers don't have to stringify first."""
        e = aiohttp.ClientConnectionError(
            f"Cannot connect to host: {FAKE_WEBHOOK_URL}"
        )
        redacted = _redact(e)
        assert "REDACTED" in redacted
        assert "abcDEF_-0123456789" not in redacted

    def test_redact_preserves_unrelated_text(self):
        """Text that doesn't contain a webhook URL is returned unchanged."""
        text = "Something bad happened: connection reset by peer"
        assert _redact(text) == text

    def test_redact_multiple_urls(self):
        """Both URLs get redacted even if the payload happens to contain
        two of them (e.g. the bare path AND the full URL)."""
        other = FAKE_WEBHOOK_URL.replace("1234567890", "9876543210")
        text = f"first={FAKE_WEBHOOK_URL} second={other}"
        redacted = _redact(text)
        assert "abcDEF_-0123456789" not in redacted
        # Both IDs should survive
        assert "1234567890123456789" in redacted
        assert "9876543210123456789" in redacted


class TestResponseBodyScrub:
    def test_scrub_caps_at_500_chars(self):
        long_body = "x" * 1000
        scrubbed = _scrub_response_body(long_body)
        assert len(scrubbed) == 500
        assert scrubbed.endswith("…")

    def test_scrub_redacts_embedded_url(self):
        body = f"Error: invalid embed. See {FAKE_WEBHOOK_URL}"
        scrubbed = _scrub_response_body(body)
        assert "REDACTED" in scrubbed
        assert "abcDEF_-0123456789" not in scrubbed

    def test_scrub_short_body_unchanged(self):
        body = "short error"
        assert _scrub_response_body(body) == body


class TestAllowedMentions:
    def test_with_allowed_mentions_adds_parse_empty(self):
        payload = {"embeds": [{"title": "x"}]}
        wrapped = _with_allowed_mentions(payload)
        assert wrapped["allowed_mentions"] == {"parse": []}

    def test_preserves_existing_allowed_mentions(self):
        """If a caller ever sets allowed_mentions explicitly, we honor it."""
        payload = {"embeds": [], "allowed_mentions": {"parse": ["users"]}}
        wrapped = _with_allowed_mentions(payload)
        assert wrapped["allowed_mentions"] == {"parse": ["users"]}

    def test_does_not_mutate_input(self):
        """Wrapping returns a new dict; the input is unchanged."""
        payload = {"embeds": [{"title": "x"}]}
        _with_allowed_mentions(payload)
        assert "allowed_mentions" not in payload


# ── Integration: allowed_mentions on every outbound payload ───────────

def _fake_decision(severity: Severity = Severity.HIGH) -> AlertDecision:
    return AlertDecision(
        severity=severity,
        title="test",
        summary="test summary",
        species=["brown_thrasher"],
        confidence=0.9,
        rule_id="predator_absent",
    )


def _fake_obs() -> NestObservation:
    return NestObservation(
        attending_parent_present="false", attending_parent_on_nest="false",
        eggs_visible="false", egg_count_estimate=None,
        nest_visible=True, nest_disturbed="false",
        species_detected=["brown_thrasher"],
        threat_species_detected=["brown_thrasher"],
        near_nest_activity=True, direct_nest_interaction=False,
        confidence=0.9, summary="thrasher at the nest",
    )


class _PayloadCaptor:
    """Stub ``session.post(...)`` that captures every outbound payload
    so tests can assert on the JSON body Discord would have received.
    Returns a programmable sequence of (status, headers, body) tuples.
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self.captured_json: list[dict] = []
        self.captured_payload_json: list[dict] = []
        self.call_count = 0

    def post(self, url, json=None, data=None, headers=None):  # noqa: A002
        if json is not None:
            self.captured_json.append(json)
        if data is not None:
            # aiohttp.FormData — walk the fields to find payload_json
            for field in getattr(data, "_fields", []):
                # aiohttp FormData stores tuples (options, headers, value)
                options = field[0]
                value = field[2]
                if options.get("name") == "payload_json":
                    self.captured_payload_json.append(
                        __import__("json").loads(value)
                    )
        self.call_count += 1
        idx = min(self.call_count - 1, len(self._responses) - 1)
        status, resp_headers, body = self._responses[idx]
        return _FakeResponseCtx(status, resp_headers, body)


class _FakeResponseCtx:
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers or {}
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def text(self):
        return self._body


@pytest.fixture
def notifier():
    n = Notifier(FAKE_WEBHOOK_URL, "TestCam")
    yield n
    # Close synchronously in tests; no real session was opened if
    # _get_session was mocked out.


def test_send_json_payload_includes_allowed_mentions(notifier):
    """The JSON payload delivered to Discord must always include
    ``allowed_mentions: {parse: []}`` (defense-in-depth)."""
    captor = _PayloadCaptor([(204, {}, "")])

    async def _run():
        fake_session = AsyncMock()
        fake_session.post = captor.post
        with patch.object(notifier, "_get_session",
                          AsyncMock(return_value=fake_session)):
            ok = await notifier._send_json({"embeds": [{"title": "x"}]})
        return ok

    ok = asyncio.run(_run())
    assert ok is True
    assert len(captor.captured_json) == 1
    assert captor.captured_json[0]["allowed_mentions"] == {"parse": []}


def test_send_multipart_payload_includes_allowed_mentions(notifier, tmp_path):
    """The multipart payload_json must also carry allowed_mentions."""
    jpg = tmp_path / "snap.jpg"
    jpg.write_bytes(b"\xff\xd8\xff\xd9")  # minimal JPEG magic
    captor = _PayloadCaptor([(200, {}, '{"id":"1"}')])

    async def _run():
        fake_session = AsyncMock()
        fake_session.post = captor.post
        with patch.object(notifier, "_get_session",
                          AsyncMock(return_value=fake_session)):
            ok = await notifier._send_multipart({"embeds": [{}]}, jpg)
        return ok

    ok = asyncio.run(_run())
    assert ok is True
    assert len(captor.captured_payload_json) == 1
    assert captor.captured_payload_json[0]["allowed_mentions"] == {"parse": []}


def test_all_public_senders_include_allowed_mentions(tmp_path):
    """End-to-end: every public ``send_*`` method builds a payload that
    includes ``allowed_mentions``. Runs each sender once and inspects the
    captured JSON / payload_json bodies.
    """
    n = Notifier(FAKE_WEBHOOK_URL, "TestCam")
    captor = _PayloadCaptor([(204, {}, "")] * 20)

    async def _run():
        fake_session = AsyncMock()
        fake_session.post = captor.post
        with patch.object(n, "_get_session",
                          AsyncMock(return_value=fake_session)):
            await n.send_test()
            await n.send_battery_status(3.9, "ok", 5)
            await n.send_heartbeat(10, 2, 5, 1.0, 0.25)
            await n.send_system_message("hi", "body")
            await n.send_lifecycle_event("incubation", "🥚 start", "ok")
            await n.send_alert(_fake_decision(), _fake_obs())

    asyncio.run(_run())

    # Every captured JSON payload (from send_json paths) must carry
    # allowed_mentions. send_alert with no snap_path goes through
    # send_json too.
    assert captor.captured_json, "no payloads captured"
    for p in captor.captured_json:
        assert p.get("allowed_mentions") == {"parse": []}, (
            f"payload missing allowed_mentions: {p.keys()}"
        )


# ── Redaction in log output ───────────────────────────────────────────

def test_transport_error_log_is_redacted(notifier, caplog):
    """An ``aiohttp.ClientError`` whose message contains the full webhook
    URL must NOT leak the token into the log stream."""
    err = aiohttp.ClientConnectionError(
        f"Cannot connect to host: {FAKE_WEBHOOK_URL}"
    )

    async def _run():
        async def do_post():
            raise err
        caplog.set_level(logging.ERROR, logger="cardinal_nest_monitor.notifier")
        return await notifier._post_with_retry(do_post, severity=Severity.MEDIUM)

    result = asyncio.run(_run())
    assert result is False
    combined = " ".join(r.getMessage() for r in caplog.records)
    assert "REDACTED" in combined
    assert "abcDEF_-0123456789" not in combined


def test_response_body_log_is_redacted_and_capped(notifier, caplog):
    """A 4xx body that echoes the webhook URL must be redacted and the
    logged text capped at 500 chars."""
    body = f"error: {FAKE_WEBHOOK_URL} — " + ("padding " * 200)

    async def _run():
        async def do_post():
            return 400, {}, body
        caplog.set_level(logging.ERROR, logger="cardinal_nest_monitor.notifier")
        return await notifier._post_with_retry(do_post, severity=None)

    asyncio.run(_run())
    combined = " ".join(r.getMessage() for r in caplog.records)
    assert "abcDEF_-0123456789" not in combined
    # Find the message line and check it is capped at 500 chars post-scrub
    error_lines = [r.getMessage() for r in caplog.records
                   if r.levelno >= logging.ERROR]
    # The body-portion inside the error log must be ≤500 chars (plus the
    # "discord: HTTP 400: " prefix).
    assert any(len(line) < 650 for line in error_lines), (
        f"error log lines appear uncapped: {[len(ln) for ln in error_lines]}"
    )


# ── Retry policy (MEDIUM/LOW keep two tries) ──────────────────────────

def test_medium_keeps_two_attempts_on_5xx(notifier):
    """MEDIUM severity falls back to the cheap 2-try behavior; a 5xx
    triggers exactly one retry, then gives up."""
    sleeps: list[float] = []

    async def _run():
        async def do_post():
            return 503, {}, "service unavailable"
        with patch("asyncio.sleep",
                   AsyncMock(side_effect=lambda s: sleeps.append(s))):
            return await notifier._post_with_retry(
                do_post, severity=Severity.MEDIUM,
            )

    ok = asyncio.run(_run())
    assert ok is False
    # exactly one 1s backoff — the default non-urgent schedule
    assert sleeps == [1.0]


def test_none_severity_keeps_two_attempts(notifier):
    """Unspecified severity (feed / heartbeat / analytics posts) also
    uses the cheap 2-try behavior."""
    sleeps: list[float] = []

    async def _run():
        async def do_post():
            return 503, {}, "x"
        with patch("asyncio.sleep",
                   AsyncMock(side_effect=lambda s: sleeps.append(s))):
            return await notifier._post_with_retry(do_post, severity=None)

    asyncio.run(_run())
    assert sleeps == [1.0]


# ── Retry policy (CRITICAL/HIGH get 3 retries + backoff + Retry-After) ─

def test_critical_retries_three_times_with_exponential_backoff(notifier):
    """CRITICAL: on repeated 5xx, retry 3 times with backoffs 1s, 3s, 10s."""
    sleeps: list[float] = []
    call_count = {"n": 0}

    async def _run():
        async def do_post():
            call_count["n"] += 1
            return 503, {}, "overloaded"
        with patch("asyncio.sleep",
                   AsyncMock(side_effect=lambda s: sleeps.append(s))):
            return await notifier._post_with_retry(
                do_post, severity=Severity.CRITICAL,
            )

    ok = asyncio.run(_run())
    assert ok is False
    # 1 initial + 3 retries = 4 calls; 3 backoffs
    assert call_count["n"] == 4
    assert sleeps == [1.0, 3.0, 10.0]


def test_high_retries_three_times_with_exponential_backoff(notifier):
    """HIGH gets the same 3-retry exponential-backoff treatment."""
    sleeps: list[float] = []
    call_count = {"n": 0}

    async def _run():
        async def do_post():
            call_count["n"] += 1
            return 500, {}, "internal error"
        with patch("asyncio.sleep",
                   AsyncMock(side_effect=lambda s: sleeps.append(s))):
            return await notifier._post_with_retry(
                do_post, severity=Severity.HIGH,
            )

    asyncio.run(_run())
    assert call_count["n"] == 4
    assert sleeps == [1.0, 3.0, 10.0]


def test_critical_honors_retry_after_on_429(notifier):
    """On HTTP 429, sleep for the parsed Retry-After value (seconds),
    capped at 30s, then retry."""
    sleeps: list[float] = []
    responses = iter([
        (429, {"Retry-After": "7"}, "rate limited"),
        (204, {}, ""),
    ])

    async def _run():
        async def do_post():
            return next(responses)
        with patch("asyncio.sleep",
                   AsyncMock(side_effect=lambda s: sleeps.append(s))):
            return await notifier._post_with_retry(
                do_post, severity=Severity.CRITICAL,
            )

    ok = asyncio.run(_run())
    assert ok is True
    # Retry-After=7 takes precedence over the backoff schedule
    assert sleeps == [7.0]


def test_critical_caps_retry_after_at_30s(notifier):
    """A runaway Retry-After (e.g. 60s) is capped at 30s so a
    misconfigured rate-limit doesn't block the pipeline forever."""
    sleeps: list[float] = []
    responses = iter([
        (429, {"Retry-After": "120"}, "slow down"),
        (204, {}, ""),
    ])

    async def _run():
        async def do_post():
            return next(responses)
        with patch("asyncio.sleep",
                   AsyncMock(side_effect=lambda s: sleeps.append(s))):
            return await notifier._post_with_retry(
                do_post, severity=Severity.CRITICAL,
            )

    ok = asyncio.run(_run())
    assert ok is True
    assert sleeps == [30.0]  # capped


def test_critical_succeeds_after_transient_5xx(notifier):
    """CRITICAL: the first attempt fails with 503 but the retry succeeds.
    Verify we return True and only sleep once (the first backoff)."""
    sleeps: list[float] = []
    responses = iter([
        (503, {}, "overloaded"),
        (204, {}, ""),
    ])

    async def _run():
        async def do_post():
            return next(responses)
        with patch("asyncio.sleep",
                   AsyncMock(side_effect=lambda s: sleeps.append(s))):
            return await notifier._post_with_retry(
                do_post, severity=Severity.CRITICAL,
            )

    ok = asyncio.run(_run())
    assert ok is True
    assert sleeps == [1.0]


def test_medium_429_honors_retry_after_but_single_retry(notifier):
    """Non-urgent severities still honor Retry-After but only get one
    retry (the normal 2-try budget). Keeps a flapping rate-limit from
    indefinitely delaying a feed post."""
    sleeps: list[float] = []
    responses = iter([
        (429, {"Retry-After": "2"}, "x"),
        (429, {"Retry-After": "2"}, "x"),
    ])

    async def _run():
        async def do_post():
            return next(responses)
        with patch("asyncio.sleep",
                   AsyncMock(side_effect=lambda s: sleeps.append(s))):
            return await notifier._post_with_retry(
                do_post, severity=Severity.MEDIUM,
            )

    ok = asyncio.run(_run())
    assert ok is False
    # Only one sleep since the budget is 2 attempts total
    assert sleeps == [2.0]


def test_critical_gives_up_on_4xx_without_retry(notifier):
    """A non-429 4xx (e.g. 400 invalid payload) is non-retryable even
    for CRITICAL — retrying would just re-fail with the same bad body."""
    sleeps: list[float] = []
    call_count = {"n": 0}

    async def _run():
        async def do_post():
            call_count["n"] += 1
            return 400, {}, "invalid embed"
        with patch("asyncio.sleep",
                   AsyncMock(side_effect=lambda s: sleeps.append(s))):
            return await notifier._post_with_retry(
                do_post, severity=Severity.CRITICAL,
            )

    ok = asyncio.run(_run())
    assert ok is False
    assert call_count["n"] == 1
    assert sleeps == []


def test_parse_retry_after_handles_bad_values(notifier):
    """Bad Retry-After values (non-numeric, missing, negative) must not
    crash the retry loop."""
    assert Notifier._parse_retry_after({}) is None
    assert Notifier._parse_retry_after({"Retry-After": "not-a-number"}) is None
    assert Notifier._parse_retry_after({"Retry-After": "-5"}) == 0.0
    assert Notifier._parse_retry_after({"Retry-After": "3.5"}) == 3.5
    # Case-insensitive header lookup
    assert Notifier._parse_retry_after({"retry-after": "4"}) == 4.0


# ── Wiring: send_alert passes severity into the retry policy ──────────

def test_send_alert_propagates_severity_critical(notifier):
    """The high-level ``send_alert`` with Severity.CRITICAL must flow
    through to ``_post_with_retry`` with ``severity=CRITICAL`` so the
    urgent retry policy kicks in."""
    captured_severity: list = []

    orig_send_json = notifier._send_json

    async def wrapped_send_json(payload, url_override=None, severity=None):
        captured_severity.append(severity)
        return True

    async def _run():
        with patch.object(notifier, "_send_json", wrapped_send_json):
            await notifier.send_alert(
                _fake_decision(Severity.CRITICAL), _fake_obs(),
            )

    asyncio.run(_run())
    assert captured_severity == [Severity.CRITICAL]


def test_send_alert_propagates_severity_high_with_snap(notifier, tmp_path):
    """When ``snap_path`` is provided, ``send_alert`` routes through
    ``_send_multipart`` and must still propagate severity."""
    jpg = tmp_path / "snap.jpg"
    jpg.write_bytes(b"\xff\xd8\xff\xd9")
    captured_severity: list = []

    async def wrapped_send_multipart(payload, image_path,
                                     url_override=None, severity=None):
        captured_severity.append(severity)
        return True

    async def _run():
        with patch.object(notifier, "_send_multipart", wrapped_send_multipart):
            await notifier.send_alert(
                _fake_decision(Severity.HIGH), _fake_obs(), snap_path=jpg,
            )

    asyncio.run(_run())
    assert captured_severity == [Severity.HIGH]

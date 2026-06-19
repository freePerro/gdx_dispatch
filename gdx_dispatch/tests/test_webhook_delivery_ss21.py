"""
gdx_dispatch/tests/test_webhook_delivery_ss21.py — SS-21 delivery + retry + circuit.

Covers:
  - retry backoff ladder matches spec (30s, 5m, 1h, 6h, 24h)
  - next_retry_delay returns None after ladder exhausted
  - deliver_webhook happy path: 200 → succeeded=True, signed header present
  - deliver_webhook 500: succeeded=False, failure recorded on circuit
  - N consecutive failures → circuit opens → subsequent attempt short-circuits
  - circuit half-open after cool-down
  - non-HTTP exception (bug in worker) propagates (v3 patch P34)
  - dual-active signing: both v1 parts present in header
  - event body is deterministic JSON (sort_keys)
"""
from __future__ import annotations

import asyncio
import json

import pytest

from gdx_dispatch.core.webhook_delivery_ss21 import (
    CIRCUIT_HALF_OPEN_AFTER_SECONDS,
    CIRCUIT_OPEN_AFTER_FAILURES,
    RETRY_BACKOFF_SECONDS,
    deliver_webhook,
    get_circuit_registry,
    next_retry_delay,
)
from gdx_dispatch.core.webhook_signing import SIGNATURE_HEADER, SigningSecret, verify_signature


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clear_circuit():
    get_circuit_registry().clear()
    yield
    get_circuit_registry().clear()


# ---------------------------------------------------------------------------
# Backoff ladder
# ---------------------------------------------------------------------------


def test_retry_ladder_matches_spec():
    assert RETRY_BACKOFF_SECONDS == (30, 300, 3600, 21600, 86400)


def test_next_retry_delay():
    assert next_retry_delay(1) == 30
    assert next_retry_delay(2) == 300
    assert next_retry_delay(3) == 3600
    assert next_retry_delay(4) == 21600
    assert next_retry_delay(5) == 86400
    assert next_retry_delay(6) is None
    assert next_retry_delay(0) is None


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


def _make_poster(status_code=200, error=None):
    captured = {}

    async def poster(url, body, headers):
        captured["url"] = url
        captured["body"] = body
        captured["headers"] = headers
        if error is not None:
            return None, type(error).__name__, str(error)
        return status_code, None, None

    return poster, captured


def test_deliver_success():
    poster, cap = _make_poster(200)
    payload = {"event": "job.created", "id": "j1"}
    secs = [SigningSecret(kid="k1", raw=b"secret")]
    result = _run(
        deliver_webhook(
            subscription_id="sub-1",
            event_id="evt-1",
            url="https://receiver.example/hook",
            event_payload=payload,
            secrets=secs,
            poster=poster,
        )
    )
    assert result.succeeded is True
    assert result.status_code == 200
    # Signed header present + validates
    header = cap["headers"][SIGNATURE_HEADER]
    assert verify_signature(header, cap["body"], secs) is True


def test_deliver_dual_active_signing():
    poster, cap = _make_poster(200)
    old = SigningSecret(kid="old", raw=b"old-sec")
    new = SigningSecret(kid="new", raw=b"new-sec")
    _run(
        deliver_webhook(
            subscription_id="sub-1",
            event_id="evt-1",
            url="https://r.example",
            event_payload={"x": 1},
            secrets=[old, new],
            poster=poster,
        )
    )
    header = cap["headers"][SIGNATURE_HEADER]
    assert header.count("v1=") == 2
    # Either secret alone validates
    assert verify_signature(header, cap["body"], [old]) is True
    assert verify_signature(header, cap["body"], [new]) is True


def test_deliver_body_is_deterministic_json():
    poster, cap = _make_poster(200)
    payload = {"b": 2, "a": 1}
    _run(
        deliver_webhook(
            subscription_id="s", event_id="e", url="https://r",
            event_payload=payload,
            secrets=[SigningSecret(kid="k", raw=b"s")],
            poster=poster,
        )
    )
    # sort_keys → "a" before "b"
    assert cap["body"] == json.dumps({"a": 1, "b": 2}, sort_keys=True, separators=(",", ":")).encode()


def test_deliver_5xx_records_failure():
    poster, _ = _make_poster(503)
    result = _run(
        deliver_webhook(
            subscription_id="sub-5xx",
            event_id="e",
            url="https://r",
            event_payload={},
            secrets=[SigningSecret(kid="k", raw=b"s")],
            poster=poster,
        )
    )
    assert result.succeeded is False
    assert result.status_code == 503
    st = get_circuit_registry().get("sub-5xx")
    assert st.consecutive_failures == 1


def test_circuit_opens_after_threshold():
    poster, _ = _make_poster(500)
    secs = [SigningSecret(kid="k", raw=b"s")]
    for i in range(CIRCUIT_OPEN_AFTER_FAILURES):
        _run(
            deliver_webhook(
                subscription_id="sub-c",
                event_id=f"e{i}",
                url="https://r",
                event_payload={},
                secrets=secs,
                poster=poster,
            )
        )
    st = get_circuit_registry().get("sub-c")
    assert st.opened_at is not None

    # Next attempt should short-circuit without calling poster
    called = []

    async def never_called(*a, **kw):
        called.append(1)
        return 200, None, None

    result = _run(
        deliver_webhook(
            subscription_id="sub-c",
            event_id="enext",
            url="https://r",
            event_payload={},
            secrets=secs,
            poster=never_called,
        )
    )
    assert called == []
    assert result.succeeded is False
    assert result.error_type == "CircuitOpen"


def test_circuit_half_open_after_cooldown():
    poster, _ = _make_poster(500)
    secs = [SigningSecret(kid="k", raw=b"s")]
    # Open the circuit
    for i in range(CIRCUIT_OPEN_AFTER_FAILURES):
        _run(
            deliver_webhook(
                subscription_id="sub-h",
                event_id=f"e{i}",
                url="https://r",
                event_payload={},
                secrets=secs,
                poster=poster,
            )
        )
    st = get_circuit_registry().get("sub-h")
    assert st.is_open(st.opened_at + 1)  # immediately after: open
    # After cool-down: half-open (is_open returns False → probe allowed)
    assert not st.is_open(st.opened_at + CIRCUIT_HALF_OPEN_AFTER_SECONDS + 1)


def test_success_closes_circuit():
    poster_fail, _ = _make_poster(500)
    poster_ok, _ = _make_poster(200)
    secs = [SigningSecret(kid="k", raw=b"s")]
    _run(
        deliver_webhook(
            subscription_id="sub-rc",
            event_id="e1",
            url="https://r",
            event_payload={},
            secrets=secs,
            poster=poster_fail,
        )
    )
    assert get_circuit_registry().get("sub-rc").consecutive_failures == 1
    _run(
        deliver_webhook(
            subscription_id="sub-rc",
            event_id="e2",
            url="https://r",
            event_payload={},
            secrets=secs,
            poster=poster_ok,
        )
    )
    st = get_circuit_registry().get("sub-rc")
    assert st.consecutive_failures == 0
    assert st.opened_at is None


def test_non_http_exception_propagates():
    """v3 patch P34: non-HTTP bugs must NOT be swallowed into silent retry."""

    async def buggy_poster(url, body, headers):
        raise AttributeError("worker bug — missing field")

    secs = [SigningSecret(kid="k", raw=b"s")]
    with pytest.raises(AttributeError):
        _run(
            deliver_webhook(
                subscription_id="sub-bug",
                event_id="e",
                url="https://r",
                event_payload={},
                secrets=secs,
                poster=buggy_poster,
            )
        )
    # Still recorded the failure so the circuit sees it
    assert get_circuit_registry().get("sub-bug").consecutive_failures == 1

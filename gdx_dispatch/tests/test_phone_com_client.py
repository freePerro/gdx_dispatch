"""pc-s2 — PhoneComClient: auth + test_token + get_account.

key_storage.test_and_cache_account is covered in pc-s2b's test file.
"""
from __future__ import annotations

import logging

import httpx
import pytest
import respx

from gdx_dispatch.modules.phone_com.client import (
    BASE_URL,
    DEFAULT_TIMEOUT,
    PhoneComAPIError,
    PhoneComClient,
)

_ACCT_PAYLOAD = {
    "filters": {},
    "sort": {"id": "desc"},
    "total": 1,
    "limit": 25,
    "offset": None,
    "items": [{
        "id": 1000000,
        "name": "Example Owner",
        "username": "owner@example.com",
        "timezone": "America/Chicago",
        "features": {"call-recording-on": False, "has-payment-method": True},
    }],
}


def test_base_url_constant():
    assert BASE_URL == "https://api.phone.com/v4"


def test_default_timeout_is_httpx_timeout():
    assert isinstance(DEFAULT_TIMEOUT, httpx.Timeout)


def test_phone_com_api_error_carries_status_and_body():
    err = PhoneComAPIError("boom", status_code=502, body_snippet="upstream gone")
    assert err.status_code == 502
    assert err.body_snippet == "upstream gone"


@respx.mock
def test_get_account_success():
    respx.get(f"{BASE_URL}/accounts").mock(
        return_value=httpx.Response(200, json=_ACCT_PAYLOAD)
    )
    with PhoneComClient(token="phc-test") as c:
        acct = c.get_account()
    assert acct["id"] == 1000000
    assert acct["name"] == "Example Owner"
    assert acct["features"]["call-recording-on"] is False


@respx.mock
def test_get_account_sends_bearer_header():
    route = respx.get(f"{BASE_URL}/accounts").mock(
        return_value=httpx.Response(200, json=_ACCT_PAYLOAD)
    )
    with PhoneComClient(token="phc-secret-xyz") as c:
        c.get_account()
    assert route.calls.last.request.headers["Authorization"] == "Bearer phc-secret-xyz"


@respx.mock
def test_get_account_404_raises_api_error():
    respx.get(f"{BASE_URL}/accounts").mock(
        return_value=httpx.Response(404, json={"error": "nope"})
    )
    with PhoneComClient(token="phc") as c, pytest.raises(PhoneComAPIError) as ei:
        c.get_account()
    assert ei.value.status_code == 404


@respx.mock
def test_test_token_success_shape():
    respx.get(f"{BASE_URL}/accounts").mock(
        return_value=httpx.Response(200, json=_ACCT_PAYLOAD)
    )
    with PhoneComClient(token="phc") as c:
        result = c.test_token()
    assert result["ok"] is True
    assert result["voip_id"] == 1000000
    assert result["account_name"] == "Example Owner"
    assert isinstance(result["latency_ms"], int) and result["latency_ms"] >= 0
    assert result["error"] is None


@respx.mock
def test_test_token_401_returns_ok_false():
    respx.get(f"{BASE_URL}/accounts").mock(
        return_value=httpx.Response(401, json={"error": "invalid_token"})
    )
    with PhoneComClient(token="phc") as c:
        result = c.test_token()
    assert result["ok"] is False
    assert "401" in (result["error"] or "")
    assert result["voip_id"] is None


@respx.mock
def test_test_token_429_then_200_retries_and_succeeds():
    route = respx.get(f"{BASE_URL}/accounts").mock(side_effect=[
        httpx.Response(429, json={"error": "rate"}),
        httpx.Response(200, json=_ACCT_PAYLOAD),
    ])
    with PhoneComClient(token="phc") as c:
        # Tighten retry backoff for test speed
        c.retry_max_attempts = 2
        result = c.test_token()
    assert result["ok"] is True
    assert route.call_count == 2


@respx.mock
def test_test_token_5xx_retries_max_then_fails():
    respx.get(f"{BASE_URL}/accounts").mock(
        return_value=httpx.Response(503, json={"error": "down"})
    )
    with PhoneComClient(token="phc") as c:
        c.retry_max_attempts = 2  # keep test under 5s
        result = c.test_token()
    assert result["ok"] is False
    assert "503" in (result["error"] or "")


@respx.mock
def test_token_never_appears_in_logs(caplog):
    respx.get(f"{BASE_URL}/accounts").mock(
        return_value=httpx.Response(200, json=_ACCT_PAYLOAD)
    )
    secret = "phc-token-MUST-NOT-LEAK-12345"
    with caplog.at_level(logging.DEBUG, logger="gdx_dispatch.modules.phone_com.client"), PhoneComClient(token=secret) as c:
        c.test_token()
    for rec in caplog.records:
        assert secret not in rec.getMessage(), f"token leaked: {rec.getMessage()}"

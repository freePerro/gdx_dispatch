"""OutlookGraphClient — 429/Retry-After + transient-error backoff.

The Graph client used to raise on ANY >=400; an uncapped attachment sweep would
get throttled and abort. These pin the retry behavior (injected sleep, mocked
transport — no real network, no real waiting).
"""
from __future__ import annotations

import httpx
import pytest

from gdx_dispatch.modules.outlook.graph_client import (
    OutlookGraphAPIError,
    OutlookGraphClient,
)


def _client(responses, delays, **kw):
    c = OutlookGraphClient("tok", sleep=lambda d: delays.append(d), **kw)
    it = iter(responses)
    c._client.request = lambda *a, **k: next(it)  # mock transport
    return c


def test_retries_on_429_and_honors_retry_after():
    delays = []
    c = _client(
        [httpx.Response(429, headers={"Retry-After": "2"}), httpx.Response(200, json={"ok": True})],
        delays,
    )
    resp = c._request("GET", "/me")
    assert resp.status_code == 200
    assert delays == [2.0]  # waited exactly the Retry-After


def test_exponential_backoff_without_retry_after():
    delays = []
    c = _client(
        [httpx.Response(503), httpx.Response(503), httpx.Response(200, json={})],
        delays,
    )
    c._request("GET", "/x")
    assert delays == [1.0, 2.0]  # 2**0, 2**1


def test_gives_up_after_max_retries_and_raises():
    delays = []
    c = _client([httpx.Response(429) for _ in range(3)], delays, max_retries=2)
    with pytest.raises(OutlookGraphAPIError) as ei:
        c._request("GET", "/x")
    assert ei.value.status_code == 429
    assert len(delays) == 2  # retried twice, then surfaced


def test_non_retryable_4xx_raises_immediately_without_sleeping():
    delays = []
    c = _client([httpx.Response(404, json={"error": "not found"})], delays)
    with pytest.raises(OutlookGraphAPIError) as ei:
        c._request("GET", "/x")
    assert ei.value.status_code == 404
    assert delays == []  # a 404 is not retried


def test_retry_after_is_capped():
    delays = []
    c = _client(
        [httpx.Response(429, headers={"Retry-After": "9999"}), httpx.Response(200, json={})],
        delays,
        retry_max_delay_s=60,
    )
    c._request("GET", "/x")
    assert delays == [60.0]  # capped, not 9999


def test_http_date_retry_after_falls_back_to_backoff():
    delays = []
    c = _client(
        [httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
         httpx.Response(200, json={})],
        delays,
    )
    c._request("GET", "/x")
    assert delays == [1.0]  # unparseable seconds → backoff (2**0)

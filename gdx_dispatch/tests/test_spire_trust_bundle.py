"""SS-32 slice C tests — trust-bundle cache."""
from __future__ import annotations

import logging

import pytest

from gdx_dispatch.core.spiffe.spire_trust_bundle import (
    TrustBundleCache,
    TrustBundleError,
)

TD = "example.com"


def _bundle(n=1):
    return {
        TD: {
            "x509_authorities": ["PEM" + str(i) for i in range(n)],
            "jwt_authorities": [{"kid": f"k{i}"} for i in range(n)],
        }
    }


def test_first_call_fetches():
    calls = []

    def fetch(ep):
        calls.append(ep)
        return _bundle(1)

    c = TrustBundleCache(endpoint="http://spire/bundle", fetcher=fetch)
    out = c.get(now=1000.0)
    assert TD in out
    assert calls == ["http://spire/bundle"]


def test_returns_cached_within_ttl():
    calls = []

    def fetch(ep):
        calls.append(ep)
        return _bundle(1)

    c = TrustBundleCache(endpoint="x", fetcher=fetch, ttl_seconds=300)
    c.get(now=1000.0)
    c.get(now=1100.0)
    c.get(now=1299.0)
    assert len(calls) == 1


def test_refreshes_after_ttl():
    calls = []

    def fetch(ep):
        calls.append(ep)
        return _bundle(len(calls))

    c = TrustBundleCache(endpoint="x", fetcher=fetch, ttl_seconds=300)
    c.get(now=1000.0)
    c.get(now=1400.0)  # past TTL
    assert len(calls) == 2


def test_first_call_fetch_failure_raises():
    def fetch(ep):
        raise RuntimeError("unreachable")

    c = TrustBundleCache(endpoint="x", fetcher=fetch)
    with pytest.raises(TrustBundleError, match="unable to fetch"):
        c.get(now=1000.0)


def test_serves_stale_within_budget(caplog):
    calls = {"n": 0}

    def fetch(ep):
        calls["n"] += 1
        if calls["n"] == 1:
            return _bundle(1)
        raise RuntimeError("spire down")

    c = TrustBundleCache(
        endpoint="x",
        fetcher=fetch,
        ttl_seconds=300,
        max_stale_seconds=3600,
        stale_warn_seconds=600,
    )
    c.get(now=1000.0)
    with caplog.at_level(logging.WARNING):
        out = c.get(now=1400.0)  # ttl passed, refresh fails
    assert out  # stale served
    assert any("refresh failed" in r.message for r in caplog.records)


def test_warns_loudly_when_deeply_stale(caplog):
    calls = {"n": 0}

    def fetch(ep):
        calls["n"] += 1
        if calls["n"] == 1:
            return _bundle(1)
        raise RuntimeError("spire down")

    c = TrustBundleCache(
        endpoint="x",
        fetcher=fetch,
        ttl_seconds=300,
        max_stale_seconds=3600,
        stale_warn_seconds=600,
    )
    c.get(now=1000.0)
    with caplog.at_level(logging.WARNING):
        c.get(now=2200.0)  # 900s past ttl > 600s warn threshold
    assert any("past TTL" in r.message for r in caplog.records)


def test_refuses_when_too_stale():
    calls = {"n": 0}

    def fetch(ep):
        calls["n"] += 1
        if calls["n"] == 1:
            return _bundle(1)
        raise RuntimeError("spire down")

    c = TrustBundleCache(
        endpoint="x",
        fetcher=fetch,
        ttl_seconds=60,
        max_stale_seconds=300,
    )
    c.get(now=1000.0)
    with pytest.raises(TrustBundleError, match="too stale|refusing|stale"):
        c.get(now=5000.0)  # massively past max stale


def test_force_refresh_success():
    calls = []

    def fetch(ep):
        calls.append(1)
        return _bundle(len(calls))

    c = TrustBundleCache(endpoint="x", fetcher=fetch, ttl_seconds=3600)
    c.get(now=1000.0)
    c.force_refresh(now=1010.0)
    assert len(calls) == 2


def test_force_refresh_failure_raises_and_does_not_clear_cache():
    calls = {"n": 0}

    def fetch(ep):
        calls["n"] += 1
        if calls["n"] == 1:
            return _bundle(1)
        raise RuntimeError("nope")

    c = TrustBundleCache(endpoint="x", fetcher=fetch)
    c.get(now=1000.0)
    with pytest.raises(TrustBundleError):
        c.force_refresh(now=1010.0)
    # cache still usable
    out = c.get(now=1020.0)
    assert TD in out


def test_snapshot_no_cache():
    c = TrustBundleCache(endpoint="http://x", fetcher=lambda _ep: _bundle(1))
    snap = c.snapshot()
    assert snap["cached"] is False
    assert snap["endpoint"] == "http://x"


def test_snapshot_with_cache():
    c = TrustBundleCache(endpoint="http://x", fetcher=lambda _ep: _bundle(2))
    c.get()
    snap = c.snapshot()
    assert snap["cached"] is True
    assert TD in snap["trust_domains"]
    assert snap["authority_counts"][TD] == {"x509": 2, "jwt": 2}
    assert snap["fresh"] is True

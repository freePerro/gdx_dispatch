"""GDXA-1597: the rate-limit key is the real client, never the proxy, never the header.

In production every request's socket peer is the nginx container, so slowapi's
`get_remote_address` gave every client one shared 120/minute bucket per worker,
and the code that read X-Forwarded-For took its first hop, which the client
writes. These run the real app with the peer set to a Docker-network address,
the way nginx arrives, and count to the 121st request.

`limiter` and its in-memory counters are module-global (see
test_slowapi_e2e_bypass_579.py), so the fixture resets and restores them.
"""
from __future__ import annotations

import logging
import os

import pytest
from fastapi.testclient import TestClient

from gdx_dispatch.core.client_ip import parse_networks, parse_trusted_proxies, resolve_client_host

os.environ.setdefault("JWT_SECRET", "test-jwt-secret-at-least-32-bytes-long-for-hs256-sha256-safety")

NGINX = "172.18.0.5"  # a Docker bridge address: what the app sees as the peer
OUTSIDER = "203.0.113.50"  # a public peer reaching the app without the proxy
PROBES = 130
PATH = "/health"


@pytest.fixture
def build(monkeypatch):
    import gdx_dispatch.app as appmod

    enabled_before = appmod.limiter.enabled
    monkeypatch.delenv("GDX_E2E_BYPASS", raising=False)
    monkeypatch.delenv("GDX_TRUSTED_PROXIES", raising=False)

    def _build(peer: str) -> TestClient:
        app = appmod.create_app()
        appmod.limiter.reset()
        return TestClient(app, client=(peer, 40000))

    yield _build

    appmod.limiter.enabled = enabled_before
    appmod.limiter.reset()


def _first_429(client: TestClient, xff) -> int | None:
    for i in range(1, PROBES + 1):
        value = xff(i) if callable(xff) else xff
        if client.get(PATH, headers={"x-forwarded-for": value}).status_code == 429:
            return i
    return None


def test_two_clients_behind_the_proxy_get_separate_buckets(build) -> None:
    client = build(NGINX)
    assert _first_429(client, "198.51.100.1") == 121, "slowapi no longer limits the probe path"
    # Before the fix both keyed on NGINX: this client was already spent.
    assert client.get(PATH, headers={"x-forwarded-for": "198.51.100.2"}).status_code == 200


def test_a_spoofed_first_hop_through_the_proxy_does_not_dodge_the_limit(build) -> None:
    """nginx appends the real peer, so the client controls only the left of it."""
    client = build(NGINX)
    assert _first_429(client, lambda i: f"10.9.{i // 256}.{i % 256}, 198.51.100.1") == 121


def test_an_untrusted_peer_cannot_pick_its_key(build) -> None:
    """Reaching the app directly, a rotating header must change nothing."""
    client = build(OUTSIDER)
    assert _first_429(client, lambda i: f"198.51.100.{i % 250}") == 121


TRUSTED = parse_trusted_proxies(None)
CF = parse_networks("cloudflare", name="test")


@pytest.mark.parametrize(
    ("peer", "xff", "expected"),
    [
        (NGINX, "198.51.100.1", "198.51.100.1"),
        (NGINX, "1.1.1.1, 198.51.100.1", "198.51.100.1"),  # spoofed left hop ignored
        (NGINX, "198.51.100.1:5555", "198.51.100.1"),
        (NGINX, "[2001:db8::1]:5555", "2001:db8::1"),
        (OUTSIDER, "198.51.100.1", OUTSIDER),  # untrusted peer: header ignored
        (NGINX, "", NGINX),
        (NGINX, " , ", NGINX),
        ("::ffff:172.18.0.5", "198.51.100.1", "198.51.100.1"),  # v4-mapped peer
        ("testclient", "198.51.100.1", "testclient"),  # non-IP peer is not trusted
    ],
)
def test_resolve_client_host(peer, xff, expected) -> None:
    assert resolve_client_host(peer, xff, TRUSTED) == expected


def test_a_private_hop_recorded_by_nginx_is_not_stepped_through() -> None:
    """Production, 2026-10-18: an IPv6 client straight to the origin reaches
    nginx through Docker's userland proxy, so nginx records the bridge gateway.
    Trusting that private hop handed the key back to the client's header."""
    for spoof in ("198.51.100.1", "198.51.100.2", "198.51.100.3"):
        assert resolve_client_host(NGINX, f"{spoof}, 172.18.0.1", TRUSTED, CF) == "172.18.0.1"


def test_cloudflare_is_stepped_through_only_when_listed() -> None:
    xff = "10.9.9.9, 198.51.100.1, 173.245.48.7"  # spoof, client (by Cloudflare), edge (by nginx)
    assert resolve_client_host(NGINX, xff, TRUSTED) == "173.245.48.7"
    assert resolve_client_host(NGINX, xff, TRUSTED, CF) == "198.51.100.1"
    # Bypassing Cloudflare to the origin: the client is the hop nginx recorded.
    assert resolve_client_host(NGINX, "173.245.48.7, 198.51.100.9", TRUSTED, CF) == "198.51.100.9"


def test_the_middleware_reads_both_env_lists(monkeypatch) -> None:
    from gdx_dispatch.core.client_ip import ClientAddressMiddleware

    monkeypatch.setenv("GDX_TRUSTED_PROXIES", "")
    monkeypatch.setenv("GDX_TRUSTED_UPSTREAM_PROXIES", "cloudflare")
    mw = ClientAddressMiddleware(app=None)
    assert mw.trusted == TRUSTED  # empty falls back to the default
    assert mw.upstream == CF and len(CF) == 21


def test_a_cloudflare_worker_hop_is_not_stepped_through() -> None:
    """A Worker's subrequest leaves from 2a06:98c0::/29 carrying whatever
    X-Forwarded-For the Worker wrote; Cloudflare appends the Worker's address,
    nginx appends the edge. Stepping through the Worker hop let the client pick
    the key (second audit of GDXA-1597)."""
    for spoof in ("198.51.100.1", "198.51.100.2"):
        xff = f"{spoof}, 2a06:98c0:3600::103, 172.68.204.218"
        assert resolve_client_host(NGINX, xff, TRUSTED, CF) == "2a06:98c0:3600::103"


def test_a_wildcard_is_refused_and_junk_is_skipped() -> None:
    assert parse_networks("*", name="test") == ()
    assert [str(n) for n in parse_networks("*, nonsense, 10.1.2.3", name="test")] == ["10.1.2.3/32"]
    # Nothing trusted: the header is never read.
    assert resolve_client_host(NGINX, "198.51.100.1", parse_trusted_proxies("*")) == NGINX


def test_a_rejected_entry_is_logged_by_position_not_value(caplog: pytest.LogCaptureFixture) -> None:
    """The value comes from the environment and may be a misplaced secret."""
    with caplog.at_level(logging.ERROR, logger="gdx_dispatch.core.client_ip"):
        parse_networks("10.1.2.3, sk_live_not_an_address", name="GDX_TRUSTED_PROXIES")
    assert "sk_live_not_an_address" not in caplog.text
    assert "GDX_TRUSTED_PROXIES: entry 2 is not an address or network" in caplog.text

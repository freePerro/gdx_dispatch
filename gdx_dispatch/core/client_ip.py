"""The real client address, with X-Forwarded-For trusted only from our proxy.

In production nginx is a separate container and uvicorn trusts forwarded
headers from 127.0.0.1 only, so every request's socket peer is nginx. Before
GDXA-1597 that split the app two ways: slowapi keyed its 120/minute bucket on
the peer, so every client shared one bucket per worker, and five other places
(the per-caller rate limiter, both audit IP derivations, two routers) took
the FIRST X-Forwarded-For hop, which the client writes itself. A client could
rotate that header to get a fresh login-limit bucket per request, and put any
address it liked in the audit trail.

`ClientAddressMiddleware` sits outermost and rewrites the ASGI `client` once,
so `request.client.host` is the real client everywhere downstream: slowapi's
`get_remote_address`, the rate limiter, the audit rows, and the routers that
already read `request.client.host`.

The rule has two trust sets, because they answer different questions:

- `GDX_TRUSTED_PROXIES` — may the socket PEER speak for the client? That is
  our own reverse proxy. Unset or empty means loopback plus the private
  ranges: only the host and its Docker networks can reach the app's port.
- `GDX_TRUSTED_UPSTREAM_PROXIES` — which addresses INSIDE the header are
  proxies too? Empty by default. `cloudflare` expands to Cloudflare's
  published ranges, for a deployment behind Cloudflare whose nginx does not
  restore the client with `real_ip`.

When the peer is trusted, the client is the LAST hop (the address our proxy
saw), stepping further left only through upstream proxies. The client can
write anything to the left of that, but never reaches past its own hop, as
long as no stepped range is one a client can send from (see
CLOUDFLARE_RANGES for the one that is left out for that reason).

The private ranges are deliberately NOT stepped through. With Docker's
userland proxy (the default), an IPv6 connection straight to the origin
reaches nginx from the bridge gateway, e.g. 172.18.0.1, so nginx records a
private address for a client on the internet. Trusting that hop handed the
key back to the header (shown on production, 2026-10-18). Stopped there, such
clients share one bucket, which is a cost, not a spoof.

Only the client address is rewritten, not the scheme (uvicorn's own
proxy-headers middleware also rewrites X-Forwarded-Proto; that would change
`url_for` and `request.base_url` output, which is a different change).
`*` is refused in both lists: trusting everyone makes the header the key.
"""
from __future__ import annotations

import ipaddress
import logging
import os
from collections.abc import Iterable
from typing import Any

log = logging.getLogger("gdx_dispatch.core.client_ip")

_Network = ipaddress.IPv4Network | ipaddress.IPv6Network

DEFAULT_TRUSTED_PROXIES = (
    "127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fc00::/7"
)

# https://www.cloudflare.com/ips-v4 and /ips-v6, fetched 2026-10-18 (API etag
# 38f79d050aa027e3be3865e495dcc9bc). Cloudflare changes these rarely and
# announces it; re-fetch when it does.
#
# Less one: 2a06:98c0::/29 is where Cloudflare Workers subrequests come from
# (developers.cloudflare.com/fundamentals/reference/http-headers, read
# 2026-10-18). A Worker writes its own X-Forwarded-For and Cloudflare appends
# to it, so stepping through that block lands on a hop anyone with a free
# Workers account chose. A real edge address in it is kept as the client,
# which costs a shared bucket, not a spoof.
CLOUDFLARE_RANGES = (
    "173.245.48.0/20,103.21.244.0/22,103.22.200.0/22,103.31.4.0/22,"
    "141.101.64.0/18,108.162.192.0/18,190.93.240.0/20,188.114.96.0/20,"
    "197.234.240.0/22,198.41.128.0/17,162.158.0.0/15,104.16.0.0/13,"
    "104.24.0.0/14,172.64.0.0/13,131.0.72.0/22,"
    "2400:cb00::/32,2606:4700::/32,2803:f800::/32,2405:b500::/32,"
    "2405:8100::/32,2c0f:f248::/32"
)


def parse_networks(raw: str | None, *, name: str) -> tuple[_Network, ...]:
    """Parse a comma-separated list of addresses, CIDRs and `cloudflare`."""
    networks: list[_Network] = []
    for item in (raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        if item == "*":
            log.error("%s: '*' ignored; trusting every address would let a client pick its own", name)
            continue
        if item.lower() == "cloudflare":
            networks.extend(parse_networks(CLOUDFLARE_RANGES, name=name))
            continue
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            log.error("%s: %r is not an address or network; ignored", name, item)
    return tuple(networks)


def parse_trusted_proxies(raw: str | None) -> tuple[_Network, ...]:
    """The peer set; an empty value means the default ranges."""
    return parse_networks((raw or "").strip() or DEFAULT_TRUSTED_PROXIES, name="GDX_TRUSTED_PROXIES")


def _in(host: str, networks: Iterable[_Network]) -> bool:
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        # A hop that is not an address (`unknown`, an obfuscated name) is never
        # a proxy we step through; debug only, since the client writes it.
        log.debug("not an address, treated as untrusted: %r", host)
        addr = None
    if addr is None:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return any(addr in net for net in networks)


def _strip_port(hop: str) -> str:
    """`1.2.3.4:5678` and `[::1]:5678` to their address; a bare address as is."""
    if hop.startswith("["):
        end = hop.find("]")
        return hop[1:end] if end != -1 else hop
    if hop.count(":") == 1:
        return hop.split(":", 1)[0]
    return hop


def resolve_client_host(
    peer: str | None,
    forwarded_for: str | None,
    trusted: Iterable[_Network],
    upstream: Iterable[_Network] = (),
) -> str | None:
    """The real client address for a request from `peer` carrying `forwarded_for`."""
    upstream = tuple(upstream)
    if not peer or not forwarded_for or not _in(peer, trusted):
        return peer
    hops = [_strip_port(h.strip()) for h in forwarded_for.split(",") if h.strip()]
    if not hops:
        return peer
    host = hops.pop()  # what our own proxy saw
    while hops and _in(host, upstream):
        host = hops.pop()
    return host


def request_client_host(request: Any) -> str | None:
    """The client address of a request (or None), for an audit row.

    Reads `request.client`, never X-Forwarded-For: the middleware below has
    already resolved the real client from the trusted proxy's hops.
    """
    client = getattr(request, "client", None) if request is not None else None
    return getattr(client, "host", None)


class ClientAddressMiddleware:
    """Rewrites the ASGI `client` to the real client address (pure ASGI)."""

    def __init__(self, app: Any, trusted_proxies: str | None = None, upstream_proxies: str | None = None) -> None:
        self.app = app
        if trusted_proxies is None:
            trusted_proxies = os.environ.get("GDX_TRUSTED_PROXIES")
        if upstream_proxies is None:
            upstream_proxies = os.environ.get("GDX_TRUSTED_UPSTREAM_PROXIES")
        self.trusted = parse_trusted_proxies(trusted_proxies)
        self.upstream = parse_networks(upstream_proxies, name="GDX_TRUSTED_UPSTREAM_PROXIES")

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] in ("http", "websocket"):
            client = scope.get("client")
            forwarded = b", ".join(v for k, v in scope.get("headers") or [] if k == b"x-forwarded-for")
            if client and forwarded:
                host = resolve_client_host(client[0], forwarded.decode("latin-1"), self.trusted, self.upstream)
                if host and host != client[0]:
                    scope = dict(scope)
                    scope["client"] = (host, 0)
        await self.app(scope, receive, send)

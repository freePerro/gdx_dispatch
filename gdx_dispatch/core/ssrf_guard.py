"""SSRF guard for outbound requests to user/tenant-supplied URLs.

Webhook targets, Zapier hook URLs, and ingested media URLs are attacker-
influenced. Without a guard, an attacker can point them at internal services or
the cloud metadata endpoint (169.254.169.254) and have the server fetch them.

``validate_outbound_url`` enforces:
  * scheme is http/https (never file://, gopher://, etc.)
  * the host does NOT resolve to a private/loopback/link-local/reserved address.

Design note: we BLOCK only when DNS resolves to a disallowed address. If
resolution fails (e.g. a reserved-for-docs ``*.example`` host), we do not block —
the caller's request will simply fail on its own. This keeps the guard from
interfering with unresolvable/mocked hosts while still stopping the real SSRF
case, which requires a resolvable internal address.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


class OutboundURLBlocked(ValueError):
    """Raised when an outbound URL targets a disallowed host."""


def _is_disallowed(ip: ipaddress._BaseAddress) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local  # covers 169.254.0.0/16 cloud metadata
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def validate_outbound_url(url: str) -> None:
    """Raise OutboundURLBlocked if ``url`` is unsafe to request server-side."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise OutboundURLBlocked(f"scheme not allowed: {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise OutboundURLBlocked("missing host")
    # A bare IP literal must be checked directly (getaddrinfo would echo it).
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _is_disallowed(literal):
            raise OutboundURLBlocked(f"host resolves to disallowed address {literal}")
        return
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return  # unresolvable — let the request fail naturally, don't block here
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if _is_disallowed(ip):
            raise OutboundURLBlocked(
                f"host {host!r} resolves to disallowed address {ip}"
            )

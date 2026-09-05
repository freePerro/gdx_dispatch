"""SS-26 Slice A: .well-known/gdx-platform manifest builder.

Builds the custom GDX discovery manifest surfaced at
``GET /.well-known/gdx-platform``. Keeping the builder separate from the
router lets tests exercise shape + content independently of HTTP wiring.

Builders accept an optional ``base_url`` so the router can derive it from
the inbound request host and the ``.well-known/*`` answers point back at the
host the caller actually reached.

What these documents may claim
------------------------------
Only endpoints this application serves. Until 2026-09-04 the manifest
advertised an OAuth authorization server, a developer portal, a metering API
and an event catalog — all of which left with the multi-tenant platform
routers. On production ``/oauth/authorize``, ``/oauth/token`` and
``/oauth/register`` answered **200 with the SPA's HTML**, not even a 404, so
a client following discovery got a login page where it expected JSON.

There is no authorization server here. ``/mcp`` accepts a bearer token minted
by ``core.mcp_bearer``; there is no endpoint or UI that issues one. Advertising
an OAuth flow that cannot complete is worse than advertising nothing, because
it sends a conforming client down a path with no exit.

The RFC 9728 Protected Resource Metadata document went with them, and that is
deliberate. MCP Authorization (revision 2025-06-18,
https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization)
makes authorization **OPTIONAL** for MCP implementations, but requires of
those that support it: *"The Protected Resource Metadata document returned by
the MCP server MUST include the authorization_servers field containing at
least one authorization server."* This server implements no OAuth
authorization, so it has no conformant PRM document to serve — one naming an
authorization server would point at the metadata deleted above, and one
omitting the field would violate that MUST. Serving nothing is the honest
answer: a client learns immediately that this server does not do OAuth,
instead of following a chain to a 404.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import Request

MANIFEST_VERSION = "1"
DEFAULT_BASE_URL = "https://gdx.example.com"
DEFAULT_CONTACT = "developers@example.com"


def _base_url() -> str:
    """Resolve the public base URL (no trailing slash)."""
    url = os.environ.get("GDX_PUBLIC_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    return url


def request_base_url(request: "Request") -> str:
    """Per-tenant issuer derived from the inbound request host.

    The MCP plan requires the OAuth issuer to equal the host the client
    saw — that's what claude.ai's connector verifies against. Behind the
    production reverse proxy (Cloudflare → nginx) the request scheme is
    HTTPS by the time it reaches the app, but TestClient defaults to
    ``http://``; the ``X-Forwarded-Proto`` header is the canonical
    upstream-protocol signal. Falls back to ``request.url.scheme``,
    then ``https`` (production-correct default).
    """
    host = request.headers.get("host")
    if not host:
        # No host header → cannot scope per-tenant. Hard-fail loud rather
        # than silently fall back to the platform host (the original bug).
        raise ValueError("request has no Host header; cannot derive tenant issuer")
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme or "https"
    return f"{proto}://{host}".rstrip("/")


def _contact_email() -> str:
    return os.environ.get("GDX_SUPPORT_EMAIL", DEFAULT_CONTACT)


def build_manifest(base_url: str | None = None, contact_email: str | None = None) -> dict[str, Any]:
    """Return the `.well-known/gdx-platform` JSON document.

    Callers may inject base_url / contact_email to make the builder
    deterministic in tests. In production the env-derived defaults apply.
    """
    base = (base_url or _base_url()).rstrip("/")
    contact = contact_email or _contact_email()

    return {
        "name": "GDX Platform",
        "version": MANIFEST_VERSION,
        "issuer": base,
        "api_docs_url": f"{base}/docs",
        "contact_email": contact,
        "supported_features": [
            "mcp.tools",
        ],
        "directory_endpoints": {
            "gdx_platform": f"{base}/.well-known/gdx-platform",
            "security_txt": f"{base}/.well-known/security.txt",
            "mcp_tools": f"{base}/.well-known/mcp-tools",
        },
        "mcp_endpoint": f"{base}/mcp",
    }


def build_security_txt(
    contact_email: str | None = None,
    expires_iso: str | None = None,
    base_url: str | None = None,
) -> str:
    """RFC 9116 security.txt body.

    Required fields: Contact, Expires, Preferred-Languages.
    ``expires_iso`` lets tests inject a deterministic Expires value; in
    production callers pass a year-from-now ISO 8601 timestamp.
    """
    contact = contact_email or os.environ.get("GDX_SECURITY_EMAIL", "security@example.com")
    base = (base_url or _base_url()).rstrip("/")
    if expires_iso is None:
        # Lazily import datetime to keep the builder easy to mock in tests.
        from datetime import datetime, timedelta, timezone

        expires_iso = (datetime.now(timezone.utc) + timedelta(days=365)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    # Contact, Expires and Preferred-Languages are the required/recommended
    # fields. Policy and Acknowledgments are optional and were pointing at
    # /security/policy and /security/hall-of-fame, neither of which exists —
    # both fall through to the SPA catch-all and return the app's HTML.
    lines = [
        f"Contact: mailto:{contact}",
        f"Expires: {expires_iso}",
        "Preferred-Languages: en",
        f"Canonical: {base}/.well-known/security.txt",
    ]
    return "\n".join(lines) + "\n"


def build_mcp_tools_manifest(base_url: str | None = None) -> dict[str, Any]:
    """.well-known/mcp-tools — where the MCP transport lives.

    Deliberately does NOT enumerate tools. This document is unauthenticated,
    and the tool set includes things like ``invoices.void``, ``email.read``
    and ``documents.read``; publishing that list tells an anonymous caller
    exactly what the server can be made to do. It is also unnecessary — the
    MCP transport answers ``tools/list`` for callers that present a bearer
    token, which is the correct place for a capability list to be gated.

    The previous version hard-coded three names (``list_customers``,
    ``create_job``, ``get_invoice``) and gave each a ``uri`` under
    ``/api/mcp/tools/<name>``, a prefix with no routes. Swapping those for
    the live registry names would have been wrong twice over: it would have
    published the real inventory, and in the wrong spelling —
    ``core/mcp_fastmcp_bridge.py`` rewrites dots to underscores on the wire
    (``invoices.void`` is exposed as ``invoices_void``) because claude.ai's
    tool-name validator rejects dots.
    """
    base = (base_url or _base_url()).rstrip("/")
    return {
        "version": MANIFEST_VERSION,
        "mcp_endpoint": f"{base}/mcp",
        "tools_discovery": "Call tools/list on mcp_endpoint with a bearer token.",
    }

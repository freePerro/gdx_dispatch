"""SS-26 Slice A: .well-known/gdx-platform manifest builder.

Builds the custom GDX discovery manifest surfaced at
``GET /.well-known/gdx-platform``. Keeping the builder separate from the
router lets tests exercise shape + content independently of HTTP wiring.

Sprint mcp-streamable-http S3 — per-tenant issuer
-------------------------------------------------
Builders accept an optional ``base_url`` so the router can derive it
from the inbound request host (``https://gdx.example.com``) and
each tenant's ``.well-known/*`` answers point back at the same tenant's
own host. Without this the issuer was hard-coded to
``gdx.example.com``, which broke RFC 8414 issuer-equality
checks in claude.ai's MCP connector.
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
            "oauth2.authorization_code",
            "oauth2.pkce.s256",
            "oauth2.refresh_token",
            "oauth2.token_introspection",
            "oauth2.token_revocation",
            "events.catalog",
            "events.outbox_replay",
            "metering.usage_summaries",
            "mcp.tools",
            "dev_portal",
        ],
        "directory_endpoints": {
            "oauth_authorization_server": f"{base}/.well-known/oauth-authorization-server",
            "oauth_protected_resource": f"{base}/.well-known/oauth-protected-resource",
            "openid_configuration": f"{base}/.well-known/openid-configuration",
            "gdx_platform": f"{base}/.well-known/gdx-platform",
            "security_txt": f"{base}/.well-known/security.txt",
            "mcp_tools": f"{base}/.well-known/mcp-tools",
        },
        "oauth_endpoints": {
            "authorization_endpoint": f"{base}/oauth/authorize",
            "token_endpoint": f"{base}/oauth/token",
            "revocation_endpoint": f"{base}/oauth/revoke",
            "introspection_endpoint": f"{base}/oauth/introspect",
            "registration_endpoint": f"{base}/oauth/register",
            "jwks_uri": f"{base}/.well-known/jwks.json",
        },
        "mcp_endpoint": f"{base}/mcp",
        "event_catalog_url": f"{base}/api/events/catalog",
        "deprecation_policy_url": f"{base}/developers/deprecation-policy",
    }


def build_oauth_authorization_server(base_url: str | None = None) -> dict[str, Any]:
    """RFC 8414 OAuth Authorization Server Metadata.

    Required + recommended fields for the GDX platform. PKCE S256
    support is load-bearing — downstream clients (SS-21) depend on it.

    Sprint mcp-streamable-http S3 additions:
        * ``resource_indicators_supported: true`` — RFC 8707. claude.ai's
          MCP connector requires this to be advertised; without it the
          token request omits ``resource=`` and S4's tenant-binding
          ``aud`` claim cannot be verified.
        * ``registration_endpoint`` — RFC 7591 DCR (S5).
    """
    base = (base_url or _base_url()).rstrip("/")
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "revocation_endpoint": f"{base}/oauth/revoke",
        "introspection_endpoint": f"{base}/oauth/introspect",
        "registration_endpoint": f"{base}/oauth/register",
        "jwks_uri": f"{base}/.well-known/jwks.json",
        "response_types_supported": ["code"],
        "grant_types_supported": [
            "authorization_code",
            "refresh_token",
            "client_credentials",
        ],
        "token_endpoint_auth_methods_supported": [
            "client_secret_basic",
            "client_secret_post",
            "private_key_jwt",
        ],
        "code_challenge_methods_supported": ["S256"],
        "scopes_supported": [
            "openid",
            "profile",
            "email",
            "events:read",
            "mcp:invoke",
            "usage:read",
        ],
        "resource_indicators_supported": True,
        "service_documentation": f"{base}/docs",
    }


def build_oauth_protected_resource(base_url: str | None = None) -> dict[str, Any]:
    """RFC 9728 OAuth 2.0 Protected Resource Metadata.

    Required by claude.ai's MCP connector flow: the connector first
    fetches ``/.well-known/oauth-protected-resource`` to discover the
    resource identifier (``<host>/mcp``) and the matching authorization
    server (``<host>``), then fetches that AS's
    ``/.well-known/oauth-authorization-server`` for endpoint discovery.

    The ``resource`` value MUST equal the canonical ``aud`` claim S4
    bakes into issued tokens.
    """
    base = (base_url or _base_url()).rstrip("/")
    return {
        "resource": f"{base}/mcp",
        "authorization_servers": [base],
        "scopes_supported": ["mcp:invoke"],
        "bearer_methods_supported": ["header"],
        "resource_documentation": f"{base}/docs",
    }


def build_openid_configuration(base_url: str | None = None) -> dict[str, Any]:
    """Minimal OpenID Connect Discovery document.

    Shares issuer + endpoints with the OAuth AS metadata plus the
    OIDC-specific subject_types_supported / id_token signing fields.
    """
    base = (base_url or _base_url()).rstrip("/")
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "userinfo_endpoint": f"{base}/oauth/userinfo",
        "jwks_uri": f"{base}/.well-known/jwks.json",
        "response_types_supported": ["code"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
        "scopes_supported": ["openid", "profile", "email"],
        "token_endpoint_auth_methods_supported": [
            "client_secret_basic",
            "client_secret_post",
        ],
        "claims_supported": ["sub", "iss", "email", "email_verified", "name"],
        "code_challenge_methods_supported": ["S256"],
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
    lines = [
        f"Contact: mailto:{contact}",
        f"Expires: {expires_iso}",
        "Preferred-Languages: en",
        f"Canonical: {base}/.well-known/security.txt",
        f"Policy: {base}/security/policy",
        f"Acknowledgments: {base}/security/hall-of-fame",
    ]
    return "\n".join(lines) + "\n"


def build_mcp_tools_manifest(tools: list[dict[str, Any]] | None = None, base_url: str | None = None) -> dict[str, Any]:
    """.well-known/mcp-tools — list of MCP tool names + invocation URIs.

    SS-18/19 clients feature-detect MCP availability from this file.
    Tools list is injected (defaults to the currently known static set)
    so tests can assert a known shape without hitting the MCP registry.
    """
    base = (base_url or _base_url()).rstrip("/")
    if tools is None:
        tools = [
            {"name": "list_customers", "uri": f"{base}/api/mcp/tools/list_customers"},
            {"name": "create_job", "uri": f"{base}/api/mcp/tools/create_job"},
            {"name": "get_invoice", "uri": f"{base}/api/mcp/tools/get_invoice"},
        ]
    return {
        "version": MANIFEST_VERSION,
        "mcp_endpoint": f"{base}/mcp",
        "legacy_mcp_endpoint": f"{base}/api/mcp",
        "tools_index_url": f"{base}/api/mcp/tools",
        "tools": tools,
    }

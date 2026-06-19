"""SS-26 Slice B: /.well-known/* discovery endpoints.

Surfaces well-known endpoints so agents + third-party integrations can
feature-detect the GDX platform from a single root URL.

Endpoints:
    GET /.well-known/oauth-authorization-server  (RFC 8414)    -> JSON
    GET /.well-known/oauth-protected-resource    (RFC 9728)    -> JSON
    GET /.well-known/openid-configuration        (OIDC Disc.)  -> JSON
    GET /.well-known/gdx-platform                (GDX custom)  -> JSON
    GET /.well-known/security.txt                (RFC 9116)    -> text/plain
    GET /.well-known/mcp-tools                   (SS-18/19)    -> JSON

Sprint mcp-streamable-http S3 — per-tenant issuer
-------------------------------------------------
Each endpoint derives its issuer/base URL from the inbound request host
via ``request_base_url(request)``. Hitting
``gdx.example.com/.well-known/oauth-authorization-server`` yields
``issuer = https://gdx.example.com``; hitting the same path on
another tenant host yields that tenant's issuer. RFC 8414 issuer-equality
checks in claude.ai's connector now succeed.
"""
from __future__ import annotations

from fastapi import APIRouter, Request, Response

from gdx_dispatch.core import well_known_manifest as wkm

# Note: no prefix — .well-known is a root-absolute standard path.
router = APIRouter(tags=["well-known"])


@router.get("/.well-known/oauth-authorization-server")
def oauth_authorization_server(request: Request) -> dict:
    """RFC 8414 OAuth 2.0 Authorization Server Metadata."""
    return wkm.build_oauth_authorization_server(wkm.request_base_url(request))


@router.get("/.well-known/oauth-protected-resource")
def oauth_protected_resource(request: Request) -> dict:
    """RFC 9728 OAuth 2.0 Protected Resource Metadata.

    claude.ai's MCP connector fetches this first, reads the ``resource``
    URI (``<host>/mcp``), then hits the ``authorization_servers[0]``'s
    ``/.well-known/oauth-authorization-server`` for endpoints. The
    ``resource`` value MUST equal the canonical ``aud`` claim S4
    bakes into issued tokens.
    """
    return wkm.build_oauth_protected_resource(wkm.request_base_url(request))


@router.get("/.well-known/oauth-protected-resource/{resource_path:path}")
def oauth_protected_resource_path(request: Request, resource_path: str) -> dict:
    """RFC 9728 §3.1 path-suffixed variant.

    Per the spec: when a protected resource lives at a sub-path (e.g.
    ``<host>/mcp``), the metadata document lookup may include the
    resource path: ``/.well-known/oauth-protected-resource/mcp``.
    claude.ai's MCP connector hits this form first; the bare-path
    variant above is a fallback. We return the same metadata for both
    since this server hosts only the /mcp resource.

    The ``resource_path`` parameter is accepted but not validated —
    misrouted requests still land on the canonical metadata, which is
    the correct shape for the only resource we host. Clients that need
    to validate they hit the right resource compare the returned
    ``resource`` field against their target URL.
    """
    return wkm.build_oauth_protected_resource(wkm.request_base_url(request))


@router.get("/.well-known/openid-configuration")
def openid_configuration(request: Request) -> dict:
    """OpenID Connect Discovery 1.0 metadata."""
    return wkm.build_openid_configuration(wkm.request_base_url(request))


@router.get("/.well-known/gdx-platform")
def gdx_platform_manifest(request: Request) -> dict:
    """GDX-custom platform discovery manifest (SS-26)."""
    return wkm.build_manifest(base_url=wkm.request_base_url(request))


@router.get("/.well-known/security.txt", response_class=Response)
def security_txt(request: Request) -> Response:
    """RFC 9116 security.txt. Served as text/plain."""
    body = wkm.build_security_txt(base_url=wkm.request_base_url(request))
    return Response(content=body, media_type="text/plain; charset=utf-8")


@router.get("/.well-known/mcp-tools")
def mcp_tools_manifest(request: Request) -> dict:
    """List of MCP tool names + invocation URIs for SS-18/19 clients."""
    return wkm.build_mcp_tools_manifest(base_url=wkm.request_base_url(request))

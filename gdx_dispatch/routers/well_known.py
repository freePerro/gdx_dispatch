"""SS-26 Slice B: /.well-known/* discovery endpoints.

Surfaces well-known endpoints so agents + third-party integrations can
feature-detect the GDX platform from a single root URL.

Endpoints:
    GET /.well-known/gdx-platform                (GDX custom)  -> JSON
    GET /.well-known/security.txt                (RFC 9116)    -> text/plain
    GET /.well-known/mcp-tools                   (SS-18/19)    -> JSON

Issuer derivation
-----------------
Each endpoint derives its base URL from the inbound request host via
``request_base_url(request)``, so the answers name the host the caller
actually reached rather than a hard-coded one.

The OAuth authorization-server (RFC 8414), OpenID Discovery and RFC 9728
Protected Resource Metadata documents were removed on 2026-09-04. This
deployment has no authorization server — ``/oauth/authorize``,
``/oauth/token`` and ``/oauth/register`` left with the multi-tenant platform
routers and answer with the SPA's HTML on production — so those documents
could only route a conforming client into a flow with no exit. MCP
Authorization (2025-06-18) makes authorization OPTIONAL but requires a PRM
document to name at least one authorization server, so there is no
conformant PRM this server could serve. ``/mcp`` takes a bearer token minted
by ``core.mcp_bearer``; nothing here issues one.
"""
from __future__ import annotations

from fastapi import APIRouter, Request, Response

from gdx_dispatch.core import well_known_manifest as wkm

# Note: no prefix — .well-known is a root-absolute standard path.
router = APIRouter(tags=["well-known"])


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

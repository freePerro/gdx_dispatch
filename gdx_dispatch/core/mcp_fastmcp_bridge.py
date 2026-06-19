"""Sprint MCP-Streamable-HTTP S1+S4 — bridge `mcp_registry` → FastMCP.

The GDX platform has an in-process tool registry (``mcp_registry``) that
holds every MCP tool's descriptor + handler, plus the canonical
``invoke_tool`` orchestrator (``mcp_invoke``) which performs validation,
capability gating, approval-gate checks, audit logging, and execution
in a fixed order.

That code path is wired to the legacy custom HTTP transport
(``/api/mcp/tools/{name}/invoke``). It is NOT wired to the FastMCP
singleton that lives at ``mcp_protocol_adapter.mcp`` — so claude.ai's
connector, which speaks Streamable-HTTP, sees zero tools.

This module is the bridge. ``bridge_registry_to_fastmcp(mcp)``
iterates every tool in the registry and registers a ``FunctionTool``
on the FastMCP instance whose body delegates back to ``invoke_tool``,
preserving the canonical order-of-operations.

S4 (D-S4-02 fold-in): ``_resolve_principal_from_context`` reads the
verified ``MCPClaims`` that the auth middleware stashed on the ASGI
request state and produces a unified ``Principal``. The middleware is
the security boundary; this module is a consumer.
"""
from __future__ import annotations

import logging
from typing import Any

from uuid import UUID, uuid5, NAMESPACE_URL

from fastmcp import Context, FastMCP
from fastmcp.tools import FunctionTool

from gdx_dispatch.core.mcp_bearer import MCPClaims
from gdx_dispatch.core.mcp_invoke import invoke_tool
from gdx_dispatch.core.mcp_registry import list_tools
from gdx_dispatch.core.mcp_tool_descriptor import ToolDescriptor
from gdx_dispatch.core.unified_principal import Principal

logger = logging.getLogger(__name__)


class PrincipalResolutionFailed(RuntimeError):
    """Raised when the auth middleware did not stash MCPClaims on the request.

    Production must always have the middleware in front of the FastMCP
    sub-app; if this exception fires it's a wiring bug, not an auth
    failure. Surfaced loudly rather than silently substituting an
    anonymous principal — silent fallback would break the cross-tenant
    isolation guarantee.
    """


# kept for backward-compatibility with S1's exception name; subclass so existing
# `except PrincipalResolutionNotWired` blocks (in the S1 test suite) still match.
class PrincipalResolutionNotWired(PrincipalResolutionFailed):
    """Alias for ``PrincipalResolutionFailed`` — retained for S1 test suite."""


class BridgeRegistrationError(RuntimeError):
    """Raised on any unexpected state during registry-→-FastMCP bridging.

    The whole sprint goal is "claude.ai sees the toolset." A silent skip
    or a malformed schema would let the connector come up half-broken.
    Fail loudly instead.
    """


# UUID5 namespace for synthesizing a stable identity_id from an OAuth
# (sub, jti) pair. The MCP-bearer JWT does NOT carry an identities-table
# row id (the OAuth "subject" is whatever the developer-portal seeded —
# email, opaque sub, etc.). The synthesized UUID gives audit/policy a
# stable handle while staying deterministic across requests.
_OAUTH_ID_NAMESPACE = NAMESPACE_URL


# Capability scope mapping. The MCP-bearer JWT carries a space-separated
# scope string (`mcp:invoke`, `mcp:read:customer`, etc). The ``mcp:invoke``
# scope is the broad "let me call any tool I have a registry capability
# for" grant — we map it to the wildcard ``("*", "*")`` capability and
# rely on the per-tool capability gate inside ``mcp_invoke.invoke_tool``
# to deny anything the descriptor restricts. Future scope-shape work
# (D-S4-future) can carve narrower mappings; for now the wildcard
# matches the legacy PAT-based MCP path.
_SCOPE_TO_CAPS: dict[str, tuple[tuple[str, str], ...]] = {
    # mcp:invoke is the broad "may call tools" grant for non-destructive (green)
    # and approval-gated (yellow) tools. It deliberately does NOT confer admin:
    # destructive red-tier tools require the separate mcp:admin scope below, so a
    # routine integration token can't void invoices etc. (security #4).
    "mcp:invoke": (("*", "*"),),
    "mcp:admin": (("admin", "*"),),
}


def _claims_to_principal(claims: MCPClaims) -> Principal:
    """Build a unified ``Principal`` from verified MCP-bearer claims.

    The middleware already verified iss/aud/gdx_tid/sig/exp; here we
    only translate. ``identity_id`` is a UUID5 of ``oauth:<jti>`` so
    audit logs get a stable per-token handle without writing a new
    row to the identities table.
    """
    caps: tuple[tuple[str, str], ...] = ()
    for scope_token in (claims.scope or "").split():
        caps = caps + _SCOPE_TO_CAPS.get(scope_token, ())
    if not caps:
        # No recognised scope → no capabilities. ``invoke_tool`` will then
        # reject every tool with `capability_denied`; this is the safe
        # default. Fail-loud at the principal layer would 500 every
        # request the moment a new scope shape ships, which is worse.
        caps = ()

    identity_id = uuid5(_OAUTH_ID_NAMESPACE, f"oauth:{claims.jti or claims.sub}")
    return Principal(
        identity_id=identity_id,
        tenant_id=str(claims.tenant_id),
        principal_role="agent",  # MCP/OAuth bearers are non-human callers
        capabilities=caps,
        auth_kind="oauth",
        oauth_token_id=None,  # no DB row for MCP-bearer JWTs (D-S4-01 follow-up)
        actor_type="ai_worker",
    )


async def _resolve_principal_from_context(ctx: Context | None) -> Principal:
    """Resolve a Principal from the FastMCP request context.

    Reads ``MCPClaims`` from the ASGI request state — the bearer
    middleware (``MCPBearerAuthMiddleware``) stashes them there after
    verifying the token against the inbound request's tenant binding.
    The bridge wrapper does not re-verify; that would either duplicate
    the middleware's work or create a window where a request reaches
    the wrapper without claims. Either is a wiring bug, surfaced as
    ``PrincipalResolutionFailed``.
    """
    from fastmcp.server.dependencies import get_http_request

    try:
        request = get_http_request()
    except Exception as exc:  # noqa: BLE001 — fastmcp raises various types
        raise PrincipalResolutionFailed(
            f"no HTTP request bound to FastMCP context: {exc}"
        ) from exc

    claims = getattr(request.state, "mcp_claims", None)
    if not isinstance(claims, MCPClaims):
        raise PrincipalResolutionFailed(
            "request.state.mcp_claims is missing or wrong type — the "
            "MCPBearerAuthMiddleware must run in front of the FastMCP "
            "sub-app; check `mount_mcp` wiring"
        )

    return _claims_to_principal(claims)


def _make_wrapper(descriptor: ToolDescriptor):
    """Build a FastMCP tool function that delegates to ``invoke_tool``.

    The wrapper accepts arbitrary keyword arguments (FastMCP unpacks the
    validated input dict as kwargs). It does NOT take an explicit
    ``Context`` — FunctionTool with an explicit ``parameters`` JSON
    Schema only forwards validated args, so an extra positional arg
    would 500 every call. The principal resolver pulls the underlying
    HTTP request via ``fastmcp.server.dependencies.get_http_request()``.
    """
    tool_name = descriptor.name

    async def wrapper(**kwargs: Any) -> dict[str, Any]:
        principal = await _resolve_principal_from_context(None)

        # Open a tenant DB session bound to the resolved tenant.
        # Tool handlers expect `db` as a SQLAlchemy session against
        # the tenant's database; the bearer middleware already
        # populated request.state.tenant via TenantMiddleware, so we
        # can reuse the same session-factory logic the legacy
        # /api/mcp/tools/{name}/invoke router uses.
        from fastmcp.server.dependencies import get_http_request

        from gdx_dispatch.core.database import SessionLocal

        request = get_http_request()
        db = SessionLocal()

        try:
            result = await invoke_tool(
                tool_name, kwargs, principal=principal, db=db,
            )
        finally:
            if db is not None:
                db.close()

        if result.ok:
            return result.result if isinstance(result.result, dict) else {"result": result.result}
        return {
            "ok": False,
            "error_type": result.error_type,
            "error": result.error_body,
        }

    wrapper.__name__ = f"bridge__{tool_name.replace('.', '__')}"
    wrapper.__doc__ = descriptor.description
    return wrapper


def bridge_registry_to_fastmcp(mcp: FastMCP) -> list[str]:
    """Register every tool from ``mcp_registry`` onto ``mcp`` (FastMCP).

    Caller invokes once at app startup. Fails loudly on:

    * Empty registry (would mean the side-effect import in
      ``gdx_dispatch/core/mcp_tools/__init__.py`` failed silently).
    * Missing ``input_schema`` on a descriptor (a malformed schema would
      let claude.ai's connector show a tool that 400s on every call).
    * A name that already exists on the FastMCP instance (re-running
      the bridge would silently double-register and FastMCP only logs a
      warning — caller bug, surface it).
    """
    descriptors = list_tools()
    if not descriptors:
        raise BridgeRegistrationError(
            "mcp_registry is empty — did `import gdx_dispatch.core.mcp_tools` run? "
            "Side-effect imports register the toolset; an empty registry "
            "means the import failed or was skipped."
        )

    existing_names = _fastmcp_tool_names(mcp)

    registered: list[str] = []
    for descriptor in descriptors:
        external_name_check = descriptor.name.replace(".", "_")
        if external_name_check in existing_names:
            raise BridgeRegistrationError(
                f"tool {descriptor.name!r} already registered on FastMCP — "
                "bridge_registry_to_fastmcp() must run exactly once per FastMCP "
                "instance"
            )
        if not descriptor.input_schema:
            raise BridgeRegistrationError(
                f"tool {descriptor.name!r} has empty/missing input_schema; "
                "every MCP tool must declare a JSON Schema (use "
                "{'type': 'object', 'properties': {}} for no-arg tools)"
            )

        wrapper = _make_wrapper(descriptor)
        # claude.ai (and the MCP frontend tool-name validator) requires
        # tool names match ^[a-zA-Z0-9_-]{1,64}$ — dots are rejected.
        # Translate the internal registry name (e.g. "documents.list")
        # to a transport-safe form ("documents_list") for the external
        # FastMCP catalog. The wrapper's invoke_tool() call still uses
        # the original dotted name so the registry lookup succeeds.
        external_name = descriptor.name.replace(".", "_")
        ft = FunctionTool(
            name=external_name,
            description=descriptor.description,
            parameters=descriptor.input_schema,
            fn=wrapper,
        )
        mcp.add_tool(ft)
        registered.append(external_name)
        logger.debug(
            "mcp_bridge.registered internal=%s external=%s",
            descriptor.name, external_name,
        )

    logger.info("mcp_bridge.bridge_registry_to_fastmcp registered=%d", len(registered))
    return registered


def _fastmcp_tool_names(mcp: "FastMCP") -> set[str]:
    """Return the set of tool names currently registered on ``mcp``.

    FastMCP's public ``list_tools()`` is async; calling it from sync
    startup code is awkward (works at module-import time, raises under
    uvicorn factory mode where the event loop is already running).
    The internal component map is authoritative and sync-readable, so
    we read it directly. Keys are shaped ``"<kind>:<name>@<version>"``;
    we filter to tools.
    """
    components = getattr(getattr(mcp, "_local_provider", None), "_components", {})
    names: set[str] = set()
    for key, comp in components.items():
        if isinstance(key, str) and key.startswith("tool:"):
            # key form: "tool:<name>@<version>" — use the component's
            # own name field rather than parsing the key, in case
            # FastMCP changes the key shape.
            name = getattr(comp, "name", None)
            if name:
                names.add(name)
    return names

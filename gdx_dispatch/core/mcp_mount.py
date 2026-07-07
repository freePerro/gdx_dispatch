"""Sprint MCP-Streamable-HTTP S2 — mount FastMCP at ``/mcp``.

The FastMCP singleton at ``mcp_protocol_adapter.mcp`` exposes a
Streamable-HTTP transport via ``mcp.http_app(path='/')``. Mounting it
at ``/mcp`` on the parent FastAPI app makes
``https://<tenant-host>/mcp`` a valid MCP endpoint.

Two responsibilities live here:

1. **Bridge then mount.** ``mount_mcp(app)`` first bridges the
   ``mcp_registry`` toolset onto the FastMCP singleton (idempotent at
   the mount layer; the bridge itself is fail-loud), then builds the
   ASGI sub-app and mounts it on the parent.

2. **Lifespan integration.** FastMCP's Streamable-HTTP needs a task
   group started by its own lifespan context. The parent FastAPI
   ``lifespan`` is responsible for entering the sub-app's lifespan;
   this module exposes ``mcp_subapp_lifespan(app)`` for that purpose.
   Every request to ``/mcp`` would 500 with "Task group is not
   initialized" if the lifespan hook is missing — no silent fallback.
"""
from __future__ import annotations

import contextlib
import inspect
import logging
from typing import TYPE_CHECKING

from gdx_dispatch.core.mcp_fastmcp_bridge import (
    _fastmcp_tool_names,
    _make_wrapper,
    bridge_registry_to_fastmcp,
)
from gdx_dispatch.core.mcp_protocol_adapter import get_mcp
from gdx_dispatch.core.mcp_registry import list_tools, repopulate_from_cache

if TYPE_CHECKING:
    from fastapi import FastAPI, Request
    from fastapi.responses import RedirectResponse
    from fastmcp import FastMCP
    from fastmcp.server.http import StarletteWithLifespan

logger = logging.getLogger(__name__)


class MCPMountError(RuntimeError):
    """Raised when the FastMCP sub-app cannot be mounted safely.

    Always loud — silent failure here means ``/mcp`` either 404s
    (catch-all SPA) or 500s (uninitialized session manager) and the
    operator finds out from claude.ai's connector telemetry.
    """


def build_mcp_subapp(mcp: "FastMCP | None" = None) -> "StarletteWithLifespan":
    """Bridge the registry onto FastMCP and build the Streamable-HTTP sub-app.

    Idempotent across multiple ``create_app()`` calls (test harness
    pattern): if FastMCP already has the full toolset registered, skip
    bridging. If FastMCP is partially populated, raise — silent
    reconciliation would mask a real bug.
    """
    if mcp is None:
        mcp = get_mcp()

    # The registry stores dotted names (e.g. "documents.list"); the
    # bridge translates them to transport-safe form ("documents_list")
    # at FastMCP registration time, since claude.ai's tool-name
    # validator rejects dots. Compare in the external (underscore)
    # space for orphan/missing checks.
    legacy_names = {d.name.replace(".", "_") for d in list_tools()}
    if not legacy_names:
        # Registry may have been cleared by a sibling test (clear_registry()).
        # The submodule objects are still in sys.modules — repopulate from them.
        repopulate_from_cache()
        legacy_names = {d.name.replace(".", "_") for d in list_tools()}
    if not legacy_names:
        raise MCPMountError(
            "mcp_registry is empty at mount time — did "
            "`import gdx_dispatch.core.mcp_tools` run? Side-effect imports register "
            "the toolset; an empty registry means the import was skipped."
        )

    # Sync-read FastMCP's component map (the bridge helper avoids the
    # async list_tools() call, which raises under uvicorn factory mode
    # where the event loop is already running).
    fastmcp_names = _fastmcp_tool_names(mcp)
    orphans = fastmcp_names - legacy_names
    missing = legacy_names - fastmcp_names

    if orphans:
        # Orphans can appear when clear_registry() ran between mounts: the
        # FastMCP singleton retains its bridges while the registry was cleared.
        # Repopulate from cached submodule objects and recheck.
        repopulate_from_cache()
        legacy_names = {d.name.replace(".", "_") for d in list_tools()}
        orphans = fastmcp_names - legacy_names
        missing = legacy_names - fastmcp_names
    if orphans:
        raise MCPMountError(
            f"FastMCP holds {len(orphans)} tool(s) not in mcp_registry: "
            f"{sorted(orphans)[:5]}{'...' if len(orphans) > 5 else ''}"
        )

    if not fastmcp_names:
        bridge_registry_to_fastmcp(mcp)
    elif missing:
        # Registry grew (e.g. test fixture registered a probe descriptor
        # after the cold-start bridge ran). Add the missing tools without
        # re-bridging existing ones.
        from fastmcp.tools import FunctionTool

        for descriptor in list_tools():
            external_name = descriptor.name.replace(".", "_")
            if external_name in fastmcp_names:
                continue
            if not descriptor.input_schema:
                raise MCPMountError(
                    f"tool {descriptor.name!r} has empty/missing input_schema; "
                    "every MCP tool must declare a JSON Schema"
                )
            wrapper = _make_wrapper(descriptor)
            mcp.add_tool(
                FunctionTool(
                    name=external_name,
                    description=descriptor.description,
                    parameters=descriptor.input_schema,
                    fn=wrapper,
                )
            )
        logger.info(
            "mcp_mount.bridge_reconciled added=%d existing=%d",
            len(missing),
            len(fastmcp_names),
        )
    else:
        logger.info(
            "mcp_mount.bridge_skipped reason=already_bridged count=%d",
            len(fastmcp_names),
        )

    # S4: ASGI bearer-auth middleware in front of the FastMCP transport.
    # Rejects requests whose Bearer token does not match the inbound
    # request's tenant binding (host → expected aud + gdx_tid).
    from starlette.middleware import Middleware

    from gdx_dispatch.core.mcp_bearer_middleware import MCPBearerAuthMiddleware

    # fastmcp 3.4.3 turned on DNS-rebinding protection by default:
    # http_app() 421s any request whose Host header is outside
    # localhost + the bound interface. That allowlist can never contain
    # our tenant hosts (per-tenant domains, resolved at request time),
    # so with it on every real /mcp request — claude.ai included — dies
    # with 421 before auth runs. MCPBearerAuthMiddleware already binds
    # host → tenant (aud + gdx_tid) and rejects unknown or mismatched
    # hosts, which is strictly stronger than the generic rebinding
    # guard, so disabling it does not widen exposure. Signature-gated
    # because fastmcp <3.4.3 (e.g. already-built docker images) has no
    # such kwarg and would TypeError at startup.
    http_app_kwargs = {}
    if "host_origin_protection" in inspect.signature(mcp.http_app).parameters:
        http_app_kwargs["host_origin_protection"] = False

    return mcp.http_app(
        path="/",
        middleware=[Middleware(MCPBearerAuthMiddleware)],
        **http_app_kwargs,
    )


def mount_mcp(app: "FastAPI", mcp: "FastMCP | None" = None) -> "StarletteWithLifespan":
    """Mount the MCP Streamable-HTTP sub-app at ``/mcp`` on ``app``.

    Stores the sub-app on ``app.state.mcp_subapp`` so the parent's
    lifespan can enter the sub-app's lifespan context. Caller MUST
    invoke ``mount_mcp`` BEFORE any catch-all route on the parent
    (otherwise the catch-all shadows ``/mcp``).
    """
    if getattr(app.state, "mcp_subapp", None) is not None:
        raise MCPMountError(
            "mount_mcp called twice on the same FastAPI app; "
            "mount is idempotent at the bridge level but not the "
            "route level (Starlette would register two Mount routes)."
        )

    # claude.ai hits "<host>/mcp" exactly (no trailing slash, per the
    # resource URL in /.well-known/oauth-protected-resource). Starlette
    # Mount("/mcp", subapp) ALSO matches bare "/mcp" and forwards to
    # the sub-app — but the FastMCP sub-app expects Streamable-HTTP
    # framing and rejects raw GETs. We need a 308 redirect from /mcp
    # to /mcp/ that wins route resolution before the mount.
    #
    # Starlette Route (not FastAPI add_api_route) sidesteps a subtle
    # bug: the file uses `from __future__ import annotations`, which
    # turns every type annotation into a forward-ref string. FastAPI
    # then tries to resolve `request: Request` as a query parameter
    # (since the string "Request" doesn't match its known dep types),
    # producing 422 "Field required" on every call. Starlette's Route
    # bypasses dep-injection entirely; the handler receives the
    # request directly.
    from starlette.responses import RedirectResponse
    from starlette.routing import Route

    async def _mcp_no_slash_redirect(request):
        target = "/mcp/"
        if request.url.query:
            target = f"/mcp/?{request.url.query}"
        return RedirectResponse(url=target, status_code=308)

    # insert(0) puts the redirect ahead of the mount so it matches
    # bare /mcp first.
    app.router.routes.insert(
        0,
        Route("/mcp", _mcp_no_slash_redirect, methods=["GET", "POST"]),
    )

    subapp = build_mcp_subapp(mcp)
    app.state.mcp_subapp = subapp
    app.mount("/mcp", subapp)

    logger.info("mcp_mount.mounted path=/mcp tools=%d", len(list_tools()))
    return subapp


@contextlib.asynccontextmanager
async def mcp_subapp_lifespan(app: "FastAPI"):
    """Async context manager the parent ``lifespan`` enters on startup.

    Hard-fails if ``mount_mcp`` was not called (the sub-app would 500
    on every request without its task group). The parent ``lifespan``
    is responsible for stacking this with the rest of its startup.
    """
    subapp = getattr(app.state, "mcp_subapp", None)
    if subapp is None:
        raise MCPMountError(
            "mcp_subapp_lifespan invoked without mount_mcp; the FastMCP "
            "Streamable-HTTP sub-app's task group never started and "
            "every /mcp request would 500."
        )
    async with subapp.lifespan(subapp):
        yield

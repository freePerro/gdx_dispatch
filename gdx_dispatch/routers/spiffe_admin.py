"""SS-32 slice F — super-admin endpoints for SPIFFE readiness.

Endpoints (all require ``super-admin``):

* ``GET  /api/admin/spiffe/trust-bundle``
      Return a redacted snapshot of the cached trust bundle.
* ``POST /api/admin/spiffe/trust-bundle/refresh``
      Force a refresh and emit ``gdx.spiffe.trust_bundle_refreshed.v1``.
* ``GET  /api/admin/spiffe/workloads``
      List every registered workload + its resolved capabilities.
* ``POST /api/admin/spiffe/workloads``
      Register a new workload (SPIFFE-ID glob + capability grant);
      emits ``gdx.spiffe.workload_registered.v1``.

The router reads the SPIFFE trust-bundle cache and workload capability
map from :data:`request.state` — callers wire both at app-start. This
keeps the router import-safe for tests that inject fake state.

INTEGRATION_TODO: router is not mounted in ``gdx_dispatch/main.py``. Mount it
behind the existing super-admin dependency at main-chain merge.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from gdx_dispatch.core.auth_dispatcher import get_current_principal
from gdx_dispatch.core.unified_principal import Principal
from gdx_dispatch.core.spiffe.spiffe_id import SpiffeIdError, parse_spiffe_id
from gdx_dispatch.core.spiffe.spire_trust_bundle import (
    TrustBundleCache,
    TrustBundleError,
)
from gdx_dispatch.core.spiffe.workload_capability_map import (
    CapabilityGrant,
    WorkloadCapabilityMap,
)

router = APIRouter(prefix="/api/admin/spiffe", tags=["spiffe-admin"])

CAPABILITY = "platform:spiffe:admin"


def _require_super_admin(principal: Principal) -> str:
    """Return an actor identity string if the caller is super-admin; else 403.

    Accepts either hyphenated (``"super-admin"``) or snake_case
    (``"super_admin"``) role spellings, plus the caps-derived
    ``is_super_admin`` flag (principal holds ``("*", "*")``).

    INTEGRATION_TODO: replace with ``require_role("super-admin")``
    / ``require_capability(CAPABILITY)`` at main-chain merge.
    """
    if (
        principal.principal_role in ("super-admin", "super_admin")
        or principal.is_super_admin
    ):
        # SPIFFE admin audit wants a string actor handle. Prefer the UUID
        # identity_id; fall back to spiffe_id for SPIFFE-auth callers.
        if principal.identity_id is not None:
            return str(principal.identity_id)
        if principal.spiffe_id:
            return principal.spiffe_id
        return "unknown"
    raise HTTPException(status_code=403, detail="super-admin required")


def _get_bundle(request: Request) -> TrustBundleCache:
    bundle = getattr(request.state, "spiffe_bundle", None)
    if bundle is None:
        raise HTTPException(
            status_code=503, detail="spiffe trust bundle not wired"
        )
    return bundle


def _get_cap_map(request: Request) -> WorkloadCapabilityMap:
    m = getattr(request.state, "spiffe_capability_map", None)
    if m is None:
        raise HTTPException(
            status_code=503, detail="spiffe capability map not wired"
        )
    return m


def _emit_event(request: Request, name: str, payload: Dict[str, Any]) -> None:
    """Append an event onto the test-visible sink if present; otherwise
    fall back to the SS-23 event bus when available.

    Kept local so this router is import-safe without the event bus.
    """
    sink = getattr(request.state, "spiffe_event_sink", None)
    if sink is not None:
        sink.append({"event": name, "payload": payload})
        return
    try:  # pragma: no cover - exercised by integration tests
        from gdx_dispatch.core.event_bus import emit_event  # type: ignore

        emit_event(name, payload)
    except Exception:
        # Fail-open: emitting is additive observability, not correctness.
        pass


# ---------------------------------------------------------------------------
# Trust bundle
# ---------------------------------------------------------------------------


@router.get("/trust-bundle")
def get_trust_bundle(
    request: Request,
    principal: Principal = Depends(get_current_principal),
) -> Dict[str, Any]:
    _require_super_admin(principal)
    bundle = _get_bundle(request)
    return bundle.snapshot()


@router.post("/trust-bundle/refresh")
def force_refresh(
    request: Request,
    principal: Principal = Depends(get_current_principal),
) -> Dict[str, Any]:
    actor = _require_super_admin(principal)
    bundle = _get_bundle(request)
    try:
        bundle.force_refresh()
    except TrustBundleError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"trust bundle refresh failed: {exc}",
        )
    _emit_event(
        request,
        "gdx_dispatch.spiffe.trust_bundle_refreshed.v1",
        {
            "actor_identity_id": actor,
            "refreshed_at": datetime.now(timezone.utc).isoformat(),
            "trust_domains": bundle.snapshot().get("trust_domains", []),
        },
    )
    return {"ok": True, "snapshot": bundle.snapshot()}


# ---------------------------------------------------------------------------
# Workloads
# ---------------------------------------------------------------------------


def _grant_to_dict(g: CapabilityGrant) -> Dict[str, Any]:
    return {
        "spiffe_id_glob": g.spiffe_id_glob,
        "capabilities": list(g.capabilities),
        "tenant_scope": g.tenant_scope,
        "metadata": dict(g.metadata),
    }


@router.get("/workloads")
def list_workloads(
    request: Request,
    principal: Principal = Depends(get_current_principal),
) -> Dict[str, Any]:
    _require_super_admin(principal)
    m = _get_cap_map(request)
    grants = [_grant_to_dict(g) for g in m.list_grants()]
    return {"count": len(grants), "workloads": grants}


@router.post("/workloads", status_code=201)
def register_workload(
    request: Request,
    body: Dict[str, Any] = Body(...),
    principal: Principal = Depends(get_current_principal),
) -> Dict[str, Any]:
    actor = _require_super_admin(principal)
    glob = body.get("spiffe_id_glob")
    if not isinstance(glob, str) or not glob.startswith("spiffe://"):
        raise HTTPException(
            status_code=400,
            detail="spiffe_id_glob must be a string starting with 'spiffe://'",
        )
    # Glob-aware validation: replace * with placeholders, then parse a
    # concretised form to catch trivially malformed trust domains.
    concrete = glob.replace("**", "x").replace("*", "x")
    try:
        parse_spiffe_id(concrete)
    except SpiffeIdError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"spiffe_id_glob is not a valid SPIFFE ID pattern: {exc}",
        )
    caps = body.get("capabilities") or []
    if not isinstance(caps, list) or not all(isinstance(c, str) for c in caps):
        raise HTTPException(
            status_code=400,
            detail="capabilities must be a list of strings",
        )
    scope = body.get("tenant_scope", "per-tenant")
    if scope not in ("global", "per-tenant"):
        raise HTTPException(
            status_code=400,
            detail="tenant_scope must be 'global' or 'per-tenant'",
        )
    md = body.get("metadata") or {}
    if not isinstance(md, dict):
        raise HTTPException(
            status_code=400, detail="metadata must be an object"
        )

    grant = CapabilityGrant(
        spiffe_id_glob=glob,
        capabilities=tuple(caps),
        tenant_scope=scope,
        metadata=md,
    )
    m = _get_cap_map(request)
    m.overlay([grant])

    _emit_event(
        request,
        "gdx_dispatch.spiffe.workload_registered.v1",
        {
            "spiffe_id_glob": glob,
            "capabilities": list(caps),
            "tenant_scope": scope,
            "registered_by": actor,
            "registered_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    return {"ok": True, "grant": _grant_to_dict(grant)}

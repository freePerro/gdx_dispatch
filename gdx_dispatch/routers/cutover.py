"""SS-30 slice D — super-admin endpoints for cutover operations.

Endpoints:

* ``POST /api/admin/cutover/{old_table}/preflight``           — dry-run safety checks
* ``POST /api/admin/cutover/{old_table}/execute``             — run the atomic cutover
* ``POST /api/admin/cutover/{old_table}/extend-deprecation``  — push scheduled_drop_at
* ``GET  /api/admin/cutover/{old_table}/status``              — current state + schedule

Security:

* All routes require the ``super-admin`` cross-tenant role. Capability
  string is ``"platform:cutover:admin"``.
* Tenant scope is supplied via the request body ``tenant_id`` field
  (same convention as the SS-29 shadow-migrations router).

Sprint 0.9-e.2b: principal resolution flows through the composite
``get_current_principal`` dispatcher (5-flow unified auth). The router
no longer reads ``request.state.principal_role`` directly.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from gdx_dispatch.core.auth_dispatcher import get_current_principal
from gdx_dispatch.core.cutover_orchestrator import (
    CutoverError,
    DEFAULT_GRACE_PERIOD_DAYS,
    MAX_GRACE_PERIOD_DAYS,
    extend_deprecation,
    run_cutover,
)
from gdx_dispatch.core.cutover_preflight import run_preflight
from gdx_dispatch.core.unified_principal import Principal

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/cutover", tags=["cutover"])

CAPABILITY = "platform:cutover:admin"

# Accept both hyphenated and snake_case super-admin role strings — different
# auth flows normalize to different casings.
_SUPER_ADMIN_ROLES = ("super-admin", "super_admin")


def _require_super_admin(principal: Principal) -> str:
    """Return the caller's identity id if super-admin; else 403."""
    if (
        principal.principal_role in _SUPER_ADMIN_ROLES
        or principal.is_super_admin
    ):
        if principal.identity_id is not None:
            return str(principal.identity_id)
        return "super-admin"
    raise HTTPException(
        status_code=403,
        detail=f"super-admin role required (capability: {CAPABILITY})",
    )


def _get_db(request: Request) -> Any:
    db = getattr(request.state, "db", None)
    if db is None:
        raise HTTPException(status_code=500, detail="no db session on request")
    return db


def _require_tenant_id(body: dict[str, Any]) -> str:
    tenant_id = body.get("tenant_id")
    if not tenant_id:
        raise HTTPException(status_code=400, detail="tenant_id required")
    return str(tenant_id)


@router.post("/{old_table}/preflight")
def preflight_route(
    old_table: str,
    request: Request,
    body: dict[str, Any] = Body(...),
    principal: Principal = Depends(get_current_principal),
) -> dict[str, Any]:
    """Run preflight checks and return the :class:`PreflightReport` dict.

    Always returns 200 — the caller inspects ``passed`` to decide
    whether to proceed. Only 400/403/500 produce error status codes.
    """
    _require_super_admin(principal)
    db = _get_db(request)
    tenant_id = _require_tenant_id(body)

    report = run_preflight(
        db, tenant_id=tenant_id, old_table=old_table,
    )
    return report.to_dict()


@router.post("/{old_table}/execute")
def execute_route(
    old_table: str,
    request: Request,
    body: dict[str, Any] = Body(...),
    principal: Principal = Depends(get_current_principal),
) -> dict[str, Any]:
    """Execute the cutover after running preflight.

    Body keys:
        tenant_id (required), dry_run (bool, default False),
        grace_period_days (int, default 30), notes (str), skip_preflight
        (bool, default False — ONLY set True with explicit override flag;
        used for re-runs where the preflight already ran in the UI).
    """
    actor = _require_super_admin(principal)
    db = _get_db(request)
    tenant_id = _require_tenant_id(body)
    dry_run = bool(body.get("dry_run", False))
    grace_period_days = int(body.get("grace_period_days", DEFAULT_GRACE_PERIOD_DAYS))
    skip_preflight = bool(body.get("skip_preflight", False))

    if not skip_preflight:
        report = run_preflight(db, tenant_id=tenant_id, old_table=old_table)
        if not report.passed:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "preflight_failed",
                    "report": report.to_dict(),
                },
            )

    try:
        result = run_cutover(
            db,
            tenant_id=tenant_id,
            old_table=old_table,
            actor_identity_id=actor,
            grace_period_days=grace_period_days,
            dry_run=dry_run,
            notes=body.get("notes"),
        )
    except CutoverError as exc:
        logger.error(
            "cutover execute: CutoverError tenant=%s table=%s err=%s",
            tenant_id, old_table, exc,
        )
        raise HTTPException(status_code=409, detail=str(exc))

    return {"status": "ok", "result": result.to_dict()}


@router.post("/{old_table}/extend-deprecation")
def extend_deprecation_route(
    old_table: str,
    request: Request,
    body: dict[str, Any] = Body(...),
    principal: Principal = Depends(get_current_principal),
) -> dict[str, Any]:
    """Push scheduled_drop_at by ``additional_days``.

    409 if the extension would exceed ``MAX_GRACE_PERIOD_DAYS`` total
    span from executed_at.
    """
    actor = _require_super_admin(principal)
    db = _get_db(request)
    tenant_id = _require_tenant_id(body)

    additional_days = body.get("additional_days")
    if additional_days is None:
        raise HTTPException(status_code=400, detail="additional_days required")
    try:
        additional_days = int(additional_days)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="additional_days must be int")

    try:
        row = extend_deprecation(
            db, tenant_id=tenant_id, old_table=old_table,
            additional_days=additional_days,
            actor_identity_id=actor,
        )
    except CutoverError as exc:
        msg = str(exc)
        if "MAX_GRACE_PERIOD_DAYS" in msg:
            raise HTTPException(status_code=409, detail=msg)
        if "no cutover_schedule" in msg:
            raise HTTPException(status_code=404, detail=msg)
        raise HTTPException(status_code=400, detail=msg)

    return {
        "status": "ok",
        "scheduled_drop_at": row.scheduled_drop_at.isoformat(),
        "extended_count": row.extended_count,
        "max_grace_period_days": MAX_GRACE_PERIOD_DAYS,
    }


@router.get("/{old_table}/status")
def status_route(
    old_table: str,
    request: Request,
    tenant_id: str | None = None,
    principal: Principal = Depends(get_current_principal),
) -> dict[str, Any]:
    """Return current cutover state + schedule for a (tenant, table) pair.

    ``tenant_id`` comes from the query string because GET has no body.
    """
    _require_super_admin(principal)
    db = _get_db(request)
    if not tenant_id:
        raise HTTPException(status_code=400, detail="tenant_id query param required")

    from gdx_dispatch.models.platform_ss29_additions import ShadowMigrationState
    from gdx_dispatch.models.platform_ss30_additions import CutoverSchedule

    state_row = (
        db.query(ShadowMigrationState)
        .filter(
            ShadowMigrationState.tenant_id == tenant_id,
            ShadowMigrationState.old_table == old_table,
        )
        .first()
    )
    sched_row = (
        db.query(CutoverSchedule)
        .filter(
            CutoverSchedule.tenant_id == tenant_id,
            CutoverSchedule.old_table == old_table,
        )
        .first()
    )

    return {
        "tenant_id": tenant_id,
        "old_table": old_table,
        "state": {
            "mode": state_row.mode if state_row else None,
            "cutover_at": (
                state_row.cutover_at.isoformat()
                if state_row and state_row.cutover_at else None
            ),
        } if state_row else None,
        "schedule": {
            "deprecated_table": sched_row.deprecated_table,
            "executed_at": sched_row.executed_at.isoformat(),
            "scheduled_drop_at": sched_row.scheduled_drop_at.isoformat(),
            "dropped_at": (
                sched_row.dropped_at.isoformat() if sched_row.dropped_at else None
            ),
            "extended_count": sched_row.extended_count,
            "dry_run": bool(sched_row.dry_run),
        } if sched_row else None,
    }

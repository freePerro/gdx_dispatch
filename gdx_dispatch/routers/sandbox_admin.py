"""SS-10 Slice F — sandbox admin router.

Tenant-scoped admin endpoints over the SS-10 Slice D sandbox lifecycle
helpers in ``gdx_dispatch.core.sandbox``. The router is intentionally not wired
into ``gdx_dispatch/app.py`` in this slice — wiring lands in Slice G.

Each endpoint owns the transaction: it calls one Slice D helper (which
stages state without flushing) and then issues exactly one ``commit``.
Slice H adds an in-transaction audit row on every successful mutation
via ``log_audit_event_sync`` so the audit write commits atomically with
the mutation in that single ``commit``.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module, require_role
from gdx_dispatch.core.sandbox import provision_sandbox, reset_sandbox, teardown_sandbox
from gdx_dispatch.core.tenant_ctx import bind_tenant_context

router = APIRouter(
    prefix="/api/admin/sandbox",
    tags=["sandbox-admin"],
    dependencies=[
        Depends(bind_tenant_context),
        Depends(require_module("jobs")),
        Depends(require_role("admin", "owner", "superadmin")),
    ],
)


class ProvisionRequest(BaseModel):
    subdomain: str = Field(..., min_length=1, max_length=128)


def _tenant_id(request: Request) -> str:
    tenant = getattr(request.state, "tenant", {}) or {}
    tid = str(tenant.get("id") or "").strip()
    if not tid:
        raise HTTPException(status_code=400, detail="Missing tenant context")
    return tid


def _user_id(request: Request) -> str:
    user = getattr(request.state, "current_user", {}) or {}
    return str(
        user.get("sub")
        or user.get("user_id")
        or user.get("id")
        or "system"
    )


def _actor_role(request: Request) -> str | None:
    user = getattr(request.state, "current_user", {}) or {}
    role = user.get("role")
    return str(role) if role else None


@router.post("")
def provision(
    payload: ProvisionRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    tenant_id = _tenant_id(request)
    row = provision_sandbox(db, tenant_id=tenant_id, subdomain=payload.subdomain)
    # Populate the Python-side ``uuid4`` default so the audit row carries a
    # real ``entity_id``; flush stages the row in the open transaction
    # without committing — the trailing ``db.commit`` still fires once.
    db.flush()
    log_audit_event_sync(
        db,
        tenant_id=tenant_id,
        user_id=_user_id(request),
        actor_role=_actor_role(request),
        action="sandbox_provisioned",
        entity_type="sandbox_env",
        entity_id=str(row.id),
        details={"subdomain": row.subdomain, "status": row.status},
        request=request,
    )
    db.commit()
    return {
        "id": str(row.id),
        "tenant_id": row.tenant_id,
        "subdomain": row.subdomain,
        "status": row.status,
    }


@router.post("/{sandbox_id}/reset")
def reset(
    sandbox_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    tenant_id = _tenant_id(request)
    row = reset_sandbox(db, tenant_id, sandbox_id)
    if row is None:
        # Also covers "exists but owned by different tenant" — same 404
        # response to avoid revealing which sandbox_ids exist across tenants.
        raise HTTPException(status_code=404, detail="Sandbox not found")
    log_audit_event_sync(
        db,
        tenant_id=tenant_id,
        user_id=_user_id(request),
        actor_role=_actor_role(request),
        action="sandbox_reset",
        entity_type="sandbox_env",
        entity_id=str(row.id),
        details={
            "status": row.status,
            "last_reset_at": row.last_reset_at.isoformat() if row.last_reset_at else None,
        },
        request=request,
    )
    db.commit()
    return {
        "id": str(row.id),
        "status": row.status,
        "last_reset_at": row.last_reset_at.isoformat() if row.last_reset_at else None,
    }


@router.delete("/{sandbox_id}")
def teardown(
    sandbox_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    tenant_id = _tenant_id(request)
    row = teardown_sandbox(db, tenant_id, sandbox_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Sandbox not found")
    log_audit_event_sync(
        db,
        tenant_id=tenant_id,
        user_id=_user_id(request),
        actor_role=_actor_role(request),
        action="sandbox_torn_down",
        entity_type="sandbox_env",
        entity_id=str(row.id),
        details={
            "status": row.status,
            "torn_down_at": row.torn_down_at.isoformat() if row.torn_down_at else None,
        },
        request=request,
    )
    db.commit()
    return {"status": "ok", "id": str(row.id)}

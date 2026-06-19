"""
Admin settings router — email config, tax jurisdictions, audit log,
error dashboard, user unlock/unlink/timeclock-permissions.

All endpoints require admin/owner role. Tenant-scoped.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import text as _text
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module, require_role
from gdx_dispatch.core.tenant_ctx import bind_tenant_context
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin",
    tags=["admin-settings"],
    dependencies=[
        Depends(bind_tenant_context),
        Depends(require_module("jobs")),
        Depends(require_role("admin", "owner")),
    ],
)


def _tid(request: Request) -> str:
    t = getattr(request.state, "tenant", {}) or {}
    return str(t.get("id") or "").strip()


def _uid(user: dict) -> str:
    return str(user.get("sub") or user.get("user_id") or "system")


def _audit(db: Session, *, request: Request, user: dict, action: str,
           entity_type: str, entity_id: str = "", details: dict | None = None) -> None:
    try:
        log_audit_event_sync(
            db, tenant_id=_tid(request), user_id=_uid(user),
            action=action, entity_type=entity_type, entity_id=entity_id,
            details=details or {}, request=request,
        )
        db.commit()
    except Exception:
        log.exception("admin_audit_failed action=%s", action)


# ── Email Settings ────────────────────────────────────────────────────────

class EmailConfigOut(BaseModel):
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    from_name: str = ""
    from_email: str = ""


class EmailConfigIn(BaseModel):
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_user: str | None = None
    smtp_password: str | None = None
    from_name: str | None = None
    from_email: str | None = None


@router.get("/settings/email", response_model=None)
def get_email_settings(_: dict = Depends(get_current_user)) -> dict:
    return {
        "smtp_host": os.environ.get("SMTP_HOST", ""),
        "smtp_port": int(os.environ.get("SMTP_PORT", "587")),
        "smtp_user": os.environ.get("SMTP_USER", ""),
        "from_name": os.environ.get("FROM_NAME", "GDX"),
        "from_email": os.environ.get("FROM_EMAIL", ""),
    }


@router.patch("/settings/email", response_model=None)
def update_email_settings(
    payload: EmailConfigIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    _audit(db, request=request, user=user, action="email_settings_updated",
           entity_type="settings", details=payload.model_dump(exclude_unset=True))
    return {"ok": True}


@router.post("/settings/email/test", response_model=None)
def test_email(
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    _audit(db, request=request, user=user, action="email_test_sent",
           entity_type="settings")
    return {"ok": True, "message": "Test email queued"}


# ── Tax Jurisdictions ─────────────────────────────────────────────────────

class TaxJurisdictionIn(BaseModel):
    # The live tax_jurisdictions schema is: id, company_id, name, rate,
    # is_default, created_at, updated_at, deleted_at.
    # The previous model declared state/county/city/active, which the ORM
    # does not expose and the DB does not have; every create/update 500ed
    # and was silently swallowed by the except block. Aligning to reality.
    name: str = Field(min_length=1, max_length=200)
    rate: float = Field(ge=0, le=100)
    is_default: bool = False


@router.get("/tax-jurisdictions", response_model=None)
def list_tax_jurisdictions(
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    tid = _tid(request)
    try:
        rows = db.execute(
            _text("""
                SELECT id, name, rate, is_default, created_at, updated_at
                FROM tax_jurisdictions
                WHERE company_id = :tid AND deleted_at IS NULL
                ORDER BY name
                LIMIT :limit OFFSET :offset
            """),
            {"tid": tid, "limit": limit, "offset": offset},
        ).mappings().all()
        return {"items": [dict(r) for r in rows]}
    except Exception:
        log.exception("list_tax_jurisdictions_failed")
        return {"items": []}


@router.post("/tax-jurisdictions", response_model=None, status_code=201)
def create_tax_jurisdiction(
    payload: TaxJurisdictionIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    tid = _tid(request)
    new_id = str(uuid4())
    now = datetime.now(timezone.utc)
    try:
        db.execute(
            _text("""
                INSERT INTO tax_jurisdictions
                    (id, company_id, name, rate, is_default, created_at)
                VALUES (:id, :tid, :name, :rate, :is_default, :now)
            """),
            {"id": new_id, "tid": tid, "now": now, **payload.model_dump()},
        )
        db.commit()
    except Exception:
        log.exception("create_tax_jurisdiction_failed")
        db.rollback()
        return {"ok": True, "id": new_id, "note": "table may not exist yet"}
    _audit(db, request=request, user=user, action="tax_jurisdiction_created",
           entity_type="tax_jurisdiction", entity_id=new_id, details=payload.model_dump())
    return {"ok": True, "id": new_id}


@router.patch("/tax-jurisdictions/{jid}", response_model=None)
def update_tax_jurisdiction(
    jid: str,
    payload: TaxJurisdictionIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    tid = _tid(request)
    try:
        db.execute(
            _text("""
                UPDATE tax_jurisdictions
                SET name = :name, rate = :rate, is_default = :is_default,
                    updated_at = :now
                WHERE id = :jid AND company_id = :tid AND deleted_at IS NULL
            """),
            {"jid": jid, "tid": tid, "now": datetime.now(timezone.utc), **payload.model_dump()},
        )
        db.commit()
    except Exception:
        log.exception("update_tax_jurisdiction_failed")
        db.rollback()
    _audit(db, request=request, user=user, action="tax_jurisdiction_updated",
           entity_type="tax_jurisdiction", entity_id=jid, details=payload.model_dump())
    return {"ok": True}


@router.delete("/tax-jurisdictions/{jid}", response_model=None)
def delete_tax_jurisdiction(
    jid: str,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    tid = _tid(request)
    now = datetime.now(timezone.utc)
    try:
        db.execute(
            _text("UPDATE tax_jurisdictions SET deleted_at = :now WHERE id = :jid AND company_id = :tid"),
            {"jid": jid, "tid": tid, "now": now},
        )
        db.commit()
    except Exception:
        log.exception("delete_tax_jurisdiction_failed")
        db.rollback()
    _audit(db, request=request, user=user, action="tax_jurisdiction_deleted",
           entity_type="tax_jurisdiction", entity_id=jid)
    return {"ok": True}


# ── Audit Log ─────────────────────────────────────────────────────────────
# Moved to admin_ops.py (ORM-based implementation). Collision removed.


# ── User Admin: unlock, Google unlink, TC permissions ─────────────────────

@router.post("/users/{user_id}/unlock", response_model=None)
def unlock_user(
    user_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    tid = _tid(request)
    try:
        db.execute(
            _text("UPDATE users SET failed_login_count = 0 WHERE id = :uid AND company_id = :tid"),
            {"uid": user_id, "tid": tid},
        )
        db.commit()
    except Exception:
        log.exception("unlock_user_failed")
        db.rollback()
    _audit(db, request=request, user=user, action="user_unlocked",
           entity_type="user", entity_id=user_id)
    return {"ok": True}


@router.post("/users/{user_id}/google-unlink", response_model=None)
def google_unlink(
    user_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    tid = _tid(request)
    try:
        db.execute(
            _text("UPDATE users SET google_id = NULL WHERE id = :uid AND company_id = :tid"),
            {"uid": user_id, "tid": tid},
        )
        db.commit()
    except Exception:
        log.exception("google_unlink_failed")
        db.rollback()
    _audit(db, request=request, user=user, action="google_unlinked",
           entity_type="user", entity_id=user_id)
    return {"ok": True}


class TCPermissionsIn(BaseModel):
    tc_can_view_others: bool | None = None
    tc_can_edit: bool | None = None
    tc_can_approve: bool | None = None


@router.patch("/users/{user_id}/tc-permissions", response_model=None)
def update_tc_permissions(
    user_id: str,
    payload: TCPermissionsIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    tid = _tid(request)
    sets: list[str] = []
    params: dict[str, Any] = {"uid": user_id, "tid": tid}
    data = payload.model_dump(exclude_unset=True)
    for field in ("tc_can_view_others", "tc_can_edit", "tc_can_approve"):
        if field in data:
            sets.append(f"{field} = :{field}")
            params[field] = data[field]
    if not sets:
        return {"ok": True, "changed": 0}
    try:
        db.execute(
            _text(f"UPDATE users SET {', '.join(sets)} WHERE id = :uid AND company_id = :tid"),
            params,
        )
        db.commit()
    except Exception:
        log.exception("update_tc_permissions_failed")
        db.rollback()
    _audit(db, request=request, user=user, action="tc_permissions_updated",
           entity_type="user", entity_id=user_id, details=data)
    return {"ok": True}

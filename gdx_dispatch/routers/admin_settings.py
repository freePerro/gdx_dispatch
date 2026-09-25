"""
Admin settings router — email config, tax jurisdictions, audit log,
error dashboard, user unlock/unlink/timeclock-permissions.

All endpoints require admin/owner role. Tenant-scoped.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import text as _text
from sqlalchemy import update as _update
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import audit_best_effort
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module, require_role
from gdx_dispatch.core.tenant_ctx import bind_tenant_context
from gdx_dispatch.models.tenant_models import User
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


def _user_uuid(user_id: str) -> UUID:
    """Parse a path user id; an unparseable id can't name a row → 404."""
    try:
        return UUID(user_id)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=404, detail="User not found") from exc


def _audit(db: Session, *, request: Request, user: dict, action: str,
           entity_type: str, entity_id: str = "", details: dict | None = None) -> None:
    """Every caller commits on the line above this one, so the change is already
    durable — ``audit_best_effort`` is the right half of the pair (GDXA-44).

    It used to hand-roll the swallow, which left the session deactivated for
    whatever ran next on it. Nothing does, here — these handlers return a plain
    literal — but that was luck, not design."""
    audit_best_effort(
        db, tenant_id=_tid(request), user_id=_uid(user),
        action=action, entity_type=entity_type, entity_id=entity_id,
        details=details or {}, request=request,
    )


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
    # Honesty fix (silent-success sweep 2026-09-19): this config is env-var
    # backed (see the GET above) — there is nowhere to write it. The old body
    # persisted nothing, yet wrote an `email_settings_updated` audit row and
    # returned ok, manufacturing a false trail. Same treatment as the
    # sibling /settings/email/test below.
    raise HTTPException(
        status_code=501,
        detail=(
            "Not implemented — SMTP settings come from environment variables. "
            "Tenant email settings live at PUT /api/settings/email."
        ),
    )


@router.post("/settings/email/test", response_model=None)
def test_email(
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    # Honesty fix (email overhaul 4b): this endpoint claimed "Test email
    # queued" while sending NOTHING. The real, working self-test lives at
    # the tenant email-settings router. 501 until this env-var transport
    # surface is actually wired.
    raise HTTPException(
        status_code=501,
        detail="Not implemented — use POST /api/settings/email/test (tenant email settings), which actually sends.",
    )


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
        # Silent-success sweep 2026-09-19: this used to roll back and still
        # answer {"ok": true, "id": ..., "note": "table may not exist yet"} —
        # a fabricated create. Fail loudly instead.
        log.exception("create_tax_jurisdiction_failed")
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to create tax jurisdiction") from None
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
        result = db.execute(
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
        raise HTTPException(status_code=500, detail="Failed to update tax jurisdiction") from None
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Tax jurisdiction not found")
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
        result = db.execute(
            _text(
                "UPDATE tax_jurisdictions SET deleted_at = :now "
                "WHERE id = :jid AND company_id = :tid AND deleted_at IS NULL"
            ),
            {"jid": jid, "tid": tid, "now": now},
        )
        db.commit()
    except Exception:
        log.exception("delete_tax_jurisdiction_failed")
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to delete tax jurisdiction") from None
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Tax jurisdiction not found")
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
    uid = _user_uuid(user_id)
    try:
        # ORM update: users.id is a Uuid column, which SQLite stores dashless
        # — a raw `id = :dashed` can never match there (CLAUDE.md sharp edge).
        result = db.execute(
            _update(User)
            .where(User.id == uid)  # tenant isolation is the connection (B1); no company_id filter
            .values(failed_login_count=0)
        )
        db.commit()
    except Exception:
        log.exception("unlock_user_failed")
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to unlock user") from None
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="User not found")
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
    uid = _user_uuid(user_id)
    try:
        result = db.execute(
            _update(User)
            .where(User.id == uid)  # tenant isolation is the connection (B1); no company_id filter
            .values(google_id=None)
        )
        db.commit()
    except Exception:
        log.exception("google_unlink_failed")
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to unlink Google account") from None
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="User not found")
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
    data = payload.model_dump(exclude_unset=True)
    values = {
        field: data[field]
        for field in ("tc_can_view_others", "tc_can_edit", "tc_can_approve")
        if field in data
    }
    if not values:
        return {"ok": True, "changed": 0}
    uid = _user_uuid(user_id)
    try:
        result = db.execute(
            _update(User)
            .where(User.id == uid)  # tenant isolation is the connection (B1); no company_id filter
            .values(**values)
        )
        db.commit()
    except Exception:
        log.exception("update_tc_permissions_failed")
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to update timeclock permissions") from None
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="User not found")
    _audit(db, request=request, user=user, action="tc_permissions_updated",
           entity_type="user", entity_id=user_id, details=values)
    return {"ok": True}

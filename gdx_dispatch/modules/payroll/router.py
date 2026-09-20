"""Payroll API — entries CRUD, source config, technician rate read/write.

Admin-gated. External-first today: 'manual' entries land here from the
Payroll admin form; 'csv_import' is planned. Integration adapters
(Gusto / QBO Payroll) are unbuilt — there is no adapters module in this
package yet, only ``service`` and this router.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy import update as sa_update
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import ensure_audit_table, log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import PayrollEntry
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/payroll", tags=["payroll"])


def _require_admin(user: dict[str, Any]) -> None:
    role = (user.get("role") or "").lower()
    if role not in {"admin", "owner", "manager"}:
        raise HTTPException(status_code=403, detail="admin / owner / manager required")


class EntryIn(BaseModel):
    tech_user_id: str = Field(..., min_length=1)
    period_start: datetime
    period_end: datetime
    hours_paid: Decimal = Field(..., ge=0)
    gross_pay: Decimal = Field(..., ge=0)
    source: str = "manual"
    external_ref: str | None = None
    notes: str | None = None


@router.get("/entries", response_model=None)
def list_entries(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
    tech_user_id: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    _require_admin(user)
    _ = request
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    where = ["deleted_at IS NULL"]
    params: dict[str, Any] = {}
    if tech_user_id:
        where.append("tech_user_id = :tid")
        params["tid"] = tech_user_id
    where_sql = " AND ".join(where)
    rows = db.execute(
        text(
            "SELECT id, tech_user_id, period_start, period_end, hours_paid, "  # noqa: S608 — WHERE is joined from literal fragments; every filter value is a bound :param
            "       gross_pay, source, external_ref, notes, created_at "
            f"FROM payroll_entries WHERE {where_sql} "
            "ORDER BY period_end DESC LIMIT :lim OFFSET :off"
        ),
        {**params, "lim": page_size, "off": (page - 1) * page_size},
    ).mappings().all()
    total = db.execute(
        text(f"SELECT COUNT(*) FROM payroll_entries WHERE {where_sql}"),  # noqa: S608 — WHERE is joined from literal fragments; every filter value is a bound :param
        params,
    ).scalar() or 0
    return {
        "items": [dict(r) for r in rows],
        "total": int(total),
        "page": page,
        "page_size": page_size,
    }


@router.post("/entries", response_model=None, status_code=201)
def create_entry(
    payload: EntryIn,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _require_admin(user)
    if payload.period_end < payload.period_start:
        raise HTTPException(status_code=422, detail="period_end must be >= period_start")
    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    new_id = uuid.uuid4()
    # ORM insert on purpose: the previous raw INSERT omitted `created_at`,
    # whose NOT NULL default is Python-side only, so on any ORM-built
    # Postgres schema the endpoint had never once succeeded (ledger
    # 2026-09-12). The ORM applies the default.
    ensure_audit_table(db)  # before staging: its first run on an engine commits
    entry = PayrollEntry(
        id=new_id,
        company_id=tenant_id,
        tech_user_id=payload.tech_user_id,
        period_start=payload.period_start,
        period_end=payload.period_end,
        hours_paid=payload.hours_paid,
        gross_pay=payload.gross_pay,
        source=payload.source[:40],
        external_ref=(payload.external_ref or None) and payload.external_ref[:100],
        notes=payload.notes,
    )
    db.add(entry)
    # Money-adjacent mutation: record the acting user (invariant #1).
    log_audit_event_sync(
        db,
        tenant_id=tenant_id,
        user_id=str(user.get("sub") or user.get("user_id") or ""),
        action="payroll_entry_created",
        entity_type="payroll_entry",
        entity_id=str(new_id),
        details={
            "tech_user_id": payload.tech_user_id,
            "period_start": payload.period_start.isoformat(),
            "period_end": payload.period_end.isoformat(),
            "hours_paid": str(payload.hours_paid),
            "gross_pay": str(payload.gross_pay),
            "source": payload.source[:40],
        },
        request=request,
    )
    db.commit()
    return {"id": str(new_id)}


@router.delete("/entries/{entry_id}", response_model=None)
def delete_entry(
    entry_id: str,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _require_admin(user)
    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    try:
        entry_uuid = UUID(entry_id)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=404, detail="Payroll entry not found") from exc
    ensure_audit_table(db)  # before staging: its first run on an engine commits
    # ORM update, not raw SQL: PayrollEntry.id is a Uuid column, which SQLite
    # stores dashless — a raw `id = :dashed` never matches there. rowcount
    # gate: without it a missing or already-deleted id answered {"ok": true}
    # and the UI toasted "Entry deleted" over nothing (silent-success class;
    # an audit row here would have been false).
    result = db.execute(
        sa_update(PayrollEntry)
        .where(PayrollEntry.id == entry_uuid, PayrollEntry.deleted_at.is_(None))
        .values(deleted_at=datetime.now(timezone.utc))
    )
    if result.rowcount == 0:
        db.rollback()
        raise HTTPException(status_code=404, detail="Payroll entry not found")
    log_audit_event_sync(
        db,
        tenant_id=tenant_id,
        user_id=str(user.get("sub") or user.get("user_id") or ""),
        action="payroll_entry_deleted",
        entity_type="payroll_entry",
        entity_id=entry_id,
        details={},
        request=request,
    )
    db.commit()
    return {"ok": True}


class SourceIn(BaseModel):
    payroll_source: str


@router.get("/config", response_model=None)
def get_config(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    cdb: Session = Depends(get_db),
) -> dict[str, Any]:
    _ = user
    tid = str(getattr(request.state, "tenant", {}).get("id", ""))
    try:
        UUID(tid)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="invalid tenant context") from exc
    row = cdb.execute(
        text("SELECT payroll_source FROM tenant_settings WHERE tenant_id = :tid"),
        {"tid": tid},
    ).first()
    return {
        "payroll_source": row[0] if row else "manual",
        "candidates": ["manual", "csv_import", "gusto", "qbo_payroll"],
        "wired_today": ["manual"],
    }


@router.patch("/config", response_model=None)
def update_config(
    payload: SourceIn,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    cdb: Session = Depends(get_db),
) -> dict[str, Any]:
    _require_admin(user)
    tid = str(getattr(request.state, "tenant", {}).get("id", ""))
    try:
        UUID(tid)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="invalid tenant context") from exc
    if payload.payroll_source not in {"manual", "csv_import", "gusto", "qbo_payroll"}:
        raise HTTPException(status_code=422, detail="invalid payroll_source")
    cdb.execute(
        text(
            "INSERT INTO tenant_settings (tenant_id, payroll_source) "
            "VALUES (:tid, :src) "
            "ON CONFLICT (tenant_id) DO UPDATE SET payroll_source = EXCLUDED.payroll_source"
        ),
        {"tid": tid, "src": payload.payroll_source},
    )
    cdb.commit()
    return {"payroll_source": payload.payroll_source}

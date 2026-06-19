"""Numbering API — read/write the per-tenant job number format + counter.

Lives on the control plane (TenantSettings owns the columns); endpoints
are admin-gated because changing the format mid-stream can collide with
existing numbers if the tenant rewinds the counter."""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db
from gdx_dispatch.modules.numbering.service import preview as render_preview
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/numbering", tags=["numbering"])


class NumberingConfigOut(BaseModel):
    job_number_format: str
    job_number_next_seq: int
    job_number_year_seen: int | None = None
    preview: str


class NumberingConfigIn(BaseModel):
    job_number_format: str = Field(..., min_length=1, max_length=200)
    job_number_next_seq: int = Field(..., ge=1)


def _tenant_uuid(request: Request) -> UUID:
    tid = str(getattr(request.state, "tenant", {}).get("id", ""))
    try:
        return UUID(tid)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="invalid tenant context") from exc


def _read_row(db: Session, tid: UUID) -> dict[str, Any]:
    row = db.execute(
        text(
            "SELECT job_number_format, job_number_next_seq, job_number_year_seen "
            "FROM tenant_settings WHERE tenant_id = :tid"
        ),
        {"tid": str(tid)},
    ).first()
    if row is None:
        # Create-on-read so admins always see a usable default.
        db.execute(
            text(
                "INSERT INTO tenant_settings (tenant_id) VALUES (:tid) "
                "ON CONFLICT (tenant_id) DO NOTHING"
            ),
            {"tid": str(tid)},
        )
        db.commit()
        row = db.execute(
            text(
                "SELECT job_number_format, job_number_next_seq, job_number_year_seen "
                "FROM tenant_settings WHERE tenant_id = :tid"
            ),
            {"tid": str(tid)},
        ).first()
    fmt = row[0] or "JOB-{year}-{seq:03d}"
    seq = int(row[1] or 1)
    yr = row[2]
    return {
        "job_number_format": fmt,
        "job_number_next_seq": seq,
        "job_number_year_seen": yr,
        "preview": render_preview(fmt, seq, customer_name="Becky Meinecke"),
    }


@router.get("/config", response_model=NumberingConfigOut)
def get_config(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _ = user
    tid = _tenant_uuid(request)
    return _read_row(db, tid)


@router.patch("/config", response_model=NumberingConfigOut)
def update_config(
    payload: NumberingConfigIn,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if (user.get("role") or "").lower() not in {"admin", "owner"}:
        raise HTTPException(status_code=403, detail="admin or owner required")
    tid = _tenant_uuid(request)
    # Validate the template renders without raising.
    try:
        render_preview(payload.job_number_format, payload.job_number_next_seq)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid format template: {exc}") from exc

    db.execute(
        text(
            "INSERT INTO tenant_settings (tenant_id, job_number_format, job_number_next_seq) "
            "VALUES (:tid, :fmt, :seq) "
            "ON CONFLICT (tenant_id) DO UPDATE "
            "  SET job_number_format = EXCLUDED.job_number_format, "
            "      job_number_next_seq = EXCLUDED.job_number_next_seq"
        ),
        {"tid": str(tid), "fmt": payload.job_number_format, "seq": payload.job_number_next_seq},
    )
    db.commit()
    return _read_row(db, tid)

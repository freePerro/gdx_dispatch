"""Billing terms API — admin read/write of tenant defaults + fee config."""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/billing/terms", tags=["billing-terms"])


_COLS = (
    "default_payment_terms_days",
    "contractor_payment_terms_days",
    "retail_payment_terms_days",
    "wholesale_payment_terms_days",
    "early_pay_discount_percent",
    "early_pay_discount_days",
    "late_fee_flat_amount",
    "late_fee_percent",
    "late_fee_grace_days",
    "interest_rate_monthly_percent",
    "interest_grace_days",
)


class TermsPayload(BaseModel):
    default_payment_terms_days: int = Field(30, ge=0, le=365)
    contractor_payment_terms_days: int | None = Field(None, ge=0, le=365)
    retail_payment_terms_days: int | None = Field(None, ge=0, le=365)
    wholesale_payment_terms_days: int | None = Field(None, ge=0, le=365)
    early_pay_discount_percent: Decimal | None = Field(None, ge=0, le=1)
    early_pay_discount_days: int | None = Field(None, ge=0, le=365)
    late_fee_flat_amount: Decimal | None = Field(None, ge=0)
    late_fee_percent: Decimal | None = Field(None, ge=0, le=1)
    late_fee_grace_days: int = Field(0, ge=0, le=365)
    interest_rate_monthly_percent: Decimal | None = Field(None, ge=0, le=1)
    interest_grace_days: int = Field(0, ge=0, le=365)


def _tenant_uuid(request: Request) -> UUID:
    tid = str(getattr(request.state, "tenant", {}).get("id", ""))
    try:
        return UUID(tid)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="invalid tenant context") from exc


def _read(db: Session, tid: UUID) -> dict[str, Any]:
    cols = ", ".join(_COLS)
    row = db.execute(
        text(f"SELECT {cols} FROM tenant_settings WHERE tenant_id = :tid"),
        {"tid": str(tid)},
    ).first()
    if row is None:
        db.execute(
            text("INSERT INTO tenant_settings (tenant_id) VALUES (:tid) ON CONFLICT (tenant_id) DO NOTHING"),
            {"tid": str(tid)},
        )
        db.commit()
        row = db.execute(
            text(f"SELECT {cols} FROM tenant_settings WHERE tenant_id = :tid"),
            {"tid": str(tid)},
        ).first()
    return {col: row[i] for i, col in enumerate(_COLS)}


@router.get("", response_model=None)
def get_terms(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _ = user
    return _read(db, _tenant_uuid(request))


@router.patch("", response_model=None)
def update_terms(
    payload: TermsPayload,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if (user.get("role") or "").lower() not in {"admin", "owner"}:
        raise HTTPException(status_code=403, detail="admin or owner required")
    tid = _tenant_uuid(request)

    set_clause = ", ".join(f"{c} = :{c}" for c in _COLS)
    db.execute(
        text(
            f"INSERT INTO tenant_settings (tenant_id, {', '.join(_COLS)}) "
            f"VALUES (:tid, {', '.join(':' + c for c in _COLS)}) "
            f"ON CONFLICT (tenant_id) DO UPDATE SET {set_clause}"
        ),
        {"tid": str(tid), **{c: getattr(payload, c) for c in _COLS}},
    )
    db.commit()
    return _read(db, tid)

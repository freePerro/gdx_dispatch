"""Estimates-features API: GET (any signed-in user) + PATCH (admin/owner)."""
from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db
from gdx_dispatch.routers.auth import get_current_user

router = APIRouter(prefix="/api/estimates-features", tags=["estimates-features"])


_COLS = (
    "estimates_allow_line_margin_override",
    "estimates_default_terms",
    "estimate_email_subject_template",
    "estimate_email_body_template",
    "estimate_deposit_pct",
    "estimates_hide_line_prices",
)


class FeaturesPayload(BaseModel):
    estimates_allow_line_margin_override: bool = True
    estimates_default_terms: str = ""
    estimate_email_subject_template: str = ""
    estimate_email_body_template: str = ""
    estimate_deposit_pct: int = 50
    estimates_hide_line_prices: bool = False


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
    text_cols = {"estimates_default_terms", "estimate_email_subject_template", "estimate_email_body_template"}
    int_cols = {"estimate_deposit_pct"}
    out: dict[str, Any] = {}
    for i, col in enumerate(_COLS):
        val = row[i]
        if col in text_cols:
            out[col] = str(val or "")
        elif col in int_cols:
            out[col] = int(val if val is not None else 50)
        else:
            out[col] = bool(val)
    return out


@router.get("", response_model=None)
def get_features_endpoint(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _ = user
    return _read(db, _tenant_uuid(request))


@router.patch("", response_model=None)
def update_features(
    payload: FeaturesPayload,
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

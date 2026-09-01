"""Estimates-features API: GET (any signed-in user) + PATCH (admin/owner)."""
from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync, resolve_audit_actor
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
    "estimate_expiry_days",
    # Invoice + receipt email copy (issue #351, migration 086). Blank = the
    # platform default in routers/invoices.py, exactly like the estimate pair.
    "invoice_email_subject_template",
    "invoice_email_body_template",
    "receipt_email_subject_template",
    "receipt_email_body_template",
)

# Per-column default for int columns. A single shared "50" fallback would have
# defaulted estimate_expiry_days to 50, not 60 — the reason this dict exists.
_INT_DEFAULTS = {"estimate_deposit_pct": 50, "estimate_expiry_days": 60}

# Free-text columns: NULL reads back as "" so the Settings inputs stay
# controlled and "blank" is one value, not two.
_TEXT_COLS = frozenset({
    "estimates_default_terms",
    "estimate_email_subject_template",
    "estimate_email_body_template",
    "invoice_email_subject_template",
    "invoice_email_body_template",
    "receipt_email_subject_template",
    "receipt_email_body_template",
})


class FeaturesPayload(BaseModel):
    estimates_allow_line_margin_override: bool = True
    estimates_default_terms: str = ""
    estimate_email_subject_template: str = ""
    estimate_email_body_template: str = ""
    estimate_deposit_pct: int = 50
    estimates_hide_line_prices: bool = False
    # 1..365 days — 0 would expire an estimate the instant it's sent.
    estimate_expiry_days: int = Field(default=60, ge=1, le=365)
    invoice_email_subject_template: str = ""
    invoice_email_body_template: str = ""
    receipt_email_subject_template: str = ""
    receipt_email_body_template: str = ""


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
    out: dict[str, Any] = {}
    for i, col in enumerate(_COLS):
        val = row[i]
        if col in _TEXT_COLS:
            out[col] = str(val or "")
        elif col in _INT_DEFAULTS:
            out[col] = int(val if val is not None else _INT_DEFAULTS[col])
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
    before = _read(db, tid)
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
    after = _read(db, tid)
    # Invariant #1 (ARCHITECTURAL_INVARIANTS.md): who changed which setting,
    # to what. These columns include the customer-facing email copy, so a
    # wrong template that went out to customers must be traceable to the
    # save that introduced it. Only the columns that actually moved are
    # recorded — a no-op save leaves an empty diff, not eleven values.
    changed = {c: after[c] for c in _COLS if before.get(c) != after.get(c)}
    log_audit_event_sync(
        db=db,
        tenant_id=str(tid),
        user_id=resolve_audit_actor(user, request),
        action="estimates_features_updated",
        entity_type="tenant_settings",
        entity_id=str(tid),
        details={"changed": changed},
        ip_address=(request.client.host if request.client else None),
        request=request,
    )
    db.commit()
    return after

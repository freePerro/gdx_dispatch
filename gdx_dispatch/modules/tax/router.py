"""Tax module router.

Endpoints under `/api/tax`. Tenant-plane (per-tenant DB), admin-only.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import (
    ensure_audit_table,
    log_audit_event_sync,
    resolve_audit_actor,
)
from gdx_dispatch.core.database import get_db
from gdx_dispatch.modules.tax.models import TaxConfig, TaxExemption
from gdx_dispatch.modules.tax.service import get_or_create_config, resolve_rate
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tax", tags=["tax"])


# ── schemas ────────────────────────────────────────────────────────────


class TaxConfigOut(BaseModel):
    id: str
    name: str
    default_rate: float
    tax_labor: bool = False
    description: str | None = None
    configured_at: datetime | None = None


class TaxConfigPatch(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    default_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    tax_labor: bool | None = None
    description: str | None = None

    @field_validator("default_rate")
    @classmethod
    def _round_4(cls, v: float | None) -> float | None:
        if v is None:
            return None
        # Numeric(5,4) — clip floating-point noise.
        return round(v, 4)


class TaxExemptionOut(BaseModel):
    id: str
    customer_id: str
    exempt: bool
    reason: str | None = None
    certificate_id: str | None = None
    exempt_from: date | None = None
    exempt_until: date | None = None
    notes: str | None = None


class TaxExemptionCreate(BaseModel):
    customer_id: str
    exempt: bool = True
    reason: str | None = None
    certificate_id: str | None = None
    exempt_from: date | None = None
    exempt_until: date | None = None
    notes: str | None = None


# ── helpers ────────────────────────────────────────────────────────────


def _require_admin(user: dict[str, Any]) -> None:
    if (user.get("role") or "").lower() not in {"admin", "owner"}:
        raise HTTPException(status_code=403, detail="admin role required")


def _config_to_out(c: TaxConfig) -> TaxConfigOut:
    return TaxConfigOut(
        id=str(c.id),
        name=c.name,
        default_rate=float(c.default_rate or 0),
        tax_labor=bool(getattr(c, "tax_labor", False)),
        description=c.description,
        configured_at=c.configured_at,
    )


def _tenant_of(user: dict[str, Any]) -> str | None:
    """The tenant the audit row belongs to.

    ``get_current_user`` returns {user_id, tenant_id, role}; without this the
    row would fall back to ``request.state.tenant``, which only the real app's
    tenant middleware sets.
    """
    return str(user.get("tenant_id") or "") or None


def _audit_value(value: Any) -> Any:
    """A column value in a form both the audit JSON and a before/after compare
    can use.

    ``default_rate`` is ``Numeric`` — a ``Decimal`` off the row and a ``float``
    off the payload, and ``Decimal("0.073800") != 0.0738`` in Python, so an
    un-normalized compare would report the rate as changed on every save,
    including a no-op one. Dates become ISO strings for the same reason (and
    because that is what the JSON column stores anyway).
    """
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _exemption_details(e: TaxExemption) -> dict[str, Any]:
    """Every column of an exemption, for the audit trail.

    DELETE below is a HARD delete — ``TaxExemption`` carries no ``deleted_at``,
    so invariant #2 does not apply and nothing is left behind. That makes the
    audit row the ONLY surviving record of a rule that suppressed sales tax on
    a customer's invoices, so it has to carry the whole row, not just the id.
    """
    return {
        "customer_id": str(e.customer_id),
        "exempt": bool(e.exempt),
        "reason": e.reason,
        "certificate_id": e.certificate_id,
        "exempt_from": _audit_value(e.exempt_from),
        "exempt_until": _audit_value(e.exempt_until),
        "notes": e.notes,
        "created_at": _audit_value(e.created_at),
    }


def _exemption_to_out(e: TaxExemption) -> TaxExemptionOut:
    return TaxExemptionOut(
        id=str(e.id),
        customer_id=str(e.customer_id),
        exempt=bool(e.exempt),
        reason=e.reason,
        certificate_id=e.certificate_id,
        exempt_from=e.exempt_from,
        exempt_until=e.exempt_until,
        notes=e.notes,
    )


# ── endpoints ──────────────────────────────────────────────────────────


@router.get("/config", response_model=TaxConfigOut)
def get_config(
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TaxConfigOut:
    """Read the tenant's default tax rate. Auth required (any role) so the
    invoice-detail UI can render the correct percentage; admin gate is
    only on writes."""
    _ = user
    cfg = get_or_create_config(db)
    return _config_to_out(cfg)


@router.patch("/config", response_model=TaxConfigOut)
def patch_config(
    payload: TaxConfigPatch,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TaxConfigOut:
    """Change the tenant's sales-tax default. Audited (#558, invariant #1):
    the rate on this row decides what every later invoice charges, so who
    moved it and from what has to stay answerable."""
    _require_admin(user)
    # ensure_audit_table COMMITS the first time it runs for an engine
    # (`core/audit.py`). Left to fire lazily from inside log_audit_event_sync it
    # would harden this handler's staged edit a moment before the audit row is
    # written, so the change and its trail stop being atomic. Run it here, where
    # committing has nothing to disturb; every later call is a no-op.
    #
    # NOT `Depends(audit_ready_db)`: that dependency resolves its OWN session
    # and would bypass the `get_db` override the tax tests install — the same
    # trap documented at routers/customers.py:1880.
    ensure_audit_table(db)
    cfg = get_or_create_config(db)
    updates = payload.model_dump(exclude_unset=True)
    changed: dict[str, Any] = {}
    for key, value in updates.items():
        before = _audit_value(getattr(cfg, key, None))
        after = _audit_value(value)
        if before != after:
            changed[key] = {"from": before, "to": after}
        setattr(cfg, key, value)
    cfg.configured_at = datetime.now(timezone.utc)
    # Staged BEFORE the commit so the change and its trail land together
    # (`core/audit.py::audit_or_rollback` states the contract).
    log_audit_event_sync(
        db,
        tenant_id=_tenant_of(user),
        user_id=resolve_audit_actor(user, request),
        action="tax_config_updated",
        entity_type="tax_config",
        entity_id=str(cfg.id),
        details={"changed": changed},
        request=request,
    )
    db.commit()
    db.refresh(cfg)
    return _config_to_out(cfg)


@router.get("/exemptions", response_model=list[TaxExemptionOut])
def list_exemptions(
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[TaxExemptionOut]:
    _require_admin(user)
    rows = db.execute(select(TaxExemption).order_by(TaxExemption.created_at.desc())).scalars().all()
    return [_exemption_to_out(r) for r in rows]


@router.post("/exemptions", response_model=TaxExemptionOut, status_code=201)
def create_exemption(
    payload: TaxExemptionCreate,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TaxExemptionOut:
    """Exempt a customer from sales tax. Audited (#558, invariant #1)."""
    _require_admin(user)
    ensure_audit_table(db)  # before staging: its first run on an engine commits
    try:
        cid = UUID(payload.customer_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail="customer_id must be a UUID")
    row = TaxExemption(
        customer_id=cid,
        exempt=payload.exempt,
        reason=payload.reason,
        certificate_id=payload.certificate_id,
        exempt_from=payload.exempt_from,
        exempt_until=payload.exempt_until,
        notes=payload.notes,
    )
    db.add(row)
    db.flush()  # assign row.id so the audit row can name what was created
    log_audit_event_sync(
        db,
        tenant_id=_tenant_of(user),
        user_id=resolve_audit_actor(user, request),
        action="tax_exemption_created",
        entity_type="tax_exemption",
        entity_id=str(row.id),
        details=_exemption_details(row),
        request=request,
    )
    db.commit()
    db.refresh(row)
    return _exemption_to_out(row)


@router.delete("/exemptions/{exemption_id}", status_code=204)
def delete_exemption(
    exemption_id: str,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    """Remove an exemption. HARD delete — the audit row below is the only
    record the exemption ever existed (#558, invariant #1)."""
    _require_admin(user)
    ensure_audit_table(db)  # before staging: its first run on an engine commits
    try:
        eid = UUID(exemption_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="exemption not found")
    row = db.execute(select(TaxExemption).where(TaxExemption.id == eid)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="exemption not found")
    details = _exemption_details(row)  # captured BEFORE the row stops existing
    db.delete(row)
    log_audit_event_sync(
        db,
        tenant_id=_tenant_of(user),
        user_id=resolve_audit_actor(user, request),
        action="tax_exemption_deleted",
        entity_type="tax_exemption",
        entity_id=str(eid),
        details=details,
        request=request,
    )
    db.commit()


@router.get("/resolve")
def resolve_rate_endpoint(
    customer_id: str | None = None,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Diagnostic endpoint — given a customer_id (optional) returns the
    tax rate that would be applied. Used by the invoice-create flow and
    by the Settings → Tax UI to preview the effect of an exemption."""
    _ = user
    rate = resolve_rate(db, customer_id)
    return {"rate": float(rate), "rate_pct": float(rate * 100)}

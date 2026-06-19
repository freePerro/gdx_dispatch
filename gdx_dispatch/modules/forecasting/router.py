"""Forecasting + QB recurring transactions router."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi import Request as FastAPIRequest
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from datetime import date as _date
from uuid import UUID

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_role
from gdx_dispatch.core.quickbooks import QBAuthError, QBError
from gdx_dispatch.modules.forecasting import observed_recurring
from gdx_dispatch.modules.forecasting import qb_recurring as qb_recurring_helper
from gdx_dispatch.modules.forecasting import service as forecast_service
from gdx_dispatch.modules.forecasting.models import (
    CADENCE_ANNUAL,
    CADENCE_BIWEEKLY,
    CADENCE_MONTHLY,
    CADENCE_QUARTERLY,
    CADENCE_SEMIANNUAL,
    CADENCE_WEEKLY,
    STREAM_STATUS_ACTIVE,
    STREAM_STATUS_CANCELLED,
    STREAM_STATUS_EXPIRED,
    STREAM_STATUS_PAID_OFF,
    STREAM_STATUS_SUGGESTED,
    QBRecurringTransaction,
    RecurringStream,
    RecurringStreamHit,
)
from gdx_dispatch.modules.quickbooks.banking import QBBankTransaction
from gdx_dispatch.routers.auth import get_current_user

_VALID_CADENCES = {
    CADENCE_WEEKLY, CADENCE_BIWEEKLY, CADENCE_MONTHLY,
    CADENCE_QUARTERLY, CADENCE_SEMIANNUAL, CADENCE_ANNUAL,
}
_END_REASONS = {STREAM_STATUS_PAID_OFF, STREAM_STATUS_CANCELLED, STREAM_STATUS_EXPIRED}

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["forecasting"])


def _tenant_id(request: FastAPIRequest, current_user: dict[str, str] | None) -> str:
    state_tenant = getattr(request.state, "tenant", {}) or {}
    tid = str(state_tenant.get("id") or "").strip()
    if not tid and current_user:
        tid = str(current_user.get("tenant_id") or "").strip()
    if not tid:
        raise HTTPException(status_code=400, detail="Missing tenant context")
    return tid


class ForecastSettingsPayload(BaseModel):
    default_window_days: int | None = Field(default=None, ge=1, le=365)
    collect_rate_0_30: float | None = Field(default=None, ge=0.0, le=1.0)
    collect_rate_31_60: float | None = Field(default=None, ge=0.0, le=1.0)
    collect_rate_61_90: float | None = Field(default=None, ge=0.0, le=1.0)
    collect_rate_90_plus: float | None = Field(default=None, ge=0.0, le=1.0)
    scheduled_realization_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    include_recurring: bool | None = None


@router.get("/forecast/settings")
def get_forecast_settings(
    request: FastAPIRequest,
    current_user: dict[str, str] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _tenant_id(request, current_user)
    s = forecast_service.get_or_create_settings(db)
    return forecast_service._settings_dict(s)


@router.put("/forecast/settings", dependencies=[Depends(require_role("admin", "owner"))])
def update_forecast_settings(
    request: FastAPIRequest,
    payload: ForecastSettingsPayload,
    current_user: dict[str, str] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request, current_user)
    body = {k: v for k, v in payload.model_dump().items() if v is not None}
    s = forecast_service.update_settings(db, body)
    log_audit_event_sync(
        db,
        tenant_id=tenant_id,
        user_id=str(current_user.get("sub") or ""),
        action="forecast_settings.update",
        entity_type="forecast_settings",
        entity_id=str(s.id),
        details=body,
    )
    return forecast_service._settings_dict(s)


@router.get("/forecast/revenue")
def get_revenue_forecast(
    request: FastAPIRequest,
    window: int | None = None,
    current_user: dict[str, str] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _tenant_id(request, current_user)
    if window is not None and (window < 1 or window > 365):
        raise HTTPException(status_code=400, detail="window must be between 1 and 365 days")
    return forecast_service.revenue_projection(db, window_days=window)


@router.post("/quickbooks/sync/recurring-transactions")
def sync_qb_recurring(
    request: FastAPIRequest,
    current_user: dict[str, str] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request, current_user)
    try:
        result = qb_recurring_helper.sync_recurring_for_tenant(tenant_id, db)
    except QBAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except QBError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    log_audit_event_sync(
        db,
        tenant_id=tenant_id,
        user_id=str(current_user.get("sub") or ""),
        action="qb.recurring_sync",
        entity_type="qb_recurring_transactions",
        entity_id="*",
        details=result,
    )
    return result


# ─── Recurring streams: observed + manual ──────────────────────────────────


def _stream_dict(s: RecurringStream, *, include_hits: bool = False) -> dict[str, Any]:
    out = {
        "id": str(s.id),
        "label": s.label,
        "source": s.source,
        "status": s.status,
        "payee_pattern": s.payee_pattern,
        "amount_min": float(s.amount_min),
        "amount_max": float(s.amount_max),
        "account_name": s.account_name,
        "cadence": s.cadence,
        "cadence_anchor_day": int(s.cadence_anchor_day) if s.cadence_anchor_day is not None else None,
        "term_total_occurrences": int(s.term_total_occurrences) if s.term_total_occurrences is not None else None,
        "term_end_date": s.term_end_date.isoformat() if s.term_end_date else None,
        "occurrences_seen": int(s.occurrences_seen),
        "start_date": s.start_date.isoformat() if s.start_date else None,
        "next_expected_date": s.next_expected_date.isoformat() if s.next_expected_date else None,
        "last_observed_date": s.last_observed_date.isoformat() if s.last_observed_date else None,
        "ended_at": s.ended_at.isoformat() if s.ended_at else None,
        "ended_reason": s.ended_reason,
        "notes": s.notes,
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }
    if include_hits:
        out["hits"] = [
            {
                "id": str(h.id),
                "qb_txn_id": h.qb_txn_id,
                "txn_date": h.txn_date.isoformat() if h.txn_date else None,
                "amount": float(h.amount),
                "confirmed": bool(h.confirmed),
            }
            for h in sorted(s.hits, key=lambda h: h.txn_date, reverse=True)
        ]
    return out


def _get_stream_or_404(db: Session, stream_id: str) -> RecurringStream:
    try:
        sid = UUID(stream_id)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid stream id") from exc
    s = db.get(RecurringStream, sid)
    if s is None or s.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Stream not found")
    return s


class StreamCreatePayload(BaseModel):
    label: str = Field(min_length=1, max_length=200)
    payee_pattern: str = Field(min_length=1, max_length=200)
    amount_min: float = Field(ge=0)
    amount_max: float = Field(ge=0)
    cadence: str
    cadence_anchor_day: int | None = Field(default=None, ge=1, le=31)
    account_name: str | None = Field(default=None, max_length=300)
    term_total_occurrences: int | None = Field(default=None, ge=1, le=600)
    term_end_date: _date | None = None
    start_date: _date | None = None
    notes: str | None = None


class StreamFromTxnPayload(BaseModel):
    qb_txn_id: str = Field(min_length=1, max_length=120)
    cadence: str
    label: str | None = Field(default=None, max_length=200)
    term_total_occurrences: int | None = Field(default=None, ge=1, le=600)
    term_end_date: _date | None = None


class StreamPatchPayload(BaseModel):
    label: str | None = Field(default=None, max_length=200)
    cadence: str | None = None
    cadence_anchor_day: int | None = Field(default=None, ge=1, le=31)
    amount_min: float | None = Field(default=None, ge=0)
    amount_max: float | None = Field(default=None, ge=0)
    term_total_occurrences: int | None = Field(default=None, ge=1, le=600)
    term_end_date: _date | None = None
    notes: str | None = None


class StreamEndPayload(BaseModel):
    reason: str = Field(pattern="^(paid_off|cancelled|expired)$")
    ended_at: _date | None = None


def _validate_cadence(c: str) -> None:
    if c not in _VALID_CADENCES:
        raise HTTPException(status_code=400, detail=f"Invalid cadence; must be one of {sorted(_VALID_CADENCES)}")


def _validate_term_shape(occurrences: int | None, end_date: _date | None) -> None:
    if occurrences is not None and end_date is not None:
        raise HTTPException(
            status_code=400,
            detail="Specify term_total_occurrences OR term_end_date, not both. Leave both unset for open-ended.",
        )


@router.get("/forecast/recurring/streams")
def list_recurring_streams(
    request: FastAPIRequest,
    status: str | None = None,
    current_user: dict[str, str] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _tenant_id(request, current_user)
    q = db.query(RecurringStream).filter(RecurringStream.deleted_at.is_(None))
    if status:
        q = q.filter(RecurringStream.status == status)
    rows = q.order_by(RecurringStream.next_expected_date.asc().nulls_last()).all()
    return {"items": [_stream_dict(s) for s in rows], "total": len(rows)}


@router.get("/forecast/recurring/streams/{stream_id}")
def get_recurring_stream(
    stream_id: str,
    request: FastAPIRequest,
    current_user: dict[str, str] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _tenant_id(request, current_user)
    s = _get_stream_or_404(db, stream_id)
    return _stream_dict(s, include_hits=True)


@router.post(
    "/forecast/recurring/streams",
    status_code=201,
    dependencies=[Depends(require_role("admin", "owner"))],
)
def create_recurring_stream(
    request: FastAPIRequest,
    payload: StreamCreatePayload,
    current_user: dict[str, str] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request, current_user)
    _validate_cadence(payload.cadence)
    _validate_term_shape(payload.term_total_occurrences, payload.term_end_date)
    if payload.amount_min > payload.amount_max:
        raise HTTPException(status_code=400, detail="amount_min must be ≤ amount_max")
    actor_uuid = None
    try:
        if current_user.get("sub"):
            actor_uuid = UUID(str(current_user["sub"]))
    except (ValueError, TypeError):
        actor_uuid = None
    s = RecurringStream(
        label=payload.label,
        source="manual",
        status=STREAM_STATUS_ACTIVE,
        payee_pattern=payload.payee_pattern.upper().strip(),
        amount_min=payload.amount_min,
        amount_max=payload.amount_max,
        cadence=payload.cadence,
        cadence_anchor_day=payload.cadence_anchor_day,
        account_name=payload.account_name,
        term_total_occurrences=payload.term_total_occurrences,
        term_end_date=payload.term_end_date,
        start_date=payload.start_date,
        notes=payload.notes,
        created_by_user_id=actor_uuid,
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    log_audit_event_sync(
        db, tenant_id=tenant_id, user_id=str(current_user.get("sub") or ""),
        action="recurring_stream.create", entity_type="recurring_stream",
        entity_id=str(s.id), details={"label": s.label, "source": s.source},
    )
    return _stream_dict(s)


@router.post(
    "/forecast/recurring/streams/from-transaction",
    status_code=201,
    dependencies=[Depends(require_role("admin", "owner"))],
)
def create_recurring_from_transaction(
    request: FastAPIRequest,
    payload: StreamFromTxnPayload,
    current_user: dict[str, str] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Convenience: derive payee/amount/account from an existing bank txn."""
    tenant_id = _tenant_id(request, current_user)
    _validate_cadence(payload.cadence)
    _validate_term_shape(payload.term_total_occurrences, payload.term_end_date)
    txn = (
        db.query(QBBankTransaction)
        .filter(QBBankTransaction.qb_txn_id == payload.qb_txn_id)
        .filter(QBBankTransaction.deleted_at.is_(None))
        .one_or_none()
    )
    if txn is None:
        raise HTTPException(status_code=404, detail="Bank transaction not found")
    amt = abs(float(txn.amount or 0))
    if amt == 0:
        raise HTTPException(status_code=400, detail="Cannot create stream from a zero-amount transaction")
    payee_norm = observed_recurring.normalize_payee(txn.payee)
    if not payee_norm:
        raise HTTPException(status_code=400, detail="Cannot derive payee pattern from this transaction")
    # Dedup: if any active/suggested stream already has this txn as a hit, refuse.
    # Prevents double-click spamming new streams with identical hit attached.
    already = (
        db.query(RecurringStreamHit)
        .join(RecurringStream, RecurringStream.id == RecurringStreamHit.stream_id)
        .filter(RecurringStreamHit.qb_txn_id == payload.qb_txn_id)
        .filter(RecurringStream.deleted_at.is_(None))
        .filter(RecurringStream.status.in_([STREAM_STATUS_SUGGESTED, STREAM_STATUS_ACTIVE]))
        .first()
    )
    if already is not None:
        raise HTTPException(
            status_code=409,
            detail=f"This transaction is already attached to stream {already.stream_id}",
        )
    actor_uuid = None
    try:
        if current_user.get("sub"):
            actor_uuid = UUID(str(current_user["sub"]))
    except (ValueError, TypeError):
        actor_uuid = None
    s = RecurringStream(
        label=payload.label or (txn.payee or payee_norm),
        source="manual",
        status=STREAM_STATUS_ACTIVE,
        payee_pattern=payee_norm,
        # Default window ±20% — matches the detector's tolerance so observed
        # detection won't fight a manually-attached stream.
        amount_min=round(amt * 0.8, 2),
        amount_max=round(amt * 1.2, 2),
        cadence=payload.cadence,
        account_name=txn.account_name,
        term_total_occurrences=payload.term_total_occurrences,
        term_end_date=payload.term_end_date,
        start_date=txn.txn_date,
        last_observed_date=txn.txn_date,
        occurrences_seen=1,
        created_by_user_id=actor_uuid,
    )
    db.add(s)
    db.flush()
    db.add(RecurringStreamHit(
        stream_id=s.id, qb_txn_id=txn.qb_txn_id,
        txn_date=txn.txn_date, amount=amt, confirmed=True,
    ))
    db.commit()
    db.refresh(s)
    log_audit_event_sync(
        db, tenant_id=tenant_id, user_id=str(current_user.get("sub") or ""),
        action="recurring_stream.create_from_txn", entity_type="recurring_stream",
        entity_id=str(s.id), details={"qb_txn_id": payload.qb_txn_id},
    )
    return _stream_dict(s, include_hits=True)


@router.post("/forecast/recurring/streams/{stream_id}/confirm", dependencies=[Depends(require_role("admin", "owner"))])
def confirm_recurring_stream(
    stream_id: str,
    request: FastAPIRequest,
    current_user: dict[str, str] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request, current_user)
    s = _get_stream_or_404(db, stream_id)
    if s.status != STREAM_STATUS_SUGGESTED:
        raise HTTPException(status_code=409, detail=f"Only suggested streams can be confirmed; current status: {s.status}")
    s.status = STREAM_STATUS_ACTIVE
    db.commit()
    db.refresh(s)
    log_audit_event_sync(
        db, tenant_id=tenant_id, user_id=str(current_user.get("sub") or ""),
        action="recurring_stream.confirm", entity_type="recurring_stream",
        entity_id=stream_id, details={"label": s.label},
    )
    return _stream_dict(s)


@router.post("/forecast/recurring/streams/{stream_id}/end", dependencies=[Depends(require_role("admin", "owner"))])
def end_recurring_stream(
    stream_id: str,
    request: FastAPIRequest,
    payload: StreamEndPayload,
    current_user: dict[str, str] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """End a stream when the payment stops (paid off / cancelled / expired).

    Preserves all historical hits + the stream row itself — past data stays
    queryable in 'Ended' tab. Future forecast projections drop this stream.
    """
    tenant_id = _tenant_id(request, current_user)
    s = _get_stream_or_404(db, stream_id)
    if s.status in {STREAM_STATUS_PAID_OFF, STREAM_STATUS_CANCELLED, STREAM_STATUS_EXPIRED}:
        raise HTTPException(status_code=409, detail=f"Stream already ended ({s.status})")
    s.status = payload.reason  # reason maps directly to a terminal status enum
    s.ended_at = payload.ended_at or _date.today()
    s.ended_reason = payload.reason
    db.commit()
    db.refresh(s)
    log_audit_event_sync(
        db, tenant_id=tenant_id, user_id=str(current_user.get("sub") or ""),
        action="recurring_stream.end", entity_type="recurring_stream",
        entity_id=stream_id, details={"reason": payload.reason, "ended_at": s.ended_at.isoformat()},
    )
    return _stream_dict(s)


@router.patch("/forecast/recurring/streams/{stream_id}", dependencies=[Depends(require_role("admin", "owner"))])
def update_recurring_stream(
    stream_id: str,
    request: FastAPIRequest,
    payload: StreamPatchPayload,
    current_user: dict[str, str] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request, current_user)
    s = _get_stream_or_404(db, stream_id)
    if s.status in {STREAM_STATUS_PAID_OFF, STREAM_STATUS_CANCELLED, STREAM_STATUS_EXPIRED}:
        raise HTTPException(status_code=409, detail="Cannot edit an ended stream")

    body = payload.model_dump(exclude_unset=True)
    if "cadence" in body and body["cadence"] is not None:
        _validate_cadence(body["cadence"])
    # Compute the resulting term shape using current row + incoming patch
    new_occ = body.get("term_total_occurrences", s.term_total_occurrences) if "term_total_occurrences" in body else s.term_total_occurrences
    new_end = body.get("term_end_date", s.term_end_date) if "term_end_date" in body else s.term_end_date
    _validate_term_shape(new_occ, new_end)
    new_min = body.get("amount_min", float(s.amount_min)) if "amount_min" in body else float(s.amount_min)
    new_max = body.get("amount_max", float(s.amount_max)) if "amount_max" in body else float(s.amount_max)
    if new_min > new_max:
        raise HTTPException(status_code=400, detail="amount_min must be ≤ amount_max")

    for key, value in body.items():
        setattr(s, key, value)
    db.commit()
    db.refresh(s)
    log_audit_event_sync(
        db, tenant_id=tenant_id, user_id=str(current_user.get("sub") or ""),
        action="recurring_stream.update", entity_type="recurring_stream",
        entity_id=stream_id, details=body,
    )
    return _stream_dict(s)


@router.delete("/forecast/recurring/streams/{stream_id}", dependencies=[Depends(require_role("admin", "owner"))])
def soft_delete_recurring_stream(
    stream_id: str,
    request: FastAPIRequest,
    current_user: dict[str, str] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Soft-delete a stream (dismiss a suggestion, or remove a manual one).

    Differs from 'end': delete = "I don't want this row at all"; end = "this
    real-world payment finished but keep the history for analytics."
    """
    tenant_id = _tenant_id(request, current_user)
    s = _get_stream_or_404(db, stream_id)
    from datetime import UTC, datetime as _dt
    s.deleted_at = _dt.now(UTC)
    db.commit()
    log_audit_event_sync(
        db, tenant_id=tenant_id, user_id=str(current_user.get("sub") or ""),
        action="recurring_stream.delete", entity_type="recurring_stream",
        entity_id=stream_id, details={"label": s.label},
    )
    return {"ok": True, "id": stream_id}


@router.post(
    "/forecast/recurring/streams/{stream_id}/hits/{hit_id}/unlink",
    dependencies=[Depends(require_role("admin", "owner"))],
)
def unlink_hit(
    stream_id: str,
    hit_id: str,
    request: FastAPIRequest,
    current_user: dict[str, str] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Remove a falsely-attached hit. Doesn't touch the source qb_bank_transactions row."""
    tenant_id = _tenant_id(request, current_user)
    s = _get_stream_or_404(db, stream_id)
    try:
        hid = UUID(hit_id)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid hit id") from exc
    hit = db.get(RecurringStreamHit, hid)
    if hit is None or hit.stream_id != s.id:
        raise HTTPException(status_code=404, detail="Hit not found on this stream")
    db.delete(hit)
    # If the user un-attached an inflated occurrence, decrement.
    if int(s.occurrences_seen) > 0:
        s.occurrences_seen = int(s.occurrences_seen) - 1
    db.commit()
    log_audit_event_sync(
        db, tenant_id=tenant_id, user_id=str(current_user.get("sub") or ""),
        action="recurring_stream.unlink_hit", entity_type="recurring_stream",
        entity_id=stream_id, details={"hit_id": hit_id, "qb_txn_id": hit.qb_txn_id},
    )
    return {"ok": True, "stream_id": stream_id, "hit_id": hit_id}


@router.post("/forecast/recurring/detect", dependencies=[Depends(require_role("admin", "owner"))])
def run_observed_recurring_detector(
    request: FastAPIRequest,
    current_user: dict[str, str] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Trigger the observed-recurring detector for this tenant on demand.

    Same algorithm the nightly Celery beat runs. Returns the counts so the
    UI can show "5 new suggestions found" after the user clicks Detect Now.
    """
    tenant_id = _tenant_id(request, current_user)
    stats = observed_recurring.run_detector(db)
    log_audit_event_sync(
        db, tenant_id=tenant_id, user_id=str(current_user.get("sub") or ""),
        action="recurring_stream.detect_now", entity_type="recurring_stream",
        entity_id="*", details=stats,
    )
    return stats


@router.get("/quickbooks/recurring-transactions")
def list_qb_recurring(
    request: FastAPIRequest,
    current_user: dict[str, str] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _tenant_id(request, current_user)
    rows = db.execute(
        select(QBRecurringTransaction).order_by(QBRecurringTransaction.next_date.asc().nulls_last())
    ).scalars().all()
    return {
        "items": [
            {
                "qb_id": r.qb_id,
                "name": r.name,
                "txn_type": r.txn_type,
                "customer_qb_id": r.customer_qb_id,
                "customer_name": r.customer_name,
                "amount": float(r.amount or 0),
                "next_date": r.next_date.isoformat() if r.next_date else None,
                "interval_type": r.interval_type,
                "num_interval": int(r.num_interval) if r.num_interval is not None else None,
                "days_of_week": r.days_of_week,
                "active": bool(r.active),
                "last_synced_at": r.last_synced_at.isoformat() if r.last_synced_at else None,
            }
            for r in rows
        ],
        "total": len(rows),
    }

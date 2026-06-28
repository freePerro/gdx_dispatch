"""Forecast measurement loop (Stage A — docs/forecasting-accuracy-roadmap.md).

Measures **within-window AR collection realization**, per aging bucket.

The headline forecast multiplies open AR by a *lifetime* collection rate
(95/80/60/30%) and ignores the window. Scoring that lifetime number against
cash collected in a 30-day window is a horizon mismatch, not forecast error
(an earlier design did exactly that; an adversarial audit rejected it). So this
loop does NOT score the lifetime number. It records, per bucket, the fraction
of snapshotted AR actually collected within the window — the empirical
within-window rate. That is dimensionally coherent and is precisely the input
Stage B needs to replace the hard-coded lifetime defaults with window-calibrated
rates.

Three operations:
  - capture_snapshot:        freeze the open-AR population, bucketed, with frozen face.
  - reconcile_due_snapshots: once the window closes, measure collected-in-window per bucket.
  - accuracy_summary:        aggregate reconciled snapshots into a per-bucket
                             calibration table (observed window rate vs assumed lifetime rate).
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from gdx_dispatch.models.tenant_models import Invoice, Payment
from gdx_dispatch.modules.forecasting import service as forecast_service
from gdx_dispatch.modules.forecasting.models import (
    AR_BUCKETS,
    SNAPSHOT_STATUS_PENDING,
    SNAPSHOT_STATUS_RECONCILED,
    ForecastSnapshot,
    ForecastSnapshotInvoice,
)


def _open_invoices(db: Session) -> list[Invoice]:
    """Invoices counted by the AR component — same filter as
    service._open_ar_projection, so the measured population matches the
    forecast population."""
    return list(
        db.execute(
            select(Invoice).where(
                Invoice.status.in_(("sent", "overdue")),
                Invoice.balance_due > 0,
            )
        ).scalars().all()
    )


def capture_snapshot(
    db: Session, window_days: int | None = None, today: date | None = None
) -> ForecastSnapshot:
    """Freeze the open-AR population (bucketed, with face frozen) as a pending
    snapshot to be scored once the window closes."""
    settings = forecast_service.get_or_create_settings(db)
    if window_days is None:
        window_days = int(settings.default_window_days)
    if today is None:
        today = date.today()

    invoices = _open_invoices(db)
    assumed_rates = {b: forecast_service._bucket_rate(settings, b) for b in AR_BUCKETS}

    snap = ForecastSnapshot(
        as_of=today,
        window_days=window_days,
        horizon_end=today + timedelta(days=window_days),
        open_ar_face=sum(float(inv.balance_due or 0) for inv in invoices),
        assumed_rates=assumed_rates,
        status=SNAPSHOT_STATUS_PENDING,
    )
    db.add(snap)
    db.flush()  # assign snap.id for the child rows

    for inv in invoices:
        ref = forecast_service._ar_reference_date(inv)
        age = max(0, (today - ref).days)
        db.add(
            ForecastSnapshotInvoice(
                snapshot_id=snap.id,
                invoice_id=inv.id,
                bucket=forecast_service._ar_aging_bucket(age),
                face_at_snapshot=float(inv.balance_due or 0),
            )
        )
    db.commit()
    db.refresh(snap)
    return snap


def _collected_per_invoice(db: Session, invoice_ids: list, start: date, end: date) -> dict:
    """Sum of payments per invoice, dated within the closed window [start, end]."""
    if not invoice_ids:
        return {}
    rows = db.execute(
        select(Payment.invoice_id, func.sum(Payment.amount)).where(
            Payment.invoice_id.in_(invoice_ids),
            Payment.payment_date >= start,
            Payment.payment_date <= end,
        ).group_by(Payment.invoice_id)
    ).all()
    return {inv_id: float(amt or 0) for inv_id, amt in rows}


def reconcile_snapshot(db: Session, snap: ForecastSnapshot) -> ForecastSnapshot:
    """Score a snapshot: per bucket, measure the fraction of snapshotted face
    collected within the window."""
    rows = list(snap.invoices)
    paid = _collected_per_invoice(db, [r.invoice_id for r in rows], snap.as_of, snap.horizon_end)

    agg = {b: {"face": 0.0, "collected": 0.0} for b in AR_BUCKETS}
    for r in rows:
        face = float(r.face_at_snapshot or 0)
        # Clamp collection to [0, face]: the cap stops an overpayment (or a
        # payment against a since-grown balance) from pushing realization above
        # 100% of what was owed; the floor stops a net-negative window (a refund
        # or credit memo nets the invoice below zero) from producing a negative
        # collection rate that would poison Stage B's calibration.
        collected = max(0.0, min(face, paid.get(r.invoice_id, 0.0)))
        bucket = r.bucket if r.bucket in agg else AR_BUCKETS[-1]
        agg[bucket]["face"] += face
        agg[bucket]["collected"] += collected

    assumed = snap.assumed_rates or {}
    results = {}
    for b in AR_BUCKETS:
        face = agg[b]["face"]
        collected = agg[b]["collected"]
        results[b] = {
            "face": face,
            "collected_in_window": collected,
            "observed_window_rate": (collected / face) if face > 0 else None,
            # The lifetime assumption, carried for reference only. Deliberately
            # NOT differenced against observed_window_rate — they're different
            # horizons (eventual vs. this window), so a subtraction would be the
            # apples-to-oranges metric an earlier design was rejected for.
            "assumed_lifetime_rate": assumed.get(b),
        }

    snap.bucket_results = results
    snap.status = SNAPSHOT_STATUS_RECONCILED
    snap.reconciled_at = datetime.now(UTC)
    db.commit()
    db.refresh(snap)
    return snap


def reconcile_due_snapshots(db: Session, today: date | None = None) -> list[ForecastSnapshot]:
    """Reconcile every pending snapshot whose window has fully closed."""
    if today is None:
        today = date.today()
    due = db.execute(
        select(ForecastSnapshot).where(
            ForecastSnapshot.status == SNAPSHOT_STATUS_PENDING,
            ForecastSnapshot.horizon_end < today,
        )
    ).scalars().all()
    return [reconcile_snapshot(db, s) for s in due]


def prune_reconciled_snapshots(db: Session, today: date | None = None, retention_days: int = 180) -> int:
    """Delete reconciled snapshots older than the retention window (child rows
    cascade). Daily capture would otherwise grow both snapshot tables without
    bound — one header + one child row per open invoice, every day, forever.
    Retention is kept ≥ the calibration lookback so pruning never removes data
    the calibration still uses. Returns the number of snapshots deleted.

    Pending snapshots are never pruned, however old: an un-reconciled snapshot
    is still owed a reconciliation (e.g. the task was down past its horizon).
    """
    if today is None:
        today = date.today()
    cutoff = today - timedelta(days=retention_days)
    stale = db.execute(
        select(ForecastSnapshot).where(
            ForecastSnapshot.status == SNAPSHOT_STATUS_RECONCILED,
            ForecastSnapshot.as_of < cutoff,
        )
    ).scalars().all()
    for s in stale:
        db.delete(s)  # cascade removes ForecastSnapshotInvoice children
    db.commit()
    return len(stale)


def _pending_count(db: Session) -> int:
    return int(
        db.execute(
            select(func.count())
            .select_from(ForecastSnapshot)
            .where(ForecastSnapshot.status == SNAPSHOT_STATUS_PENDING)
        ).scalar_one()
    )


def accuracy_summary(db: Session) -> dict[str, Any]:
    """Per-bucket calibration table aggregated across reconciled snapshots.

    For each aging bucket: the face-weighted observed within-window collection
    rate (= total collected / total face across all reconciled snapshots) — the
    deliverable Stage B consumes — plus the assumed *lifetime* rate carried for
    reference. The two are reported side by side but deliberately NOT
    differenced: they are different horizons (eventual vs. this window), so a
    gap/MAPE would be the apples-to-oranges metric an earlier design was
    rejected for. The observed window rate IS the calibrated number; the
    lifetime rate is only there to show what the model currently assumes.
    """
    snaps = db.execute(
        select(ForecastSnapshot).where(ForecastSnapshot.status == SNAPSHOT_STATUS_RECONCILED)
    ).scalars().all()
    pending = _pending_count(db)

    empty_buckets = {
        b: {
            "face": 0.0,
            "collected_in_window": 0.0,
            "observed_window_rate": None,
            "assumed_lifetime_rate": None,
        }
        for b in AR_BUCKETS
    }
    if not snaps:
        return {"sample_size": 0, "pending": pending, "window_days": None, "buckets": empty_buckets}

    agg = {b: {"face": 0.0, "collected": 0.0} for b in AR_BUCKETS}
    for s in snaps:
        for b, r in (s.bucket_results or {}).items():
            if b not in agg:
                continue
            agg[b]["face"] += float(r.get("face") or 0)
            agg[b]["collected"] += float(r.get("collected_in_window") or 0)

    latest = max(snaps, key=lambda s: s.created_at)
    latest_assumed = latest.assumed_rates or {}

    buckets = {}
    for b in AR_BUCKETS:
        face = agg[b]["face"]
        collected = agg[b]["collected"]
        observed = (collected / face) if face > 0 else None
        buckets[b] = {
            "face": face,
            "collected_in_window": collected,
            "observed_window_rate": observed,
            "assumed_lifetime_rate": latest_assumed.get(b),
        }

    return {
        "sample_size": len(snaps),
        "pending": pending,
        "window_days": int(latest.window_days),
        "buckets": buckets,
    }


def snapshot_dict(s: ForecastSnapshot) -> dict[str, Any]:
    return {
        "id": str(s.id),
        "as_of": s.as_of.isoformat() if s.as_of else None,
        "window_days": int(s.window_days),
        "horizon_end": s.horizon_end.isoformat() if s.horizon_end else None,
        "open_ar_face": float(s.open_ar_face or 0),
        "invoice_count": len(s.invoices),
        "assumed_rates": s.assumed_rates,
        "status": s.status,
        "bucket_results": s.bucket_results,
        "reconciled_at": s.reconciled_at.isoformat() if s.reconciled_at else None,
        "created_at": s.created_at.isoformat() if s.created_at else None,
    }

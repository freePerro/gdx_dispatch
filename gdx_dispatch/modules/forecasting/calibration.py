"""Stage B — window-rate calibration from Stage A measurements.

Stage A (accuracy.py) records, per AR aging bucket, the fraction of open AR that
is actually collected *within the forecast window*. Stage B aggregates those
reconciled snapshots into a calibrated per-bucket WINDOW collection rate and
feeds it back into the AR projection (service._open_ar_projection), replacing the
hard-coded lifetime-rate priors once enough evidence exists.

Cold-start guard: a bucket is only treated as calibrated once at least
CALIBRATION_MIN_SNAPSHOTS reconciled snapshots (at the *matching* window) have
contributed face to it, so one fluky window can't swing the forecast. Until
then the projection falls back to the configured (prior) rate.

Window matching matters: a calibrated rate measured over a 30-day window only
describes a 30-day window. We only use snapshots whose `window_days` equals the
forecast window; other windows fall back to priors.

This is computed live from the snapshots on each forecast — no persisted
calibration state, so nothing can go stale and there is no extra table to drift.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.modules.forecasting.models import (
    AR_BUCKETS,
    SNAPSHOT_STATUS_RECONCILED,
    ForecastSnapshot,
)

# Minimum reconciled snapshots (at the matching window) contributing face to a
# bucket before its measured rate is trusted over the configured prior.
CALIBRATION_MIN_SNAPSHOTS = 3

# Only reconciled snapshots whose as_of is within this many days are used, so a
# stale collection regime can't dominate forever and the snapshot tables stay
# bounded (the daily task prunes beyond this — see accuracy.prune_reconciled_snapshots).
CALIBRATION_LOOKBACK_DAYS = 180


def calibrated_window_rates(
    db: Session,
    window_days: int,
    today: date,
    min_snapshots: int = CALIBRATION_MIN_SNAPSHOTS,
    lookback_days: int = CALIBRATION_LOOKBACK_DAYS,
) -> dict[str, dict[str, Any]]:
    """Per-bucket calibrated window collection rate from reconciled snapshots at
    the given window, within the recency lookback.

    Returns ``{bucket: {rate, face, collected, sample_size, calibrated}}`` where
    ``rate`` is the face-weighted observed within-window collection rate (None if
    no face observed) and ``calibrated`` is True only when ``sample_size`` (the
    number of reconciled snapshots that gave this bucket any face) meets the
    threshold AND face > 0.

    KNOWN LIMITATIONS (documented, not hidden — see roadmap doc):
      - Daily snapshots over the same open AR are correlated, so `sample_size`
        counts closed windows, not independent samples; the effective sample
        size is lower than the count.
      - A long-unpaid invoice recurs in many daily snapshots all showing
        "not collected", so persistent non-payers are over-weighted, biasing
        the rate down. A cohort rate (fraction of invoices ENTERING a bucket
        that pay in-window) would remove this — deferred to Stage C.
    """
    cutoff = today - timedelta(days=lookback_days)
    snaps = db.execute(
        select(ForecastSnapshot).where(
            ForecastSnapshot.status == SNAPSHOT_STATUS_RECONCILED,
            ForecastSnapshot.window_days == window_days,
            ForecastSnapshot.as_of >= cutoff,
        )
    ).scalars().all()

    agg = {b: {"face": 0.0, "collected": 0.0, "sample_size": 0} for b in AR_BUCKETS}
    for s in snaps:
        for b, r in (s.bucket_results or {}).items():
            if b not in agg:
                continue
            face = float(r.get("face") or 0)
            if face <= 0:
                continue
            agg[b]["face"] += face
            agg[b]["collected"] += float(r.get("collected_in_window") or 0)
            agg[b]["sample_size"] += 1

    out: dict[str, dict[str, Any]] = {}
    for b in AR_BUCKETS:
        face = agg[b]["face"]
        collected = agg[b]["collected"]
        n = agg[b]["sample_size"]
        out[b] = {
            "rate": (collected / face) if face > 0 else None,
            "face": face,
            "collected": collected,
            "sample_size": n,
            "calibrated": (n >= min_snapshots and face > 0),
        }
    return out


def calibration_status(
    db: Session, settings, window_days: int, today: date, min_snapshots: int = CALIBRATION_MIN_SNAPSHOTS
) -> dict[str, Any]:
    """Reporting view: per-bucket calibrated window rate next to the configured
    prior, so a user can see which buckets the forecast has learned and which
    are still running on the default prior. Both are window collection rates
    (the configured value is the window-rate prior); they are shown side by side
    but NOT differenced.
    """
    from gdx_dispatch.modules.forecasting import service as forecast_service

    rates = calibrated_window_rates(db, window_days, today, min_snapshots=min_snapshots)
    buckets = {}
    for b in AR_BUCKETS:
        info = rates[b]
        buckets[b] = {
            **info,
            "configured_rate": forecast_service._bucket_rate(settings, b),
            "rate_in_use": info["rate"] if info["calibrated"] else forecast_service._bucket_rate(settings, b),
            "source_in_use": "calibrated" if info["calibrated"] else "configured",
        }
    return {
        "window_days": window_days,
        "min_snapshots": min_snapshots,
        "any_calibrated": any(v["calibrated"] for v in rates.values()),
        "buckets": buckets,
    }

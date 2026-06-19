"""P3.11 — nightly call-reports reconcile.

Compares Phone.com's server-computed ``/call-reports`` numbers for the
last N days against our local ``phone_com_stats_daily`` rows. Reports
drift but does NOT auto-fix — drift means either:

- a webhook went missing (should be rare; rotation grace + nightly
  reconcile sync will catch it the same night).
- our roll-up logic is wrong (action: investigate, not silently overwrite).

Logs a single line per tenant with the drift summary so ops can spot
patterns without paging.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from gdx_dispatch.modules.phone_com.client import PhoneComAPIError, PhoneComClient
from gdx_dispatch.modules.phone_com.models import PhoneComStatsDaily

log = logging.getLogger("gdx_dispatch.modules.phone_com.reconcile")


def _phone_com_report_to_daily(
    items: list[dict[str, Any]],
) -> dict[date, dict[str, int]]:
    """Phone.com's call-report shape varies by account; tolerate the common
    fields. Each row should at minimum carry a ``date`` (YYYY-MM-DD or epoch)
    and one or more counters (calls_in / calls_out / minutes / missed)."""
    out: dict[date, dict[str, int]] = {}
    for row in items:
        d_raw = row.get("date") or row.get("day") or row.get("start_date")
        if not d_raw:
            continue
        try:
            d = (
                date.fromisoformat(d_raw)
                if isinstance(d_raw, str)
                else datetime.fromtimestamp(int(d_raw), tz=timezone.utc).date()
            )
        except (TypeError, ValueError):
            continue
        out[d] = {
            "calls_in": int(row.get("calls_in") or row.get("inbound_count") or 0),
            "calls_out": int(row.get("calls_out") or row.get("outbound_count") or 0),
            "calls_missed": int(row.get("calls_missed") or row.get("missed_count") or 0),
            "total_call_minutes": int(
                row.get("total_minutes") or row.get("call_minutes") or 0,
            ),
        }
    return out


def reconcile_recent(
    tenant_db: Session,
    client: PhoneComClient,
    *,
    days: int = 7,
    drift_threshold: int = 2,
) -> dict[str, Any]:
    """Compare the last ``days`` of stats. ``drift_threshold`` is the
    minimum |diff| we report (small jitter is normal — webhook delivery
    timing means a call near midnight may land on different sides of the
    boundary in our table vs. Phone.com's).
    """
    today = datetime.now(timezone.utc).date()
    since_date = today - timedelta(days=days)
    since_epoch = int(datetime.combine(since_date, datetime.min.time(), tzinfo=timezone.utc).timestamp())

    try:
        report = client.get_call_report(from_epoch=since_epoch)
    except PhoneComAPIError as exc:
        log.warning("phone_com.reconcile_recent upstream failed: %s", exc)
        return {"ok": False, "error": str(exc), "days": days}

    upstream = _phone_com_report_to_daily(report.get("items") or [])
    local_rows = (
        tenant_db.query(PhoneComStatsDaily)
        .filter(PhoneComStatsDaily.stat_date >= since_date)
        .all()
    )
    local = {r.stat_date: r for r in local_rows}

    drifts: list[dict[str, Any]] = []
    for d, up in upstream.items():
        loc = local.get(d)
        loc_vals = {
            "calls_in": loc.calls_in if loc else 0,
            "calls_out": loc.calls_out if loc else 0,
            "calls_missed": loc.calls_missed if loc else 0,
            "total_call_minutes": loc.total_call_minutes if loc else 0,
        }
        diffs = {
            k: up[k] - loc_vals[k]
            for k in up
            if abs(up[k] - loc_vals[k]) >= drift_threshold
        }
        if diffs:
            drifts.append({"date": d.isoformat(), "diffs": diffs,
                           "upstream": up, "local": loc_vals})

    if drifts:
        log.warning(
            "phone_com.reconcile_recent drift days=%d count=%d sample=%s",
            days, len(drifts), drifts[0],
        )
    else:
        log.info("phone_com.reconcile_recent ok days=%d", days)

    return {
        "ok": True,
        "days": days,
        "drift_count": len(drifts),
        "drifts": drifts,
    }

"""Effective labor cost resolver."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


@dataclass
class LaborCost:
    true_cost: Decimal | None
    estimated_cost: Decimal | None
    source: str  # "true" | "estimated" | "none"
    rate_used: Decimal | None
    period_id: str | None  # payroll_entries.id when source == "true"


def effective_labor_cost(
    tenant_db: Session,
    *,
    tech_user_id: str | UUID | None,
    hours: float | Decimal,
    when: datetime | None = None,
) -> LaborCost:
    """Compute labor cost for a tech labor entry.

    Priority:
      1. payroll_entries — most recent entry whose period covers `when`
         for this tech. Rate = gross_pay / hours_paid. Returns true_cost.
      2. technicians.hourly_rate — estimated rate. Returns estimated_cost.
      3. Neither — returns 0 / source='none'.

    Always returns both fields when both are available, so callers can
    show "Estimated $120, True $135 (variance +$15)" in reporting.
    """
    h = Decimal(str(hours or 0))
    when_ts = when or datetime.now(timezone.utc)
    if not tech_user_id:
        return LaborCost(None, None, "none", None, None)

    tech_id_str = str(tech_user_id)

    # 1) True rate from payroll_entries (most recent containing period).
    true_row = None
    try:
        true_row = tenant_db.execute(
            text(
                "SELECT id, hours_paid, gross_pay "
                "FROM payroll_entries "
                "WHERE tech_user_id = :tid "
                "  AND deleted_at IS NULL "
                "  AND period_start <= :when "
                "  AND period_end   >= :when "
                "  AND hours_paid > 0 "
                "ORDER BY period_end DESC LIMIT 1"
            ),
            {"tid": tech_id_str, "when": when_ts},
        ).first()
    except Exception:
        # payroll_entries table may not exist yet on this tenant
        log.debug("payroll_entries lookup failed; falling back to estimated", exc_info=True)
        true_row = None

    true_rate: Decimal | None = None
    period_id: str | None = None
    if true_row and true_row[1] and Decimal(str(true_row[1])) > 0:
        period_id = str(true_row[0])
        true_rate = Decimal(str(true_row[2] or 0)) / Decimal(str(true_row[1]))

    # 2) Estimated rate from technicians.hourly_rate.
    est_rate: Decimal | None = None
    try:
        est_row = tenant_db.execute(
            text(
                "SELECT hourly_rate FROM technicians "
                "WHERE (user_id = :tid OR id = :tid) AND deleted_at IS NULL "
                "LIMIT 1"
            ),
            {"tid": tech_id_str},
        ).first()
        if est_row and est_row[0] is not None:
            est_rate = Decimal(str(est_row[0]))
    except Exception:
        log.debug("technicians lookup failed", exc_info=True)

    true_cost = (h * true_rate) if true_rate is not None else None
    estimated_cost = (h * est_rate) if est_rate is not None else None
    if true_cost is not None:
        return LaborCost(true_cost, estimated_cost, "true", true_rate, period_id)
    if estimated_cost is not None:
        return LaborCost(None, estimated_cost, "estimated", est_rate, None)
    return LaborCost(None, None, "none", None, None)


def labor_cost_to_dict(lc: LaborCost) -> dict[str, Any]:
    return {
        "true_cost": float(lc.true_cost) if lc.true_cost is not None else None,
        "estimated_cost": float(lc.estimated_cost) if lc.estimated_cost is not None else None,
        "source": lc.source,
        "rate_used": float(lc.rate_used) if lc.rate_used is not None else None,
        "period_id": lc.period_id,
    }

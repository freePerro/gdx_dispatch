"""Estimate totals — single source of truth for subtotal/discount/tax/total.

Estimate.total stores the line subtotal (sum of EstimateLine.line_total).
Tax and discount are computed at render time using:
  - Estimate.tax_rate (per-estimate override; nullable decimal e.g. 0.0825)
  - Estimate.discount (per-estimate flat dollar amount; nullable)
  - gdx_dispatch.modules.tax.service.resolve_rate(db, customer_id) when no override
    (reads TaxConfig.default_rate, honors customer exemptions).
  - TaxConfig.tax_labor (when False, excludes lines marked category=='labor'
    from the taxable subtotal — most US states don't tax service labor).

Use this helper from every surface that shows a customer-facing total —
PDF, email body, mobile quoting — so a tenant changing their tax rate
in one place updates everywhere consistently.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, TypedDict

from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.modules.proposals.models import Estimate, EstimateLine
from gdx_dispatch.modules.tax.service import resolve_rate as _resolve_tax_rate


class EstimateTotals(TypedDict):
    subtotal: float
    discount: float
    labor_subtotal: float  # Sum of category=='labor' lines (display + audit)
    taxable_subtotal: float  # subtotal − labor_subtotal when tax_labor=False, else subtotal
    tax: float
    tax_rate: float        # decimal (0.0825)
    tax_rate_pct: float    # display (8.25)
    tax_labor: bool        # whether labor was included in tax base
    total: float


def _to_float(v: Any) -> float:
    return float(v or 0)


def _is_labor_line(line: EstimateLine) -> bool:
    """Match the category convention used by EstimateView.vue: dropdown sets
    "Labor" (title case); labor-matrix picks set "Labor". Compare lower-case
    so future case drift in either UI doesn't silently break the rule.
    """
    cat = (getattr(line, "category", None) or "").strip().lower()
    return cat == "labor"


def _load_tax_labor_flag(db: Session | None) -> bool:
    if db is None:
        return False
    try:
        # Avoid circular import — TaxConfig pulled at call time.
        from gdx_dispatch.modules.tax.models import TaxConfig

        cfg = db.execute(select(TaxConfig).limit(1)).scalar_one_or_none()
        if cfg is None:
            return False
        return bool(getattr(cfg, "tax_labor", False))
    except Exception:
        return False


def _load_lines(estimate: Estimate, db: Session | None) -> list[EstimateLine]:
    # Use already-loaded relationship if the caller eager-loaded it.
    loaded = getattr(estimate, "lines", None)
    if loaded is not None:
        try:
            return list(loaded)
        except Exception:
            pass
    if db is None:
        return []
    try:
        return list(
            db.execute(
                select(EstimateLine).where(EstimateLine.estimate_id == estimate.id)
            ).scalars()
        )
    except Exception:
        return []


def compute_estimate_totals(estimate: Estimate, db: Session | None) -> EstimateTotals:
    subtotal = _to_float(estimate.total)
    discount = _to_float(getattr(estimate, "discount", None))
    if estimate.tax_rate is not None:
        rate = _to_float(estimate.tax_rate)
    elif db is not None:
        try:
            rate = _to_float(_resolve_tax_rate(db, getattr(estimate, "customer_id", None)))
        except Exception:
            rate = 0.0
    else:
        rate = 0.0

    tax_labor = _load_tax_labor_flag(db)
    labor_subtotal = 0.0
    if not tax_labor:
        for line in _load_lines(estimate, db):
            if _is_labor_line(line):
                labor_subtotal += _to_float(getattr(line, "line_total", None))

    # taxable = (subtotal − labor) − discount, floored at 0. Labor is removed
    # BEFORE the discount so a labor-heavy estimate with a $50 discount still
    # gives the customer the discount on the materials.
    taxable_pre_discount = max(subtotal - labor_subtotal, 0.0)
    taxable = max(taxable_pre_discount - discount, 0.0)
    tax = round(taxable * rate, 2)
    total = round(max(subtotal - discount, 0.0) + tax, 2)
    return {
        "subtotal": subtotal,
        "discount": discount,
        "labor_subtotal": round(labor_subtotal, 2),
        "taxable_subtotal": round(taxable, 2),
        "tax": tax,
        "tax_rate": rate,
        "tax_rate_pct": round(rate * 100, 4),
        "tax_labor": tax_labor,
        "total": total,
    }


__all__ = ["EstimateTotals", "compute_estimate_totals"]

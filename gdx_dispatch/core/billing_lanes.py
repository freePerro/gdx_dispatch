"""Pricing lanes for invoice-from-closeout — plan §8.

Decided by Doug 2026-07-29, encoded here and nowhere else:

Precedence (the §15.1 correction — the estimate outranks everything):
    1. Accepted estimate  → the invoice copies the ESTIMATE's lines; nothing
       in this module runs. The price was agreed with the customer.
    2. job_type lane "service" (Service Call / Repair / Maintenance)
                          → hourly labor line, computed below.
    3. job_type lane "install" with a picked matrix item
                          → flat price (picker ships in a later piece; until
                            then installs without an estimate fall to 4).
    4. Everything else    → OFFICE lane: labor goes UNPRICED. The §11
       verification gate is the flag — the invoice cannot reach a customer
       until the office reviewed it, so an unpriced-labor invoice is caught
       there, never silently $0-lined to a customer (a $0 line on a PDF the
       customer reads is worse than a flagged draft).

Service-lane math (§11/§15 decisions, ORDER IS LOAD-BEARING):
    man_hours = roundup_to_half(hours_worked) × techs_on_site
    man_hours = max(1.0, man_hours)                # first-hour minimum
    amount    = first_hour_price + hourly_rate × (man_hours − 1)

    Round FIRST, floor SECOND: 0.25h → 0.5 → floor 1.0 → $100.
    Both settings are $100 today so this collapses to man_hours × 100, but
    they are two columns (pricing_settings) so "first hour $125 then $100"
    is a settings change, not a code change.

BILLED ≠ ATTESTED, permanently: rounding and the floor produce the CUSTOMER
quantity on the invoice line; `hours_worked` and the labor time_entry keep
the exact attested figure for payroll and costing. Pricing policy may round
a bill; nothing may rewrite an attestation.
"""
from __future__ import annotations

import math
import uuid as _uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.job_taxonomy import pricing_lane
from gdx_dispatch.models.pricing_engine import PricingSettings


@dataclass(frozen=True)
class ServiceLaborLine:
    description: str
    quantity: int          # always 1 — the math lives in the description
    unit_price: Decimal    # == line_total
    line_total: Decimal
    billed_man_hours: float
    attested_hours: float
    techs_on_site: int
    first_hour_price: Decimal
    hourly_rate: Decimal


def _money(v: float | Decimal) -> Decimal:
    return Decimal(str(v)).quantize(Decimal("0.01"))


def _as_uuid(v: str) -> _uuid.UUID:
    return v if isinstance(v, _uuid.UUID) else _uuid.UUID(str(v))


def roundup_to_half(hours: float) -> float:
    """2.10 → 2.5; 3.00 → 3.0; 0.25 → 0.5. Never rounds down."""
    return math.ceil(float(hours) * 2) / 2


def service_rates(db: Session) -> tuple[Decimal, Decimal]:
    """(first_hour_price, hourly_rate) from pricing_settings; $100/$100 when
    unset — the same number the tenant's target blended rate already carries
    ("Per Doug rule: GDX targets $100/hr blended")."""
    settings = db.execute(select(PricingSettings).limit(1)).scalar_one_or_none()
    first = getattr(settings, "service_call_first_hour_price", None) if settings else None
    hourly = getattr(settings, "service_call_hourly_rate", None) if settings else None
    return (
        _money(first if first is not None else 100),
        _money(hourly if hourly is not None else 100),
    )


def billed_man_hours(hours_worked: float, techs_on_site: int) -> float:
    """Round to the next half hour FIRST, multiply by crew, THEN apply the
    one-hour minimum. The order is a decided rule — flooring before rounding
    would bill 0.75h jobs differently than the worked examples Doug approved."""
    rounded = roundup_to_half(hours_worked)
    man = rounded * max(1, int(techs_on_site or 1))
    return max(1.0, man)


# The auto-filled line text (Doug 2026-08-07: the FIELD was editable but
# what it fills in was not). Tenants override it via
# pricing_settings.service_labor_description_template (migration 060);
# these are the supported placeholders. A broken template falls back here —
# a settings typo must never 500 a closeout, an autodraft, or an invoice.
DEFAULT_SERVICE_LABOR_TEMPLATE = (
    "Service labor — {man_hours:.2f} man-hours"
    " ({hours:.2f} h on site × {techs} tech{tech_plural};"
    " first hour ${first_hour_price}, then ${hourly_rate}/hr)"
)


def service_labor_description_template(db: Session) -> str:
    settings = db.execute(select(PricingSettings).limit(1)).scalar_one_or_none()
    tpl = getattr(settings, "service_labor_description_template", None) if settings else None
    return tpl.strip() if tpl and tpl.strip() else DEFAULT_SERVICE_LABOR_TEMPLATE


def service_labor_line(
    db: Session, *, hours_worked: float, techs_on_site: int
) -> ServiceLaborLine:
    first, hourly = service_rates(db)
    man = billed_man_hours(hours_worked, techs_on_site)
    amount = _money(first + hourly * Decimal(str(man - 1.0)))
    crew = max(1, int(techs_on_site or 1))
    _vars = {
        "man_hours": man,
        "hours": float(hours_worked),
        "techs": crew,
        "tech_plural": "s" if crew != 1 else "",
        "first_hour_price": first,
        "hourly_rate": hourly,
    }
    tpl = service_labor_description_template(db)
    try:
        desc = tpl.format(**_vars)
    except Exception:  # noqa: BLE001 — any format error → safe default
        import logging  # noqa: PLC0415

        logging.getLogger(__name__).warning(
            "service_labor_template_invalid, falling back to default: %r", tpl
        )
        desc = DEFAULT_SERVICE_LABOR_TEMPLATE.format(**_vars)
    return ServiceLaborLine(
        description=desc[:500],
        quantity=1,
        unit_price=amount,
        line_total=amount,
        billed_man_hours=man,
        attested_hours=float(hours_worked),
        techs_on_site=crew,
        first_hour_price=first,
        hourly_rate=hourly,
    )


@dataclass(frozen=True)
class InstallLaborLine:
    description: str
    quantity: int
    unit_price: Decimal
    line_total: Decimal
    matrix_item_id: str


def install_labor_line(db: Session, item_id: str) -> InstallLaborLine | None:
    """Flat install price from the picked labor-matrix row. Reads flat_price
    LIVE (the row may have been repriced since closeout — the bill reflects
    the current book, and the office verification gate is the backstop).
    Returns None if the row is gone/inactive → caller falls to office-priced,
    never guesses."""
    from gdx_dispatch.models.labor_pricing import LaborPriceItem

    try:
        item = db.execute(
            select(LaborPriceItem).where(LaborPriceItem.id == _as_uuid(item_id))
        ).scalar_one_or_none()
    except (ValueError, AttributeError):
        return None
    if item is None or not getattr(item, "active", True):
        return None
    # A row retired by date is not billable even if active still reads True
    # (LaborPriceItem documents supersede-by-effective_to). Audit round 2.
    eff_to = getattr(item, "effective_to", None)
    if eff_to is not None:
        import datetime as _dt

        if eff_to < _dt.date.today():
            return None
    price = _money(item.flat_price or 0)
    if price <= 0:
        return None  # a $0 matrix row is not a customer line (F-75)
    return InstallLaborLine(
        description=f"Install — {item.description}"[:500],
        quantity=1,
        unit_price=price,
        line_total=price,
        matrix_item_id=str(item.id),
    )


def lane_for_job(job_type: str | None) -> str:
    """Thin alias so callers import one module for §8 decisions."""
    return pricing_lane(job_type)

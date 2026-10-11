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

Service-lane math (§11/§15 decisions, ORDER IS LOAD-BEARING), extended for
multi-day jobs (D15, D16):
    final_day = roundup_to_half(hours_worked) × techs_on_site   (0 without one)
    day_rows  = Σ unbilled day-row minutes, rounded up to the half hour ONCE
    man_hours = final_day + day_rows
    man_hours = max(1.0 - elsewhere, man_hours)   # floor, only when the first hour applies

    Round FIRST, floor SECOND: 0.25h → 0.5 → floor 1.0 → $100.
    The first-hour price and the floor apply once per JOB: not when another
    live invoice on the job already CARRIES them — an attested line priced
    at anything but the hourly rate (the "first hour" line, or a pre-#926
    single line), attested lines from before the rates last changed
    summing to ≥ 1 h, or, at equal rates, attested hours summing to ≥ 1 h.
    Hours on a sibling that does not carry it only shrink the floor, so the
    job as a whole bills at least 1 h, never more (GDXA-489: a void makes
    the next build take the first hour back; no sibling invoice is ever
    re-priced).

The line says what it bills: quantity = hours, unit price = the rate.
    rates equal, or first hour already charged → one "Service labor" line,
        quantity = man_hours at the hourly rate
    rates differ → "Service labor — first hour" 1 × first-hour price, then
        "Service labor" (man_hours − 1) × hourly rate, omitted at 0
    Either way the total is first + hourly × (man_hours − 1), as before.

BILLED ≠ ATTESTED, permanently: rounding and the floor produce the CUSTOMER
quantity on the invoice line; `hours_worked` and the labor time_entry keep
the exact attested figure for payroll and costing. Pricing policy may round
a bill; nothing may rewrite an attestation.
"""
from __future__ import annotations

import datetime as _dt
import math
import uuid as _uuid
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from gdx_dispatch.core.job_taxonomy import pricing_lane
from gdx_dispatch.models.pricing_engine import PricingSettings


def _money(v: float | Decimal) -> Decimal:
    # HALF_UP, as routers/invoices.py rounds: the default HALF_EVEN would bill
    # 2.5 × $33.33 = 83.325 as $83.32.
    return Decimal(str(v)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _utc(dt: _dt.datetime | None) -> _dt.datetime | None:
    # SQLite hands back naive datetimes; every stored one is UTC.
    if dt is None or dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=_dt.timezone.utc)


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


FIRST_HOUR_DESCRIPTION = "Service labor — first hour"


def _render_service_description(db: Session, _vars: dict) -> str:
    tpl = service_labor_description_template(db)
    try:
        desc = tpl.format(**_vars)
    except Exception:  # noqa: BLE001 — any format error → safe default
        import logging  # noqa: PLC0415

        logging.getLogger(__name__).warning(
            "service_labor_template_invalid, falling back to default: %r", tpl
        )
        desc = DEFAULT_SERVICE_LABOR_TEMPLATE.format(**_vars)
    return desc


def first_hour_elsewhere(db: Session, job_id, *, invoice_id=None) -> tuple[bool, Decimal]:
    """``(charged, hours)`` from the job's OTHER live, non-void invoices and
    their live ``labor_attested`` lines (lines that came from attested hours).

    ``charged``: the first hour was billed there and must not be again. A
    line priced on today's rates says so by its text or its price: the
    ``FIRST_HOUR_DESCRIPTION`` line, or any line NOT at exactly the hourly
    rate (a pre-#926 single line with the premium folded in, migration 110;
    a carrier whose text staff edited). At equal rates the floor is all the
    first hour is, so ``hours`` >= 1 carries it.
    An invoice created before ``pricing_settings`` last changed was priced
    on rates we no longer know, so its price says nothing (a carrier built
    at $100/$100 bills $100/hr; an hourly-only sibling built at $125/$100
    bills $100, which is not $110 once the hourly rate moves). Those
    invoices carry it when their hours SUM to >= 1 h, the floor every build
    under the old rates kept for the job; a first hour split across two
    short invoices by the shared floor is still one first hour. Any save
    moves that stamp. A wrong call here costs an under-bill, never a second
    premium.
    Any other sibling whose lines all bill the hourly rate is hourly-only
    and does not carry the first hour: it was priced beside a carrier that
    may since have been voided, deleted or stripped (GDXA-489), and counting
    it would lose the first-hour price for every later build too.

    ``hours``: their attested quantity, which shrinks this invoice's floor
    (when nothing carries) so the job bills at least 1 h and never more.

    ``invoice_id`` is the invoice asking; it is never "another" invoice.
    """
    from gdx_dispatch.models.tenant_models import Invoice, InvoiceLine  # noqa: PLC0415

    try:
        jid = _as_uuid(job_id)
    except (ValueError, AttributeError, TypeError):
        return False, Decimal(0)
    q = (
        select(InvoiceLine.description, InvoiceLine.quantity, InvoiceLine.unit_price)
        .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
        .where(
            Invoice.job_id == jid,
            Invoice.deleted_at.is_(None),
            or_(Invoice.status.is_(None), Invoice.status != "void"),
            InvoiceLine.deleted_at.is_(None),
            InvoiceLine.pricing_source == "labor_attested",
        )
    )
    if invoice_id is not None:
        q = q.where(Invoice.id != _as_uuid(invoice_id))
    rows = db.execute(q.add_columns(Invoice.created_at)).all()
    if not rows:
        return False, Decimal(0)
    first, hourly = service_rates(db)
    rates_since = _utc(db.execute(select(PricingSettings.updated_at).limit(1)).scalar())

    hours = Decimal(0)
    older_hours = Decimal(0)
    carried = False
    for desc, qty, unit, created in rows:
        q_h = Decimal(str(qty or 0))
        hours += q_h
        if (desc or "") == FIRST_HOUR_DESCRIPTION:
            carried = True
        elif rates_since is not None and (created is None or _utc(created) < rates_since):
            older_hours += q_h
        elif _money(unit or 0) != hourly:
            carried = True
    carried = carried or older_hours >= 1 or (first == hourly and hours >= 1)
    return carried, hours


def first_hour_charged_elsewhere(db: Session, job_id, *, invoice_id=None) -> bool:
    """True when another live invoice on the job carries the first hour; see
    ``first_hour_elsewhere``."""
    return first_hour_elsewhere(db, job_id, invoice_id=invoice_id)[0]


def job_labor_lines(
    db: Session,
    job,
    closeout,
    *,
    invoice_id=None,
    include_day_rows: bool = True,
    day_rows: list | None = None,
) -> list[dict]:
    """The service-lane labor lines for a job: one line, or a first-hour line
    and an hourly line. Empty when the job is not service-lane or has no
    attested man-hours (nothing invents hours).

    ``job`` is a ``Job``. ``closeout`` is its current closeout or None.

    ``invoice_id`` is the invoice asking: its own claimed day rows count as
    unbilled for it, and it is excluded from the first-hour test.

    ``include_day_rows=False`` leaves the day rows out (the suggestion offers
    them only on a completed job). ``day_rows`` overrides the lookup with the
    rows the caller actually claimed, as ``(id, clock_in, minutes)``.

    Each dict: description, quantity, unit_price, line_total (Decimal),
    source "attested", labor_price_item_id None, man_hours (raw attested,
    float), and on exactly one line — the hourly line, or the first-hour line
    when it stands alone — ``time_entry_ids`` (list of str, possibly empty)
    and ``estimated_man_hours`` (Decimal, the raw attested man-hours).
    """
    from gdx_dispatch.core.closeout_billing import unbilled_day_rows  # noqa: PLC0415

    if lane_for_job(getattr(job, "job_type", None)) != "service":
        return []

    final_hours = Decimal(str(getattr(closeout, "hours_worked", 0) or 0)) if closeout else Decimal(0)
    techs = max(1, int(getattr(closeout, "techs_on_site", 1) or 1)) if closeout else 1
    final_man = (
        Decimal(str(roundup_to_half(final_hours))) * techs if final_hours > 0 else Decimal(0)
    )

    if day_rows is None:
        day_rows = unbilled_day_rows(db, job.id, invoice_id=invoice_id) if include_day_rows else []
    minutes = sum(int(m) for _id, _c, m in day_rows)
    # Rounded up to the half hour ONCE, in exact integer arithmetic: a float
    # 7.2 * 2 is not guaranteed to ceil to 15.
    rows_man = Decimal(-(-minutes // 30)) / 2 if minutes > 0 else Decimal(0)

    man = final_man + rows_man
    if man <= 0:
        return []

    first, hourly = service_rates(db)
    charged, hours_elsewhere = first_hour_elsewhere(db, job.id, invoice_id=invoice_id)
    first_applies = not charged
    if first_applies:
        # Topped up to 1 h for the JOB: hours already billed on a sibling
        # that does not carry the first hour count toward the floor (clamped:
        # a negative attested line never lifts the floor past 1 h).
        man = max(Decimal(1) - min(Decimal(1), max(Decimal(0), hours_elsewhere)), man)
    man = man.quantize(Decimal("0.01"))

    raw_man = (final_hours * techs + Decimal(minutes) / 60).quantize(Decimal("0.01"))

    # The days billed: each priced day row's shop day, plus the closeout's
    # own day when it attests hours. Without day rows it is one day, and the
    # shop's time zone is not read at all.
    days = 1
    if day_rows:
        from gdx_dispatch.core.pay_periods import (  # noqa: PLC0415
            shop_day_of,
            shop_tz_name_from_settings,
        )

        tz_name = shop_tz_name_from_settings(db)
        days_set = {shop_day_of(c, tz_name) for _id, c, _m in day_rows}
        if final_hours > 0 and closeout is not None and getattr(closeout, "closed_at", None):
            days_set.add(shop_day_of(closeout.closed_at, tz_name))
        days_set.discard(None)
        days = max(1, len(days_set))

    over = f" over {days} days" if days > 1 else ""

    def _line(description: str, qty: Decimal, unit: Decimal) -> dict:
        return {
            "description": description[:500],
            "quantity": qty,
            "unit_price": unit,
            "line_total": _money(qty * unit),
            "source": "attested",
            "labor_price_item_id": None,
            "man_hours": float(raw_man),
        }

    # The tenant's template describes one day's crew ({hours} on site ×
    # {techs}) and both rates on a single line, so it is true only for that
    # case: no day rows, one line, the first hour charged here. Every other
    # line gets a text that states its own quantity and rate, so a customer
    # never reads hours or a first-hour price the line does not bill (D16).
    lines: list[dict] = []
    if not first_applies or first == hourly:
        if first_applies and not day_rows and not hours_elsewhere:
            desc = _render_service_description(db, {
                "man_hours": float(man),
                "hours": float(final_hours),
                "techs": techs,
                "tech_plural": "s" if techs != 1 else "",
                "first_hour_price": first,
                "hourly_rate": hourly,
            })
        else:
            # No word on where the first hour went: rule 3 reads the job's
            # other live invoices at build time, and a later void can make any
            # such sentence false on a line already sent.
            desc = f"Service labor — {man:.2f} man-hours at ${hourly}/hr{over}"
        lines.append(_line(desc, man, hourly))
    else:
        # Below 1 h only when a sibling holds the rest of the floor.
        lines.append(_line(FIRST_HOUR_DESCRIPTION, min(Decimal("1.00"), man), first))
        rest = man - 1
        if rest > 0:
            lines.append(_line(
                f"Service labor — {rest:.2f} h after the first hour at ${hourly}/hr"
                f" ({man:.2f} man-hours{over})",
                rest, hourly,
            ))
    carrier = lines[-1]
    carrier["time_entry_ids"] = [str(_id) for _id, _c, _m in day_rows]
    carrier["estimated_man_hours"] = raw_man
    return lines


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

    # Deliberately NOT `contained_read`-wrapped — GDXA-160 traced this one out
    # and it is a false positive for the swallowed-read class, by the check the
    # parent issue asks for (follow the DB error to the frame that CATCHES it,
    # do not stop at this `except`). The tuple here is for `_as_uuid` on a
    # malformed `item_id`; `ProgrammingError`/`OperationalError` propagate. All
    # three callers were read:
    #   * `closeout_billing.autodraft_invoice_for_closeout` → `closeout_job`,
    #     where the call is already inside `db.begin_nested()` with the flush
    #     done first, so a failure rolls back to the savepoint and the tech's
    #     closeout survives — the containment is there, one frame up.
    #   * `routers/jobs.py`'s read-only `closeout_billing_suggestion`: the error
    #     reaches the route and 500s, with no pending work behind it.
    #   * `routers/mobile_invoicing.py::mobile_create_invoice`, which DOES hold
    #     pending work — the tech's Invoice is `db.add`ed well before this and
    #     not committed until the end of the handler. Be exact: that work is
    #     lost, but losing it is CORRECT, because nothing swallows here. The
    #     error reaches the route, the request fails, and the session closes
    #     without committing a half-built invoice. That is the difference
    #     between this site and the class — a swallow would have returned a
    #     degraded line and then lost the invoice while reporting success.
    # Nothing here swallows a DB error, so there is nothing to contain.
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
        from gdx_dispatch.core.pay_periods import shop_today_from_settings  # noqa: PLC0415

        # The shop's day: a row retired today stays billable until the
        # shop's midnight, not UTC's (GDXA-505).
        if eff_to < shop_today_from_settings(db):
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

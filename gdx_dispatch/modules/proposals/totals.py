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

from decimal import ROUND_HALF_UP, Decimal
from typing import Any, TypedDict

from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import contained_read
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
    """Whether this tenant taxes labor. Degrades to False (do NOT tax labor).

    GDXA-157: the read is SAVEPOINT-contained because ``db`` belongs to the
    CALLER — the public proposal page, the accept flow, the invoice seed — and
    on Postgres a failed statement aborts the whole transaction. Swallowing it
    bare answered "False" while leaving the caller's uncommitted estimate or
    invoice unsavable. What containment does NOT do is make the degraded answer
    right: see ``_load_lines`` for the half of that which is a money bug rather
    than a transaction one.
    """
    if db is None:
        return False
    try:
        # Avoid circular import — TaxConfig pulled at call time.
        from gdx_dispatch.modules.tax.models import TaxConfig

        with contained_read(db):
            cfg = db.execute(select(TaxConfig).limit(1)).scalar_one_or_none()
        if cfg is None:
            return False
        return bool(getattr(cfg, "tax_labor", False))
    except Exception:
        return False


def _lines_would_query(estimate: Estimate) -> bool:
    """True when reading ``estimate.lines`` will actually emit SQL.

    Unloaded (or expired) relationship → yes. Eager-loaded, or a non-ORM
    stand-in, → no. Used to keep the SAVEPOINT below off the free path; see the
    comment at its call site for why that is worth a helper.
    """
    state = sa_inspect(estimate, raiseerr=False)
    if state is None:  # not an ORM instance at all
        return False
    return "lines" in state.unloaded


def _load_lines(estimate: Estimate, db: Session | None) -> list:
    # Accepted TIER (2026-08-14): est.total is the tier's contract subtotal,
    # so the labor-exclusion base must be the TIER's lines — never the base
    # estimate lines (they aren't in the subtotal) and never other tiers'.
    # A flat tier has no lines → no exclusion → the package taxes in full,
    # which is exactly how its synthesized invoice line taxes. This branch
    # runs BEFORE the relationship shortcut on purpose: estimate.lines holds
    # the base lines even when a tier is accepted.
    if getattr(estimate, "accepted_tier_id", None) is not None and db is not None:
        # No try/except here ON PURPOSE (gate-audit catch): the table is
        # guaranteed by create_orm_tables at boot, and swallowing a real DB
        # error would silently tax the labor in a money path (and leave a
        # poisoned PG transaction behind). Loud beats wrong.
        #
        # GDXA-157 adds a SAVEPOINT and NO handler, which keeps "loud" exactly as
        # it is — `contained_read` re-raises — while making the parenthesis above
        # true. It is not optional decoration: a caller ABOVE this one can have a
        # bare `except` (`modules/deposits/service.py`'s `deposit_ask_for` does),
        # and an aborted transaction reaching that swallow leaves any savepoint it
        # sits inside unreleasable — `RELEASE SAVEPOINT` on an aborted PG
        # transaction is itself an error. Containing here is what lets a caller
        # wrap this function at all.
        from gdx_dispatch.modules.proposals.models import ProposalTierLine

        with contained_read(db):
            return list(
                db.execute(
                    select(ProposalTierLine).where(
                        ProposalTierLine.tier_id == estimate.accepted_tier_id
                    )
                ).scalars()
            )
    # Use already-loaded relationship if the caller eager-loaded it. When it is a
    # LAZY relationship this getattr emits the SELECT, so it is contained for the
    # same reason as the tier read above — and it raises for the same reason too,
    # since a lines list that cannot be read must not become a silent tax base.
    #
    # Only when it will ACTUALLY emit SQL, though. A `selectinload`ed estimate
    # reads the attribute for free, and an unconditional savepoint here would pay
    # SAVEPOINT+RELEASE for nothing. The real beneficiaries, checked rather than
    # assumed — an audit caught the first version of this comment naming a caller
    # that does NOT eager-load: `routers/pdf.py::estimate_pdf`,
    # `routers/invoices.py::create_invoice` (the invoice seed, a money path) and
    # `routers/estimates.py`'s `_get_estimate_or_404` and
    # `estimates_pipeline_summary` all pass
    # `options(selectinload(Estimate.lines))`, so on those the gate is the
    # difference between 0 and 2 statements per estimate.
    #
    # `routers/portal.py::_portal_estimate_totals` is the opposite case and worth naming as a cost
    # rather than hiding: it does NOT eager-load, so the gate fires and that
    # customer-facing LIST pays 3 savepoint pairs per estimate (rate, tax_labor,
    # lines) for containment its GET cannot use. Adding `selectinload` there would
    # fix both that and its own N+1, but `routers/portal.py` is customers-crm's
    # file — reported, not reached into.
    if db is not None and _lines_would_query(estimate):
        with contained_read(db):
            loaded = getattr(estimate, "lines", None)
    else:
        loaded = getattr(estimate, "lines", None)
    if loaded is not None:
        try:
            return list(loaded)
        except Exception:
            pass
    if db is None:
        return []
    # GDXA-157: contained for the caller's transaction, NOT for the number.
    # Be blunt about the limit — the degraded `[]` below means "no labor lines",
    # so the labor-exclusion is skipped and labor is taxed IN FULL. On a
    # Minnesota garage-door contract that is the one outcome that must never
    # happen. Containment stops a failed read here from also destroying the
    # caller's uncommitted work; it does not make `[]` an honest answer, and a
    # read that CANNOT tell labor from materials has no business returning a
    # tax base. Fixing that means raising instead of degrading, which is a
    # money-behaviour change and wants its own decision (the tier branch above
    # already went that way, deliberately, for exactly this reason).
    try:
        with contained_read(db):
            return list(
                db.execute(
                    select(EstimateLine).where(EstimateLine.estimate_id == estimate.id)
                ).scalars()
            )
    except Exception:
        return []


def compute_estimate_totals(estimate: Estimate, db: Session | None) -> EstimateTotals:
    subtotal = _to_float(estimate.total)
    # Accepted TIER (2026-08-14): derive the subtotal from the tier itself,
    # not the stored est.total. The accept paths write est.total = the tier's
    # contract subtotal, but rows accepted BEFORE that fix (self-hosted
    # installs; GDX prod verified zero) still carry the base-lines sum — and
    # this engine is what the public page and the invoice seed display, so it
    # must be true regardless of when the row was accepted.
    if getattr(estimate, "accepted_tier_id", None) is not None and db is not None:
        from gdx_dispatch.modules.proposals.models import ProposalTier
        from gdx_dispatch.modules.proposals.service import tier_contract_subtotal

        # GDXA-157: SAVEPOINT, no handler — raises exactly as before, but a caller
        # with a bare `except` above it no longer inherits an aborted transaction.
        # Same reasoning as the tier branch in `_load_lines`.
        with contained_read(db):
            _tier = db.execute(
                select(ProposalTier).where(ProposalTier.id == estimate.accepted_tier_id)
            ).scalar_one_or_none()
            if _tier is not None:
                # Inside the block: `tier_contract_subtotal` reads the tier's
                # lines on this same session, so leaving it out would leave the
                # one read most likely to fail uncontained.
                subtotal = _to_float(tier_contract_subtotal(db, _tier))
    discount = _to_float(getattr(estimate, "discount", None))
    if estimate.tax_rate is not None:
        rate = _to_float(estimate.tax_rate)
    elif db is not None:
        # GDXA-157: `resolve_rate` reads TaxConfig (and the customer's exempt
        # flag) on the CALLER's session and re-raises, so the swallow — and the
        # aborted Postgres transaction behind it — is this frame's. Containment
        # is also what keeps the two reads BELOW independent of this one: bare,
        # a failed rate read poisoned the session, and then `_load_lines`'s lazy
        # `estimate.lines` load failed too, so the whole totals block vanished
        # from the public proposal page. Contained, the customer still sees a
        # correct subtotal and total with tax 0.
        try:
            with contained_read(db):
                rate = _to_float(
                    _resolve_tax_rate(db, getattr(estimate, "customer_id", None))
                )
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
    # M21: float round() here vs the invoice's Decimal ROUND_HALF_UP meant
    # taxable $36.25 at 10% gave $3.62 on the estimate and $3.63 on the
    # invoice — the customer accepts one number and is billed another.
    # Same convention as invoices: Decimal, half-up, at cents.
    tax = float(
        (Decimal(str(taxable)) * Decimal(str(rate))).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    )
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

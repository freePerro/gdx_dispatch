"""How an invoice line was priced, recorded next to the number.

The gap this closes, measured on the live tenant 2026-08-29:

    invoice_lines            832 rows
      unit_price             832
      cost_snapshot           63     <- all 63 have unit_price > 0
      margin_pct_snapshot      0     <- every one of the 63 was derivable

So the amount was always kept and the *reason* never was. Meanwhile
`estimate_lines` gets this right — 298 of 336 carry cost + margin + a
`pricing_source` naming the lane — and only 9 of 365 invoices are created from
an estimate, so ~98% of billed lines had no pricing record at all.

**This module records. It never prices.** There is no code path here that
produces a `unit_price`, reads a catalog, or calls the pricing engine. On the
invoice side the client is the pricing authority (see `LineItemEditor.vue`), and
changing that is a different, riskier change. Everything below describes a
number that was already chosen.

Two rules do the work:

* **A cost that is unknown yields NULL.** There is no branch that invents one.
  A guessed provenance on money data is worse than an honest blank — the same
  reasoning migration 075 recorded when it refused to backfill.
* **A cost that is meaningless is unwriteable.** Discounts, deposit lines and
  imported documents are `COST_FORBIDDEN`: passing `cost_snapshot=0` there
  raises, because `(price - 0) / price = 1.0` would mint a fake 100%-margin row
  in every profit report.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import get_args

from gdx_dispatch.models.tenant_models import InvoiceLine
from gdx_dispatch.services.pricing_engine import PricingSource


def derive_margin_pct(
    cost: Decimal | float | None, unit_price: Decimal | float | None
) -> Decimal | None:
    """Back-derive a margin_pct_snapshot from cost + unit_price.

    Moved here verbatim from ``routers/estimates.py`` so the estimate and
    invoice sides cannot drift into two different formulas for the same money
    question. ``routers/estimates.py`` re-imports it under its old private name.

    Returns None when either value is missing/<=0 (a genuine free-form/manual
    line), or when the result would not fit the Numeric(6,4) snapshot column —
    a price far below cost (a fat-fingered $5 on a $1000 cost derives -199.0)
    would raise DataError on Postgres, and SQLite CI ignores Numeric precision,
    so the guard has to live here rather than be discovered in production.
    """
    if cost is None or unit_price is None:
        return None
    c = Decimal(str(cost))
    u = Decimal(str(unit_price))
    if u <= 0 or c < 0:
        return None
    derived = ((u - c) / u).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    if derived <= Decimal("-99.9999"):
        return None
    return derived


# The lanes a line may claim. Free text rather than a DB CHECK for the reason
# migration 075 already recorded: a CHECK needs a migration every time a lane is
# added, and the writers are the contract. Validated here so a typo cannot
# silently truncate into VARCHAR(32) and become a lane nobody can query for.
# Every lane the ENGINE can stamp, taken from its own Literal rather than
# transcribed. Transcribing it is how `customer_override` went missing: it is a
# real PricingSource (set a customer's margin_override_pct), it reaches
# EstimateLine.pricing_source, and the estimate->invoice copy would then have
# raised ValueError inside a request handler — a 500 on a path that worked
# before. Deriving the set means a new engine lane can never 500 this module.
_ENGINE_LANES = frozenset(get_args(PricingSource))

PRICING_SOURCES = _ENGINE_LANES | frozenset({
    # forwarded verbatim from estimate_lines.pricing_source
    "tier", "labor_matrix", "line_override", "client_cost", "wholesale_tier",
    # forwarded verbatim from job_parts_needed.price_source (core.part_pricing)
    "office", "job_quote", "inventory", "catalog", "catalog_cost", "chi", "chi_cost",
    # invoice-side lanes with no upstream tag
    "manual",             # a human typed the price on an invoice form
    "estimate_copy",      # copied from an estimate line that recorded no lane
    "accepted_tier",      # copied off a signed good/better/best tier's lines
    "tier_package",       # ONE synthesised line at a flat tier's total_price
    "estimate_discount",  # Estimate.discount, materialised as a negative line
    "operator_discount",  # a discount typed at billing time, not on the quote
    "change_order",       # copied from a signed ChangeOrderLine
    "co_amount",          # synthesised from a lump-sum signed CO amount
    "labor_attested",     # attested hours x the tenant's configured rates
    "part_row_legacy",    # a JobPartNeeded captured before migration 075
    "deposit_schedule", "deposit_credit", "qb_import",
})

# Lanes where a cost is definitionally meaningless: price concessions, payment
# schedule artefacts, and documents another system priced.
#
# Deliberately NOT including change_order / co_amount / tier_package /
# accepted_tier. Those carry no cost *today* only because the upstream model has
# no column for one; banning it there would be a landmine the day ChangeOrderLine
# gains cost.
COST_FORBIDDEN = frozenset({
    "estimate_discount", "operator_discount",
    "deposit_schedule", "deposit_credit", "qb_import",
})


def _money(value) -> Decimal | None:
    return None if value is None else Decimal(str(value)).quantize(Decimal("0.01"))


def _rate(value) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def build_invoice_line(
    *,
    pricing_source: str,
    cost_snapshot: Decimal | float | None = None,
    margin_pct_snapshot: Decimal | float | None = None,
    margin_pct_override: Decimal | float | None = None,
    derive_margin: bool = True,
    pricing_inputs: dict | None = None,
    **fields,
) -> InvoiceLine:
    """Construct an InvoiceLine that carries its own pricing provenance.

    `pricing_source` is keyword-only with no default on purpose: a new write
    path that forgets it fails immediately and loudly, rather than adding
    another silently unprovenanced money row.

    Returns an UNATTACHED instance — no session, no add, no flush. Callers keep
    doing `db.add(...)` exactly as before.

    `source` is deliberately NOT a parameter. It is a different axis
    (authorship, read by `closeout_billing.is_untouched_autodraft`); whatever a
    caller puts in `**fields` passes through untouched, and nothing here reads,
    writes, defaults or infers it.
    """
    if not isinstance(pricing_source, str) or pricing_source not in PRICING_SOURCES:
        raise ValueError(
            f"unknown pricing_source {pricing_source!r} — add it to "
            "core.pricing_provenance.PRICING_SOURCES with a comment saying what "
            "lane it names"
        )

    cost = _money(cost_snapshot)

    if pricing_source in COST_FORBIDDEN and (
        cost is not None or margin_pct_snapshot is not None
    ):
        raise ValueError(
            f"{pricing_source!r} lines have no cost: a discount, a deposit "
            "artefact or an imported document was never priced by us. "
            "cost_snapshot=0 here would derive a 100% margin and poison every "
            "profit report."
        )

    if margin_pct_snapshot is not None:
        margin = _rate(margin_pct_snapshot)
    elif derive_margin and cost is not None:
        if "unit_price" not in fields:
            raise ValueError("cannot derive a margin without unit_price")
        margin = derive_margin_pct(cost, fields["unit_price"])
    else:
        margin = None

    return InvoiceLine(
        pricing_source=pricing_source,
        cost_snapshot=cost,
        margin_pct_snapshot=margin,
        margin_pct_override=_rate(margin_pct_override),
        pricing_inputs=pricing_inputs,
        **fields,
    )

"""The ONE sell-price resolver for a part a tech recorded on a job.

Why this exists
---------------
``billing-capture-hardening-plan.md`` PR 4 specified the capture step as
"``unit_price`` from ``Part.unit_price`` when ``part_id`` resolves (**catalog
sell price** — NOT the closeout ``unit_cost``), else NULL". The shipped code
reads only bench inventory (``modules/inventory.Part``) — but a part picked
from a catalog is *structurally guaranteed* to have no ``part_id``:
``job_parts.part_id`` is an FK to ``parts.id``, so ``sku_suggest`` returns
catalog rows as ``source='catalog'`` with ``part_id=None`` on purpose
(``routers/parts_needed.py``). In this tenant the real parts live in
``custom_catalog_items``, so the pricing path was unreachable for exactly the
rows it was written to cover, and every catalog-picked part was captured NULL
and silently dropped from the invoice.

The two rules that shape every tier below
-----------------------------------------
1. **Never invent a price.** No match, or an ambiguous one, returns ``None``
   and the office prices it. ``None`` is a supported, documented state
   (``JobPartNeeded.unit_price``: "NULL = office prices it"), not a failure.
2. **Never bill at cost.** 1,493 of 1,843 priced ``custom_catalog_items`` rows
   carry ``price == cost`` — QuickBooks imports where the price column was
   filled with the cost. Taking those at face value bills a $2,207 door at
   zero margin. A catalog ``price`` is only believed when it is actually above
   cost; otherwise the cost goes through the tenant's own margin engine, which
   ``services/pricing_engine`` already declares to be the single source of
   truth for cost→sell math.

Matching is **exact SKU only**. The 2026-07-07 AUDIT-R1 review ruled fuzzy
sku/name matching an undercount generator and that ruling stands.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

__all__ = ["resolve_sell_price"]


def _money(v) -> Decimal:
    return Decimal(str(v)).quantize(Decimal("0.01"))


def _norm(sku: str | None) -> str:
    return (sku or "").strip().lower()


class _PriceCost:
    """Minimal (price, cost) pair so one believability rule serves every
    source — tenant catalog rows, CHI rows, and callers holding a row."""

    __slots__ = ("price", "cost")

    def __init__(self, price, cost):
        self.price = price
        self.cost = cost


def _believable_sell(item) -> Decimal | None:
    """The row's ``price``, but only when it can be believed as a SELL price.

    1,493 of 1,843 priced catalog rows in production carry ``price == cost``:
    QuickBooks imports where the price column was filled with the cost.
    Billing those at face value is a zero-margin sale on every door.

    - ``price > cost``            → a real sell price.
    - ``price`` with no cost recorded → believed. Nothing marks it as an
      import, and refusing it would silently unprice legitimately-priced
      items that simply never had a cost entered.
    - ``price <= cost``          → not believed. Covers the import shape and
      a stale cost above a real price; both are for a human to settle.
    """
    price = item.price
    if price is None or Decimal(str(price)) <= 0:
        return None
    cost = item.cost
    if cost is None or Decimal(str(cost)) <= 0:
        return _money(price)
    if Decimal(str(price)) > Decimal(str(cost)):
        return _money(price)
    return None


def _distinct(values: list[Decimal]) -> Decimal | None:
    """One agreed price, or None. Two catalog rows sharing a SKU but
    disagreeing on price is exactly the case a human must settle."""
    uniq = {v for v in values if v is not None and v > 0}
    if len(uniq) == 1:
        return uniq.pop()
    return None


def _cached_settings(db: Session):
    """Tier settings, hydrated at most once per session.

    ``hydrate_settings_from_db`` costs roughly one query per configured tier
    set (production has 15). Calling it per part turned a 20-part closeout
    into hundreds of queries on a phone over LTE. The cache lives on
    ``Session.info`` so it dies with the request.
    """
    from gdx_dispatch.services.pricing_engine import hydrate_settings_from_db

    cached = db.info.get("_part_pricing_settings")
    if cached is None:
        cached = hydrate_settings_from_db(db)
        db.info["_part_pricing_settings"] = cached
    return cached


def _customer_view(db: Session, *, job_id, customer_id):
    """The customer whose margin rules apply, resolved from the job when the
    caller didn't pass one.

    Callers must not have to remember this. Two of the three capture paths
    originally passed nothing, which silently priced every mobile and van
    part at ``retail`` — the most expensive class — for contractor and
    wholesale customers alike. Resolving from the job here makes the correct
    behaviour the default rather than the careful caller's reward.
    """
    from gdx_dispatch.services.pricing_engine import CustomerView

    default = CustomerView(pricing_class="retail", margin_override_pct=None)
    cid = customer_id
    if cid is None and job_id is not None:
        from uuid import UUID as _UUID

        from gdx_dispatch.models.tenant_models import Job

        # jobs.id is a Uuid column; every capture path hands this module a
        # str. Binding the raw string is an AttributeError on SQLite and a
        # type mismatch on Postgres, so coerce and give up quietly on junk.
        try:
            jid = job_id if isinstance(job_id, _UUID) else _UUID(str(job_id))
        except (ValueError, AttributeError, TypeError):
            return default
        cid = db.execute(
            select(Job.customer_id).where(Job.id == jid)
        ).scalar_one_or_none()
    if cid is None:
        return default

    from gdx_dispatch.models.tenant_models import Customer

    cust = db.execute(select(Customer).where(Customer.id == cid)).scalar_one_or_none()
    if cust is None:
        return default
    pc = cust.pricing_class
    return CustomerView(
        pricing_class=pc if pc in ("retail", "contractor", "wholesale") else None,
        margin_override_pct=(
            Decimal(str(cust.margin_override_pct))
            if cust.margin_override_pct is not None
            else None
        ),
    )


def _engine_sell(
    db: Session, *, cost: Decimal, pricing_category: str | None, job_id, customer_id
) -> Decimal | None:
    """Run a cost through the tenant's margin tiers. None on any config gap.

    Deliberately swallows ``PricingConfigError``: an unconfigured tier set is
    a reason to let the office price the line, never a reason to 500 a tech's
    closeout or to guess a margin.
    """
    from gdx_dispatch.services.pricing_engine import PricingConfigError, price_line

    category = (pricing_category or "parts").lower()
    # The engine raises on labor by design (the 2026-05-07 $91k cascade) —
    # labor is priced by the matrix, never tier-marked-up.
    if category == "labor":
        return None
    try:
        result = price_line(
            cost=cost,
            pricing_category=category,
            customer=_customer_view(db, job_id=job_id, customer_id=customer_id),
            settings=_cached_settings(db),
        )
        return _money(result.sell)
    except PricingConfigError as exc:
        log.info("part_pricing: no tier set for category=%r (%s) — office prices it", category, exc)
        return None
    except Exception:  # pragma: no cover — defensive; never break a closeout
        log.exception("part_pricing: margin engine failed — office prices it")
        return None


def _resolve_sell_price(
    db: Session,
    *,
    job_id: str | None,
    sku: str | None,
    part_id=None,
    customer_id=None,
) -> Decimal | None:
    """Best available SELL price for a part, or None for the office to set.

    Tier order, first hit wins:

    1. **The job's own priced parts row for this SKU.** The office scoped and
       priced this job up front (Doug 2026-08-18: "the office says it will be
       X, figured out when the parts are added to the job"), and that number
       may be one they adjusted for this customer. It outranks list price
       because it is specific to this job.
    2. **Bench inventory** ``Part.unit_price`` via ``part_id`` — the original
       behaviour, unchanged.
    3. **The tenant's catalog**, exact SKU: ``price`` when it is a real sell
       price (above cost), else ``cost`` through the margin engine.
    4. **CHI's parts catalog**, exact SKU: ``sell_price``, else ``cost``
       through the margin engine.

    Copying a price is not the row-merging AUDIT-R1 forbade: nothing is
    overwritten, every capture row survives, and
    ``closeout_billing._one_line_per_physical_part`` is what keeps a physical
    part to one line.
    """
    from gdx_dispatch.models.tenant_models import (
        ChiPartsCatalog,
        CustomCatalogItem,
        JobPartNeeded,
    )

    needle = _norm(sku)

    # --- Tier 1: the office's own number for this job ---------------------
    # ``source='request'`` ONLY, and that filter is load-bearing, not tidiness.
    # This resolver WRITES its results into job_parts_needed as capture rows
    # (closeout / mobile / van). Without the filter it reads its own output
    # back on the next capture and treats a machine-derived number as the
    # office's decision — a price laundered into authority, one capture at a
    # time. Only rows a human created through the Parts card or the job screen
    # are 'the office's number'.
    #
    # SKU match happens in SQL, not by loading the table and filtering in
    # Python: this runs per part on every closeout and mobile capture.
    if needle and job_id:
        priced = [
            _money(v)
            for (v,) in db.execute(
                select(JobPartNeeded.unit_price).where(
                    JobPartNeeded.job_id == str(job_id),
                    JobPartNeeded.source == "request",
                    JobPartNeeded.unit_price.is_not(None),
                    JobPartNeeded.unit_price > 0,
                    func.lower(func.trim(JobPartNeeded.sku)) == needle,
                )
            ).all()
        ]
        agreed = _distinct(priced)
        if agreed is not None:
            return agreed

    # --- Tier 2: bench inventory ------------------------------------------
    if part_id:
        from gdx_dispatch.modules.inventory.models import Part

        part = db.execute(
            select(Part).where(Part.id == part_id)
        ).scalar_one_or_none()
        if part is not None and part.unit_price and Decimal(str(part.unit_price)) > 0:
            return _money(part.unit_price)

    if not needle:
        return None

    # --- Tier 3: the tenant's own catalog ---------------------------------
    items = db.execute(
        select(CustomCatalogItem).where(
            CustomCatalogItem.active.is_(True),
            CustomCatalogItem.deleted_at.is_(None),
            func.lower(func.trim(CustomCatalogItem.sku)) == needle,
        )
    ).scalars().all()
    if items:
        real_sells = [p for p in (_believable_sell(it) for it in items) if p is not None]
        agreed = _distinct(real_sells)
        if agreed is not None:
            return agreed

        # Nothing believable as a sell price, so mark the cost up — except
        # where a recorded price sits BELOW cost. `price == cost` is the
        # QuickBooks-import shape and is exactly what the margin engine is
        # here to rescue; `price < cost` is a contradiction (a stale cost, or
        # a clearance price someone meant), and marking that cost up would
        # quietly bill above a price a human actually typed. Leave it to them.
        if any(
            it.price is not None
            and Decimal(str(it.price)) > 0
            and it.cost is not None
            and Decimal(str(it.price)) < Decimal(str(it.cost))
            for it in items
        ):
            return None
        costs = [
            _money(it.cost)
            for it in items
            if it.cost and Decimal(str(it.cost)) > 0
        ]
        agreed_cost = _distinct(costs)
        if agreed_cost is not None:
            category = next(
                (it.pricing_category for it in items if it.pricing_category), "parts"
            )
            marked_up = _engine_sell(
                db,
                cost=agreed_cost,
                pricing_category=category,
                job_id=job_id,
                customer_id=customer_id,
            )
            if marked_up is not None:
                return marked_up

    # --- Tier 4: CHI's parts catalog --------------------------------------
    chi = db.execute(
        select(ChiPartsCatalog).where(
            ChiPartsCatalog.is_active.is_(True),
            func.lower(func.trim(ChiPartsCatalog.sku)) == needle,
        )
    ).scalars().all()
    if chi:
        # Same believability guard as tier 3 — a CHI feed can carry
        # sell_price == cost just as a QuickBooks import can, and without
        # this the capture path and the SKU-suggest path returned different
        # numbers for the identical row.
        sells = [
            p
            for p in (_believable_sell(_PriceCost(c.sell_price, c.cost)) for c in chi)
            if p is not None
        ]
        agreed = _distinct(sells)
        if agreed is not None:
            return agreed

        if any(
            c.sell_price is not None
            and Decimal(str(c.sell_price)) > 0
            and c.cost is not None
            and Decimal(str(c.sell_price)) < Decimal(str(c.cost))
            for c in chi
        ):
            return None

        costs = [_money(c.cost) for c in chi if c.cost and Decimal(str(c.cost)) > 0]
        agreed_cost = _distinct(costs)
        if agreed_cost is not None:
            category = next((c.pricing_category for c in chi if c.pricing_category), "parts")
            marked_up = _engine_sell(
                db,
                cost=agreed_cost,
                pricing_category=category,
                job_id=job_id,
                customer_id=customer_id,
            )
            if marked_up is not None:
                return marked_up

    return None



def resolve_sell_price(
    db: Session,
    *,
    job_id: str | None,
    sku: str | None,
    part_id=None,
    customer_id=None,
) -> Decimal | None:
    """Guarded entry point — see ``_resolve_sell_price`` for the tier order.

    A price lookup must never be the reason a tech cannot close out a job.
    The closeout is the one transaction in this system that has to succeed:
    the part is already in the door, the customer is standing there, and the
    tech is on a phone. Every failure here degrades to ``None``, which is a
    supported state meaning "the office prices it at invoicing" — the exact
    behaviour that existed before this resolver was written.

    This cannot rescue a poisoned session (a failed statement inside a shared
    transaction is already fatal by the time we see it), so it is a floor,
    not a guarantee. It does cover the far more likely shapes: a malformed
    decimal, an unexpected None, a model that moved.
    """
    try:
        return _resolve_sell_price(
            db, job_id=job_id, sku=sku, part_id=part_id, customer_id=customer_id
        )
    except Exception:
        log.exception(
            "part_pricing: resolve failed for sku=%r job=%r — office prices it",
            sku, job_id,
        )
        return None

def duplicate_capture_groups(db: Session, job_id: str) -> list[dict]:
    """Unbilled capture rows on a job that name the same part more than once.

    **Detection, never suppression.** The system's standing ruling (AUDIT-R1,
    2026-07-07) is that capture rows are never machine-merged: any automatic
    dedup either undercounts by destroying a legitimate line or double-counts,
    and a human-reviewed list with full provenance can only over-show. The
    closeout dialog already shows the tech what was logged during the job for
    exactly this reason — so a duplicate is a data-entry event to surface, not
    a row for the machine to quietly drop.

    That matters more now that these rows carry prices: before, a duplicate
    cost nothing because neither row was priced. The office needs to see it
    *before* verifying a draft, which is what this feeds.

    Grouping key is (sku or name, qty) — the same key ``closeout_job``'s
    ``_billed_keys`` uses. Qty is deliberately part of it: two rows for the
    same part at different quantities are the ambiguous case a human settles,
    so they are reported separately rather than treated as one.

    Known limit, stated rather than papered over: a free-text row with no sku
    and a catalog row for the same physical part key differently and will not
    be reported as duplicates of each other.
    """
    from gdx_dispatch.models.tenant_models import JobPartNeeded

    rows = db.execute(
        select(JobPartNeeded).where(
            JobPartNeeded.job_id == str(job_id),
            JobPartNeeded.source.in_(("closeout", "mobile", "van")),
            JobPartNeeded.billed_invoice_id.is_(None),
            func.coalesce(JobPartNeeded.status, "") != "wont_bill",
        )
    ).scalars().all()

    groups: dict[tuple[str, int], list] = {}
    for row in rows:
        key = (
            (row.sku or "").strip().lower() or (row.part_name or "").strip().lower(),
            int(row.quantity or 1),
        )
        if not key[0]:
            continue
        groups.setdefault(key, []).append(row)

    return [
        {
            "part": next((r.part_name for r in group if r.part_name), key[0]),
            "sku": next((r.sku for r in group if r.sku), None),
            "quantity": key[1],
            "times_captured": len(group),
            "sources": sorted({(r.source or "") for r in group}),
            "row_ids": [r.id for r in group],
        }
        for key, group in groups.items()
        if len(group) > 1
    ]


def sell_price_for_row(
    db: Session,
    *,
    price,
    cost,
    pricing_category: str | None = "parts",
    job_id=None,
    customer_id=None,
) -> Decimal | None:
    """Sell price for a catalog row already in hand, or None.

    Same two rules the resolver applies — believe a price only above cost (or
    with no cost recorded), otherwise mark the cost up through the tenant's
    margin tiers. Exposed so callers holding a row (the SKU suggest endpoint)
    price it identically to the capture path instead of forking a second
    implementation of the same money decision.
    """

    believable = _believable_sell(_PriceCost(price, cost))
    if believable is not None:
        return believable
    if (
        price is not None
        and Decimal(str(price)) > 0
        and cost is not None
        and Decimal(str(price)) < Decimal(str(cost))
    ):
        return None
    if cost is None or Decimal(str(cost)) <= 0:
        return None
    return _engine_sell(
        db,
        cost=_money(cost),
        pricing_category=pricing_category,
        job_id=job_id,
        customer_id=customer_id,
    )

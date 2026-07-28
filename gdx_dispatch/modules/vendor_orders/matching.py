"""Suggest which job a supplier order belongs to.

Reuses the vendor-bill matcher's name scoring rather than inventing a second
one — a bill and an order confirmation for the SAME purchase must not rank jobs
differently, or confirming them separately would file one purchase against two
jobs.

An order confirmation is a better matching subject than a bill, because it
carries two independent name signals where a bill carries one:

    ship_to      the jobsite the doors are going to   ("SFL Trende")
    customer_po  the free text the office typed        ("D&E Rose City")

Both are matched, and the better score wins with the field that produced it
recorded in the reason — so when a human reviews the suggestion they can see
WHY it was suggested, not just how confident the machine claims to be.

Nothing here mutates anything. Suggesting is not confirming: the office
confirms, and only then does anything get filed against a job (``confirm.py``).
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.models.tenant_models import Customer, Job
from gdx_dispatch.modules.vendor_invoices.matching import _similarity
from gdx_dispatch.modules.vendor_orders.models import VendorOrder

# Below this a "match" is noise. Same floor the bill matcher uses, deliberately:
# two views of one purchase should agree about what counts as a candidate.
DEFAULT_THRESHOLD = 0.55
DEFAULT_LIMIT = 5


@dataclass
class OrderJobSuggestion:
    job_id: str
    score: float
    reason: str
    job_title: str | None = None
    job_number: str | None = None
    customer_id: str | None = None
    customer_name: str | None = None
    lifecycle_stage: str | None = None


def _signals(order: VendorOrder) -> list[tuple[str, str]]:
    """(field name, text) pairs worth matching on, best signal first.

    ship_to leads: it is where the doors are physically going, which is the
    jobsite. customer_po is whatever the office typed and is sometimes a date
    or a door size rather than a name.
    """
    out: list[tuple[str, str]] = []
    for field, value in (("ship_to", order.ship_to), ("customer_po", order.customer_po)):
        text = (value or "").strip()
        if text:
            out.append((field, text))
    return out


def suggest_order_job_matches(
    db: Session,
    order: VendorOrder,
    *,
    limit: int = DEFAULT_LIMIT,
    threshold: float = DEFAULT_THRESHOLD,
) -> list[OrderJobSuggestion]:
    """Rank likely jobs for this order, best first. Never mutates."""
    signals = _signals(order)
    if not signals:
        return []

    customers = db.execute(
        select(Customer).where(Customer.deleted_at.is_(None))
    ).scalars().all()

    # Best score per customer, remembering which field earned it.
    scored: dict[object, tuple[float, str, Customer]] = {}
    for field, text in signals:
        for customer in customers:
            score = _similarity(text, customer.name)
            if score < threshold:
                continue
            best = scored.get(customer.id)
            if best is None or score > best[0]:
                scored[customer.id] = (score, f"{field} “{text}” ≈ customer “{customer.name}”", customer)

    if not scored:
        return []

    top = sorted(scored.values(), key=lambda t: t[0], reverse=True)[:3]
    customer_ids = [c.id for _, _, c in top]
    jobs = db.execute(
        select(Job)
        .where(Job.customer_id.in_(customer_ids))
        .where(Job.lifecycle_stage != "cancelled")
    ).scalars().all()

    by_customer: dict[object, list[Job]] = {}
    for job in jobs:
        by_customer.setdefault(job.customer_id, []).append(job)

    suggestions: list[OrderJobSuggestion] = []
    for score, reason, customer in top:
        for job in by_customer.get(customer.id, []):
            suggestions.append(OrderJobSuggestion(
                job_id=str(job.id),
                score=round(score, 3),
                reason=reason,
                job_title=getattr(job, "title", None),
                job_number=getattr(job, "job_number", None),
                customer_id=str(customer.id),
                customer_name=customer.name,
                lifecycle_stage=getattr(job, "lifecycle_stage", None),
            ))

    # Score first; then newest job, since a customer with several jobs is most
    # likely ordering for the current one.
    suggestions.sort(
        key=lambda s: (s.score, s.job_number or ""),
        reverse=True,
    )
    return suggestions[:limit]

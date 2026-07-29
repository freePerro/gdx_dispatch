"""Suggest which job a supplier order belongs to.

Two signals from the order, and two things to match them against.

FROM the order:
    ship_to      the jobsite the doors are going to   ("SFL Trende")
    customer_po  the free text the office typed        ("D&E Rose City")

AGAINST:
    job.title      what the office CALLED the work     ("Trende")
    customer.name  who is on the account               ("Bill Greenwaldt")

Matching only customer names was the original design and it found a job for 11
of 36 real orders. Measured on production data, the job TITLE is by far the
stronger signal — the same person types "Wickham A+" on the supplier order and
titles the GDX job "Wickham", whereas the customer record is often a different
name or absent entirely:

    order text        best customer   best job title
    SFL Swenstad      0.52            1.00  "Swenstad"
    Wickham A+        0.52            1.00  "Wickham"
    SFL Trende        0.48            0.78  "A+ trende"
    Y Schlagel        0.58            0.78  "schlagle"
    D&E Rose City     0.52            0.67  "Rose city beck"

Adding the title signal rescued 6 of the 25 misses, taking coverage to 17/36.
Both signals are kept, not swapped: a job with a useless title ("Install",
"Service call") still reaches its customer by name.

The scoring function itself is imported from the vendor-bill matcher rather
than reimplemented — a bill and an order for the SAME purchase must not rank
jobs differently, or confirming them separately would file one purchase against
two jobs.

Nothing here mutates anything. Suggesting is not confirming (see confirm.py).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.models.tenant_models import Customer, Job
from gdx_dispatch.modules.vendor_invoices.matching import _similarity, normalize_name
from gdx_dispatch.modules.vendor_orders.models import VendorOrder

# Below this a "match" is noise — measured: at 0.50 the closest job title to
# "2.26.26" is "2 7in long step rollers". Same floor the bill matcher uses, so
# two views of one purchase agree about what counts as a candidate.
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


@dataclass
class CustomerWithoutJob:
    """A customer the order clearly belongs to, who has no job on file.

    Reported separately and deliberately. Collapsing this into "no match" is a
    lie the UI told for a whole release: six of 36 real orders matched a
    customer at up to 0.89 and were shown as "the reference doesn't look like a
    customer name". The office needs to tell those apart — one means the
    reference is junk, the other means CREATE THE JOB.
    """
    customer_id: str
    customer_name: str
    score: float
    reason: str


@dataclass
class OrderMatchResult:
    suggestions: list[OrderJobSuggestion] = field(default_factory=list)
    customers_without_jobs: list[CustomerWithoutJob] = field(default_factory=list)


# The shared scorer returns max(sequence_ratio, token_overlap), and
# token_overlap divides by the SHORTER side's token count — so a single token
# appearing anywhere in the other string scores a flat 1.0. That is what makes
# "Wickham A+" ≈ job "Wickham" work, and it is why a reference that normalises
# to one tiny token ("A+" → "a") would match any title containing that letter
# as a word, at full confidence. A confidently-wrong suggestion is the worst
# outcome in a confirm flow, so signals that thin are dropped rather than
# scored.
MIN_SIGNAL_CHARS = 3


def _signals(order: VendorOrder) -> list[tuple[str, str]]:
    """(field name, text) worth matching on. ship_to leads — it is where the
    doors physically go, i.e. the jobsite; customer_po is sometimes a date or a
    door size rather than a name."""
    out: list[tuple[str, str]] = []
    for field_name, value in (("ship_to", order.ship_to), ("customer_po", order.customer_po)):
        text = (value or "").strip()
        if text and len(normalize_name(text).replace(" ", "")) >= MIN_SIGNAL_CHARS:
            out.append((field_name, text))
    return out


def suggest_order_job_matches(
    db: Session,
    order: VendorOrder,
    *,
    limit: int = DEFAULT_LIMIT,
    threshold: float = DEFAULT_THRESHOLD,
) -> OrderMatchResult:
    """Rank likely jobs for this order, best first. Never mutates."""
    result = OrderMatchResult()
    signals = _signals(order)
    if not signals:
        return result

    customers = {
        c.id: c for c in db.execute(
            select(Customer).where(Customer.deleted_at.is_(None))
        ).scalars().all()
    }
    jobs = db.execute(
        select(Job)
        .where(Job.deleted_at.is_(None))
        .where(Job.lifecycle_stage != "cancelled")
    ).scalars().all()

    jobs_by_customer: dict[object, list[Job]] = {}
    for job in jobs:
        jobs_by_customer.setdefault(job.customer_id, []).append(job)

    # job_id -> (score, reason). Best score wins when both signals reach a job.
    best: dict[object, tuple[float, str]] = {}

    def _offer(job, score: float, reason: str) -> None:
        current = best.get(job.id)
        if current is None or score > current[0]:
            best[job.id] = (score, reason)

    for field_name, text in signals:
        # (1) the job's own title — the office's name for the work.
        for job in jobs:
            score = _similarity(text, job.title or "")
            if score >= threshold:
                _offer(job, score, f'{field_name} "{text}" ≈ job "{job.title}"')

        # (2) the customer's name, reaching that customer's jobs. Keeps jobs
        # whose titles say nothing useful ("Service call") findable.
        for customer in customers.values():
            score = _similarity(text, customer.name)
            if score < threshold:
                continue
            owned = jobs_by_customer.get(customer.id, [])
            if owned:
                for job in owned:
                    _offer(job, score, f'{field_name} "{text}" ≈ customer "{customer.name}"')
            else:
                existing = next(
                    (c for c in result.customers_without_jobs if c.customer_id == str(customer.id)),
                    None,
                )
                if existing is None:
                    result.customers_without_jobs.append(CustomerWithoutJob(
                        customer_id=str(customer.id),
                        customer_name=customer.name,
                        score=round(score, 3),
                        reason=f'{field_name} "{text}" ≈ customer "{customer.name}"',
                    ))
                elif score > existing.score:
                    existing.score = round(score, 3)
                    existing.reason = f'{field_name} "{text}" ≈ customer "{customer.name}"'

    jobs_by_id = {j.id: j for j in jobs}
    for job_id, (score, reason) in best.items():
        job = jobs_by_id[job_id]
        customer = customers.get(job.customer_id)
        result.suggestions.append(OrderJobSuggestion(
            job_id=str(job.id),
            score=round(score, 3),
            reason=reason,
            job_title=job.title,
            job_number=getattr(job, "job_number", None),
            customer_id=str(job.customer_id) if job.customer_id else None,
            customer_name=customer.name if customer else None,
            lifecycle_stage=getattr(job, "lifecycle_stage", None),
        ))

    result.suggestions.sort(key=lambda s: (s.score, s.job_number or ""), reverse=True)
    result.suggestions = result.suggestions[:limit]
    result.customers_without_jobs.sort(key=lambda c: c.score, reverse=True)

    # Only worth showing when there is nothing better on offer.
    if result.suggestions:
        result.customers_without_jobs = []
    return result

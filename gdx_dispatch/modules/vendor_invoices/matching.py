"""Vendor-invoice matching + duplicate detection.

Two jobs:

1. **Dedup** (design §3a) — layer 2 (vendor + invoice_number uniqueness) and
   layer 3 (advisory same-vendor/same-total double-billing hint). Layer 1
   (content-hash) lives in the service; layer 4 (statement reconciliation) is
   Phase 3.
2. **Job matching** — suggest which job a bill belongs to. The order chain the
   design wants (in-app PO → open parts-needed → customer-name fuzzy on the
   PO# text) is only partly available: ``purchase_orders`` carries no job link,
   so the customer-name fuzzy match on ``po_reference`` is the primary signal,
   with a PO-received result surfaced as a double-receive guard.

Everything here is a *suggestion*; nothing mutates money. Confirmation is a
separate, human-driven step (``confirm.py``).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from difflib import SequenceMatcher
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from gdx_dispatch.models.tenant_models import Customer, Job, JobPartNeeded, Vendor
from gdx_dispatch.modules.vendor_invoices.models import STATUS_VOID, VendorInvoice

log = logging.getLogger(__name__)

# Layer-3 window: same-total bills more than this far apart are treated as
# distinct (a legitimate reorder), not a possible double-bill.
DUP_WINDOW_DAYS = 45
DUP_TOTAL_TOLERANCE = Decimal("0.02")

# Job-match stages that plausibly expect material to arrive, ranked first.
_MATERIAL_STAGES = ("estimate", "scheduled", "in_progress", "service_call")

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_name(name: str | None) -> str:
    """Casefold, strip punctuation, collapse whitespace — for tolerant name
    matching and the raw-vendor dedup fallback."""
    if not name:
        return ""
    return _NON_ALNUM.sub(" ", name.casefold()).strip()


def _similarity(a: str, b: str) -> float:
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return 0.0
    ratio = SequenceMatcher(None, na, nb).ratio()
    # Token-overlap bonus so a PO# note like "Smith Job" ↔ customer "Smith"
    # (substring) scores well even when overall length differs.
    ta, tb = set(na.split()), set(nb.split())
    overlap = len(ta & tb) / max(1, min(len(ta), len(tb)))
    return max(ratio, overlap)


# --------------------------------------------------------------------------- #
# Vendor resolution
# --------------------------------------------------------------------------- #
def resolve_vendor(db: Session, vendor_name_raw: str | None) -> Vendor | None:
    """Resolve a raw/parsed vendor name to a ``Vendor`` row via exact name,
    alias list, or normalized-name match. Returns None if no confident match."""
    if not vendor_name_raw:
        return None
    target = normalize_name(vendor_name_raw)
    if not target:
        return None

    vendors = db.execute(
        select(Vendor).where(Vendor.deleted_at.is_(None))
    ).scalars().all()

    for v in vendors:
        if normalize_name(v.name) == target:
            return v
        aliases = _load_aliases(v.name_aliases)
        if any(normalize_name(a) == target for a in aliases):
            return v
    return None


def compute_vendor_key(vendor_id: UUID | None, vendor_name_raw: str | None) -> str:
    """The stable dedup key: the resolved vendor id (as text) when known, else the
    normalized raw name. Matches the ``vendor_invoices.vendor_key`` unique index."""
    if vendor_id is not None:
        return str(vendor_id)
    return normalize_name(vendor_name_raw)


def find_invoice_by_key(
    db: Session, *, vendor_key: str, invoice_number: str
) -> VendorInvoice | None:
    """Direct (vendor_key, invoice_number) lookup — the race fallback re-queries
    with this after the DB unique index rejects a concurrent duplicate insert. Kept
    separate from ``find_duplicate_invoice`` so tests can stub the app-level check
    without disabling the fallback."""
    return db.execute(
        select(VendorInvoice)
        .where(VendorInvoice.deleted_at.is_(None))
        .where(VendorInvoice.vendor_key == vendor_key)
        .where(func.lower(VendorInvoice.invoice_number) == invoice_number.lower())
        .limit(1)
    ).scalar_one_or_none()


def _load_aliases(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if isinstance(data, list):
        return [str(x) for x in data]
    return []


# --------------------------------------------------------------------------- #
# Dedup layer 2 — (vendor, invoice_number) uniqueness
# --------------------------------------------------------------------------- #
def find_duplicate_invoice(
    db: Session,
    *,
    vendor_id: UUID | None,
    vendor_name_raw: str,
    invoice_number: str,
    exclude_id: UUID | None = None,
) -> VendorInvoice | None:
    """Return an existing non-deleted invoice with the same vendor +
    invoice_number, matched by ``vendor_id`` when resolved, else by normalized
    raw vendor name. This catches a re-print/re-scan whose bytes differ (so the
    content-hash layer misses it)."""
    stmt = (
        select(VendorInvoice)
        .where(VendorInvoice.deleted_at.is_(None))
        .where(func.lower(VendorInvoice.invoice_number) == invoice_number.lower())
    )
    if exclude_id is not None:
        stmt = stmt.where(VendorInvoice.id != exclude_id)

    candidates = db.execute(stmt).scalars().all()
    target_norm = normalize_name(vendor_name_raw)
    for c in candidates:
        if vendor_id is not None and c.vendor_id == vendor_id:
            return c
        if vendor_id is None and normalize_name(c.vendor_name_raw) == target_norm:
            return c
    return None


# --------------------------------------------------------------------------- #
# Dedup layer 3 — advisory double-billing hint (NOT a control, see [AUDIT-R3])
# --------------------------------------------------------------------------- #
def flag_possible_duplicate(db: Session, invoice: VendorInvoice) -> VendorInvoice | None:
    """If another open invoice from the same vendor has the same total within a
    short window (and a DIFFERENT invoice number), stamp
    ``possible_duplicate_of_id`` on *this* invoice and return the other. This is
    a weak hint the office reviews — identical reorders legitimately collide, so
    it never blocks or auto-skips."""
    if invoice.invoice_date is None:
        return None

    lo = invoice.invoice_date - timedelta(days=DUP_WINDOW_DAYS)
    hi = invoice.invoice_date + timedelta(days=DUP_WINDOW_DAYS)

    stmt = (
        select(VendorInvoice)
        .where(VendorInvoice.deleted_at.is_(None))
        .where(VendorInvoice.id != invoice.id)
        .where(VendorInvoice.status != STATUS_VOID)
        .where(VendorInvoice.invoice_number != invoice.invoice_number)
        .where(VendorInvoice.invoice_date.is_not(None))
        .where(VendorInvoice.invoice_date >= lo)
        .where(VendorInvoice.invoice_date <= hi)
    )
    candidates = db.execute(stmt).scalars().all()
    for c in candidates:
        same_vendor = (
            (invoice.vendor_id is not None and c.vendor_id == invoice.vendor_id)
            or (
                invoice.vendor_id is None
                and normalize_name(c.vendor_name_raw) == normalize_name(invoice.vendor_name_raw)
            )
        )
        if same_vendor and abs(Decimal(c.total) - Decimal(invoice.total)) <= DUP_TOTAL_TOLERANCE:
            invoice.possible_duplicate_of_id = c.id
            return c
    return None


# --------------------------------------------------------------------------- #
# Job matching (suggestions)
# --------------------------------------------------------------------------- #
@dataclass
class JobSuggestion:
    job_id: str
    score: float
    reason: str
    job_title: str | None = None
    customer_name: str | None = None
    lifecycle_stage: str | None = None


def suggest_job_matches(
    db: Session, invoice: VendorInvoice, *, limit: int = 5, threshold: float = 0.55
) -> list[JobSuggestion]:
    """Rank likely jobs for this bill. Primary signal: the PO# text fuzzy-
    matched to a customer name, then that customer's jobs (material-expecting
    stages first). Secondary: open parts-needed rows whose supplier matches the
    vendor."""
    po = invoice.po_reference or ""
    suggestions: dict[str, JobSuggestion] = {}

    # (1) Customer-name fuzzy on the PO# text.
    if po.strip():
        customers = db.execute(
            select(Customer).where(Customer.deleted_at.is_(None))
        ).scalars().all()
        scored = [
            (c, _similarity(po, c.name)) for c in customers
        ]
        scored = [(c, s) for c, s in scored if s >= threshold]
        scored.sort(key=lambda t: t[1], reverse=True)
        for customer, score in scored[:3]:
            jobs = db.execute(
                select(Job)
                .where(Job.customer_id == customer.id)
                .where(Job.lifecycle_stage != "cancelled")
            ).scalars().all()
            jobs.sort(key=_job_sort_key, reverse=True)
            for job in jobs[:3]:
                jid = str(job.id)
                cand = JobSuggestion(
                    job_id=jid,
                    score=round(score, 3),
                    reason=f"PO# '{po}' ~ customer '{customer.name}'",
                    job_title=job.title,
                    customer_name=customer.name,
                    lifecycle_stage=job.lifecycle_stage,
                )
                _keep_best(suggestions, cand)

    # (2) Open parts-needed rows whose supplier matches the vendor.
    vendor_name = invoice.vendor_name_raw or ""
    if vendor_name.strip():
        rows = db.execute(
            select(JobPartNeeded).where(JobPartNeeded.status.in_(("needed", "ordered")))
        ).scalars().all()
        for row in rows:
            if _similarity(vendor_name, row.supplier or "") >= 0.7 and row.job_id:
                cand = JobSuggestion(
                    job_id=str(row.job_id),
                    score=0.6,
                    reason=f"open parts order from '{row.supplier}'",
                )
                _keep_best(suggestions, cand)

    ranked = sorted(suggestions.values(), key=lambda s: s.score, reverse=True)
    return ranked[:limit]


def _job_sort_key(job: Job):
    stage_rank = (
        len(_MATERIAL_STAGES) - _MATERIAL_STAGES.index(job.lifecycle_stage)
        if job.lifecycle_stage in _MATERIAL_STAGES
        else 0
    )
    created = getattr(job, "created_at", None)
    # Coerce to a float so a mix of set/None created_at can't compare
    # datetime-vs-int (that raised TypeError and 500'd suggestions).
    created_ts = created.timestamp() if created is not None else 0.0
    return (stage_rank, created_ts)


def _keep_best(store: dict[str, JobSuggestion], cand: JobSuggestion) -> None:
    existing = store.get(cand.job_id)
    if existing is None or cand.score > existing.score:
        store[cand.job_id] = cand

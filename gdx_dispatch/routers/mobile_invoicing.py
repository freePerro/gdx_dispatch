"""Sprint tech_mobile Phase 2.2 — On-Site Invoicing.

Mobile-scoped wrappers around invoices so techs can generate and email
an invoice from the truck immediately after job completion.

Payment capture is NOT deferred any more (the old note here said it was, long
after the truck started taking cash and checks): the field posts money through
the shared ``POST /api/invoices/{id}/payments``, which carries no permission
dependency. LIST reads are the asymmetry — a technician has no invoices
permission at all, so `GET /api/invoices` and `/api/invoices/summary` 403 them
— which is why /invoices/open below exists. Note the single-invoice endpoints
(`GET /api/invoices/{id}`, `/send`, `/payments`) carry no permission
dependency either; that pre-existing gap is not closed here, so scoping rests
on this list never handing over an id the caller does not own.

Endpoints (all under /api/mobile, gated on the "mobile" module):

    POST /api/mobile/jobs/{job_id}/invoice         — create invoice from
                                                      completed job + accepted quote
    GET  /api/mobile/jobs/{job_id}/financial       — financial summary at close-out
    GET  /api/mobile/invoices/open                 — unpaid invoices scoped to
                                                      the tech's own jobs
    POST /api/mobile/invoices/{invoice_id}/send    — email invoice to customer
    POST /api/mobile/invoices/{invoice_id}/send-receipt — send receipt for a
                                                          recorded payment
"""
from __future__ import annotations

import json as _json
import logging
import secrets
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID as _UUID
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy import text as _text
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import (
    Customer,
    Invoice,
    InvoiceLine,
    JobPartNeeded,
    TimeEntry,
)
from gdx_dispatch.modules.ledger.service import transition_invoice_status
from gdx_dispatch.modules.proposals.models import Estimate

log = logging.getLogger(__name__)

try:
    from gdx_dispatch.routers.auth import get_current_user
except Exception:
    log.exception("mobile_invoicing_auth_import_failed_using_fallback")

    async def get_current_user() -> dict[str, Any]:
        return {}


router = APIRouter(
    prefix="/api/mobile",
    tags=["mobile-invoicing"],
    dependencies=[Depends(require_module("mobile"))],
)


def _jr(content: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(content=content, status_code=status_code)


# Most rows the tech-scoped open-invoice list will return. When it bites, the
# response carries `truncated: true` and the server logs it — a capped list
# that looks complete is how "I never saw that invoice" starts.
_OPEN_INVOICE_CAP = 200

# Job ids per IN-list. A module constant rather than a literal so a test can
# shrink it and actually exercise the multi-chunk path — with the real value
# inline, any realistic fixture fits in one chunk and the cross-chunk sort
# below is never proven.
_JOB_ID_CHUNK = 200


def _money(v: Decimal | float | str) -> Decimal:
    return Decimal(str(v)).quantize(Decimal("0.01"))


def _tenant_id(request: Request) -> str:
    state = getattr(request, "state", None)
    tenant = getattr(state, "tenant", None) or {}
    return str(tenant.get("id") or getattr(state, "tenant_id", "") or "")


def _user_id(user: dict[str, Any]) -> str:
    return str(user.get("user_id") or user.get("sub") or "")


def _job_belongs_to_tech(db: Session, request: Request, job_id: str, user_id: str) -> bool:
    """Delegates to core/job_access.job_belongs_to_user — ONE ownership gate.

    A1 (adversarial audit, 2026-07-29): this used to carry its own SQL, and
    its first check compared jobs.assigned_to to a USER id. That column holds
    a TECHNICIAN id in 22 of 22 prod rows, so the check could never match and
    ownership rested entirely on the job_assignments fallback — which only
    19 of 190 completed jobs have. Net effect: field billing 404'd for the
    tech on ~90%% of jobs, and mobile_invoice_created has ZERO audit rows
    ever — the feature never once ran in prod. The shared gate resolves all
    four ownership paths (assigned technician-id, legacy user-id, appointment,
    crew assignment); never fork a second implementation of it.
    """
    if not job_id or not user_id:
        return False
    from gdx_dispatch.core.job_access import job_belongs_to_user

    return job_belongs_to_user(db, _tenant_id(request), job_id, user_id)


def restate_invoice_header(invoice: Any, line_sum: Any) -> None:
    """Point the header at the lines that were just written, tax included.

    Both estimate branches below rebuild the invoice's lines and then have to
    restate the header. They used to assign ``total = line_sum`` while
    ``tax_amount`` kept the value computed from the estimate, so the
    money-correctness invariant (migration 056) refused the commit and EVERY
    field invoice 500'd.

    The tax is deliberately NOT recomputed here. compute_estimate_totals taxes
    (subtotal − labor − discount) and this business does not tax labor; a naive
    ``line_sum × rate`` would bill sales tax on labor and disagree with the
    estimate the customer already accepted. All the header owes the invariant
    is total == Σlines + tax.

    A named function because both branches need the identical restatement and
    the previous version was two hand-written copies — one of which is how the
    bug got in twice.
    """
    subtotal = Decimal(str(line_sum or 0))
    tax = Decimal(str(invoice.tax_amount or 0))
    invoice.subtotal = _money(subtotal)
    invoice.total = _money(subtotal + tax)
    invoice.balance_due = invoice.total


def _next_invoice_number(db: Session) -> str:
    # Moved to core/closeout_billing (2026-08-07) so the closeout autodraft
    # shares the sequence without importing from a router. Thin wrapper kept:
    # callers here and the ownership test's source-slicing both key on it.
    from gdx_dispatch.core.closeout_billing import next_invoice_number

    return next_invoice_number(db)


def _serialize_invoice(inv: Invoice, *, include_lines: bool = False, db: Session | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": str(inv.id),
        "invoice_number": inv.invoice_number,
        "job_id": str(inv.job_id) if inv.job_id else None,
        "customer_id": str(inv.customer_id) if inv.customer_id else None,
        "status": inv.status,
        # Deposit lifecycle (2026-07-24): without billing_type the truck
        # can't tell a deposit invoice from a final one.
        "billing_type": inv.billing_type or "standard",
        "estimate_id": str(inv.estimate_id) if getattr(inv, "estimate_id", None) else None,
        "subtotal": float(inv.subtotal or 0),
        "tax_amount": float(inv.tax_amount or 0),
        "total": float(inv.total or 0),
        "balance_due": float(inv.balance_due or 0),
        "amount_paid": float(inv.amount_paid or 0) if inv.amount_paid is not None else 0.0,
        "due_date": inv.due_date.isoformat() if inv.due_date else None,
        "invoice_date": inv.invoice_date.isoformat() if inv.invoice_date else None,
        "sent_at": inv.sent_at.isoformat() if inv.sent_at else None,
        "verified_at": inv.verified_at.isoformat() if inv.verified_at else None,
        "paid_at": inv.paid_at.isoformat() if inv.paid_at else None,
        "notes": inv.notes,
    }
    if include_lines and db is not None:
        rows = db.execute(
            _text(
                """
                SELECT id, description, quantity, unit_price, line_total, sort_order
                FROM invoice_lines
                WHERE invoice_id = :iid OR invoice_id = :iid_dashed
                ORDER BY sort_order ASC
                """
            ),
            {"iid": inv.id.hex if hasattr(inv.id, "hex") else str(inv.id).replace("-", ""),
             "iid_dashed": str(inv.id)},
        ).all()
        out["lines"] = [
            {
                "id": str(r[0]),
                "description": r[1],
                "quantity": int(r[2] or 0),
                "unit_price": float(r[3] or 0),
                "line_total": float(r[4] or 0),
                "sort_order": int(r[5] or 0),
            }
            for r in rows
        ]
    return out


# ---------------------------------------------------------------------------
# Pydantic input shapes
# ---------------------------------------------------------------------------


class CreateInvoiceIn(BaseModel):
    """Body for POST /api/mobile/jobs/{job_id}/invoice.

    estimate_id optional — when provided, the invoice is built from the
    accepted quote's lines. If absent, the invoice is created with no
    lines (office can add lines later via the existing /api/invoices
    routes).
    """

    estimate_id: str | None = Field(default=None)
    notes: str | None = Field(default=None, max_length=5000)
    send_email: bool = Field(default=True)
    # 2026-08-12 — job photos to print on the PDF. The tech shot them; on a
    # send_email=true invoice this dialog is the ONLY moment anyone can pick
    # them before the customer gets the bill. Validated server-side against
    # this job, same rule and same helper as the office paths.
    attached_photo_ids: list[str] = Field(default_factory=list, max_length=20)


class SendReceiptIn(BaseModel):
    """Body for POST /api/mobile/invoices/{invoice_id}/send-receipt.

    Optional payment_id when receipting a specific payment; when absent,
    the most recent payment on the invoice is used.
    """

    payment_id: str | None = Field(default=None)


# ---------------------------------------------------------------------------
# GET /api/mobile/jobs/{job_id}/financial — close-out summary
# ---------------------------------------------------------------------------


@router.get("/jobs/{job_id}/financial", response_model=None)
def job_financial_summary(
    job_id: str,
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Per-job financial summary at close-out.

    Returns:
        - parts_cost: sum of estimated cost on parts_needed rows for this job
        - labor_hours: total labor hours from time entries
        - accepted_quote: { id, total } if any accepted quote on this job
        - invoices: [{id, invoice_number, total, status, balance_due}]
        - payment_status: 'pending' (mobile-side default) | 'paid' (any
          invoice paid_at set)
    """
    user = current_user or {}
    user_id = _user_id(user)
    if not _job_belongs_to_tech(db, request, job_id, user_id):
        return _jr({"detail": "job not found or not assigned to you"}, 404)

    # The parts a tech recorded for this job. Three things were wrong here and
    # each one alone 500s the whole endpoint on Postgres:
    #   * the table is `job_parts_needed` — `parts_needed` has never existed;
    #   * it has no `estimated_cost` (price is unit_price × quantity);
    #   * it has no `deleted_at` either.
    # The except below was meant to degrade gracefully ("optional in some
    # tenant DBs"), but a failed statement ABORTS the Postgres transaction, so
    # swallowing it just meant every later query in the request died with
    # InFailedSqlTransaction. Net effect: /financial 500'd for every job, so
    # the mobile invoice dialog rendered blank and offered to "Generate empty
    # invoice" — the tech could not show a customer their bill at all.
    # Roll back so a genuinely-missing table degrades instead of poisoning.
    # Via the ORM, not raw SQL: JobPartNeeded.job_id is a String while
    # TimeEntry.job_id below is a Uuid, and a Uuid column stores 32-hex on
    # SQLite vs dashed on Postgres — a hand-written `job_id = :jid` matches on
    # one and silently nothing on the other. Let the mapper handle the dialect.
    try:
        parts_cost = float(db.execute(
            select(
                func.coalesce(
                    func.sum(
                        func.coalesce(JobPartNeeded.unit_price, 0)
                        * func.coalesce(JobPartNeeded.quantity, 1)
                    ),
                    0,
                )
            ).where(JobPartNeeded.job_id == str(job_id))
        ).scalar() or 0)
    except Exception:
        db.rollback()
        log.exception("financial_summary_parts_cost_unavailable job=%s", job_id)
        parts_cost = 0.0

    # Per-job labor. The previous version claimed to be "portable across SQLite
    # + PG" and was neither portable nor pointed at a real table:
    #   * julianday() is SQLite-only — it does not exist on Postgres;
    #   * the table is `timeclock_entries_router`, not `timeclock_entries`;
    #   * and that table is the DAY-level shift anyway — it has no job_id.
    # So this silently reported 0 labor hours on every job in production, the
    # same defect the 2026-07-15 walk found in mobile_day_summary and fixed
    # only there. Per-job time lives in `time_entries`, and duration_minutes is
    # already computed and portable — no dialect maths needed. Only closed,
    # attested rows count (PR #154: an open timer is not evidence of work).
    try:
        labor_minutes = db.execute(
            select(func.coalesce(func.sum(func.coalesce(TimeEntry.duration_minutes, 0)), 0))
            .where(
                TimeEntry.job_id == _UUID(str(job_id)),
                TimeEntry.clock_out.is_not(None),
                TimeEntry.deleted_at.is_(None),
            )
        ).scalar() or 0
        labor_hours = round(float(labor_minutes) / 60.0, 2)
    except Exception:
        db.rollback()
        log.exception("financial_summary_labor_hours_unavailable job=%s", job_id)
        labor_hours = 0.0

    accepted = db.execute(
        select(Estimate)
        .where(
            Estimate.job_id == _UUID(job_id),
            Estimate.status == "accepted",
            Estimate.deleted_at.is_(None),
        )
        .order_by(Estimate.accepted_at.desc())
        .limit(1)
    ).scalar_one_or_none()

    invoices = db.execute(
        select(Invoice)
        .where(Invoice.job_id == _UUID(job_id), Invoice.deleted_at.is_(None))
        .order_by(Invoice.created_at.desc())
    ).scalars().all()

    # payment_status describes FINAL billing — deposit invoices are money
    # BEFORE the work (2026-07-23) and must not flip this to "pending", or
    # the Build-invoice CTA disappears for every deposit-taking job (same
    # rule as core/billing_predicates.job_billed_exists). Deposit state is
    # surfaced separately (job payload "deposit" + the invoices list below).
    _final_invoices = [inv for inv in invoices if (inv.billing_type or "") != "deposit"]
    any_paid = any(inv.paid_at is not None for inv in _final_invoices)
    payment_status = "paid" if any_paid else ("pending" if _final_invoices else "no_invoice")

    return _jr({
        "job_id": job_id,
        "parts_cost": parts_cost,
        "labor_hours": round(labor_hours, 2),
        "accepted_quote": (
            {"id": str(accepted.id), "total": float(accepted.total or 0), "estimate_number": accepted.estimate_number}
            if accepted else None
        ),
        "invoices": [
            {
                "id": str(inv.id),
                "invoice_number": inv.invoice_number,
                "total": float(inv.total or 0),
                "balance_due": float(inv.balance_due or 0),
                "status": inv.status,
                # Without billing_type the Bill/collect dialog counted a
                # deposit invoice as "the job is invoiced" and hid the
                # Generate button — a deposit-taking job couldn't be final-
                # billed from the truck at all.
                "billing_type": inv.billing_type or "standard",
                "sent_at": inv.sent_at.isoformat() if inv.sent_at else None,
                # §11: the tech UI shows "awaiting office verification" and
                # disables Send with the reason — an invisible gate reads as
                # a broken button and gets retried (the §4 lesson).
                "verified_at": inv.verified_at.isoformat() if inv.verified_at else None,
            }
            for inv in invoices
        ],
        "payment_status": payment_status,
    })


# ---------------------------------------------------------------------------
# POST /api/mobile/jobs/{job_id}/invoice
# ---------------------------------------------------------------------------


@router.post("/jobs/{job_id}/invoice", response_model=None, status_code=201)
def mobile_create_invoice(
    job_id: str,
    payload: CreateInvoiceIn,
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Create + (optionally) email an invoice from the truck.

    Mirrors /api/invoices POST but tech-scoped: the calling user must
    own the job. When ``estimate_id`` is supplied (must be ``accepted``
    and on the same job), lines are copied from the estimate.
    """
    user = current_user or {}
    user_id = _user_id(user)
    tenant_id = _tenant_id(request)
    if not _job_belongs_to_tech(db, request, job_id, user_id):
        return _jr({"detail": "job not found or not assigned to you"}, 404)

    # Raw SQL — avoids SQLAlchemy Uuid type quirks across SQLite/PG when
    # the seed used a hyphenated UUID string vs. a 32-char hex.
    job_row = db.execute(
        _text(
            "SELECT customer_id, job_type FROM jobs WHERE id = :jid AND deleted_at IS NULL"
        ),
        {"jid": job_id},
    ).first()
    if job_row is None:
        return _jr({"detail": "job not found"}, 404)
    job_customer_id = job_row[0]
    job_job_type = job_row[1]

    # Estimate (optional — if present must be accepted + on this job).
    estimate: Estimate | None = None
    if payload.estimate_id:
        estimate = db.execute(
            select(Estimate).where(
                Estimate.id == _UUID(payload.estimate_id),
                Estimate.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
        if estimate is None or estimate.job_id != _UUID(job_id):
            return _jr({"detail": "estimate not found for this job"}, 404)
        if estimate.status != "accepted":
            return _jr(
                {"detail": f"estimate must be 'accepted' to invoice; current: {estimate.status}"},
                409,
            )
    else:
        # Plan §15.1 (audit round: precedence must be SERVER-side): if the job
        # has an accepted estimate, the customer agreed that price — bill from
        # it, never hourly, even when the caller forgot to pass estimate_id.
        estimate = db.execute(
            select(Estimate)
            .where(
                Estimate.job_id == _UUID(job_id),
                Estimate.status == "accepted",
                Estimate.deleted_at.is_(None),
            )
            .order_by(Estimate.accepted_at.desc())
            .limit(1)
        ).scalar_one_or_none()

    customer_id = None
    if job_customer_id is not None:
        if isinstance(job_customer_id, _UUID):
            customer_id = job_customer_id
        else:
            try:
                customer_id = _UUID(str(job_customer_id))
            except (ValueError, AttributeError):
                customer_id = None
    if customer_id is None:
        # 2026-05-11 — invoices.customer_id is NOT NULL. Refuse with a 400
        # so the tech app surfaces a clear message rather than a 500 from
        # the NOT NULL violation at db.flush().
        return _jr(
            {"detail": "Job has no customer assigned — set a customer before invoicing"},
            400,
        )
    invoice_date_value = date.today()
    due_date_value = invoice_date_value + timedelta(days=30)

    # M9 (money audit 2026-08-04): capture the RATE, not just the dollar tax.
    # This path stamped tax_amount with tax_rate left NULL, which puts the
    # invoice on _recalculate_invoice's legacy branch — the flat tax is then
    # preserved verbatim while the subtotal moves, so an office line edit grew
    # a $1,000 invoice to $1,500 with the tax frozen at $73.75.
    tax_rate_value = None
    if estimate is not None:
        from gdx_dispatch.modules.proposals.totals import compute_estimate_totals
        _t = compute_estimate_totals(estimate, db)
        subtotal_value = _t["subtotal"]
        tax_amount_value = _t["tax"]
        total_value = _t["total"]
        tax_rate_value = _t.get("tax_rate")
    else:
        subtotal_value = 0.0
        tax_amount_value = 0.0
        total_value = 0.0

    # Snapshot the source estimate's "total-only" display onto the invoice so
    # the invoice PDF the customer receives matches the estimate they already
    # saw. Best-effort — a features read must never block invoicing (mirrors
    # the office path in routers/invoices.py).
    hide_line_prices_value = False
    if estimate is not None:
        try:
            from gdx_dispatch.modules.estimates_features import (
                effective_hide_line_prices,
                get_features,
            )
            _hide_default = get_features(str(tenant_id)).hide_line_prices
            hide_line_prices_value = effective_hide_line_prices(
                estimate.hide_line_prices, _hide_default
            )
        except Exception:
            log.exception("mobile_invoice_hide_line_prices_resolve_failed")
            hide_line_prices_value = False

    # One implementation of "may this photo print on this invoice", shared
    # with the office create/PATCH paths — a hand-rolled copy here is how the
    # truck ends up able to attach a photo the office could not.
    from gdx_dispatch.routers.invoices import _validated_attached_photo_ids

    attached_photo_ids = _validated_attached_photo_ids(
        db, job_id=_UUID(job_id), raw_ids=payload.attached_photo_ids
    )

    invoice = Invoice(
        id=uuid4(),
        job_id=_UUID(job_id),
        attached_photo_ids=(_json.dumps(attached_photo_ids) if attached_photo_ids else None),
        # Provenance for deposit netting (2026-07-23).
        estimate_id=(estimate.id if estimate is not None else None),
        invoice_number=_next_invoice_number(db),
        billing_type="standard",
        sequence_number=1,
        subtotal=_money(subtotal_value),
        tax_rate=(Decimal(str(tax_rate_value)) if tax_rate_value else None),
        tax_amount=_money(tax_amount_value),
        total=_money(total_value),
        balance_due=_money(total_value),
        hide_line_prices=hide_line_prices_value,
        status="draft",
        invoice_date=invoice_date_value,
        due_date=due_date_value,
        notes=(payload.notes.strip() if payload.notes else None),
        public_token=secrets.token_urlsafe(48)[:64],
        locked=False,
        customer_id=customer_id,
        company_id=str(tenant_id),
    )
    db.add(invoice)
    db.flush()

    # Copy estimate → invoice lines via raw SQL (portable across SQLite/PG).
    # Two paths:
    #   - Proposal mode (accepted_tier_id set): single summary line per the
    #     chosen tier — the customer signed against ONE tier total, so the
    #     invoice mirrors that without ambiguity. Per-tier sub-lines stay
    #     on the Estimate for audit / commission analytics.
    #   - Plain mode: copy every estimate line verbatim (legacy behaviour).
    if estimate:
        if estimate.accepted_tier_id is not None:
            tid_val = estimate.accepted_tier_id
            tid_str = tid_val.hex if hasattr(tid_val, "hex") else str(tid_val).replace("-", "")
            tier_row = db.execute(
                _text(
                    """
                    SELECT tier_name, description, total_price
                    FROM proposal_tiers
                    WHERE id = :tid OR id = :tid_dashed
                    """
                ),
                {"tid": tid_str, "tid_dashed": str(tid_val)},
            ).first()
            # LINE-BUILT tier (2026-08-14): copy its lines with per-line
            # taxable flags instead of one all-taxable summary line. The
            # summary line was recalc-UNSTABLE for line-built tiers with
            # Labor: the seeded tax excludes labor (compute_estimate_totals'
            # accepted branch), but the first office _recalculate_invoice
            # (record_payment, any line edit) re-taxed the whole package —
            # silent growth after acceptance. Flat tiers have no labor to
            # exclude, so their summary line stays (and stays stable).
            from gdx_dispatch.modules.proposals.models import ProposalTierLine
            from gdx_dispatch.modules.proposals.totals import (
                _is_labor_line,
                _load_tax_labor_flag,
            )

            tier_lines = list(db.execute(
                select(ProposalTierLine)
                .where(ProposalTierLine.tier_id == estimate.accepted_tier_id)
                .order_by(ProposalTierLine.sort_order.asc(), ProposalTierLine.id.asc())
            ).scalars().all())
            if tier_lines:
                # F-75 zero-price gate (gate-audit catch): BOTH sibling
                # branches enforce it; a line-built tier must not dodge
                # tenant policy just because it came from the truck.
                if any(float(tl.unit_price or 0) <= 0 for tl in tier_lines):
                    from gdx_dispatch.modules.catalog_policy import get_policy
                    if get_policy(str(tenant_id)).block_zero_price_on_invoice:
                        db.rollback()
                        return _jr(
                            {"detail": "the accepted tier has an unpriced line — price it before invoicing (tenant policy blocks zero-price invoice lines)"},
                            422,
                        )
                _tax_labor = bool(_load_tax_labor_flag(db))
                line_sum = Decimal("0")
                for tl in tier_lines:
                    lt = _money(tl.line_total or 0)
                    db.add(InvoiceLine(
                        id=uuid4(),
                        invoice_id=invoice.id,
                        description=(tl.description or "Item")[:500],
                        quantity=int(tl.quantity or 1),
                        unit_price=_money(tl.unit_price or 0),
                        line_total=lt,
                        taxable=True if _tax_labor else not _is_labor_line(tl),
                        category=tl.category,
                        sort_order=tl.sort_order,
                        company_id=str(tenant_id),
                    ))
                    line_sum += lt
                # M7 (gate-audit catch): the seeded tax already subtracted
                # est.discount, so the lines must carry it too or the truck
                # over-bills by exactly the discount. Same materialized
                # negative line the plain branch writes.
                _disc = Decimal(str(getattr(estimate, "discount", None) or 0))
                if _disc > 0:
                    db.add(InvoiceLine(
                        id=uuid4(),
                        invoice_id=invoice.id,
                        description="Discount",
                        quantity=1,
                        unit_price=_money(-_disc),
                        line_total=_money(-_disc),
                        taxable=True,
                        sort_order=len(tier_lines) + 1,
                        company_id=str(tenant_id),
                    ))
                    line_sum -= _disc
                restate_invoice_header(invoice, line_sum)
            elif tier_row is not None:
                tier_name, tier_desc, tier_total = tier_row
                price = _money(tier_total or 0)
                # PR1-billing-capture: F-75 zero-price policy applies from
                # the truck too (this path bypassed the desktop guard).
                # Block-only — the tech app has no warning-banner surface,
                # so warn-mode is intentionally desktop-only.
                if float(price) <= 0:
                    from gdx_dispatch.modules.catalog_policy import get_policy
                    if get_policy(str(tenant_id)).block_zero_price_on_invoice:
                        db.rollback()
                        return _jr(
                            {"detail": "accepted tier has no price — price it before invoicing (tenant policy blocks zero-price invoice lines)"},
                            422,
                        )
                desc = (
                    f"{(estimate.label or 'Service').strip()} — "
                    f"{tier_name.title()} Tier"
                    + (f" ({tier_desc})" if tier_desc else "")
                )
                db.add(
                    InvoiceLine(
                        id=uuid4(),
                        invoice_id=invoice.id,
                        description=desc[:500],
                        quantity=1,
                        unit_price=price,
                        line_total=price,
                        sort_order=1,
                        company_id=str(tenant_id),
                    )
                )
                # Restate the header from the ONE line this branch wrote —
                # including tax. Assigning `total = price` dropped the tax
                # that was computed from the estimate a few lines above, so
                # the money-correctness invariant (migration 056) refused the
                # commit: "total 2090.00 != Σlines 2090.00 + tax 154.24". Every
                # tiered-proposal invoice generated from the truck 500'd on
                # that, which is why mobile_invoice_created has never written
                # an audit row in production. Found by driving a real phone,
                # 2026-08-12 — the tap fired, the server refused, and the
                # dialog just sat there.
                # One line was written; the header follows it, tax included.
                restate_invoice_header(invoice, price)
        else:
            # Plain estimate — copy all lines.
            line_rows = db.execute(
                _text(
                    """
                    SELECT description, quantity, unit_price, line_total, sort_order
                    FROM estimate_lines
                    WHERE estimate_id = :eid
                    ORDER BY sort_order ASC
                    """
                ),
                {"eid": str(estimate.id)},
            ).all()
            # PR1-billing-capture: same block-only F-75 guard as the tier
            # path — a $0 estimate line must not slip onto an invoice from
            # the truck when the tenant blocks zero-price invoicing.
            if any(float(ln[2] or 0) <= 0 for ln in line_rows):
                from gdx_dispatch.modules.catalog_policy import get_policy
                if get_policy(str(tenant_id)).block_zero_price_on_invoice:
                    db.rollback()
                    return _jr(
                        {"detail": "estimate contains a zero-price line — price it before invoicing (tenant policy blocks zero-price invoice lines)"},
                        422,
                    )
            for ln in line_rows:
                db.add(
                    InvoiceLine(
                        id=uuid4(),
                        invoice_id=invoice.id,
                        description=ln[0],
                        quantity=int(ln[1]),
                        unit_price=_money(ln[2]),
                        line_total=_money(ln[3]),
                        sort_order=int(ln[4]),
                        company_id=str(tenant_id),
                    )
                )
            # M7, on the truck's path this time (2026-08-13): materialize the
            # estimate's discount as a real negative line BEFORE the header is
            # restated. The copied lines are the estimate's PRE-discount
            # amounts while compute_estimate_totals took the discount off the
            # taxed base, so a header of "Σlines + tax" over-bills by exactly
            # the discount — a $1,000 estimate with $100 off invoices at
            # $1,066.38 instead of $966.38, and the totals invariant is
            # satisfied the whole time because the numbers are internally
            # consistent and simply wrong. The office path has carried this
            # line since the 2026-08-04 money audit; this one didn't, because
            # before the header fix it 500'd before it could be wrong.
            #
            # Taxable ON PURPOSE, same as invoices.py: the discount reduces the
            # taxable base upstream, so the line must reduce it here too.
            _est_discount = Decimal(str(getattr(estimate, "discount", None) or 0))
            if _est_discount > 0:
                db.add(
                    InvoiceLine(
                        id=uuid4(),
                        invoice_id=invoice.id,
                        description="Discount",
                        quantity=1,
                        unit_price=_money(-_est_discount),
                        line_total=_money(-_est_discount),
                        taxable=True,
                        category="discount",
                        sort_order=(max((int(ln[4]) for ln in line_rows), default=0) + 1),
                        company_id=str(tenant_id),
                    )
                )
            db.flush()
            new_subtotal = float(db.execute(
                _text("SELECT COALESCE(SUM(line_total), 0) FROM invoice_lines WHERE invoice_id = :iid"),
                {"iid": str(invoice.id)},
            ).scalar() or 0)
            # Same restatement as the tier branch — one implementation, so the
            # two cannot drift into disagreeing about tax again.
            restate_invoice_header(invoice, new_subtotal)

    if estimate is None:
        # Plan §8 — invoice FROM THE CLOSEOUT. No accepted estimate, so the
        # lanes decide. The line construction lives in
        # core/closeout_billing.build_closeout_lines — the ONE builder this
        # path shares with the closeout autodraft (2026-08-07); pricing
        # policy and the unpriced-parts rule are documented there.
        from gdx_dispatch.core.closeout_billing import build_closeout_lines
        from gdx_dispatch.core.closeouts import get_current_closeout

        closeout = get_current_closeout(db, _UUID(job_id))
        _new_lines_total = Decimal("0")
        if closeout is not None:
            _lines_added, _new_lines_total, _taxable_total = build_closeout_lines(
                db,
                tenant_id=str(tenant_id),
                invoice=invoice,
                closeout=closeout,
                job_type=job_job_type,
                job_id=str(job_id),
            )

        if _new_lines_total > 0:
            invoice.subtotal = _money(_new_lines_total)
            invoice.total = _money(_new_lines_total)
            invoice.balance_due = _money(_new_lines_total)

    # Deposit netting (2026-07-23): the truck's final invoice subtracts the
    # job's PAID deposit as a negative line and supersedes any unpaid
    # deposit remainder — same rules as the office create paths. Totals
    # hand-adjusted like the tier/line blocks above (this path never calls
    # _recalculate_invoice).
    from gdx_dispatch.modules.deposits import apply_deposits_to_final
    _dep_result = apply_deposits_to_final(db, invoice, actor=user_id)
    if _dep_result and float(_dep_result.get("deposit_paid_applied") or 0) > 0:
        _dep_applied = float(_dep_result["deposit_paid_applied"])
        invoice.subtotal = _money(float(invoice.subtotal or 0) - _dep_applied)
        invoice.total = _money(float(invoice.total or 0) - _dep_applied)
        invoice.balance_due = _money(float(invoice.total or 0))

    db.commit()
    db.refresh(invoice)

    log_audit_event_sync(
        db=db,
        tenant_id=tenant_id,
        user_id=user_id,
        action="mobile_invoice_created",
        entity_type="invoice",
        entity_id=str(invoice.id),
        details={
            "job_id": job_id,
            "estimate_id": str(estimate.id) if estimate else None,
            "subtotal": float(invoice.subtotal or 0),
        },
        request=request,
    )
    db.commit()

    # Plan §11 (audit): a tech-created invoice is NEVER verified at creation,
    # and NOTHING unverified reaches a customer — so send-on-create is
    # deferred, not honored. The office verifies from the billing screen, then
    # the invoice becomes sendable (mobile send / the office's own send). The
    # response's awaiting_verification tells the truck why it didn't go.
    # (This closes the bypass the old send_email=True branch left open: it
    # emailed unverified, memory-typed hours straight to the customer.)
    awaiting_verification = bool(payload.send_email)

    # Surface the netting result to the truck — the office create paths
    # return it (InvoiceCreateView renders the toast) but this one dropped
    # it, so a tech never learned a deposit was applied, superseded, voided,
    # or — worst — left UNAPPLIED (deposit exceeds the final total).
    resp_payload = _serialize_invoice(invoice, include_lines=True, db=db)
    if _dep_result:
        resp_payload["deposit_netting"] = _dep_result
    resp_payload["awaiting_verification"] = awaiting_verification
    # Unpriced parts left on the office checklist (audit: a partial invoice
    # must not look complete) — the office prices them before the invoice is
    # verified/sent. Counts EVERY capture source, not just 'closeout': a part
    # logged live during the job is exactly as unbilled, and a warning that
    # reports zero while money sits on the checklist is worse than no warning.
    _unpriced = db.execute(
        select(func.count()).select_from(JobPartNeeded).where(
            JobPartNeeded.job_id == str(job_id),
            JobPartNeeded.source.in_(("closeout", "mobile", "van")),
            JobPartNeeded.billed_invoice_id.is_(None),
            or_(JobPartNeeded.unit_price.is_(None), JobPartNeeded.unit_price <= 0),
        )
    ).scalar() or 0
    resp_payload["unpriced_parts_remaining"] = int(_unpriced)
    return _jr(resp_payload, 201)


def _send_invoice_email(
    db: Session,
    invoice: Invoice,
    *,
    tenant_id: str,
    user_id: str | None = None,
) -> bool:
    """Send the invoice email to the customer, if email available.

    Mirrors the path in invoices.send_estimate (lines 743–795) but for
    invoices. Failures are logged not raised — invoice creation already
    succeeded; "email failed" must not undo the financial record.

    Returns True only when a provider acknowledged delivery, so callers can
    gate the sent_at stamp on a real send instead of an attempt.
    """
    try:
        cust = None
        if invoice.customer_id is not None:
            cust = db.execute(
                select(Customer).where(
                    Customer.id == invoice.customer_id,
                    Customer.deleted_at.is_(None),
                )
            ).scalar_one_or_none()
        if cust is None or not cust.email:
            log.info("mobile_invoice_email_skipped no_customer_email invoice=%s", invoice.id)
            return False

        from gdx_dispatch.core.email_layout import email_branding as _email_branding
        from gdx_dispatch.core.transactional_email import send_transactional_email
        try:
            from gdx_dispatch.core.email_sender import build_invoice_email_html  # type: ignore[attr-defined]
        except Exception:
            build_invoice_email_html = None  # type: ignore[assignment]

        # One AppSettings read serves both the subject line and the branded
        # shell — two lookups drifted before.
        _branding = _email_branding(db)
        company_name = _branding["company_name"]

        # 2026-07-20 — attach the invoice PDF (built BEFORE the body so the
        # fallback wording can be honest about whether it's really attached).
        # Best-effort + size-guarded: a render failure or an oversized PDF
        # downgrades to the html-only mail rather than blocking the send or
        # having Graph reject the whole message.
        attachments = None
        try:
            import base64 as _b64

            from gdx_dispatch.core.pdf_generator import generate_invoice_pdf
            from gdx_dispatch.core.transactional_email import MAX_INLINE_ATTACHMENT_BYTES
            from gdx_dispatch.routers.pdf import _branding_payload, _invoice_payload, _template_config
            pdf_bytes = generate_invoice_pdf(
                invoice_data=_invoice_payload(invoice, cust, db),
                tenant_branding=_branding_payload(db),
                template_config=_template_config(db, "invoice"),
            )
            if len(pdf_bytes) > MAX_INLINE_ATTACHMENT_BYTES:
                log.warning(
                    "mobile_invoice_pdf_too_large_to_attach invoice=%s bytes=%s",
                    invoice.id, len(pdf_bytes),
                )
            else:
                attachments = [{
                    "name": f"invoice-{invoice.invoice_number or str(invoice.id)[:8]}.pdf",
                    "content_type": "application/pdf",
                    "content_base64": _b64.b64encode(pdf_bytes).decode("ascii"),
                }]
        except Exception:
            log.exception("mobile_invoice_pdf_attach_failed invoice=%s", invoice.id)

        # Same "View & Pay" CTA as the desktop send path; None (→ no
        # button) unless base URL + Stripe keys make the link chargeable.
        from gdx_dispatch.core.payments import public_pay_url
        if not invoice.public_token and float(invoice.balance_due or 0) > 0:
            invoice.public_token = secrets.token_urlsafe(48)[:64]
            db.commit()
            db.refresh(invoice)
        pay_url = (
            public_pay_url(invoice.public_token)
            if float(invoice.balance_due or 0) > 0
            else None
        )

        # Build a simple email body if no specialised builder exists.
        from gdx_dispatch.routers.pdf import _invoice_settlement
        _paid, _credits = _invoice_settlement(invoice, db)
        if build_invoice_email_html is not None:
            # deleted_at filter matches the canonical desktop send path
            # (routers/invoices.py) — without it a re-send after an office
            # edit emails soft-deleted ghost lines whose sum doesn't foot
            # with the printed subtotal.
            lines = db.execute(
                select(InvoiceLine)
                .where(
                    InvoiceLine.invoice_id == invoice.id,
                    InvoiceLine.deleted_at.is_(None),
                )
                .order_by(InvoiceLine.sort_order.asc())
            ).scalars().all()
            # subtotal/tax_amount/balance_due are REQUIRED kwargs with no
            # defaults (core/email_sender.py). This call omitted them from
            # the day mobile invoicing shipped: the TypeError was swallowed
            # by the enclosing try/except (and a `# type: ignore[misc]`
            # silenced the checker), so every truck "Generate & email" and
            # re-send returned False having delivered nothing.
            html = build_invoice_email_html(
                company_name=company_name,
                invoice_number=invoice.invoice_number,
                customer_name=cust.name or "Valued Customer",
                line_items=[
                    {
                        "description": ln.description,
                        "quantity": ln.quantity,
                        "unit_price": float(ln.unit_price or 0),
                        "line_total": float(ln.line_total or 0),
                    }
                    for ln in lines
                ],
                subtotal=float(invoice.subtotal or 0),
                tax_amount=float(invoice.tax_amount or 0),
                total=float(invoice.total or 0),
                balance_due=float(invoice.balance_due or 0),
                due_date=invoice.due_date.isoformat() if invoice.due_date else "",
                tax_rate=float(invoice.tax_rate) if getattr(invoice, "tax_rate", None) is not None else None,
                notes=invoice.notes or "",
                portal_url=pay_url or "",
                paid_to_date=_paid,
                credits_applied=_credits,
                branding=_branding,
            )
        else:
            html = (
                f"<p>Hi {cust.name or 'there'},</p>"
                f"<p>Please find your invoice <strong>#{invoice.invoice_number}</strong> "
                f"from {company_name} {'attached' if attachments else 'summarized below'}.</p>"
                f"<p><strong>Total: ${float(invoice.total or 0):,.2f}</strong></p>"
                f"<p>Due {invoice.due_date.isoformat() if invoice.due_date else 'on receipt'}.</p>"
                + (f'<p><a href="{pay_url}">Pay this invoice online</a></p>' if pay_url else "")
            )
        sent, _provider, _skip_reason = send_transactional_email(
            tenant_db=db,
            tenant_id=str(tenant_id),
            user_id=str(user_id) if user_id else None,
            to_email=cust.email,
            to_name=cust.name or "",
            subject=f"Invoice #{invoice.invoice_number} from {company_name}",
            html_body=html,
            attachments=attachments,
        )
        return bool(sent)
    except Exception:
        log.exception("mobile_invoice_email_failed invoice=%s", invoice.id)
        return False


# ---------------------------------------------------------------------------
# POST /api/mobile/invoices/{invoice_id}/send
# ---------------------------------------------------------------------------


@router.post("/invoices/{invoice_id}/send", response_model=None)
def mobile_send_invoice(
    invoice_id: str,
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Re-send the invoice email. Stamps sent_at only when a provider
    acknowledged delivery; the response carries email_sent so the tech
    console can be honest about non-delivery."""
    invoice = db.execute(
        select(Invoice).where(Invoice.id == _UUID(invoice_id), Invoice.deleted_at.is_(None))
    ).scalar_one_or_none()
    if invoice is None:
        return _jr({"detail": "invoice not found"}, 404)

    user = current_user or {}
    user_id = _user_id(user)
    tenant_id = _tenant_id(request)
    if not _job_belongs_to_tech(db, request, str(invoice.job_id), user_id):
        return _jr({"detail": "invoice not on a job assigned to you"}, 403)

    # Plan §11 (audit A5): NOTHING a tech types from a truck reaches a
    # customer until the office has verified the invoice. On the hourly lane
    # the closeout hours ARE the price, typed from memory — a fat-fingered 8
    # instead of 3 mails an $800 invoice with no second pair of eyes. The
    # office verifies from the billing screen; this endpoint just refuses
    # until then, with a message that says what happens next.
    if invoice.verified_at is None:
        return _jr(
            {
                "detail": (
                    "This invoice is waiting for office verification — it "
                    "will be sendable once the office has checked the hours."
                ),
                "awaiting_verification": True,
            },
            409,
        )

    # PR1-billing-capture (audit catch): the desktop /send now 409s on void,
    # but this path still EMAILED voided invoices to customers. Same guard.
    if invoice.status == "void":
        return _jr({"detail": "invoice is void — it cannot be re-sent"}, 409)

    delivered = _send_invoice_email(db, invoice, tenant_id=tenant_id)
    if invoice.status == "draft":
        transition_invoice_status(db, invoice, "sent")  # GL S5
    if delivered:
        invoice.sent_at = datetime.now(UTC)
        invoice.sent_via = "email"
    db.commit()
    db.refresh(invoice)

    log_audit_event_sync(
        db=db,
        tenant_id=tenant_id,
        user_id=user_id,
        action="mobile_invoice_sent",
        entity_type="invoice",
        entity_id=str(invoice.id),
        details={"invoice_number": invoice.invoice_number},
        request=request,
    )
    db.commit()
    resend_payload = _serialize_invoice(invoice)
    resend_payload["email_sent"] = bool(delivered)
    return _jr(resend_payload)


# ---------------------------------------------------------------------------
# GET /api/mobile/invoices/open
# ---------------------------------------------------------------------------


@router.get("/invoices/open", response_model=None)
def mobile_open_invoices(
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Issued, unpaid invoices the calling tech legitimately touches.

    "Issued" is load-bearing: drafts are excluded (see the WHERE clause) so a
    tech cannot collect against a bill the office has not reviewed.

    Why this exists: a technician has NO invoices permission at all
    (core/permissions.py — no invoices.read_all, no invoices.write), so
    ``GET /api/invoices`` 403s for the field tier and the mobile billing screen
    was unreachable for its only intended user. Granting invoices.read_all
    would hand every tech the entire receivables book; this returns only what
    their own work produced.

    Scope — a job the tech owns, via core/job_access.user_job_ids (the set form
    of the single-row ownership gate, so the two can never drift), plus deposit
    invoices whose ESTIMATE points at one of those jobs. That second clause is
    the one that matters here: a deposit minted at estimate acceptance carries
    job_id NULL until the estimate becomes a job, so a job_id-only filter would
    hide exactly the invoice the tech is trying to settle.

    Deliberately NOT covered: a deposit whose estimate has no job either. There
    is no ownership signal on such a row — including it would leak other
    people's money — so the answer for those is to record the payment at accept
    time (the capture form in the accept dialog), or let the office handle it.
    """
    tenant_id = _tenant_id(request)
    user_id = _user_id(current_user or {})
    if not user_id:
        return _jr({"detail": "unauthorized"}, 401)

    from gdx_dispatch.core.job_access import user_job_ids

    job_ids = user_job_ids(db, tenant_id, user_id)
    if not job_ids:
        return _jr({"invoices": []})

    # Chunked IN-lists: a tech with hundreds of jobs would otherwise build a
    # bind-parameter list long enough to trip SQLite's limit.
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for start in range(0, len(job_ids), _JOB_ID_CHUNK):
        chunk = job_ids[start:start + _JOB_ID_CHUNK]
        binds = {f"j{i}": v for i, v in enumerate(chunk)}
        placeholders = ", ".join(f":{k}" for k in binds)
        rows = db.execute(
            _text(
                "SELECT CAST(i.id AS TEXT) AS id, i.invoice_number, i.total, "
                "       i.balance_due, i.status, i.billing_type, i.due_date "
                "FROM invoices i "
                "LEFT JOIN estimates e ON CAST(e.id AS TEXT) = CAST(i.estimate_id AS TEXT) "
                f"WHERE i.company_id = :t AND i.deleted_at IS NULL "
                # DRAFTS ARE EXCLUDED ON PURPOSE. Since the closeout autodraft
                # (#286) every finished job mints a draft invoice on the tech's
                # own job. Listing those here would let a tech collect against
                # a bill the office has not reviewed — and recording a payment
                # drives balance_due to zero, which flips the draft straight to
                # "paid" and routes around the Ready-for-Billing verify gate
                # entirely. Only issued bills are collectable.
                "AND i.status NOT IN ('void', 'paid', 'draft') AND i.balance_due > 0 "
                f"AND (CAST(i.job_id AS TEXT) IN ({placeholders}) "
                f"     OR CAST(e.job_id AS TEXT) IN ({placeholders})) "
                "ORDER BY i.due_date ASC, i.invoice_number ASC"
            ),
            {"t": tenant_id, **binds},
        ).fetchall()
        for r in rows:
            if r[0] in seen:
                continue
            seen.add(r[0])
            out.append({
                "id": r[0],
                "invoice_number": r[1],
                "total": float(r[2] or 0),
                "balance_due": float(r[3] or 0),
                "status": r[4],
                "billing_type": r[5] or "standard",
                # Raw SQL hands back a date on Postgres and a plain string on
                # SQLite, so isoformat() alone crashes under test.
                "due_date": (
                    r[6].isoformat() if hasattr(r[6], "isoformat") else (str(r[6]) if r[6] else None)
                ),
            })
    # Sort and cap ACROSS chunks, not inside the loop. A per-chunk LIMIT would
    # both truncate silently and hand back an order that is only sorted within
    # each chunk — the soonest-due invoice could land below a later one purely
    # because of how the job ids happened to be batched.
    out.sort(key=lambda i: (i["due_date"] or "9999-12-31", i["invoice_number"] or ""))
    truncated = len(out) > _OPEN_INVOICE_CAP
    if truncated:
        log.info(
            "mobile_open_invoices_truncated user=%s total=%d cap=%d",
            user_id, len(out), _OPEN_INVOICE_CAP,
        )
        out = out[:_OPEN_INVOICE_CAP]
    # `truncated` is in the BODY, not just the log: a capped list that looks
    # complete is how "I never saw that invoice" starts, and a server-side log
    # line is invisible to the person holding the phone.
    return _jr({"invoices": out, "truncated": truncated})


# ---------------------------------------------------------------------------
# POST /api/mobile/invoices/{invoice_id}/send-receipt
# ---------------------------------------------------------------------------


@router.post("/invoices/{invoice_id}/send-receipt", response_model=None)
def mobile_send_receipt(
    invoice_id: str,
    payload: SendReceiptIn,
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Send a payment receipt for an office-recorded payment.

    Mobile cannot record payments (deferred — Stripe / cash capture is
    Sprint future). This is for the case where dispatch / office took
    a payment over the phone and the tech wants the customer to get a
    receipt in their inbox before leaving.
    """
    invoice = db.execute(
        select(Invoice).where(Invoice.id == _UUID(invoice_id), Invoice.deleted_at.is_(None))
    ).scalar_one_or_none()
    if invoice is None:
        return _jr({"detail": "invoice not found"}, 404)

    user = current_user or {}
    user_id = _user_id(user)
    tenant_id = _tenant_id(request)
    if not _job_belongs_to_tech(db, request, str(invoice.job_id), user_id):
        return _jr({"detail": "invoice not on a job assigned to you"}, 403)

    # §11 (audit round 2): a receipt EMAILS the customer too — same rule as
    # send. Nothing a tech triggers reaches the customer's inbox over
    # unverified hours.
    if invoice.verified_at is None:
        return _jr(
            {
                "detail": (
                    "This invoice is waiting for office verification — "
                    "receipts unlock once the office has checked the hours."
                ),
                "awaiting_verification": True,
            },
            409,
        )

    # Find the payment to receipt — explicit payment_id wins, else most recent.
    payment_row = None
    if payload.payment_id:
        payment_row = db.execute(
            _text(
                """
                SELECT id, amount, method, payment_date, reference
                FROM payments
                WHERE id = :pid AND invoice_id = :iid
                """
            ),
            {"pid": payload.payment_id, "iid": invoice_id},
        ).first()
    else:
        payment_row = db.execute(
            _text(
                """
                SELECT id, amount, method, payment_date, reference
                FROM payments
                WHERE invoice_id = :iid
                ORDER BY payment_date DESC, created_at DESC
                LIMIT 1
                """
            ),
            {"iid": invoice_id},
        ).first()

    if payment_row is None:
        return _jr(
            {"detail": "no payment found to receipt; office must record the payment first"},
            404,
        )

    # Best-effort email — we re-use the invoice email path with a "receipt"
    # subject prefix. A dedicated receipt template can land later.
    try:
        cust = None
        if invoice.customer_id is not None:
            cust = db.execute(
                select(Customer).where(
                    Customer.id == invoice.customer_id,
                    Customer.deleted_at.is_(None),
                )
            ).scalar_one_or_none()
        if cust and cust.email:
            from gdx_dispatch.core.email_sender import send_email
            html = (
                f"<p>Hi {cust.name or 'there'},</p>"
                f"<p>Thank you for your payment of "
                f"<strong>${float(payment_row[1] or 0):,.2f}</strong> "
                f"on invoice <strong>#{invoice.invoice_number}</strong>.</p>"
                f"<p>Method: {payment_row[2]}<br>"
                f"Date: {payment_row[3].isoformat() if payment_row[3] else 'today'}</p>"
            )
            send_email(
                db,
                str(tenant_id),
                cust.email,
                f"Receipt for invoice #{invoice.invoice_number}",
                html,
                cust.name,
            )
    except Exception:
        log.exception("mobile_send_receipt_failed invoice=%s", invoice.id)
        return _jr({"detail": "receipt email failed"}, 500)

    log_audit_event_sync(
        db=db,
        tenant_id=tenant_id,
        user_id=user_id,
        action="mobile_invoice_receipt_sent",
        entity_type="invoice",
        entity_id=str(invoice.id),
        details={"payment_id": str(payment_row[0])},
        request=request,
    )
    db.commit()
    return _jr({"sent": True, "invoice_id": str(invoice.id), "payment_id": str(payment_row[0])})

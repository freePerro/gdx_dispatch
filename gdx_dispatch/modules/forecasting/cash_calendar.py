"""Cash calendar: the next N days, dated, against the real bank balance.

One answer to "what's coming up, and is any day tight?". It adds no new data
source; it lines up what the app already knows by date:

  Starting point  the synced bank balances of the OPERATING accounts
                  (tenant_forecast_settings.operating_account_ids; until
                  chosen, every synced active account with a non-negative
                  balance, and the response says it is a default).
  Money in        open customer invoices on their due date; jobs scheduled in
                  the window at their accepted estimate total less what is
                  already invoiced on the job; QBO income templates.
  Money out       open vendor bills on their due date (their open balance,
                  net of recorded payments); recurring payments on their
                  projected dates; QBO bill/purchase templates.

Each dated row carries two running balances. ``balance_after`` counts every
row. ``balance_if_nothing_comes_in`` counts only money going out, because
customers paying on the due date is a hope and a bill's due date is not: if
that line crosses the floor, the day is tight whatever customers do.

What has no usable date is NOT forced onto one. Customer invoices already past
due, finished jobs not yet billed, and vendor bills past due or with no due
date at all, are reported beside the calendar with their totals
("unscheduled") and never enter the running balance — putting them all on today would invent a crash, and dropping them
would hide real money.

The window is exactly ``days`` dates: today through today + days - 1.

Nothing here writes. Amounts are summed as Decimal and returned as floats
rounded to cents, like the rest of this module.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import func, not_, select
from sqlalchemy.orm import Session

from gdx_dispatch.core.billing_predicates import job_billing_resolved
from gdx_dispatch.models.tenant_models import Customer, Invoice, Job
from gdx_dispatch.modules.bank_feeds.models import BankFeedAccount
from gdx_dispatch.modules.bank_feeds.service import tenant_zoneinfo
from gdx_dispatch.modules.forecasting.accuracy import _open_invoices
from gdx_dispatch.modules.forecasting.service import (
    DIRECTION_IN,
    DIRECTION_OUT,
    _combined_recurring,
    _observed_stream_projection,
    _qbo_template_projection,
    get_or_create_settings,
)
from gdx_dispatch.modules.proposals.models import Estimate
from gdx_dispatch.modules.vendor_invoices.models import STATUS_OPEN as BILL_STATUS_OPEN
from gdx_dispatch.modules.vendor_invoices.models import VendorInvoice
from gdx_dispatch.modules.vendor_invoices.payments import open_balance

MIN_DAYS = 1
MAX_DAYS = 90
DEFAULT_DAYS = 14
UNSCHEDULED_ITEM_LIMIT = 25

CERTAINTY_SCHEDULED = "scheduled"  # a due date someone owes us / we owe
CERTAINTY_EXPECTED = "expected"  # a projection (job not yet billed, recurring)

_CENT = Decimal("0.01")


def _money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(_CENT, rounding=ROUND_HALF_UP)


def _f(value: Decimal | None) -> float | None:
    return None if value is None else float(value.quantize(_CENT, rounding=ROUND_HALF_UP))


def _iso(value: date | datetime | None) -> str | None:
    return value.isoformat() if value else None


# ── starting balance ────────────────────────────────────────────────────────


def _accounts(db: Session, chosen_ids: list[str] | None) -> dict[str, Any]:
    rows = db.execute(select(BankFeedAccount)).scalars().all()
    chosen = {str(i) for i in (chosen_ids or [])}
    included: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for a in sorted(rows, key=lambda r: (r.name or "").lower()):
        info = {
            "id": str(a.id),
            "name": a.name or "Unnamed account",
            "balance": _f(_money(a.balance)) if a.balance is not None else None,
            "balance_as_of": _iso(a.balance_as_of),
        }
        if chosen:
            if str(a.id) in chosen:
                if a.balance is None:
                    excluded.append({**info, "reason": "No balance has synced for this account yet."})
                else:
                    included.append(info)
            else:
                excluded.append({**info, "reason": "Not chosen as an operating account."})
            continue
        # Default selection, until someone chooses.
        if not a.sync_enabled or a.is_inactive:
            excluded.append({**info, "reason": "Syncing is off for this account."})
        elif a.balance is None:
            excluded.append({**info, "reason": "No balance has synced for this account yet."})
        elif _money(a.balance) < 0:
            excluded.append({**info, "reason": "Negative balance; looks like a loan or credit line."})
        else:
            included.append(info)
    starting = sum((_money(a["balance"]) for a in included), Decimal("0"))
    as_of_values = [a["balance_as_of"] for a in included if a["balance_as_of"]]
    return {
        "chosen": bool(chosen),
        "included": included,
        "excluded": excluded,
        "starting_balance": starting,
        # The OLDEST sync among the included accounts: the total is no fresher.
        "balance_as_of": min(as_of_values) if as_of_values else None,
    }


# ── money in ────────────────────────────────────────────────────────────────


def _customer_names(db: Session, invoices: list[Invoice]) -> dict[Any, str]:
    """Invoice id → customer name. invoices.customer_id is NOT NULL (ORM and
    the live schema, checked 2026-09-29), so there is no job fallback."""
    ids = {inv.customer_id for inv in invoices}
    names: dict[Any, str] = {}
    if ids:
        for cid, name in db.execute(select(Customer.id, Customer.name).where(Customer.id.in_(ids))).all():
            names[cid] = name
    return {inv.id: names.get(inv.customer_id) or "Customer" for inv in invoices}


# Pre-completion stages a dated job can be in. "estimate" is deliberately not
# here: an estimate visit is a sales call, not money anyone has agreed to pay.
JOB_STAGES = ("scheduled", "service_call", "in_progress")
BILLED_INVOICE_STATUSES = ("sent", "overdue", "paid")


def _accepted_totals(db: Session, ids: list[Any]) -> dict[Any, Decimal]:
    """Job id → total of its ACCEPTED, non-deleted estimates."""
    return {
        jid: _money(total)
        for jid, total in db.execute(
            select(Estimate.job_id, func.sum(Estimate.total))
            .where(Estimate.job_id.in_(ids), Estimate.status == "accepted", Estimate.deleted_at.is_(None))
            .group_by(Estimate.job_id)
        ).all()
    }


def _unbilled_value(db: Session, jobs: list[Job]) -> tuple[dict[Any, Decimal], set[Any], set[Any]]:
    """What each job is still expected to bring in: ACCEPTED, non-deleted
    estimates, minus what is already BILLED on it — sent/overdue invoices
    (each already a row or a past-due line of its own) and paid ones (already
    in the bank). A draft invoice is neither, so it is not subtracted; the job
    carries that money until the invoice is sent.

    Returns (remaining > 0 by job id, ids with no accepted estimate, ids with
    something billed)."""
    if not jobs:
        return {}, set(), set()
    ids = [j.id for j in jobs]
    accepted = _accepted_totals(db, ids)
    billed: dict[Any, Decimal] = {}
    for jid, total in db.execute(
        select(Invoice.job_id, func.sum(Invoice.total))
        .where(Invoice.job_id.in_(ids), Invoice.status.in_(BILLED_INVOICE_STATUSES), Invoice.deleted_at.is_(None))
        .group_by(Invoice.job_id)
    ).all():
        billed[jid] = _money(total)
    remaining = {
        jid: accepted[jid] - billed.get(jid, Decimal("0"))
        for jid in accepted
        if accepted[jid] - billed.get(jid, Decimal("0")) > 0
    }
    return remaining, set(ids) - set(accepted), set(billed)


def _job_labels(db: Session, jobs: list[Job]) -> dict[Any, str]:
    """"Customer · Job <n> · title". Most jobs carry no job_number (207 of 262
    on production, 2026-09-29), and titles repeat ("Door service"), so the
    customer name is what tells two rows apart."""
    cids = {j.customer_id for j in jobs if j.customer_id}
    names: dict[Any, str] = {}
    if cids:
        for cid, name in db.execute(select(Customer.id, Customer.name).where(Customer.id.in_(cids))).all():
            names[cid] = name
    return {
        j.id: " · ".join(
            p for p in (names.get(j.customer_id), f"Job {j.job_number}" if j.job_number else None, j.title) if p
        )
        for j in jobs
    }


def _scheduled_jobs(db: Session, tz, today: date, last_day: date) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Jobs scheduled in the window (shop-local days) with their unbilled
    value. Not the revenue forecast's jobs projection, which sums every
    estimate on a job and is discounted by a realization rate; a dated cash
    row cannot be."""
    start = datetime.combine(today, datetime.min.time(), tz).astimezone(UTC).replace(tzinfo=None)
    end = datetime.combine(last_day + timedelta(days=1), datetime.min.time(), tz).astimezone(UTC).replace(tzinfo=None)
    jobs = list(db.execute(
        select(Job).where(
            Job.scheduled_at.is_not(None),
            Job.scheduled_at >= start,
            Job.scheduled_at < end,
            Job.lifecycle_stage.in_(JOB_STAGES),
            Job.deleted_at.is_(None),
        )
    ).scalars().all())
    remaining, no_estimate, billed = _unbilled_value(db, jobs)
    labels = _job_labels(db, jobs)
    notes = {
        "jobs_without_accepted_estimate": len(no_estimate),
        "jobs_already_invoiced": sum(1 for j in jobs if j.id not in remaining and j.id not in no_estimate),
    }
    out: list[dict[str, Any]] = []
    for j in jobs:
        if j.id not in remaining:
            continue
        scheduled = j.scheduled_at if j.scheduled_at.tzinfo else j.scheduled_at.replace(tzinfo=UTC)
        out.append({
            "date": scheduled.astimezone(tz).date(),
            "label": labels[j.id],
            "amount": remaining[j.id],
            "job_id": str(j.id),
            "partly_invoiced": j.id in billed,
        })
    return out, notes


def _finished_unbilled(db: Session, tz) -> list[dict[str, Any]]:
    """Completed jobs whose billing is NOT resolved, by the app's one rule
    (core/billing_predicates.job_billing_resolved: no finalized non-deposit
    invoice, and not marked "not billable" by the office) — the same set the
    Ready for Billing list shows. Valued at its latest draft invoice; else at
    the accepted estimate total less billed deposits; else listed with no
    amount ("not priced yet"). Money the business has earned —
    but has not asked for, so it has no date: beside the calendar, never in
    the running balance.

    A deposit does not resolve billing: an installation with a paid deposit
    and no final invoice still owes the rest, and the scheduled-job row that
    carried it is gone the moment the job completes. Not "accepted minus
    everything billed": once a final invoice exists it is built from the
    closeout, not copied from the estimate, so a paid job would show the gap
    as owed (on production, $1,948.86 of such gaps on two paid jobs)."""
    jobs = list(db.execute(
        select(Job).where(
            Job.lifecycle_stage == "completed",
            Job.deleted_at.is_(None),
            not_(job_billing_resolved()),
        )
    ).scalars().all())
    if not jobs:
        return []
    ids = [j.id for j in jobs]
    accepted = _accepted_totals(db, ids)
    deposits: dict[Any, Decimal] = {}
    for jid, total in db.execute(
        select(Invoice.job_id, func.sum(Invoice.total))
        .where(
            Invoice.job_id.in_(ids),
            Invoice.billing_type == "deposit",
            Invoice.status.in_(BILLED_INVOICE_STATUSES),
            Invoice.deleted_at.is_(None),
        )
        .group_by(Invoice.job_id)
    ).all():
        deposits[jid] = _money(total)
    # A repair is priced at closeout, not by an estimate: its value is the
    # closeout's draft invoice. Only the LATEST live non-deposit draft counts,
    # as in Ready for Billing — an office-made draft beside the automatic one
    # must not double it.
    latest_draft: dict[Any, Decimal] = {}
    for jid, total in db.execute(
        select(Invoice.job_id, Invoice.total)
        .where(
            Invoice.job_id.in_(ids),
            Invoice.status == "draft",
            Invoice.billing_type != "deposit",
            Invoice.deleted_at.is_(None),
        )
        .order_by(Invoice.job_id, Invoice.created_at.desc())
    ).all():
        latest_draft.setdefault(jid, _money(total))
    # The latest draft wins when there is one: it is what will be billed,
    # change orders included (they are copied onto the invoice, never onto
    # the estimate), and a final invoice already nets its deposits. Only a
    # job with no draft falls back to its accepted estimate less deposits.
    owed: dict[Any, Decimal | None] = {}
    for j in jobs:
        from_estimate = accepted[j.id] - deposits.get(j.id, Decimal("0")) if j.id in accepted else None
        if latest_draft.get(j.id, Decimal("0")) > 0:
            owed[j.id] = latest_draft[j.id]
        elif from_estimate is not None and from_estimate > 0:
            owed[j.id] = from_estimate
        else:
            owed[j.id] = None  # "not priced yet"
    # Every unresolved finished job is listed — exactly the set Ready for
    # Billing shows. None is dropped for having no value, or for deposits
    # that cover its estimate: billing is still unresolved.
    done = jobs
    labels = _job_labels(db, done)

    def _local_day(when: datetime | None) -> str | None:
        if when is None:
            return None
        return (when if when.tzinfo else when.replace(tzinfo=UTC)).astimezone(tz).date().isoformat()

    return [
        {
            "label": labels[j.id],
            "amount": owed[j.id],
            # "done <date>": the shop-local day it was completed, else the
            # day it was scheduled; many completed jobs carry neither.
            "due_date": _local_day(j.completed_at or j.scheduled_at),
            "link": {"kind": "job", "id": str(j.id)},
        }
        for j in done
    ]


# ── money out ───────────────────────────────────────────────────────────────


def _open_bills(db: Session) -> list[tuple[VendorInvoice, Decimal]]:
    bills = db.execute(
        select(VendorInvoice).where(
            VendorInvoice.status == BILL_STATUS_OPEN,
            VendorInvoice.deleted_at.is_(None),
        )
    ).scalars().all()
    out: list[tuple[VendorInvoice, Decimal]] = []
    for b in bills:
        bal = _money(open_balance(db, b))
        if bal > 0:
            out.append((b, bal))
    return out


# ── assembly ────────────────────────────────────────────────────────────────


def cash_calendar(db: Session, *, days: int = DEFAULT_DAYS, today: date | None = None) -> dict[str, Any]:
    if not MIN_DAYS <= days <= MAX_DAYS:
        raise ValueError(f"days must be between {MIN_DAYS} and {MAX_DAYS}")
    tz = tenant_zoneinfo(db)
    # The shop's calendar day, not the server's: the container runs on UTC,
    # which is already tomorrow after ~7pm in Minnesota.
    today = today or datetime.now(tz).date()
    last_day = today + timedelta(days=days - 1)
    settings = get_or_create_settings(db)
    floor = _money(settings.cash_floor) if settings.cash_floor is not None else None

    accounts = _accounts(db, settings.operating_account_ids)
    rows: list[dict[str, Any]] = []

    def add(d: date, direction: str, kind: str, label: str, amount: Decimal, certainty: str,
            link: dict[str, str] | None, detail: str | None = None) -> None:
        rows.append({
            "date": d.isoformat(),
            "direction": direction,
            "kind": kind,
            "label": label,
            "detail": detail,
            "amount": amount,
            "certainty": certainty,
            "link": link,
        })

    # Customer invoices.
    invoices = _open_invoices(db)
    names = _customer_names(db, invoices)
    overdue_invoices: list[dict[str, Any]] = []
    for inv in invoices:
        amount = _money(inv.balance_due)
        label = f"{names[inv.id]} · invoice {inv.invoice_number}"
        link = {"kind": "invoice", "id": str(inv.id)}
        if inv.due_date is None or inv.due_date < today:
            overdue_invoices.append({
                "label": label, "amount": amount, "due_date": _iso(inv.due_date), "link": link,
            })
        elif inv.due_date <= last_day:
            add(inv.due_date, DIRECTION_IN, "invoice", label, amount, CERTAINTY_SCHEDULED, link, "Due")

    # Scheduled jobs: accepted estimate total not yet invoiced.
    job_rows, job_notes = _scheduled_jobs(db, tz, today, last_day)
    for j in job_rows:
        detail = "Scheduled; accepted estimate" + (" less what is already invoiced" if j["partly_invoiced"] else "")
        add(j["date"], DIRECTION_IN, "job", j["label"], j["amount"], CERTAINTY_EXPECTED,
            {"kind": "job", "id": j["job_id"]}, detail)

    # Recurring payments and QBO templates (both directions).
    recurring = _combined_recurring(
        _qbo_template_projection(db, today, days - 1),
        _observed_stream_projection(db, today, days - 1),
    )
    for it in recurring["items"]:
        if it["direction"] not in (DIRECTION_IN, DIRECTION_OUT):
            continue
        when = date.fromisoformat(it["next_date"])
        if not today <= when <= last_day:
            continue
        if it.get("stream_id"):
            label = it.get("label") or it.get("payee_pattern") or "Recurring payment"
            link = {"kind": "recurring_stream", "id": it["stream_id"]}
            kind = "recurring"
        else:
            label = it.get("name") or it.get("customer_name") or "QuickBooks recurring"
            link = None
            kind = "qbo_template"
        add(when, it["direction"], kind, label, _money(it["amount"]), CERTAINTY_EXPECTED, link,
            "Recurring")

    # Vendor bills.
    unscheduled_bills: list[dict[str, Any]] = []
    for bill, bal in _open_bills(db):
        label = f"{bill.vendor_name_raw} · bill {bill.invoice_number}"
        link = {"kind": "vendor_bill", "id": str(bill.id)}
        if bill.due_date is None or bill.due_date < today:
            unscheduled_bills.append({
                "label": label, "amount": bal, "due_date": _iso(bill.due_date), "link": link,
            })
        elif bill.due_date <= last_day:
            add(bill.due_date, DIRECTION_OUT, "vendor_bill", label, bal, CERTAINTY_SCHEDULED, link, "Due")

    # Money out first within a day: the low point of a day is after its bills.
    rows.sort(key=lambda r: (r["date"], 0 if r["direction"] == DIRECTION_OUT else 1, r["label"]))

    balance = accounts["starting_balance"]
    worst = accounts["starting_balance"]
    total_in = Decimal("0")
    total_out = Decimal("0")
    lowest = (balance, today.isoformat())
    lowest_worst = (worst, today.isoformat())
    first_below = today.isoformat() if floor is not None and balance < floor else None
    first_below_worst = first_below
    for r in rows:
        if r["direction"] == DIRECTION_IN:
            balance += r["amount"]
            total_in += r["amount"]
        else:
            balance -= r["amount"]
            worst -= r["amount"]
            total_out += r["amount"]
        r["balance_after"] = balance
        r["balance_if_nothing_comes_in"] = worst
        if balance < lowest[0]:
            lowest = (balance, r["date"])
        if worst < lowest_worst[0]:
            lowest_worst = (worst, r["date"])
        if floor is not None:
            if first_below is None and balance < floor:
                first_below = r["date"]
            if first_below_worst is None and worst < floor:
                first_below_worst = r["date"]

    def _items(items: list[dict[str, Any]]) -> dict[str, Any]:
        items = sorted(items, key=lambda i: (i["due_date"] or "", i["label"]))
        return {
            "count": len(items),
            "total": _f(sum((i["amount"] for i in items if i["amount"] is not None), Decimal("0"))),
            # Items with no amount (a finished job nobody has priced yet).
            "unpriced": sum(1 for i in items if i["amount"] is None),
            "items": [{**i, "amount": _f(i["amount"])} for i in items[:UNSCHEDULED_ITEM_LIMIT]],
        }

    return {
        "as_of": today.isoformat(),
        "days": days,
        "last_day": last_day.isoformat(),
        "floor": _f(floor),
        "accounts": {
            "chosen": accounts["chosen"],
            "included": accounts["included"],
            "excluded": accounts["excluded"],
        },
        "starting_balance": _f(accounts["starting_balance"]),
        "balance_as_of": accounts["balance_as_of"],
        "rows": [
            {
                **r,
                "amount": _f(r["amount"]),
                "balance_after": _f(r["balance_after"]),
                "balance_if_nothing_comes_in": _f(r["balance_if_nothing_comes_in"]),
            }
            for r in rows
        ],
        "summary": {
            "total_in": _f(total_in),
            "total_out": _f(total_out),
            "ending_balance": _f(balance),
            "lowest_balance": _f(lowest[0]),
            "lowest_date": lowest[1],
            "first_below_floor": first_below,
            "lowest_if_nothing_comes_in": _f(lowest_worst[0]),
            "lowest_if_nothing_comes_in_date": lowest_worst[1],
            "first_below_floor_if_nothing_comes_in": first_below_worst,
        },
        "unscheduled": {
            "customer_invoices_past_due": _items(overdue_invoices),
            "finished_jobs_not_billed": _items(_finished_unbilled(db, tz)),
            "vendor_bills_past_due_or_undated": _items(unscheduled_bills),
        },
        "notes": job_notes,
    }


def parse_account_ids(db: Session, ids: list[str]) -> list[str]:
    """Validate operating-account ids against bank_feed_accounts; returns the
    canonical string ids or raises ValueError naming the unknown ones."""
    wanted: list[UUID] = []
    bad: list[str] = []
    for raw in ids:
        try:
            wanted.append(UUID(str(raw)))
        except ValueError:
            bad.append(str(raw))
    if wanted:
        found = {
            r for (r,) in db.execute(select(BankFeedAccount.id).where(BankFeedAccount.id.in_(wanted))).all()
        }
        bad.extend(str(w) for w in wanted if w not in found)
    if bad:
        raise ValueError(f"unknown bank account id(s): {', '.join(sorted(bad))}")
    return [str(w) for w in dict.fromkeys(wanted)]

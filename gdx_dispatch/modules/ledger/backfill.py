"""Cutover backfill (S10, spec §5.7 / plan §S10).

Three idempotent phases, run by ``tools/gl_backfill.py`` in the §11 rollout
order:

1. **opening** (flag may still be OFF — the rollout posts and reconciles
   opening balances BEFORE the flip): per open pre-cutover invoice, post the
   P8 anchor (AR debit / 3950 credit, ``opening_balance_cents`` formula), and
   ensure a period lock at ``cutover − 1 day`` so nothing back-posts into the
   pre-ledger era.
2. **replay** (flag must be ON): re-drive every post-cutover money event
   through the SAME builders the live chokepoint uses —
   ``repost_invoice_issuance`` / ``resettle_invoice_payments`` /
   ``post_credit_memo``·``post_refund``·``post_credit_application`` /
   ``repost_expense`` — so a backfill replay lands on keys identical to what
   live posting would have minted (§5.6: same state → same content → same
   key). Events the period lock refuses are collected, not crashed on.
3. **report**: per-invoice AR reconciliation — operational ``balance_due``
   vs the ledger's attributable AR — plus the GL-wide AR total. This is the
   §5.7 hand-check against QBO's aging; discrepancies are resolved by hand
   before the flag flips.

Everything here is re-runnable by construction: posting is content-keyed
idempotent, the lock ensure is existence-checked, and the report is
read-only.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from gdx_dispatch.modules.ledger import service as ledger_service
from gdx_dispatch.modules.ledger.coa import LedgerConfigError
from gdx_dispatch.modules.ledger.engine import PeriodLockedError, reverse_entry
from gdx_dispatch.modules.ledger.models import (
    ENTRY_STATUS_POSTED,
    GlJournalEntry,
    GlPeriodLock,
)
from gdx_dispatch.modules.ledger.money import to_cents
from gdx_dispatch.modules.ledger.rules import (
    EVENT_OPENING,
    EVENT_OPENING_PAYMENT_VOID,
    ISSUED_STATUSES,
    ExpenseCompositionError,
    IssuanceCompositionError,
    _created_date,
    _dec,
    _effective_at,
    _existed_at_cutover,
    ar_role_lines,
    has_opening_anchor,
    invoice_ar_balance_cents,
    opening_balance_cents,
    post_credit_application,
    post_credit_memo,
    post_opening_balance,
    post_refund,
    pre_cutover_era,
    repost_expense,
    repost_invoice_issuance,
    resettle_invoice_payments,
)

log = logging.getLogger(__name__)

ACTOR = "gl_backfill"


@dataclass
class PhaseResult:
    posted: int = 0  # events processed (idempotent replays count — see entries_created)
    skipped: int = 0
    entries_created: int = 0  # NEW journal entries this run actually minted
    locked: list[str] = field(default_factory=list)  # period-lock refusals
    refused: list[str] = field(default_factory=list)  # data that won't reconcile
    rows: list[dict] = field(default_factory=list)


def _entry_count(session: Session, company_id: str) -> int:
    from sqlalchemy import func

    return session.scalar(
        select(func.count()).select_from(GlJournalEntry).where(
            GlJournalEntry.company_id == company_id
        )
    ) or 0


def _require_cutover(session: Session, company_id: str) -> date:
    settings = ledger_service.get_gl_settings(session, company_id)
    cutover = settings.cutover_month if settings else None
    if cutover is None:
        raise LedgerConfigError(
            "cutover_month is not set — plan the cutover on the Accounting "
            "Settings page first"
        )
    return cutover


def ensure_cutover_lock(session: Session, company_id: str, cutover: date) -> bool:
    """Lock every period before the cutover date. Idempotent: no-op when an
    equal-or-later lock already exists. The P8 entries themselves post AT the
    cutover date, which stays open."""
    lock_through = cutover - timedelta(days=1)
    latest = session.scalar(
        select(GlPeriodLock.lock_date)
        .where(GlPeriodLock.company_id == company_id)
        .order_by(GlPeriodLock.lock_date.desc())
        .limit(1)
    )
    if latest is not None and latest >= lock_through:
        return False
    session.add(
        GlPeriodLock(
            lock_date=lock_through,
            note=f"cutover lock — pre-ledger era closed through {lock_through}",
            created_by=ACTOR,
            company_id=company_id,
        )
    )
    session.flush()
    return True


def _company_invoices(session: Session, company_id: str):
    from gdx_dispatch.models.tenant_models import Invoice

    return session.scalars(
        select(Invoice).where(
            Invoice.company_id == company_id,
            Invoice.deleted_at.is_(None),
        )
    ).all()


def run_opening(session: Session, company_id: str) -> PhaseResult:
    """Phase 1 — P8 anchors for every open pre-cutover invoice + the era
    lock. Runs with the posting flag in either state (§11 posts these before
    the flip). Skips drafts (not receivables) and voids (net-zero either
    way — see spec §5.7 / the S10 PR discussion)."""
    cutover = _require_cutover(session, company_id)
    result = PhaseResult()
    if ensure_cutover_lock(session, company_id, cutover):
        result.rows.append({"event": "period_lock", "through": str(cutover - timedelta(days=1))})

    for invoice in _company_invoices(session, company_id):
        if invoice.status not in ISSUED_STATUSES:
            result.skipped += 1
            continue
        if _effective_at(invoice) >= cutover:
            result.skipped += 1
            continue
        opening = opening_balance_cents(session, invoice, cutover)
        row = {
            "invoice_number": invoice.invoice_number,
            "invoice_id": str(invoice.id),
            "customer_id": str(invoice.customer_id) if invoice.customer_id else None,
            "effective_at": str(_effective_at(invoice)),
            "opening_cents": opening,
        }
        if opening <= 0:
            # settled (or over-credited) at cutover — nothing open to anchor.
            # opening < 0 = the customer was OWED at cutover; that credit
            # stays in the QBO-era books (no 2300 minted) — hand-review.
            row["posted"] = False
            row["overpaid_at_cutover"] = opening < 0
            result.skipped += 1
        else:
            entry = post_opening_balance(session, invoice, actor=ACTOR)
            row["posted"] = entry is not None
            if entry is not None:
                result.posted += 1
        result.rows.append(row)
    return result


def run_replay(session: Session, company_id: str) -> PhaseResult:
    """Phase 2 — replay post-cutover money events through the live builders.
    Requires the flag ON (the rules it drives are flag-gated; a silent
    no-op replay would read as success)."""
    if not ledger_service.ledger_posting_enabled(session, company_id):
        raise LedgerConfigError(
            "ledger_posting_enabled is off — replay re-drives the flag-gated "
            "posting rules and would silently do nothing. Flip the flag "
            "(Accounting Settings → enable posting) first."
        )
    cutover = _require_cutover(session, company_id)
    from gdx_dispatch.models.tenant_models import Expense, InvoiceAdjustment

    result = PhaseResult()
    entries_at_start = _entry_count(session, company_id)

    all_invoices = _company_invoices(session, company_id)
    invoices = [i for i in all_invoices if i.status != "void"]
    by_id = {i.id: i for i in invoices}

    # Issuances (post-cutover invoices; era invoices anchor inside repost).
    for invoice in invoices:
        if invoice.status not in ISSUED_STATUSES:
            continue
        try:
            repost_invoice_issuance(session, invoice, actor=ACTOR)
            result.posted += 1
        except PeriodLockedError as exc:
            result.locked.append(f"invoice {invoice.invoice_number}: {exc}")
        except IssuanceCompositionError as exc:
            # Legacy rows whose total ≠ lines + tax beyond rounding: refuse
            # THIS invoice into the report, never the whole replay.
            result.refused.append(f"invoice {invoice.invoice_number}: {exc}")

    # Adjustments — dispatch by kind; opening-era adjustments on anchored
    # invoices are inside the P8 amount and stay off the ledger.
    poster_by_kind = {
        "credit_memo": post_credit_memo,
        "refund": post_refund,
        "credit_applied": post_credit_application,
    }
    adjustments = (
        session.scalars(
            select(InvoiceAdjustment).where(
                InvoiceAdjustment.invoice_id.in_(list(by_id))
            )
        ).all()
        if by_id
        else []
    )
    for adjustment in adjustments:
        invoice = by_id.get(adjustment.invoice_id)
        if invoice is None:
            continue
        if pre_cutover_era(session, invoice) and _existed_at_cutover(adjustment, cutover):
            # inside the opening amount (or the settledness) — stays off-ledger
            result.skipped += 1
            continue
        poster = poster_by_kind.get(adjustment.kind)
        if poster is None:
            result.skipped += 1
            continue
        try:
            poster(session, adjustment, invoice, actor=ACTOR)
            result.posted += 1
        except PeriodLockedError as exc:
            result.locked.append(
                f"adjustment {adjustment.id} on {invoice.invoice_number}: {exc}"
            )

    # Payments — resettle per invoice reposts every payment at current state
    # (and skips opening-era rows on anchored invoices).
    #
    # GDXA-1220: a void invoice still holding a LIVE payment (money the Stripe
    # recorder booked after the void, #422) is resettled too. Before the fix
    # that payment credited 1200 AR on an invoice with no AR left; the replay
    # is what moves an entry already posted that way onto 2300. Void invoices
    # with no live payment stay out, exactly as before.
    #
    # Pre-cutover-era voids stay out too, and are named for hand review: a
    # late-learned era payment on one resettles through `post_era_settlement`,
    # which credits AR from 3950 and does not know about voids — on an
    # invoice with no anchor that drives AR negative (audit 2026-10-15,
    # executed). What such money should post to is an accounting decision,
    # not something a replay may guess.
    from gdx_dispatch.models.tenant_models import Payment

    voids_with_live_money = []
    for i in all_invoices:
        if i.status != "void" or session.scalar(
            select(Payment.id).where(
                Payment.invoice_id == i.id, Payment.voided_at.is_(None)
            ).limit(1)
        ) is None:
            continue
        if pre_cutover_era(session, i):
            result.refused.append(
                f"void pre-cutover invoice {i.invoice_number} holds a live "
                "payment — not replayed; review by hand"
            )
            continue
        voids_with_live_money.append(i)
    for invoice in [*invoices, *voids_with_live_money]:
        try:
            resettle_invoice_payments(session, invoice, actor=ACTOR)
        except PeriodLockedError as exc:
            result.locked.append(f"payments on {invoice.invoice_number}: {exc}")

    # Expenses — post-cutover dated only; pre-cutover expense history stays
    # off-ledger (P8 is AR-only; spec §5.7).
    expenses = session.scalars(
        select(Expense).where(Expense.company_id == company_id)
    ).all()
    for expense in expenses:
        when = expense.date or _created_date(expense)
        if when is None or when < cutover:
            result.skipped += 1
            continue
        try:
            repost_expense(session, expense, actor=ACTOR)
            result.posted += 1
        except PeriodLockedError as exc:
            result.locked.append(f"expense {expense.id}: {exc}")
        except ExpenseCompositionError as exc:
            result.refused.append(f"expense {expense.id}: {exc}")

    result.entries_created = _entry_count(session, company_id) - entries_at_start
    return result


def gl_ar_balance_cents(session: Session, company_id: str) -> int:
    """The AR control balance across every entry (reversals net out), on every
    AR-role account, so it totals the same lines the per-invoice
    ``invoice_ar_balance_cents`` rows attribute (GDXA-1231)."""
    rows = session.execute(ar_role_lines()).all()
    return sum(amount for (_type, _id, amount) in rows)


def _refunded_off_credit_cents(session: Session, invoice) -> int:
    """Cents this invoice's live refunds paid out beyond 2300 Customer Credits.

    ``post_refund`` takes the 2300 overpayment credit first, then debits
    contra-revenue for the rest. Money posted nowhere was never on 2300, so a
    refund of it lands in the second part; only that part can have returned
    an uncovered era payment (GDXA-1247). Read from the posted entries, not
    from the adjustment rows: a refund whose entry was reversed has returned
    nothing to net, and a refund that never posted is not on the books."""
    from gdx_dispatch.models.tenant_models import InvoiceAdjustment
    from gdx_dispatch.modules.ledger.models import (
        ENTRY_STATUS_POSTED,
        ROLE_CUSTOMER_CREDITS,
        GlAccount,
        GlJournalLine,
    )

    refund_ids = [
        str(aid)
        for aid in session.scalars(
            select(InvoiceAdjustment.id).where(
                InvoiceAdjustment.invoice_id == invoice.id,
                InvoiceAdjustment.kind == "refund",
            )
        ).all()
    ]
    if not refund_ids:
        return 0
    debits = session.scalars(
        select(GlJournalLine.amount_cents)
        .join(GlJournalEntry, GlJournalLine.entry_id == GlJournalEntry.id)
        .join(GlAccount, GlJournalLine.account_id == GlAccount.id)
        .where(
            GlJournalEntry.source_type == "adjustment",
            GlJournalEntry.source_id.in_(refund_ids),
            GlJournalEntry.status == ENTRY_STATUS_POSTED,
            GlJournalEntry.reverses_entry_id.is_(None),
            GlJournalLine.amount_cents > 0,
            GlAccount.role != ROLE_CUSTOMER_CREDITS,
        )
    ).all()
    return sum(debits)


def era_settlement_excess(session: Session, company_id: str) -> list[dict]:
    """Every late-learned era payment the anchor pool couldn't cover, one row
    per payment, including those a later refund has since paid back.

    ``refunded_cents`` is the part of the excess returned to the customer:
    the invoice's refunds that did not come out of 2300, handed to its
    payments in allocation order and capped at each one's excess.
    ``uncovered_cents`` is what is left, money in that is still on no ledger
    line. A fully refunded row reads 0 there; ``era_settlement_shortfalls``
    keeps only the rows that do not."""
    from gdx_dispatch.models.tenant_models import Payment
    from gdx_dispatch.modules.ledger.rules import (
        _cutover_date,
        _late_era_allocation,
    )

    rows: list[dict] = []
    cutover = _cutover_date(session, company_id)
    if cutover is None:
        return rows
    # ar_control runs this on every Ledger page load: one query narrows it to
    # the invoices holding a payment that could be late-era, instead of four
    # per era invoice. A superset on purpose: the day of slack absorbs how
    # created_at is stored, and _late_era_allocation decides exactly.
    recorded_since = datetime.combine(cutover - timedelta(days=1), time.min)
    candidates = set(
        session.scalars(
            select(Payment.invoice_id).where(
                Payment.payment_date < cutover,
                or_(Payment.created_at.is_(None), Payment.created_at >= recorded_since),
            )
        ).all()
    )
    for invoice in _company_invoices(session, company_id):
        if invoice.id not in candidates or invoice.status not in ISSUED_STATUSES:
            continue
        if not pre_cutover_era(session, invoice):
            continue
        allocation = _late_era_allocation(session, invoice, cutover)
        if not allocation:
            continue
        by_id = {
            p.id: p
            for p in session.scalars(
                select(Payment).where(Payment.invoice_id == invoice.id)
            ).all()
        }
        refund_pool = None  # read only for an invoice that has an excess
        # allocation is built in (payment_date, created_at, id) order, so the
        # refund pool is handed out the same way on every run.
        for pid, allocated in allocation.items():
            p = by_id.get(pid)
            if p is None or p.voided_at is not None:
                continue
            excess = to_cents(_dec(p.amount)) - allocated
            if excess <= 0:
                continue
            if refund_pool is None:
                refund_pool = _refunded_off_credit_cents(session, invoice)
            refunded = min(excess, refund_pool)
            refund_pool -= refunded
            rows.append(
                {
                    "invoice_id": str(invoice.id),
                    "invoice_number": invoice.invoice_number,
                    "payment_id": str(pid),
                    "payment_date": (
                        p.payment_date.isoformat() if p.payment_date else None
                    ),
                    "amount_cents": to_cents(_dec(p.amount)),
                    "allocated_cents": allocated,
                    "refunded_cents": refunded,
                    "uncovered_cents": excess - refunded,
                }
            )
    return rows


def era_settlement_shortfalls(
    session: Session, company_id: str, excess: list[dict] | None = None
) -> list[dict]:
    """Late-learned era payments the anchor pool couldn't cover and no refund
    has paid back, one row per payment with the uncovered cents.

    The uncovered part posts NOTHING (post_era_settlement caps at the pool),
    and balance_due clamps at zero too, so an operational-vs-ledger compare
    reads both sides as 0. Surface every uncovered cent; silence here would
    read as "reconciled" when money vanished (2026-08-17 audit). Money that
    came in on no ledger line and went back out as a refund is no longer
    missing, so a refund nets it (GDXA-1247). Shared by
    ``reconciliation_report`` and ``reports.ar_control`` so the two cannot
    disagree about it (GDXA-1223). Pass ``excess`` when the caller already
    holds ``era_settlement_excess`` for the same session."""
    if excess is None:
        excess = era_settlement_excess(session, company_id)
    return [r for r in excess if r["uncovered_cents"] > 0]


def reconciliation_report(session: Session, company_id: str) -> dict:
    """Per-invoice AR: operational ``balance_due`` vs ledger attribution.
    The §5.7 hand-check — compare the per-invoice rows against QBO's AR
    aging before flipping the flag; the totals row is the running monthly
    check afterwards."""
    rows = []
    op_total = 0
    gl_attributed = 0
    for invoice in _company_invoices(session, company_id):
        if invoice.status not in ISSUED_STATUSES:
            continue
        op_cents = to_cents(_dec(invoice.balance_due))
        gl_cents = invoice_ar_balance_cents(session, invoice)
        op_total += op_cents
        gl_attributed += gl_cents
        # The `legacy_credit_suspects` probe lived here. It flagged every
        # invoice carrying a non-zero `amount_paid` so a QBO aging hand-check
        # could give those rows a hard look — pre-GL credit memos were recorded
        # as amount_paid mutations, invisible to both the opening formula and
        # balance recomputation.
        #
        # Removed 2026-08-22 with the column (migration 073). It had already
        # stopped meaning anything: the 2026-07-31 repair repurposed
        # `amount_paid` from "legacy credit residue" into a payment cache and
        # wrote it on 287 rows, so the probe flagged 286 invoices — 278 of them
        # pre-cutover, i.e. effectively the whole book. A probe designed to
        # surface a handful that surfaces everything surfaces nothing. The
        # hand-check it fed is separately unsatisfiable: it compares against a
        # QBO we can no longer reach (gl-phase1-core-ledger §11 step 4).
        if op_cents or gl_cents:
            rows.append(
                {
                    "invoice_number": invoice.invoice_number,
                    "status": invoice.status,
                    "operational_cents": op_cents,
                    "gl_cents": gl_cents,
                    "delta_cents": op_cents - gl_cents,
                    "anchored": has_opening_anchor(session, invoice),
                }
            )
    mismatches = [r for r in rows if r["delta_cents"] != 0]
    excess = era_settlement_excess(session, company_id)
    return {
        "rows": rows,
        "mismatches": mismatches,
        "era_settlement_shortfalls": era_settlement_shortfalls(
            session, company_id, excess
        ),
        "totals": {
            "operational_ar_cents": op_total,
            "gl_attributed_ar_cents": gl_attributed,
            "gl_ar_account_cents": gl_ar_balance_cents(session, company_id),
            "era_settlement_refunded_cents": sum(r["refunded_cents"] for r in excess),
        },
    }


# (source_type, event) of every one-shot posting: one live entry per source
# row, ever. ``rules._live_event_entry`` guards them going forward (GDXA-1238);
# this list is what the repair below looks for in entries posted before it.
ONE_SHOT_EVENTS = (
    ("invoice", EVENT_OPENING),
    ("payment", EVENT_OPENING_PAYMENT_VOID),
    ("adjustment", "credit_memo"),
    ("adjustment", "refund"),
    ("adjustment", "credit_applied"),
)


def duplicate_one_shot_entries(session: Session, company_id: str) -> list[dict]:
    """One-shot postings with more than one live entry for the same source row.

    Each group keeps its EARLIEST live entry, the one the posting rules now
    return (``rules._live_event_entry``); every later one is a duplicate a
    replay or recalc posted because an input of the content key moved.
    ``ar_cents`` is the net AR the duplicates hold.

    A duplicate's AR is not always still uncorrected: a later entry computed
    from the invoice's ledger balance absorbed it (``settle_opening_on_void``
    clears whatever AR the ledger shows, duplicates included), and reversing
    it then drives the invoice the other way (audit, GDXA-1238). So a group is
    ``reversible`` only when the invoice's live drift (its ledger AR less what
    the row owes) still holds at least that AR, the same way round, after the
    groups ahead of it claimed theirs. Everything else, including a duplicate
    that holds no AR (its cash or 2300 effect is not something the AR control
    can prove), is listed with a ``hold_reason`` and left alone. Read-only.
    """
    from gdx_dispatch.models.tenant_models import Invoice, InvoiceAdjustment, Payment
    from sqlalchemy import func

    from gdx_dispatch.modules.ledger.rules import RECEIVABLE_STATUSES

    wanted = set(ONE_SHOT_EVENTS)
    groups: dict[tuple[str, str, str], list[GlJournalEntry]] = {}
    for entry in session.scalars(
        select(GlJournalEntry)
        .where(
            GlJournalEntry.status == ENTRY_STATUS_POSTED,
            GlJournalEntry.source_type.in_({s for s, _ in ONE_SHOT_EVENTS}),
        )
        .order_by(GlJournalEntry.created_at, GlJournalEntry.entry_no, GlJournalEntry.id)
    ).all():
        # key = source_type:source_id:event:hash:seq; reversal entries carry
        # "reversal:<id>" and fall out here.
        parts = (entry.idempotency_key or "").split(":")
        if len(parts) != 5 or parts[0] != entry.source_type or parts[1] != entry.source_id:
            continue
        if (entry.source_type, parts[2]) not in wanted:
            continue
        groups.setdefault((entry.source_type, entry.source_id, parts[2]), []).append(entry)

    def _invoice_id(source_type: str, source_id: str):
        from uuid import UUID

        try:
            sid = UUID(source_id)
        except ValueError:
            log.warning("one-shot duplicate scan: %s source_id %r is not a UUID", source_type, source_id)
            return None
        if source_type == "invoice":
            return sid
        model = Payment if source_type == "payment" else InvoiceAdjustment
        return session.scalar(select(model.invoice_id).where(model.id == sid))

    out = []
    for (source_type, source_id, event), entries in groups.items():
        if len(entries) < 2:
            continue
        keeper, duplicates = entries[0], entries[1:]

        def _ar(entry) -> int:
            # Every AR-role account, the one read ar_control shares (GDXA-1231).
            lines = ar_role_lines().where(GlJournalEntry.id == entry.id).subquery()
            return int(session.scalar(select(func.coalesce(func.sum(lines.c.amount_cents), 0))))

        ar_cents = sum(_ar(d) for d in duplicates)
        # "Keep the earliest" is only safe when the earliest is the right one.
        # An anchor is what was open at cutover; when a later anchor, not the
        # kept one, matches that figure as the invoice computes it now, which
        # one is wrong is a judgement, not arithmetic (audit, GDXA-1238).
        later_matches = False
        if event == EVENT_OPENING:
            from uuid import UUID

            from gdx_dispatch.modules.ledger.rules import _cutover_date

            inv = session.get(Invoice, UUID(source_id))
            cutover = _cutover_date(session, company_id)
            if inv is not None and cutover is not None:
                now = opening_balance_cents(session, inv, cutover)
                later_matches = _ar(keeper) != now and any(_ar(d) == now for d in duplicates)
        invoice_id = _invoice_id(source_type, source_id)
        invoice_number = (
            session.scalar(select(Invoice.invoice_number).where(Invoice.id == invoice_id))
            if invoice_id is not None
            else None
        )
        out.append(
            {
                "source_type": source_type,
                "source_id": source_id,
                "event": event,
                "invoice_id": str(invoice_id) if invoice_id is not None else None,
                "invoice_number": invoice_number,
                "keeper_entry_id": str(keeper.id),
                "keeper_entry_no": keeper.entry_no,
                "duplicate_entry_ids": [str(d.id) for d in duplicates],
                "duplicate_entry_nos": [d.entry_no for d in duplicates],
                "ar_cents": ar_cents,
                "later_entry_matches": later_matches,
            }
        )
    out.sort(key=lambda r: (-abs(r["ar_cents"]), r["invoice_number"] or ""))

    unclaimed: dict[str, int] = {}  # invoice id -> live drift not yet claimed
    for row in out:
        row["reversible"], row["hold_reason"] = False, None
        if row["ar_cents"] == 0:
            row["hold_reason"] = "holds no AR; its cash or credit effect needs review by hand"
            continue
        if row["invoice_id"] is None:
            row["hold_reason"] = "no invoice to measure its drift against"
            continue
        if row["later_entry_matches"]:
            row["hold_reason"] = (
                "a later anchor, not the kept one, matches the opening balance "
                "the invoice computes now; choose by hand"
            )
            continue
        if row["invoice_id"] not in unclaimed:
            from uuid import UUID

            inv = session.get(Invoice, UUID(row["invoice_id"]))
            owed = (
                to_cents(_dec(inv.balance_due))
                if inv.status in RECEIVABLE_STATUSES and inv.deleted_at is None
                else 0
            )
            unclaimed[row["invoice_id"]] = invoice_ar_balance_cents(session, inv) - owed
        drift = unclaimed[row["invoice_id"]]
        row["invoice_drift_cents"] = drift
        if (drift > 0) != (row["ar_cents"] > 0) or abs(drift) < abs(row["ar_cents"]):
            row["hold_reason"] = (
                f"the invoice's ledger drift ({drift} cents) does not still hold "
                "this AR; a later entry may have absorbed it"
            )
            continue
        row["reversible"] = True
        unclaimed[row["invoice_id"]] = drift - row["ar_cents"]
    return out


def reverse_duplicate_one_shot_entries(
    session: Session,
    company_id: str,
    *,
    actor: str,
    effective_at: date | None = None,
) -> PhaseResult:
    """Reverse every REVERSIBLE duplicate ``duplicate_one_shot_entries``
    finds; held groups go to ``result.refused``. Never commits; the caller
    audits and commits.

    Each reversal is dated at its duplicate's own date unless ``effective_at``
    is given (a closed period: date the correction in the open one). A period
    lock refusal skips that group into ``result.locked`` and leaves it whole;
    the rest still reverse. ``result.rows`` names each reversal entry, which is
    the rollback handle: reversing a reversal restores the duplicate.
    """
    if not actor or not actor.strip():
        raise ValueError("an acting user is required to reverse ledger entries")
    from uuid import UUID

    from sqlalchemy import func

    lock_date = session.scalar(select(func.max(GlPeriodLock.lock_date)))
    result = PhaseResult()
    for group in duplicate_one_shot_entries(session, company_id):
        label = f"{group['event']} on {group['invoice_number'] or group['source_id']}"
        if not group["reversible"]:
            result.refused.append(f"{label}: held, {group['hold_reason']}")
            result.skipped += 1
            continue
        duplicates = [session.get(GlJournalEntry, UUID(i)) for i in group["duplicate_entry_ids"]]
        # Checked for the whole group before any reversal, so a lock never
        # leaves a group half reversed (and no savepoint wraps the writer).
        blocked = [
            d for d in duplicates
            if lock_date is not None and (effective_at or d.effective_at) <= lock_date
        ]
        if blocked:
            result.locked.append(
                f"{label}: period locked through {lock_date}; entry "
                f"#{blocked[0].entry_no} dated {blocked[0].effective_at} "
                "(pass an open-period effective date)"
            )
            continue
        reversals = [
            reverse_entry(session, dup, effective_at=effective_at, created_by=actor)
            for dup in duplicates
        ]
        result.posted += 1
        result.entries_created += len(reversals)
        result.rows.append(
            {
                **group,
                "reversal_entry_ids": [str(r.id) for r in reversals],
                "reversal_entry_nos": [r.entry_no for r in reversals],
                "reversed_on": str(reversals[0].effective_at),
            }
        )
    return result

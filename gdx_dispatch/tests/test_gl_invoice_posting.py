"""GL Phase 1 (S5) — P1 invoice issuance posting + /void.

Plan gates: flag off = identical behavior; draft→sent/paid posts P1 (AR
debit, per-line revenue credits with the 4000 fallback, mirrored tax,
rounding residual); post-issuance edits reverse+repost; /void reverses the
live P1 and refuses while payments exist.
"""
from __future__ import annotations

import datetime as dt
import secrets
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from gdx_dispatch.models.tenant_models import Invoice, InvoiceLine, Payment
from gdx_dispatch.modules.ledger.models import (
    GlAccount,
    GlJournalEntry,
    GlJournalLine,
)
from gdx_dispatch.modules.ledger.rules import (
    build_issuance_lines,
    repost_invoice_issuance,
)
from gdx_dispatch.modules.ledger.service import (
    ensure_gl_seed,
    transition_invoice_status,
)
from gdx_dispatch.routers.invoices import _recalculate_invoice, void_invoice

COMPANY = "11111111-1111-1111-1111-111111111111"
USER = {"tenant_id": COMPANY, "sub": "tester"}


@pytest.fixture
def db(tenant_db, monkeypatch):
    monkeypatch.delenv("GDX_ENV", raising=False)
    ensure_gl_seed(tenant_db, COMPANY)
    tenant_db.commit()
    return tenant_db


def _enable(db):
    settings = ensure_gl_seed(db, COMPANY)
    settings.ledger_posting_enabled = True
    db.commit()


def _invoice(db, *, total="1000.00", tax="65.00", status="draft", lines=None):
    inv = Invoice(
        id=uuid4(),
        customer_id=uuid4(),
        invoice_number=f"INV-{uuid4().hex[:8].upper()}",
        status=status,
        subtotal=Decimal(total) - Decimal(tax),
        tax_amount=Decimal(tax),
        total=Decimal(total),
        amount_paid=Decimal("0.00"),
        invoice_date=dt.date(2026, 7, 1),
        public_token=secrets.token_urlsafe(48)[:64],
        company_id=COMPANY,
    )
    db.add(inv)
    db.flush()
    for descr, amount, category in lines or [("Spring replacement", str(Decimal(total) - Decimal(tax)), None)]:
        db.add(
            InvoiceLine(
                invoice_id=inv.id,
                description=descr,
                quantity=1,
                unit_price=Decimal(amount),
                line_total=Decimal(amount),
                category=category,
                company_id=COMPANY,
            )
        )
    db.commit()
    db.refresh(inv)
    return inv


def _entries(db):
    return db.scalars(select(GlJournalEntry).order_by(GlJournalEntry.entry_no)).all()


def _lines_by_code(db, entry):
    out = {}
    for line in db.scalars(select(GlJournalLine).where(GlJournalLine.entry_id == entry.id)):
        acct = db.get(GlAccount, line.account_id)
        out.setdefault(acct.code, 0)
        out[acct.code] += line.amount_cents
    return out


# ---------------------------------------------------------------------------
# flag OFF — identical behavior
# ---------------------------------------------------------------------------

def test_flag_off_send_posts_nothing(db):
    inv = _invoice(db)
    transition_invoice_status(db, inv, "sent")
    db.commit()
    assert inv.status == "sent"
    assert _entries(db) == []


def test_flag_off_void_endpoint_works_without_posting(db):
    inv = _invoice(db)
    result = void_invoice(inv.id, _=USER, db=db)
    assert result["status"] == "void"
    assert _entries(db) == []


# ---------------------------------------------------------------------------
# P1 — issuance
# ---------------------------------------------------------------------------

def test_draft_to_sent_posts_p1(db):
    _enable(db)
    inv = _invoice(db, total="1065.00", tax="65.00")
    transition_invoice_status(db, inv, "sent", actor="tester")
    db.commit()

    entries = _entries(db)
    assert len(entries) == 1
    by_code = _lines_by_code(db, entries[0])
    assert by_code["1200"] == 106_500          # AR debit
    assert by_code["4000"] == -100_000         # NULL category → fallback
    assert by_code["2100"] == -6_500           # mirrored tax
    assert entries[0].idempotency_key == f"invoice:{inv.id}:issued:" + entries[0].idempotency_key.split(":")[3] + ":0"


def test_draft_to_paid_autoflip_posts_p1(db):
    _enable(db)
    inv = _invoice(db, total="500.00", tax="0.00")
    inv.balance_due = Decimal("0.00")
    transition_invoice_status(db, inv, "paid", actor="tester")
    db.commit()
    assert len(_entries(db)) == 1


def test_zero_total_invoice_posts_nothing(db):
    _enable(db)
    inv = _invoice(db, total="0.00", tax="0.00", lines=[])
    transition_invoice_status(db, inv, "sent")
    db.commit()
    assert _entries(db) == []


def test_rounding_residual_posts_to_6990(db):
    _enable(db)
    # total 100.00 but lines sum to 99.99 and no tax → 1-cent residual
    inv = _invoice(db, total="100.00", tax="0.00", lines=[("Widget", "99.99", None)])
    transition_invoice_status(db, inv, "sent")
    db.commit()
    by_code = _lines_by_code(db, _entries(db)[0])
    assert by_code["6990"] == -1
    assert sum(by_code.values()) == 0


def test_mapped_category_credits_configured_account(db):
    _enable(db)
    settings = ensure_gl_seed(db, COMPANY)
    install_acct = db.scalars(select(GlAccount).where(GlAccount.code == "4100")).one()
    settings.revenue_category_account_map = {"Installation": str(install_acct.id)}
    db.commit()

    inv = _invoice(
        db, total="2000.00", tax="0.00",
        lines=[("New door install", "1500.00", "Installation"), ("Misc", "500.00", "Weird Category")],
    )
    transition_invoice_status(db, inv, "sent")
    db.commit()
    by_code = _lines_by_code(db, _entries(db)[0])
    assert by_code["4100"] == -150_000       # mapped
    assert by_code["4000"] == -50_000        # unmapped → fallback, memo-flagged
    memos = [
        l.memo
        for l in db.scalars(select(GlJournalLine))
        if l.memo and "unmapped category" in l.memo
    ]
    assert any("Weird Category" in m for m in memos)


# ---------------------------------------------------------------------------
# Post-issuance edits — reverse + repost
# ---------------------------------------------------------------------------

def test_recalculate_reposts_on_content_change(db):
    _enable(db)
    inv = _invoice(db, total="1065.00", tax="65.00")
    transition_invoice_status(db, inv, "sent")
    db.commit()

    # edit a line: 1000 → 1200 net
    line = db.scalars(select(InvoiceLine).where(InvoiceLine.invoice_id == inv.id)).one()
    line.line_total = Decimal("1200.00")
    line.unit_price = Decimal("1200.00")
    _recalculate_invoice(inv, db)
    db.commit()

    entries = _entries(db)
    posted = [e for e in entries if e.status == "posted" and e.reverses_entry_id is None]
    reversed_ = [e for e in entries if e.status == "reversed"]
    assert len(posted) == 1 and len(reversed_) == 1
    assert _lines_by_code(db, posted[0])["1200"] > 106_500  # new content


def test_recalculate_without_content_change_is_noop(db):
    _enable(db)
    inv = _invoice(db, total="1065.00", tax="65.00")
    transition_invoice_status(db, inv, "sent")
    db.commit()

    _recalculate_invoice(inv, db)
    db.commit()
    assert len(_entries(db)) == 1  # idempotent — no reversal, no repost


def test_edit_back_to_original_leaves_one_live_entry(db):
    """A→B→A through the real recalc path — the §5.6 liveness property."""
    _enable(db)
    inv = _invoice(db, total="1065.00", tax="65.00")
    transition_invoice_status(db, inv, "sent")
    db.commit()
    line = db.scalars(select(InvoiceLine).where(InvoiceLine.invoice_id == inv.id)).one()

    line.line_total = Decimal("1200.00")
    _recalculate_invoice(inv, db)
    db.commit()
    line.line_total = Decimal("1000.00")
    _recalculate_invoice(inv, db)
    db.commit()

    live = [e for e in _entries(db) if e.status == "posted" and e.reverses_entry_id is None]
    assert len(live) == 1
    assert live[0].idempotency_key.endswith(":1")  # reposted A at seq 1


def test_flag_off_recalculate_never_touches_ledger(db):
    inv = _invoice(db, status="sent")
    line = db.scalars(select(InvoiceLine).where(InvoiceLine.invoice_id == inv.id)).one()
    line.line_total = Decimal("42.00")
    _recalculate_invoice(inv, db)
    db.commit()
    assert _entries(db) == []


# ---------------------------------------------------------------------------
# /void
# ---------------------------------------------------------------------------

def test_void_reverses_live_p1(db):
    _enable(db)
    inv = _invoice(db)
    transition_invoice_status(db, inv, "sent")
    db.commit()

    result = void_invoice(inv.id, _=USER, db=db)
    assert result["status"] == "void"
    entries = _entries(db)
    assert len(entries) == 2
    assert entries[0].status == "reversed"
    assert entries[1].reverses_entry_id == entries[0].id


def test_void_refuses_while_payments_exist(db):
    _enable(db)
    inv = _invoice(db)
    transition_invoice_status(db, inv, "sent")
    db.add(
        Payment(
            invoice_id=inv.id, amount=Decimal("10.00"), method="cash",
            payment_date=dt.date(2026, 7, 2), company_id=COMPANY,
        )
    )
    db.commit()
    with pytest.raises(HTTPException) as exc:
        void_invoice(inv.id, _=USER, db=db)
    assert exc.value.status_code == 409
    assert "payments" in exc.value.detail


def test_void_is_idempotent(db):
    _enable(db)
    inv = _invoice(db)
    transition_invoice_status(db, inv, "sent")
    db.commit()
    void_invoice(inv.id, _=USER, db=db)
    again = void_invoice(inv.id, _=USER, db=db)  # no error, no double reversal
    assert again["status"] == "void"
    assert len([e for e in _entries(db) if e.status == "posted"]) == 1  # just the reversal


def test_void_draft_posts_nothing(db):
    _enable(db)
    inv = _invoice(db)
    void_invoice(inv.id, _=USER, db=db)
    assert _entries(db) == []


# ---------------------------------------------------------------------------
# build_issuance_lines composition (audit round 2 — the reproduced bug)
# ---------------------------------------------------------------------------

def test_clean_content_produces_no_residual_line(db):
    inv = _invoice(db, total="123.45", tax="7.89", lines=[("A", "100.00", None), ("B", "15.56", None)])
    lines = build_issuance_lines(db, inv)
    assert sum(l.amount_cents for l in lines) == 0
    assert all(l.role != "ROUNDING" for l in lines), "clean content must not need 6990"


def test_soft_deleted_lines_do_not_credit_revenue(db):
    """Audit round 2 (reproduced): a $500 line deleted while drafting still
    credited revenue at send, silently absorbed by 6990. Live lines only."""
    _enable(db)
    inv = _invoice(db, total="100.00", tax="0.00", lines=[("Keeper", "100.00", None)])
    db.add(
        InvoiceLine(
            invoice_id=inv.id, description="Deleted in draft", quantity=1,
            unit_price=Decimal("500.00"), line_total=Decimal("500.00"),
            deleted_at=dt.datetime(2026, 6, 30, tzinfo=dt.UTC), company_id=COMPANY,
        )
    )
    db.commit()
    transition_invoice_status(db, inv, "sent")
    db.commit()
    by_code = _lines_by_code(db, _entries(db)[0])
    assert by_code["4000"] == -10_000       # only the live line
    assert "6990" not in by_code            # and nothing to absorb


def test_unreconcilable_invoice_refuses_to_post(db):
    """total ≠ lines + tax beyond rounding is a data bug, not 'rounding' —
    it must refuse loudly, never launder into 6990."""
    from gdx_dispatch.modules.ledger.rules import IssuanceCompositionError

    _enable(db)
    inv = _invoice(db, total="100.00", tax="0.00", lines=[("Tiny", "5.00", None)])
    with pytest.raises(IssuanceCompositionError, match="doesn't reconcile"):
        transition_invoice_status(db, inv, "sent")
    db.rollback()


def test_effective_at_is_stable_for_null_invoice_date(db):
    """effective_at joins the content hash — a today() fallback would mint
    phantom reversal+repost pairs on every day-crossing touch."""
    from gdx_dispatch.modules.ledger.rules import _effective_at

    inv = _invoice(db)
    inv.invoice_date = None
    inv.sent_at = dt.datetime(2026, 7, 3, 15, 0, tzinfo=dt.UTC)
    assert _effective_at(inv) == dt.date(2026, 7, 3)
    inv.sent_at = None
    assert _effective_at(inv) == inv.created_at.date()


# ---------------------------------------------------------------------------
# Period locks × live money paths (audit round 2)
# ---------------------------------------------------------------------------

def _lock_through(db, day):
    from gdx_dispatch.modules.ledger.models import GlPeriodLock

    db.add(GlPeriodLock(lock_date=day, company_id=COMPANY))
    db.commit()


def test_noop_replay_survives_period_lock(db):
    """Recording a payment re-runs _recalculate on a locked-period invoice;
    unchanged content must return the live entry, not 500."""
    _enable(db)
    inv = _invoice(db)
    transition_invoice_status(db, inv, "sent")
    db.commit()
    _lock_through(db, dt.date(2026, 7, 31))  # invoice_date 2026-07-01 now locked

    _recalculate_invoice(inv, db)  # content unchanged → idempotent, no raise
    db.commit()
    assert len(_entries(db)) == 1


def test_locked_period_content_edit_is_a_409(db):
    _enable(db)
    inv = _invoice(db)
    transition_invoice_status(db, inv, "sent")
    db.commit()
    _lock_through(db, dt.date(2026, 7, 31))

    line = db.scalars(select(InvoiceLine).where(InvoiceLine.invoice_id == inv.id)).one()
    line.line_total = Decimal("1200.00")
    with pytest.raises(HTTPException) as exc:
        _recalculate_invoice(inv, db)
    assert exc.value.status_code == 409
    assert "locked accounting period" in exc.value.detail
    db.rollback()
    assert repost_invoice_issuance  # public surface stays importable

"""GDXA-1220: money that lands on a void invoice is customer credit (2300).

#422 made the void terminal at the ledger chokepoint: a Stripe payment that
arrives after a void is recorded in full, the invoice stays void, and an
operator gets a `payment_on_voided_invoice` audit row. The P3 posting never
read the status, though. `build_payment_lines` sized "remaining AR" off the
invoice total, which the void leaves alone, so the payment credited 1200 AR on
an invoice whose AR the void had already reversed — GL AR went negative and
2300 never learned the customer was owed anything.

These drive the real recorder (`_mark_invoice_paid`) with the GL on and read
the journal back; nothing here mocks the posting.
"""
from __future__ import annotations

import secrets
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from gdx_dispatch.core.audit import AuditLog
from gdx_dispatch.models.tenant_models import Invoice, InvoiceLine, Payment
from gdx_dispatch.modules.ledger import backfill
from gdx_dispatch.modules.ledger.coa import resolve_role_account
from gdx_dispatch.modules.ledger.engine import PostingEvent, PostingLine, post_for_event
from gdx_dispatch.modules.ledger.models import (
    ROLE_AR,
    ROLE_CUSTOMER_CREDITS,
    GlJournalEntry,
    GlJournalLine,
)
from gdx_dispatch.modules.ledger.rules import (
    EVENT_PAYMENT,
    customer_credit_balance_cents,
    reverse_invoice_adjustments,
)
from gdx_dispatch.modules.ledger.service import ensure_gl_seed, transition_invoice_status

COMPANY = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def db(tenant_db, monkeypatch):
    monkeypatch.delenv("GDX_ENV", raising=False)
    # The recorder queues a stale-intent sweep after its commit; there is no
    # broker here and the sweep is not what these tests are about.
    monkeypatch.setattr(
        "gdx_dispatch.tasks.stale_intent_sweep.enqueue_stale_intent_sweep",
        lambda *a, **k: None,
    )
    settings = ensure_gl_seed(tenant_db, COMPANY)
    settings.ledger_posting_enabled = True
    settings.cutover_month = date(2026, 7, 1)
    tenant_db.commit()
    return tenant_db


def _issued(db, total="500.00"):
    inv = Invoice(
        id=uuid4(), customer_id=uuid4(), invoice_number=f"INV-{uuid4().hex[:8].upper()}",
        status="draft", subtotal=Decimal(total), tax_amount=Decimal("0"), total=Decimal(total),
        balance_due=Decimal(total), invoice_date=date(2026, 9, 10),
        public_token=secrets.token_urlsafe(48)[:64], company_id=COMPANY,
    )
    db.add(inv)
    db.flush()
    db.add(InvoiceLine(invoice_id=inv.id, description="Door service", quantity=1,
                       unit_price=Decimal(total), line_total=Decimal(total), company_id=COMPANY))
    db.flush()
    transition_invoice_status(db, inv, "sent", actor="office-user")
    db.commit()
    return inv


def _void(db, inv):
    # What `void_invoice` does to the books: reverse P1 and the adjustments.
    transition_invoice_status(db, inv, "void", actor="office-user")
    reverse_invoice_adjustments(db, inv, actor="office-user")
    inv.balance_due = Decimal("0")
    db.commit()


def _stripe_pays(db, inv, reference, amount):
    from gdx_dispatch.core.payments import _mark_invoice_paid

    _mark_invoice_paid(inv, db, external_ref=reference, method="card",
                       amount=amount, source="stripe-webhook")
    return db.scalars(select(Payment).where(Payment.reference == reference)).one()


def _role_balance(db, inv, role):
    """Signed cents on ``role`` for this invoice's customer, every entry
    (reversals net their originals, so no status filter)."""
    acct = resolve_role_account(db, COMPANY, role)
    rows = db.execute(
        select(GlJournalLine.amount_cents)
        .join(GlJournalEntry, GlJournalLine.entry_id == GlJournalEntry.id)
        .where(GlJournalLine.account_id == acct.id,
               GlJournalLine.customer_id == inv.customer_id,
               GlJournalEntry.company_id == COMPANY)
    ).all()
    return sum(c for (c,) in rows)


def _live_payment_lines(db, payment):
    entries = db.scalars(select(GlJournalEntry).where(
        GlJournalEntry.source_type == "payment",
        GlJournalEntry.source_id == str(payment.id),
        GlJournalEntry.status == "posted",
        GlJournalEntry.reverses_entry_id.is_(None),
    )).all()
    out = []
    for entry in entries:
        out.extend(db.scalars(select(GlJournalLine).where(
            GlJournalLine.entry_id == entry.id)).all())
    return out


def test_control_a_payment_on_a_live_invoice_still_relieves_ar(db):
    """CONTROL. Without this the void tests could pass with posting off."""
    inv = _issued(db)
    _stripe_pays(db, inv, "pi_live", 200.0)
    assert _role_balance(db, inv, ROLE_AR) == 300_00, "500 issued − 200 paid"
    assert _role_balance(db, inv, ROLE_CUSTOMER_CREDITS) == 0


def test_a_stripe_payment_on_a_void_invoice_is_customer_credit_not_ar(db):
    inv = _issued(db)
    _void(db, inv)
    assert _role_balance(db, inv, ROLE_AR) == 0, "setup: the void cleared AR"

    payment = _stripe_pays(db, inv, "pi_after_void", 500.0)

    db.refresh(inv)
    assert inv.status == "void", "#422: a void is terminal"
    assert _role_balance(db, inv, ROLE_AR) == 0, "the payment must not touch AR"
    assert _role_balance(db, inv, ROLE_CUSTOMER_CREDITS) == -500_00, "all of it is 2300"
    assert customer_credit_balance_cents(db, COMPANY, inv.customer_id) == 500_00

    lines = _live_payment_lines(db, payment)
    credit = [ln for ln in lines if ln.amount_cents < 0]
    assert len(credit) == 1
    assert credit[0].memo == f"payment on voided invoice {inv.invoice_number} → customer credit"

    # The operator alert from #422 still lands with it.
    audit = db.scalars(select(AuditLog).where(
        AuditLog.action == "payment_on_voided_invoice",
        AuditLog.entity_id == str(inv.id))).all()
    assert len(audit) == 1


def test_a_partial_payment_on_a_void_is_credit_too(db):
    """Under the old arithmetic a payment below the total was ALL AR."""
    inv = _issued(db)
    _void(db, inv)
    _stripe_pays(db, inv, "pi_partial_void", 120.0)
    assert _role_balance(db, inv, ROLE_AR) == 0
    assert _role_balance(db, inv, ROLE_CUSTOMER_CREDITS) == -120_00


def test_refund_void_then_won_dispute_moves_the_credit_out_and_back(db):
    """The resettle path: a refund voids the payment (the 2300 credit
    reverses), a won dispute un-voids it (the credit is reposted as 2300,
    not as AR). There is no invoice un-void — the chokepoint refuses to leave
    void — so this is the only way a void invoice's payment re-posts."""
    from gdx_dispatch.core.payments import apply_payment_unvoid, apply_payment_void

    inv = _issued(db)
    _void(db, inv)
    payment = _stripe_pays(db, inv, "pi_disputed", 500.0)

    apply_payment_void(db, payment, reason="stripe_refund", actor="stripe-webhook")
    db.commit()
    assert _role_balance(db, inv, ROLE_CUSTOMER_CREDITS) == 0
    assert _role_balance(db, inv, ROLE_AR) == 0

    apply_payment_unvoid(db, payment, actor="stripe-webhook")
    db.commit()
    assert _role_balance(db, inv, ROLE_CUSTOMER_CREDITS) == -500_00
    assert _role_balance(db, inv, ROLE_AR) == 0


def test_replay_moves_an_entry_posted_before_the_fix_onto_2300(db):
    """Existing rows. A payment that posted before this fix credited AR; the
    GL replay is the repair, and it used to skip void invoices entirely."""
    inv = _issued(db)
    _void(db, inv)
    payment = Payment(
        id=uuid4(), invoice_id=inv.id, amount=Decimal("500.00"), method="card",
        reference="pi_pre_fix", payment_date=date(2026, 9, 20), company_id=COMPANY,
    )
    db.add(payment)
    db.flush()
    # Exactly what the old builder minted.
    post_for_event(db, PostingEvent(
        company_id=COMPANY, source_type="payment", source_id=str(payment.id),
        event=EVENT_PAYMENT, effective_at=date(2026, 9, 20), created_by="stripe-webhook",
        lines=(
            PostingLine(amount_cents=500_00, role="UNDEPOSITED", customer_id=inv.customer_id,
                        memo=f"payment on {inv.invoice_number} (card)"),
            PostingLine(amount_cents=-500_00, role=ROLE_AR, customer_id=inv.customer_id,
                        memo=f"payment applied to {inv.invoice_number}"),
        ),
    ))
    db.commit()
    assert _role_balance(db, inv, ROLE_AR) == -500_00, "setup: the negative AR the bug left"

    backfill.run_replay(db, COMPANY)
    db.commit()
    assert _role_balance(db, inv, ROLE_AR) == 0
    assert _role_balance(db, inv, ROLE_CUSTOMER_CREDITS) == -500_00

    entries_after_first = backfill._entry_count(db, COMPANY)
    backfill.run_replay(db, COMPANY)
    db.commit()
    assert backfill._entry_count(db, COMPANY) == entries_after_first, "replay is idempotent"


def test_replay_leaves_a_void_era_invoice_alone_and_names_it(db):
    """A late-learned era payment on a void pre-cutover invoice would resettle
    through `post_era_settlement`, which credits AR and does not know about
    voids — the replay drove AR to −$1000 on one (audit 2026-10-15, executed).
    It stays out of the replay and lands on the refused list for a human."""
    from datetime import datetime

    from gdx_dispatch.modules.ledger.rules import invoice_ar_balance_cents

    # Historic state, written the way an import would: with posting off (the
    # chokepoint guard refuses an invoice born void while it is on).
    settings = ensure_gl_seed(db, COMPANY)
    settings.ledger_posting_enabled = False
    db.commit()
    inv = Invoice(id=uuid4(), customer_id=uuid4(), invoice_number="INV-ERAVOID",
                  status="void", subtotal=Decimal("1000"), tax_amount=Decimal("0"),
                  total=Decimal("1000"), balance_due=Decimal("0"),
                  invoice_date=date(2026, 5, 12),
                  public_token=secrets.token_urlsafe(48)[:64], company_id=COMPANY)
    db.add(inv)
    db.flush()
    db.add(InvoiceLine(invoice_id=inv.id, description="Door", quantity=1,
                       unit_price=Decimal("1000"), line_total=Decimal("1000"), company_id=COMPANY))
    late = Payment(id=uuid4(), invoice_id=inv.id, amount=Decimal("1000"), method="check",
                   payment_date=date(2026, 6, 20), company_id=COMPANY)
    db.add(late)
    db.flush()
    late.created_at = datetime(2026, 8, 1, 12, 0)  # learned after cutover
    db.commit()
    settings.ledger_posting_enabled = True
    db.commit()

    result = backfill.run_replay(db, COMPANY)
    db.commit()
    assert invoice_ar_balance_cents(db, inv) == 0
    assert any("INV-ERAVOID" in r for r in result.refused)


def test_refund_overpayment_on_a_void_is_every_live_dollar(db):
    """`_refund_overpayment_cents` and `build_payment_lines` share one
    arithmetic so the two halves of an overpayment cannot disagree."""
    from gdx_dispatch.modules.ledger.rules import _refund_overpayment_cents

    inv = _issued(db)
    _void(db, inv)
    _stripe_pays(db, inv, "pi_refundable", 120.0)
    assert _refund_overpayment_cents(db, inv) == 120_00

"""GDXA-615 — every invoice and payment GL entry names who caused it.

Prod carried 10 journal entries with a NULL `created_by` (4 invoice, 6
payment). The callers that dropped the actor: `_mark_invoice_paid` (the
processor paths) posted P3 with none, and `_recalculate_invoice` reposted P1
and auto-flipped to paid with none — so even an office `record_payment` that
settled a draft posted its P1 anonymously.

These read the entries back from the database; they do not mock the engine.
"""
from __future__ import annotations

import datetime as dt
import logging
import secrets
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from gdx_dispatch.core.payments import _mark_invoice_paid
from gdx_dispatch.models.tenant_models import Invoice, InvoiceLine
from gdx_dispatch.modules.ledger.engine import PostingEvent, PostingLine, post_for_event
from gdx_dispatch.modules.ledger.models import ROLE_AR, ROLE_SALES_FALLBACK, GlJournalEntry
from gdx_dispatch.modules.ledger.service import ensure_gl_seed, transition_invoice_status
from gdx_dispatch.routers.invoices import PaymentCreateIn, _recalculate_invoice, record_payment

COMPANY = "11111111-1111-1111-1111-111111111111"
USER = {"tenant_id": COMPANY, "sub": "office-user-615"}


@pytest.fixture
def db(tenant_db, monkeypatch):
    monkeypatch.delenv("GDX_ENV", raising=False)
    settings = ensure_gl_seed(tenant_db, COMPANY)
    settings.ledger_posting_enabled = True
    tenant_db.commit()
    return tenant_db


def _invoice(db, total="500.00", status="draft"):
    inv = Invoice(
        id=uuid4(),
        customer_id=uuid4(),
        invoice_number=f"INV-{uuid4().hex[:8].upper()}",
        status=status,
        subtotal=Decimal(total),
        tax_amount=Decimal("0.00"),
        total=Decimal(total),
        balance_due=Decimal(total),
        invoice_date=dt.date(2026, 7, 1),
        public_token=secrets.token_urlsafe(48)[:64],
        company_id=COMPANY,
    )
    db.add(inv)
    db.flush()
    db.add(
        InvoiceLine(
            invoice_id=inv.id, description="Work", quantity=1,
            unit_price=Decimal(total), line_total=Decimal(total), company_id=COMPANY,
        )
    )
    db.commit()
    db.refresh(inv)
    return inv


def _entries(db, inv):
    """Every entry on this invoice or its payments (P1 keys on the invoice id;
    P3 keys on the payment id, so match on the invoice's source rows)."""
    from gdx_dispatch.models.tenant_models import Payment

    payment_ids = [
        str(p) for p in db.scalars(select(Payment.id).where(Payment.invoice_id == inv.id))
    ]
    return db.scalars(
        select(GlJournalEntry)
        .where(GlJournalEntry.source_id.in_([str(inv.id), *payment_ids]))
        .order_by(GlJournalEntry.entry_no)
    ).all()


@pytest.mark.parametrize(
    "source", ["stripe-webhook", "stripe-confirm", "stripe-recovered", "portal-charge-method"]
)
def test_processor_payment_on_draft_names_the_recording_source(db, source):
    """`_mark_invoice_paid` (core/payments.py) — P1 from the auto-flip and P3
    both carry the named system identity that recorded the money."""
    inv = _invoice(db)
    _mark_invoice_paid(inv, db, external_ref=f"pi_615_{source}", method="card", source=source)

    entries = _entries(db, inv)
    assert {e.source_type for e in entries} == {"invoice", "payment"}
    assert [e.created_by for e in entries] == [source] * len(entries)


def test_processor_payment_default_source_is_named(db):
    """The default `source` is the webhook, never None."""
    inv = _invoice(db)
    transition_invoice_status(db, inv, "sent", actor="office-user-615")
    db.commit()
    _mark_invoice_paid(inv, db, external_ref="pi_615_default", method="card")

    p3 = [e for e in _entries(db, inv) if e.source_type == "payment"]
    assert len(p3) == 1
    assert p3[0].created_by == "stripe-webhook"


def test_office_payment_on_draft_names_the_user_on_p1_and_p3(db):
    """`record_payment` → `_recalculate_invoice` auto-flips the draft to paid,
    which posts P1. Before GDXA-615 that P1 carried NULL; P3 already had it."""
    inv = _invoice(db)
    record_payment(inv.id, PaymentCreateIn(amount=500.0, method="cash"), _=USER, db=db)

    entries = _entries(db, inv)
    assert [e.source_type for e in entries] == ["invoice", "payment"]
    assert [e.created_by for e in entries] == ["office-user-615"] * 2


def test_recalculate_repost_and_its_reversal_carry_the_actor(db):
    """`_recalculate_invoice` → `repost_invoice_issuance`: a content change
    reverses the live P1 and reposts; both new rows name the editor."""
    inv = _invoice(db, status="draft")
    transition_invoice_status(db, inv, "sent", actor="issuer")
    db.commit()

    line = db.scalars(select(InvoiceLine).where(InvoiceLine.invoice_id == inv.id)).one()
    line.line_total = Decimal("650.00")
    line.unit_price = Decimal("650.00")
    _recalculate_invoice(inv, db, actor="editor")
    db.commit()

    entries = _entries(db, inv)
    assert len(entries) == 3  # original P1, its reversal, the repost
    assert entries[0].created_by == "issuer"
    assert [e.created_by for e in entries[1:]] == ["editor", "editor"]


def _event(source_type, created_by):
    return PostingEvent(
        company_id=COMPANY,
        source_type=source_type,
        source_id=f"{source_type}-{uuid4().hex[:8]}",
        event="test",
        effective_at=dt.date(2026, 7, 1),
        lines=(
            PostingLine(amount_cents=1_000, role=ROLE_AR),
            PostingLine(amount_cents=-1_000, role=ROLE_SALES_FALLBACK),
        ),
        created_by=created_by,
    )


@pytest.mark.parametrize("source_type", ["invoice", "payment"])
def test_engine_logs_but_still_writes_an_actorless_money_posting(db, caplog, source_type):
    """The guard is log-only: refusing would abort the operational write that
    recorded the money (and mobile send still passes none until its own fix)."""
    caplog.set_level(logging.WARNING, logger="gdx_dispatch.modules.ledger.engine")
    entry = post_for_event(db, _event(source_type, None))
    db.flush()

    assert entry.id is not None and entry.created_by is None
    hits = [r for r in caplog.records if "gl_posting_without_actor" in r.getMessage()]
    assert len(hits) == 1
    assert f"source_type={source_type}" in hits[0].getMessage()


@pytest.mark.parametrize(
    ("source_type", "created_by"),
    [("payment", "someone"), ("expense", None)],
)
def test_engine_is_quiet_when_actor_present_or_source_out_of_scope(db, caplog, source_type, created_by):
    caplog.set_level(logging.WARNING, logger="gdx_dispatch.modules.ledger.engine")
    post_for_event(db, _event(source_type, created_by))
    db.flush()
    assert not [r for r in caplog.records if "gl_posting_without_actor" in r.getMessage()]

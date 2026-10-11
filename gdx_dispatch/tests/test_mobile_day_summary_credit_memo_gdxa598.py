"""GDXA-598 — the phone's day summary counts what was billed, net of credit memos.

A credit memo lowers ``balance_due`` and leaves ``Invoice.total`` as issued
(by design, GDXA-590), so ``revenue_invoiced`` read as ``SUM(i.total)``
overstated a credited invoice by the whole credit. The case is the issue's: an
invoice of 100 with a credit memo of 20 shows 80. Fixture and frozen shop day
are GDXA-511's.
"""
from __future__ import annotations

import secrets
from decimal import Decimal
from uuid import uuid4

from gdx_dispatch.models.tenant_models import Invoice, InvoiceAdjustment
from gdx_dispatch.tests.test_mobile_shop_day_gdxa511 import (  # noqa: F401 — fixture
    SHOP_TODAY,
    TENANT,
    USER,
    _customer,
    _job,
    _utc,
    client_and_db,
)


def _invoice(db, job, total: str) -> Invoice:
    inv = Invoice(
        id=uuid4(), customer_id=job.customer_id, job_id=job.id,
        invoice_number=f"INV-{uuid4().hex[:8].upper()}", status="sent",
        subtotal=Decimal(total), tax_amount=Decimal("0.00"), total=Decimal(total),
        balance_due=Decimal(total), public_token=secrets.token_urlsafe(48)[:64],
        company_id=TENANT, created_at=_utc(10, 1),  # 20:00 CDT on the shop day
    )
    db.add(inv)
    return inv


def _credit_memo(db, inv, amount: str) -> None:
    db.add(InvoiceAdjustment(
        invoice_id=inv.id, kind="credit_memo", amount=Decimal(amount),
        reason="warranty", company_id=TENANT,
    ))
    inv.balance_due = inv.balance_due - Decimal(amount)


def test_a_credited_invoice_counts_net_of_its_credit_memo(client_and_db):  # noqa: F811 — the fixture param IS the import
    client, db = client_and_db
    c = _customer(db)
    j = _job(db, c, title="Spring swap", scheduled_at=_utc(9, 15), assigned_to=USER)
    db.flush()
    inv = _invoice(db, j, "100.00")
    db.flush()
    _credit_memo(db, inv, "20.00")
    db.commit()

    body = client.get(f"/api/mobile/day-summary?date={SHOP_TODAY}").json()
    assert body["invoices_count"] == 1
    assert body["revenue_invoiced"] == 80.0


def test_uncredited_invoice_beside_a_twice_credited_one(client_and_db):  # noqa: F811 — the fixture param IS the import
    client, db = client_and_db
    c = _customer(db)
    j1 = _job(db, c, title="Opener", scheduled_at=_utc(9, 15), assigned_to=USER)
    j2 = _job(db, c, title="Cable", scheduled_at=_utc(9, 17), assigned_to=USER)
    db.flush()
    credited = _invoice(db, j1, "100.00")
    _invoice(db, j2, "250.00")
    db.flush()
    _credit_memo(db, credited, "20.00")
    _credit_memo(db, credited, "5.00")
    db.commit()

    body = client.get(f"/api/mobile/day-summary?date={SHOP_TODAY}").json()
    assert body["invoices_count"] == 2
    assert body["revenue_invoiced"] == 325.0


def test_a_voided_invoice_is_neither_counted_nor_billed(client_and_db):  # noqa: F811 — the fixture param IS the import
    """A void-and-reissue on the same day counts once: void_invoice sets
    status='void' and leaves deleted_at NULL, so the deleted_at filter alone
    reported the voided bill as revenue beside its replacement."""
    client, db = client_and_db
    c = _customer(db)
    j = _job(db, c, title="Reissued", scheduled_at=_utc(9, 15), assigned_to=USER)
    db.flush()
    _invoice(db, j, "100.00").status = "void"
    _invoice(db, j, "100.00")
    db.commit()

    body = client.get(f"/api/mobile/day-summary?date={SHOP_TODAY}").json()
    assert body["invoices_count"] == 1
    assert body["revenue_invoiced"] == 100.0

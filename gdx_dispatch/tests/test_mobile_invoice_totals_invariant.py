"""Field billing from the truck must satisfy the money-correctness invariant.

Found by driving a real phone (2026-08-12): the tech taps "Generate & email
invoice", the request 500s, and the dialog just sits there — no toast, no
change. Server-side it was always the same refusal:

    InvoiceTotalsInvariantError: invoice INV-000336: total 2090.00 !=
    Σlines 2090.00 + tax 154.24 (= 2244.24)

Both estimate branches of mobile_create_invoice restated the header from the
line sum and dropped the tax that had just been computed from the estimate:

    invoice.subtotal = price
    invoice.total    = price      # <- tax_amount still 154.24
    invoice.balance_due = price

The invariant (migration 056's money-correctness rails) refuses that commit, so
EVERY mobile invoice from an estimate failed. It matches what the audit trail
says: mobile_invoice_created has never written a row in production.

Pinned here: the header a mobile invoice commits with is internally consistent —
total == Σlines + tax — for the tiered-proposal branch and the plain-estimate
branch alike, and the invariant that caught it is left armed.
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.invoice_invariants import check_invoice
from gdx_dispatch.models.tenant_models import (
    Customer,
    Invoice,
    InvoiceAdjustment,
    InvoiceLine,
    Job,
    Payment,
)
from starlette.requests import Request


def _request() -> Request:
    req = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    req.state.tenant = {"id": TENANT}
    req.state.tenant_id = TENANT
    return req

TENANT = "tenant-mobile-inv"


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    for tbl in [
        Job.__table__,
        Customer.__table__,
        Invoice.__table__,
        InvoiceLine.__table__,
        InvoiceAdjustment.__table__,
        Payment.__table__,
    ]:
        tbl.create(bind=engine, checkfirst=True)
    TenantBase.metadata.create_all(bind=engine, checkfirst=True)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _invoice_with_lines(db, *, line_totals, tax_amount, header_total):
    """Build an invoice the way a handler does, then ask the invariant."""
    inv = Invoice(
        id=uuid4(),
        customer_id=uuid4(),
        invoice_number=f"INV-{uuid4().hex[:6].upper()}",
        subtotal=Decimal(str(sum(line_totals))),
        tax_amount=Decimal(str(tax_amount)),
        total=Decimal(str(header_total)),
        balance_due=Decimal(str(header_total)),
        status="draft",
        public_token=f"tok-{uuid4().hex}",
        company_id=TENANT,
    )
    db.add(inv)
    db.flush()
    for i, amount in enumerate(line_totals):
        db.add(InvoiceLine(
            id=uuid4(),
            invoice_id=inv.id,
            description=f"line {i}",
            quantity=1,
            unit_price=Decimal(str(amount)),
            line_total=Decimal(str(amount)),
            sort_order=i,
            company_id=TENANT,
        ))
    db.flush()
    return inv


def test_dropping_the_tax_from_the_header_is_a_violation(db) -> None:
    """The exact shape the mobile handler used to commit. If this ever stops
    being a violation, the invariant has been weakened and the 500 this test
    exists for would return as a silent money bug instead."""
    inv = _invoice_with_lines(db, line_totals=[2090.00], tax_amount=154.24, header_total=2090.00)
    violation = check_invoice(db, inv)
    assert violation is not None
    assert "154.24" in violation


def test_header_including_tax_satisfies_the_invariant(db) -> None:
    """What the fixed handler commits."""
    inv = _invoice_with_lines(db, line_totals=[2090.00], tax_amount=154.24, header_total=2244.24)
    assert check_invoice(db, inv) is None


def test_the_restatement_keeps_the_estimate_tax_and_satisfies_the_invariant(db) -> None:
    """The restatement both estimate branches now share.

    Two failures this pins, both of which happened:
      1. `total = line_sum` with tax_amount still set → the invariant refuses
         the commit and every field invoice 500s (the original bug);
      2. recomputing the tax as `line_sum × rate` → sales tax charged on
         LABOR, disagreeing with the estimate the customer accepted (the bug an
         adversarial review found in the first fix).

    The earlier version of this test read the handler's SOURCE and asserted on
    substrings; it would have passed with (2) shipped. Call the code.
    """
    from gdx_dispatch.routers.mobile_invoicing import restate_invoice_header

    # $1000 materials + $500 labor; tax computed upstream on materials only.
    inv = _invoice_with_lines(
        db, line_totals=[1000.00, 500.00], tax_amount=100.00, header_total=0,
    )

    restate_invoice_header(inv, 1500.00)
    db.flush()

    assert Decimal(str(inv.subtotal)) == Decimal("1500.00")
    # Tax UNTOUCHED — not 1500 * 0.10 = 150.
    assert Decimal(str(inv.tax_amount)) == Decimal("100.00"), "the tax was recomputed"
    assert Decimal(str(inv.total)) == Decimal("1600.00")
    assert Decimal(str(inv.balance_due)) == Decimal(str(inv.total))
    # And the rail that started all this agrees.
    assert check_invoice(db, inv) is None


def test_the_restatement_handles_a_zero_tax_invoice(db) -> None:
    """No estimate tax (MN construction contracts are not taxed to the
    customer) — the header is just the lines, and the invariant still holds."""
    from gdx_dispatch.routers.mobile_invoicing import restate_invoice_header

    inv = _invoice_with_lines(db, line_totals=[250.00], tax_amount=0, header_total=0)
    restate_invoice_header(inv, 250.00)
    db.flush()

    assert Decimal(str(inv.total)) == Decimal("250.00")
    assert check_invoice(db, inv) is None


def test_both_branches_use_the_shared_restatement(db) -> None:
    """Two hand-written copies is how the tax bug got written twice. If a
    branch stops calling the shared function, this fails."""
    import inspect

    from gdx_dispatch.routers import mobile_invoicing

    src = inspect.getsource(mobile_invoicing.mobile_create_invoice)
    assert src.count("restate_invoice_header(") == 2
    # No branch may hand-assign the header again.
    assert "invoice.total = price" not in src
    assert "invoice.total = _money(new_subtotal)" not in src


# ---------------------------------------------------------------------------
# The discount (adversarial audit, 2026-08-13)
# ---------------------------------------------------------------------------
#
# The header fix turned a 500 into a WRONG INVOICE for discounted estimates:
# the copied lines are the estimate's pre-discount amounts, while the tax was
# computed on the post-discount base, so `total = Σlines + tax` over-billed by
# exactly the discount — and the totals invariant passed the whole time,
# because the numbers were internally consistent and simply wrong. Found by
# executing the real handler against Postgres, which is also the only place the
# estimate→invoice line copy runs (SQLite's uuid shape skips it silently).
#
# The office path has carried a materialized discount LINE since the 2026-08-04
# money audit (M7). These pin that the truck now does the same arithmetic.


def _totals_after_discount_line(line_totals, discount, tax_on_discounted_base):
    """What the handler builds: lines + a negative discount line, then the
    shared restatement. Mirrors mobile_create_invoice's plain-estimate branch
    without needing the Postgres-only line copy."""
    from gdx_dispatch.routers.mobile_invoicing import restate_invoice_header

    line_sum = sum(Decimal(str(x)) for x in line_totals) - Decimal(str(discount))
    inv = Invoice(
        id=uuid4(), customer_id=uuid4(), invoice_number=f"INV-{uuid4().hex[:6]}",
        subtotal=0, tax_amount=Decimal(str(tax_on_discounted_base)), total=0,
        balance_due=0, status="draft", public_token=f"tok-{uuid4().hex}",
        company_id=TENANT,
    )
    restate_invoice_header(inv, line_sum)
    return inv


def test_a_discounted_estimate_bills_what_the_customer_accepted() -> None:
    """$1,000 of work, $100 off, 7.375% on the discounted base.

    Accepted total: 900 + 66.38 = 966.38. Before the discount line existed this
    path billed 1,066.38 — the discount silently evaporated.
    """
    inv = _totals_after_discount_line([1000.00], discount=100.00, tax_on_discounted_base=66.38)
    assert Decimal(str(inv.subtotal)) == Decimal("900.00")
    assert Decimal(str(inv.total)) == Decimal("966.38")
    assert Decimal(str(inv.balance_due)) == Decimal("966.38")


def test_a_discount_with_no_tax_still_comes_off() -> None:
    """MN construction contracts are not taxed to the customer, so the
    zero-tax case is the common one here — and it was equally wrong."""
    inv = _totals_after_discount_line([1000.00], discount=100.00, tax_on_discounted_base=0)
    assert Decimal(str(inv.total)) == Decimal("900.00")


def test_the_plain_branch_writes_a_discount_line() -> None:
    """The arithmetic above only holds if the handler actually materializes the
    discount as a line. Invoice has no discount column; a header-only discount
    is lost on the first recalc."""
    import inspect

    from gdx_dispatch.routers import mobile_invoicing

    src = inspect.getsource(mobile_invoicing.mobile_create_invoice)
    assert 'description="Discount"' in src
    assert 'category="discount"' in src
    # Taxable on purpose — the discount reduced the taxed base upstream.
    assert "taxable=True" in src

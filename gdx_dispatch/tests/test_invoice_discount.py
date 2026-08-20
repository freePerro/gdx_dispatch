"""A whole-invoice discount the office can actually enter.

Doug 2026-08-19, asked line-vs-field: "whole invoice."

Before this the office could not enter one at all. `InvoiceLineCreateIn` has
`unit_price: ge=0` and `quantity: gt=0`, so a negative line is unrepresentable,
and the only discount row the system mints comes from the estimate-copy path —
which `/billing/new` never triggers, because it prefills lines client-side and
sends `line_items`, not `estimate_id`.

The discount is materialized server-side as the SAME `category="discount"`
negative line the estimate copy mints, so both surfaces produce identical rows
and `_recalculate_invoice` needs no special case.
"""
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from gdx_dispatch.models.tenant_models import Invoice
from gdx_dispatch.routers.invoices import InvoiceCreateIn, create_invoice
from gdx_dispatch.tests.test_invoices import (  # noqa: E402
    _current_user,
    _seed_job,
    tenant_db_session,  # noqa: F401  — pytest fixture, used by name
)


def _create(db, job, **kw):
    payload = InvoiceCreateIn(
        job_id=job.id,
        customer_id=job.customer_id,
        line_items=[{"description": "Door", "quantity": 1, "unit_price": 1000}],
        **kw,
    )
    return create_invoice(payload=payload, _=_current_user(), db=db)


class TestDiscountLine:
    def test_it_mints_the_same_shape_the_estimate_copy_mints(self, tenant_db_session):  # noqa: F811
        created = _create(tenant_db_session, _seed_job(tenant_db_session), discount=150)
        row = tenant_db_session.get(Invoice, UUID(created["id"]))

        disc = [ln for ln in row.lines if (ln.category or "") == "discount"]
        assert len(disc) == 1, "no discount line was minted"
        assert disc[0].description == "Discount"
        assert float(disc[0].unit_price) == -150
        assert float(disc[0].line_total) == -150
        # taxable=True mirrors the estimate-copy line: the discount must reduce
        # the taxable base, not sit outside it.
        assert disc[0].taxable is True

    def test_no_discount_means_no_line(self, tenant_db_session):  # noqa: F811
        created = _create(tenant_db_session, _seed_job(tenant_db_session))
        row = tenant_db_session.get(Invoice, UUID(created["id"]))
        assert not [ln for ln in row.lines if (ln.category or "") == "discount"]

    def test_zero_is_not_a_discount(self, tenant_db_session):  # noqa: F811
        created = _create(tenant_db_session, _seed_job(tenant_db_session), discount=0)
        row = tenant_db_session.get(Invoice, UUID(created["id"]))
        assert not [ln for ln in row.lines if (ln.category or "") == "discount"]

    def test_it_reduces_the_invoice_total(self, tenant_db_session):  # noqa: F811
        """The point of the feature. $1000 of goods less $150 is $850."""
        created = _create(tenant_db_session, _seed_job(tenant_db_session), discount=150)
        row = tenant_db_session.get(Invoice, UUID(created["id"]))
        assert float(row.subtotal) == 850

    def test_the_discount_line_sorts_last(self, tenant_db_session):  # noqa: F811
        job = _seed_job(tenant_db_session)
        created = create_invoice(
            payload=InvoiceCreateIn(
                job_id=job.id,
                customer_id=job.customer_id,
                discount=50,
                line_items=[
                    {"description": "A", "quantity": 1, "unit_price": 100},
                    {"description": "B", "quantity": 1, "unit_price": 200},
                ],
            ),
            _=_current_user(),
            db=tenant_db_session,
        )
        row = tenant_db_session.get(Invoice, UUID(created["id"]))
        last = max(row.lines, key=lambda ln: ln.sort_order or 0)
        assert (last.category or "") == "discount"


class TestDiscountContract:
    def test_negative_discounts_are_rejected(self):
        with pytest.raises(ValidationError):
            InvoiceCreateIn(customer_id=uuid4(), discount=-10)

    def test_it_cannot_be_combined_with_an_estimate_copy(self):
        """The copied estimate carries its OWN discount. Accepting one here too
        would bill the customer two discounts for one negotiation."""
        with pytest.raises(ValidationError) as exc:
            InvoiceCreateIn(
                customer_id=uuid4(),
                job_id=uuid4(),
                estimate_id=uuid4(),
                discount=100,
            )
        assert "carries its own discount" in str(exc.value)

    def test_it_is_fine_alongside_provenance(self):
        """A prefilled invoice's lines are the operator's, so its discount is
        the operator's too — no double-count risk."""
        payload = InvoiceCreateIn(
            customer_id=uuid4(),
            job_id=uuid4(),
            source_estimate_id=uuid4(),
            discount=100,
        )
        assert payload.discount == 100


class TestDiscountCannotGoNegative:
    """A discount bigger than the goods is a refund, not a discount.

    Found by review: `_recalculate_invoice` floors the TAXABLE BASE at zero but
    not the TOTAL. $1,000 of goods less a $1,500 discount wrote
    `subtotal=-500, total=-500`, posted negative revenue through
    `repost_invoice_issuance`, and floored `balance_due` to 0 — which skips the
    paid auto-flip, so the row sat in AR at -$500 forever. The client showed
    $0.00 for the same input, so nothing on screen revealed it.
    """

    def test_a_discount_larger_than_the_goods_is_refused(self, tenant_db_session):  # noqa: F811
        from fastapi import HTTPException

        job = _seed_job(tenant_db_session)
        with pytest.raises(HTTPException) as exc:
            _create(tenant_db_session, job, discount=1500)  # goods are 1000
        assert exc.value.status_code == 422
        assert "negative" in str(exc.value.detail)

    def test_a_discount_equal_to_the_goods_is_allowed(self, tenant_db_session):  # noqa: F811
        """Zeroing an invoice is legitimate — a full goodwill write-off."""
        created = _create(tenant_db_session, _seed_job(tenant_db_session), discount=1000)
        row = tenant_db_session.get(Invoice, UUID(created["id"]))
        assert float(row.subtotal) == 0

    def test_no_invoice_can_end_up_negative(self, tenant_db_session):  # noqa: F811
        """The property, stated directly."""
        job = _seed_job(tenant_db_session)
        created = _create(tenant_db_session, job, discount=999.99)
        row = tenant_db_session.get(Invoice, UUID(created["id"]))
        assert float(row.subtotal) >= 0
        assert float(row.total) >= 0


class TestDiscountWithRealTax:
    """The tax base, at the tenant's real 7.38%."""

    def test_tax_is_charged_on_the_discounted_taxable_base(self, tenant_db_session):  # noqa: F811
        job = _seed_job(tenant_db_session)
        created = create_invoice(
            payload=InvoiceCreateIn(
                job_id=job.id,
                customer_id=job.customer_id,
                tax_rate=0.0738,
                discount=200,
                line_items=[
                    {"description": "Goods", "quantity": 1, "unit_price": 1000, "taxable": True},
                    {"description": "Labor", "quantity": 1, "unit_price": 500, "taxable": False},
                ],
            ),
            _=_current_user(),
            db=tenant_db_session,
        )
        row = tenant_db_session.get(Invoice, UUID(created["id"]))
        # Taxable base: 1000 - 200 = 800. 800 * 0.0738 = 59.04.
        assert float(row.tax_amount) == pytest.approx(59.04, abs=0.01)
        # Subtotal: 1000 + 500 - 200 = 1300. Total: 1300 + 59.04.
        assert float(row.subtotal) == pytest.approx(1300, abs=0.01)
        assert float(row.total) == pytest.approx(1359.04, abs=0.01)

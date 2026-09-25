"""The server half of "a document names its customer, and the name is the
route to the record".

The mobile estimate and invoice dialogs turn the customer name into a
router-link to /mobile/customers/{customer_id}. That only works if the detail
payload carries BOTH halves, and it is only *safe* if the two halves always
describe the same customer and the name is withheld when the record is gone.
This file pins the three properties the UI guard depends on:

1. **Name present when the record is reachable.** `_serialize_estimate` and
   `_serialize_invoice` have no db handle, so neither emits a real
   customer_name — the estimate payload carried none at all, and
   `_serialize_invoice`'s ``getattr(invoice, 'customer_name', ...)`` reads a
   column `invoices` does not have and always yields "". The detail handlers
   resolve it.

2. **Name withheld when GET /api/customers/{id} would 404.** That endpoint
   filters `deleted_at` (`customers.py:_ensure_customer_exists`), so a
   soft-deleted customer must arrive with no name — the UI guards its link on
   the name, and that is what keeps a tech off a dead end.

3. **The id and the name never describe different customers.** The estimate
   LIST endpoint falls back to the JOB's customer name without changing
   `Estimate.customer_id`; a link built from that pair would name one customer
   and navigate to another. The detail handler uses the estimate's own
   customer or nothing.

The client half is pinned in
frontend/src/views/__tests__/MobileCustomerRecordLinks.spec.js.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models.tenant_models import (
    Customer,
    Invoice,
    InvoiceLine,
    Job,
    JobPartNeeded,
    Payment,
)
from gdx_dispatch.modules.proposals.models import Estimate, EstimateLine
from gdx_dispatch.routers.estimates import get_estimate
from gdx_dispatch.routers.invoices import InvoiceCreateIn, create_invoice, get_invoice


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # Same two-step as test_invoice_customer_contact: the money tables hang off
    # Base, the rest off TenantBase, and create_all on one misses the other.
    for tbl in [
        Job.__table__,
        Estimate.__table__,
        EstimateLine.__table__,
        Invoice.__table__,
        InvoiceLine.__table__,
        Payment.__table__,
        JobPartNeeded.__table__,
    ]:
        tbl.create(bind=engine, checkfirst=True)
    TenantBase.metadata.create_all(bind=engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _user() -> dict[str, str]:
    return {"user_id": "user-1", "tenant_id": "tenant-test", "role": "admin"}


def _customer(db, name: str = "Acme Door Co", *, deleted: bool = False) -> Customer:
    cust = Customer(name=name, company_id="tenant-test")
    if deleted:
        cust.deleted_at = datetime.now(UTC)
    db.add(cust)
    db.commit()
    db.refresh(cust)
    return cust


def _job(db, customer_id) -> Job:
    job = Job(
        customer_id=customer_id,
        title="Service call",
        lifecycle_stage="service_call",
        dispatch_status="unassigned",
        billing_status="unbilled",
        company_id="tenant-test",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _estimate(db, *, customer_id=None, job_id=None) -> Estimate:
    est = Estimate(
        estimate_number=f"E-{uuid4().hex[:8]}",
        status="sent",
        customer_id=customer_id,
        job_id=job_id,
        company_id="tenant-test",
        public_token=uuid4().hex,
    )
    db.add(est)
    db.commit()
    db.refresh(est)
    return est


# ── GET /api/estimates/{id} ───────────────────────────────────────────────


def test_estimate_detail_names_its_own_customer(db):
    cust = _customer(db)
    est = _estimate(db, customer_id=cust.id)

    payload = get_estimate(est.id, _=_user(), db=db)

    assert payload["customer_id"] == str(cust.id)
    assert payload["customer_name"] == "Acme Door Co"


def test_estimate_detail_flags_a_soft_deleted_customer_without_losing_the_name(db):
    """The name still comes back; `customer_deleted` is what suppresses a link.

    Blanking the name instead would be the wrong lever — this payload is shared
    with the desktop estimate header, which would then show nothing while its
    own link kept pointing at a record /api/customers/{id} answers with 404.
    Lose the information, keep the dead end.
    """
    cust = _customer(db, deleted=True)
    est = _estimate(db, customer_id=cust.id)

    payload = get_estimate(est.id, _=_user(), db=db)

    assert payload["customer_id"] == str(cust.id)
    assert payload["customer_name"] == "Acme Door Co"
    assert payload["customer_deleted"] is True


def test_estimate_detail_marks_a_live_customer_not_deleted(db):
    cust = _customer(db)
    est = _estimate(db, customer_id=cust.id)

    payload = get_estimate(est.id, _=_user(), db=db)

    assert payload["customer_deleted"] is False


def test_estimate_detail_never_borrows_the_jobs_customer_name(db):
    """The list endpoint's job fallback, applied here, would pair customer B's
    name with customer A's id — a link that names one customer and navigates to
    another. The detail handler answers with the id's own name."""
    on_estimate = _customer(db, "Customer On The Estimate")
    on_job = _customer(db, "Customer On The Job")
    job = _job(db, on_job.id)
    est = _estimate(db, customer_id=on_estimate.id, job_id=job.id)

    payload = get_estimate(est.id, _=_user(), db=db)

    assert payload["customer_id"] == str(on_estimate.id)
    assert payload["customer_name"] == "Customer On The Estimate"


def test_estimate_detail_with_no_customer_id_carries_no_name(db):
    """A QB-imported estimate linked only through its job: no id means no link,
    so it must not be handed a name that implies one."""
    on_job = _customer(db, "Customer On The Job")
    job = _job(db, on_job.id)
    est = _estimate(db, customer_id=None, job_id=job.id)

    payload = get_estimate(est.id, _=_user(), db=db)

    assert payload["customer_id"] is None
    assert not payload.get("customer_name")


# ── GET /api/invoices/{id} ────────────────────────────────────────────────


def _invoice_for(db, *, customer_id, job_id=None) -> dict:
    return create_invoice(
        payload=InvoiceCreateIn(
            job_id=job_id,
            customer_id=customer_id,
            due_date=date.today() + timedelta(days=30),
            line_items=[{"description": "Spring", "quantity": 1, "unit_price": Decimal("75.00")}],
        ),
        _=_user(),
        db=db,
    )


def test_invoice_detail_names_its_own_customer_with_no_job(db):
    """`invoices` has no customer_name column, and the Job fallback cannot fire
    without a job. Before this the payload was an id with no name — 43 of 415
    invoices on the local book, measured 2026-09-24."""
    cust = _customer(db)
    created = _invoice_for(db, customer_id=cust.id)

    payload = get_invoice(UUID(created["id"]), _=_user(), db=db)

    assert payload["customer_id"] == str(cust.id)
    assert payload["customer_name"] == "Acme Door Co"


def test_invoice_detail_flags_a_soft_deleted_customer_without_losing_the_name(db):
    """The regression guard for InvoiceDetailView.vue:65.

    That page guards its customer link on `customer_id` ALONE and normalizes a
    missing name to "Unknown". If this payload dropped the name for a deleted
    customer, the desktop invoice would read "Unknown" AND keep linking to a
    record that 404s — information lost, dead end kept. So the name survives
    and `customer_deleted` carries the fact.
    """
    cust = _customer(db)
    created = _invoice_for(db, customer_id=cust.id)
    cust.deleted_at = datetime.now(UTC)
    db.commit()

    payload = get_invoice(UUID(created["id"]), _=_user(), db=db)

    assert payload["customer_id"] == str(cust.id)
    assert payload["customer_name"] == "Acme Door Co"
    assert payload["customer_deleted"] is True


def test_invoice_detail_marks_a_live_customer_not_deleted(db):
    cust = _customer(db)
    created = _invoice_for(db, customer_id=cust.id)

    payload = get_invoice(UUID(created["id"]), _=_user(), db=db)

    assert payload["customer_deleted"] is False


def test_invoice_detail_prefers_its_own_customer_over_the_jobs(db):
    """The name and the id must describe the same customer.

    The 2026-04-29 Job → Customer fallback was written for QB-imported invoices
    with a NULL `Invoice.customer_id`, but it was reached on *every* invoice
    with a job, because `_serialize_invoice` always leaves customer_name empty.
    So an invoice whose own customer is A, hanging off a job whose customer is
    B, came back named B — and it OVERWROTE customer_id with B's. A link built
    from that pair sends the tech to the wrong customer's record.
    """
    on_invoice = _customer(db, "Customer On The Invoice")
    on_job = _customer(db, "Customer On The Job")
    job = _job(db, on_job.id)
    created = _invoice_for(db, customer_id=on_invoice.id, job_id=job.id)

    payload = get_invoice(UUID(created["id"]), _=_user(), db=db)

    assert payload["customer_id"] == str(on_invoice.id)
    assert payload["customer_name"] == "Customer On The Invoice"


def test_invoice_detail_names_a_deleted_own_customer_rather_than_the_jobs(db):
    """An invoice whose own customer is soft-deleted, hanging off a job.

    The own-customer branch still wins: the invoice belongs to the customer its
    own `customer_id` names, deleted or not, and `customer_id` in this payload
    is that one. Falling through to the job here would pair the job's customer
    NAME with the invoice's own customer ID.
    """
    own = _customer(db, "Deleted Own Customer")
    on_job = _customer(db, "Customer On The Job")
    job = _job(db, on_job.id)
    created = _invoice_for(db, customer_id=own.id, job_id=job.id)
    own.deleted_at = datetime.now(UTC)
    db.commit()

    payload = get_invoice(UUID(created["id"]), _=_user(), db=db)

    assert payload["customer_id"] == str(own.id)
    assert payload["customer_name"] == "Deleted Own Customer"
    assert payload["customer_deleted"] is True

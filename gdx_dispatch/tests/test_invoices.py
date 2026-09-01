from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models.tenant_models import Invoice, InvoiceLine, Job, JobPartNeeded, Payment
from gdx_dispatch.modules.proposals.models import Estimate, EstimateLine
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.invoices import (
    InvoiceCreateIn,
    InvoiceLineCreateIn,
    InvoicePatchIn,
    PaymentCreateIn,
    add_invoice_line,
    create_invoice,
    finalize_invoice,
    get_invoice,
    list_invoices,
    list_payments,
    patch_invoice,
    record_payment,
    router,
    send_invoice,
)


@pytest.fixture
def tenant_db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

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

    db = Session()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def _seed_job(db, title: str = "Garage repair") -> Job:
    job = Job(
        customer_id=uuid4(),
        title=title,
        description="Test job",
        lifecycle_stage="estimate",
        dispatch_status="unassigned",
        billing_status="unbilled",
        company_id="tenant-test",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _seed_estimate(db, job_id, total: Decimal = Decimal("125.00")) -> Estimate:
    estimate = Estimate(
        job_id=job_id,
        customer_id=uuid4(),
        estimate_number=f"EST-{uuid4().hex[:8]}",
        label="Approved estimate",
        notes="reference",
        proposal_mode=False,
        total=total,
        status="accepted",
        public_token=uuid4().hex,
        company_id="tenant-test",  # NOT NULL since Build Rule 5 hardening
    )
    db.add(estimate)
    db.commit()
    db.refresh(estimate)
    return estimate


def _seed_estimate_line(db, estimate_id, description: str, qty: int, unit_price: Decimal, sort_order: int = 1, company_id: str = "tenant-1"):
    line = EstimateLine(
        estimate_id=estimate_id,
        description=description,
        quantity=qty,
        unit_price=unit_price,
        line_total=(unit_price * qty),
        sort_order=sort_order,
        company_id=company_id,
    )
    db.add(line)
    db.commit()
    db.refresh(line)
    return line


def _current_user() -> dict[str, str]:
    return {"user_id": "user-1", "tenant_id": "tenant-1", "role": "admin"}


def _mock_request():
    from types import SimpleNamespace
    return SimpleNamespace(state=SimpleNamespace(tenant={"id": "tenant-1"}))


def _verify(db, inv: dict) -> None:
    """§11 rail (2026-08-08): delivery endpoints refuse unverified drafts.
    Tests exercising send/compose/mark-sent mechanics stamp the verification
    the office would click; the rail itself is pinned in its own tests."""
    from datetime import UTC, datetime
    row = db.get(Invoice, UUID(inv["id"]))
    row.verified_at = datetime.now(UTC)
    db.commit()


def test_routes_require_auth_dependency():
    import inspect

    protected = []
    for route in router.routes:
        sig = inspect.signature(route.endpoint)
        for param in sig.parameters.values():
            dep = param.default
            if hasattr(dep, "dependency") and dep.dependency is get_current_user:
                protected.append(route.path)
                break

    assert len(protected) >= 9


def test_list_invoices_empty(tenant_db_session):
    rows = list_invoices(request=_mock_request(), status=None, customer_id=None, _=_current_user(), db=tenant_db_session)
    assert rows == []


def test_create_invoice_from_job_only(tenant_db_session):
    job = _seed_job(tenant_db_session)
    payload = InvoiceCreateIn(job_id=job.id, customer_id=job.customer_id)

    data = create_invoice(payload=payload, _=_current_user(), db=tenant_db_session)

    assert data["id"]
    assert data["job_id"] == str(job.id)
    assert data["status"] == "draft"
    assert data["invoice_number"].startswith("INV-")
    assert data["subtotal"] == 0.0
    assert data["tax_amount"] == 0.0
    assert data["total"] == 0.0


def test_create_invoice_from_estimate_copies_total_and_lines(tenant_db_session):
    job = _seed_job(tenant_db_session)
    est = _seed_estimate(tenant_db_session, job.id, Decimal("150.00"))
    _seed_estimate_line(tenant_db_session, est.id, "Spring replacement", 1, Decimal("100.00"), sort_order=1)
    _seed_estimate_line(tenant_db_session, est.id, "Labor", 1, Decimal("50.00"), sort_order=2)

    created = create_invoice(
        payload=InvoiceCreateIn(job_id=job.id, customer_id=job.customer_id, estimate_id=est.id),
        _=_current_user(),
        db=tenant_db_session,
    )
    invoice = get_invoice(UUID(created["id"]), _=_current_user(), db=tenant_db_session)

    assert created["subtotal"] == 150.0
    assert created["total"] == 150.0
    assert len(invoice["lines"]) == 2
    assert invoice["lines"][0]["description"] == "Spring replacement"
    assert invoice["lines"][1]["description"] == "Labor"


def test_create_invoice_snapshots_estimate_hide_line_prices(tenant_db_session):
    """A "total-only" estimate (hide_line_prices=True) carries that onto the
    invoice at conversion so the invoice PDF matches the estimate the customer
    already saw. The per-estimate override wins even if the tenant-default read
    is unavailable in this hermetic env (best-effort get_features → default)."""
    job = _seed_job(tenant_db_session)
    est = _seed_estimate(tenant_db_session, job.id, Decimal("150.00"))
    est.hide_line_prices = True
    tenant_db_session.commit()
    _seed_estimate_line(tenant_db_session, est.id, "Door", 1, Decimal("150.00"))

    created = create_invoice(
        payload=InvoiceCreateIn(job_id=job.id, customer_id=job.customer_id, estimate_id=est.id),
        _=_current_user(),
        db=tenant_db_session,
    )
    assert created["hide_line_prices"] is True

    # A normal estimate (NULL override, no tenant default) → invoice shows prices.
    # force=True: the job already carries the first invoice and the 2026-07-23
    # double-billing guard would 409 a silent second create.
    est2 = _seed_estimate(tenant_db_session, job.id, Decimal("75.00"))
    _seed_estimate_line(tenant_db_session, est2.id, "Spring", 1, Decimal("75.00"))
    created2 = create_invoice(
        payload=InvoiceCreateIn(job_id=job.id, customer_id=job.customer_id, estimate_id=est2.id, force=True),
        _=_current_user(),
        db=tenant_db_session,
    )
    assert created2["hide_line_prices"] is False


def test_create_invoice_inherits_tenant_default_hide_line_prices(tenant_db_session, monkeypatch):
    """The headline "company default flows onto the invoice" path: a NULL-override
    estimate inherits the TENANT DEFAULT at conversion. The hermetic env can't
    read the control DB, so stub get_features to return the default = True and
    assert the invoice snapshots it (audit gap: this branch was otherwise only
    exercised in production)."""
    from gdx_dispatch.modules import estimates_features as ef
    monkeypatch.setattr(ef, "get_features", lambda tid: ef.EstimatesFeatures(hide_line_prices=True))

    job = _seed_job(tenant_db_session)
    est = _seed_estimate(tenant_db_session, job.id, Decimal("100.00"))  # hide_line_prices = None (inherit)
    _seed_estimate_line(tenant_db_session, est.id, "Door", 1, Decimal("100.00"))
    created = create_invoice(
        payload=InvoiceCreateIn(job_id=job.id, customer_id=job.customer_id, estimate_id=est.id),
        _=_current_user(),
        db=tenant_db_session,
    )
    assert created["hide_line_prices"] is True


def test_create_invoice_validation_allows_missing_job_id():
    """Counter-sale invoices have no job. customer_id stays required; job_id
    is optional after the 2026-05-14 counter-sale flip."""
    payload = InvoiceCreateIn(customer_id=uuid4())
    assert payload.job_id is None


def test_create_invoice_validation_estimate_requires_job_id():
    """Estimates are job-scoped — estimate_id without job_id is incoherent."""
    with pytest.raises(ValidationError):
        InvoiceCreateIn(customer_id=uuid4(), estimate_id=uuid4())


def test_create_invoice_validation_from_part_ids_requires_job_id():
    """Parts checklists are job-scoped."""
    with pytest.raises(ValidationError):
        InvoiceCreateIn(customer_id=uuid4(), from_part_ids=[uuid4()])


def test_create_invoice_rejects_unknown_fields_d100(tenant_db_session):
    """D100 regression — an earlier session, 2026-04-25.

    Pre-fix the model accepted extras and silently dropped them, so the
    frontend's `customer_id` + `line_items` + a F1-audit-style `total`
    payload all evaporated and the row landed with $0 totals. Strict
    mode now surfaces unknown fields as 422 at the contract layer.
    """
    with pytest.raises(ValidationError):
        InvoiceCreateIn.model_validate({"job_id": str(uuid4()), "totally_made_up_field": 42})


def test_create_invoice_counter_sale_no_job(tenant_db_session):
    """Counter-sale flow (2026-05-14) — invoice with line items but no job.
    Customer is the only required link; job_id stays NULL on the row."""
    customer_uuid = uuid4()
    payload = InvoiceCreateIn(
        customer_id=customer_uuid,
        line_items=[
            {"description": "Spring (counter sale)", "quantity": 2, "unit_price": 35.00},
        ],
    )
    data = create_invoice(payload=payload, _=_current_user(), db=tenant_db_session)
    assert data["job_id"] is None
    assert data["customer_id"] == str(customer_uuid)
    assert float(data["subtotal"]) == 70.0
    full = get_invoice(UUID(data["id"]), _=_current_user(), db=tenant_db_session)
    assert len(full["lines"]) == 1


def test_create_invoice_with_line_items_computes_totals_d100(tenant_db_session):
    """D100 — bare-create path now consumes line_items + sets totals + customer_id."""
    job = _seed_job(tenant_db_session)
    payload = InvoiceCreateIn(
        job_id=job.id,
        customer_id=job.customer_id,        line_items=[
            {"description": "Service call", "quantity": 1, "unit_price": 75.00},
            {"description": "Spring", "quantity": 2, "unit_price": 25.00},
        ],
        tax_amount=10.00,
    )
    data = create_invoice(payload=payload, _=_current_user(), db=tenant_db_session)
    # Sum: 75 + (2 * 25) = 125 subtotal; +10 tax = 135 total
    assert float(data["subtotal"]) == 125.0
    assert float(data["total"]) == 135.0
    # customer_id derived from job
    assert data["customer_id"] == str(job.customer_id)
    full = get_invoice(UUID(data["id"]), _=_current_user(), db=tenant_db_session)
    assert len(full["lines"]) == 2


def test_create_invoice_sets_invoice_date_d99(tenant_db_session):
    """D99 regression — an earlier session, 2026-04-25.

    Pre-fix every invoice landed with `invoice_date = NULL` so all
    period-filtered metrics (Dashboard Revenue, Reports) read $0
    against $712k of real revenue. Default to today; honour explicit
    payload value.
    """
    job = _seed_job(tenant_db_session)
    today = date.today()
    data = create_invoice(payload=InvoiceCreateIn(job_id=job.id, customer_id=job.customer_id), _=_current_user(), db=tenant_db_session)
    assert data["invoice_date"] == today.isoformat()

    explicit = date(2026, 1, 15)
    data2 = create_invoice(
        payload=InvoiceCreateIn(job_id=job.id, customer_id=job.customer_id, invoice_date=explicit),
        _=_current_user(),
        db=tenant_db_session,
    )
    assert data2["invoice_date"] == explicit.isoformat()


def test_create_invoice_rejects_unknown_job(tenant_db_session):
    with pytest.raises(HTTPException) as exc:
        create_invoice(
            payload=InvoiceCreateIn(job_id=uuid4(), customer_id=uuid4()),
            _=_current_user(),
            db=tenant_db_session,
        )
    assert exc.value.status_code == 404


def test_create_invoice_requires_customer_id():
    """2026-05-11 — Invoice.customer_id is NOT NULL and the contract requires
    it. Omitting must fail at Pydantic, not silently fall back to the job's
    customer (which can itself be NULL)."""
    with pytest.raises(ValidationError):
        InvoiceCreateIn(job_id=uuid4())


def test_get_invoice_not_found(tenant_db_session):
    with pytest.raises(HTTPException) as exc:
        get_invoice(invoice_id=uuid4(), _=_current_user(), db=tenant_db_session)
    assert exc.value.status_code == 404


def test_patch_invoice_updates_draft_fields(tenant_db_session):
    job = _seed_job(tenant_db_session)
    created = create_invoice(payload=InvoiceCreateIn(job_id=job.id, customer_id=job.customer_id), _=_current_user(), db=tenant_db_session)

    updated = patch_invoice(
        invoice_id=UUID(created["id"]),
        payload=InvoicePatchIn(tax_amount=10.25, notes="  apply tax  "),
        current_user=_current_user(),
        db=tenant_db_session,
    )

    assert updated["tax_amount"] == 10.25
    assert updated["notes"] == "apply tax"


def test_patch_invoice_rejects_non_draft(tenant_db_session):
    job = _seed_job(tenant_db_session)
    inv = create_invoice(payload=InvoiceCreateIn(job_id=job.id, customer_id=job.customer_id), _=_current_user(), db=tenant_db_session)
    _verify(tenant_db_session, inv)
    send_invoice(invoice_id=UUID(inv["id"]), _=_current_user(), db=tenant_db_session)

    with pytest.raises(HTTPException) as exc:
        patch_invoice(
            invoice_id=UUID(inv["id"]),
            payload=InvoicePatchIn(tax_amount=9.99),
            current_user=_current_user(),
            db=tenant_db_session,
        )
    assert exc.value.status_code == 409


def test_send_invoice_marks_sent_and_sets_public_token(tenant_db_session):
    job = _seed_job(tenant_db_session)
    inv = create_invoice(payload=InvoiceCreateIn(job_id=job.id, customer_id=job.customer_id), _=_current_user(), db=tenant_db_session)

    _verify(tenant_db_session, inv)

    sent = send_invoice(invoice_id=UUID(inv["id"]), _=_current_user(), db=tenant_db_session)

    assert sent["status"] == "sent"
    assert sent["public_token"]
    # 2026-07-22 — sent_at is a DELIVERY fact (it feeds Billing's "Last Sent"
    # column). This invoice's customer_id points at no Customer row, so no
    # email went out and the stamp must stay empty even though status flipped.
    assert sent["email_sent"] is False
    assert sent["sent_at"] is None
    # No delivery → no channel either (migration 057).
    assert sent["sent_via"] is None


def test_send_invoice_skips_oversized_pdf_but_still_delivers(tenant_db_session, monkeypatch):
    """2026-07-20 (audit catch) — Graph rejects the WHOLE message when inline
    attachments blow the ~4MB request cap, which would turn 'html-only email'
    into 'no email at all'. An oversized PDF must be skipped (pdf_attached
    False) while the email itself still goes out."""
    from gdx_dispatch.core.transactional_email import MAX_INLINE_ATTACHMENT_BYTES
    from gdx_dispatch.models.tenant_models import Customer

    captured = {}

    def fake_send(**kwargs):
        captured.update(kwargs)
        return True, "outlook_graph", None

    monkeypatch.setattr(
        "gdx_dispatch.core.transactional_email.send_transactional_email", fake_send
    )
    monkeypatch.setattr(
        "gdx_dispatch.core.pdf_generator.generate_invoice_pdf",
        lambda **kw: b"%PDF-1.4 " + b"x" * (MAX_INLINE_ATTACHMENT_BYTES + 1),
    )

    cust = Customer(
        name="Big PDF Customer", email="bigpdf@example.com",
        phone="555-0400", company_id="tenant-test",
    )
    tenant_db_session.add(cust)
    tenant_db_session.commit()
    tenant_db_session.refresh(cust)

    job = _seed_job(tenant_db_session)
    inv = create_invoice(
        payload=InvoiceCreateIn(
            job_id=job.id, customer_id=cust.id,
            due_date=date.today() + timedelta(days=30),
        ),
        _=_current_user(), db=tenant_db_session,
    )

    _verify(tenant_db_session, inv)

    sent = send_invoice(invoice_id=UUID(inv["id"]), _=_current_user(), db=tenant_db_session)

    assert sent["email_sent"] is True
    assert sent["pdf_attached"] is False
    assert sent["sent_at"] is not None  # acknowledged delivery → stamp moves
    assert sent["sent_via"] == "email"  # server-send records its channel
    assert captured["attachments"] is None


def test_send_invoice_twice_is_a_valid_resend(tenant_db_session, monkeypatch):
    """2026-07-20 — re-send must work: the concrete case is an invoice whose
    first email went out without the PDF. sent→sent transitions again without
    a 409 and re-stamps sent_at (only paid/void are locked). Delivery is
    faked as acknowledged because sent_at only moves on a real send
    (2026-07-22 — it feeds Billing's "Last Sent" column)."""
    from gdx_dispatch.models.tenant_models import Customer

    monkeypatch.setattr(
        "gdx_dispatch.core.transactional_email.send_transactional_email",
        lambda **kwargs: (True, "outlook_graph", None),
    )
    monkeypatch.setattr(
        "gdx_dispatch.core.pdf_generator.generate_invoice_pdf",
        lambda **kw: b"%PDF-1.4 tiny",
    )

    cust = Customer(
        name="Resend Customer", email="resend@example.com",
        phone="555-0401", company_id="tenant-test",
    )
    tenant_db_session.add(cust)
    tenant_db_session.commit()
    tenant_db_session.refresh(cust)

    job = _seed_job(tenant_db_session)
    inv = create_invoice(payload=InvoiceCreateIn(job_id=job.id, customer_id=cust.id), _=_current_user(), db=tenant_db_session)

    _verify(tenant_db_session, inv)

    first = send_invoice(invoice_id=UUID(inv["id"]), _=_current_user(), db=tenant_db_session)
    _verify(tenant_db_session, inv)
    second = send_invoice(invoice_id=UUID(inv["id"]), _=_current_user(), db=tenant_db_session)

    assert first["status"] == "sent"
    assert second["status"] == "sent"
    assert first["email_sent"] is True
    assert first["sent_at"] is not None
    assert second["sent_at"] >= first["sent_at"]


def _seed_paid_invoice_with_customer(db, email: str, monkeypatch):
    """Customer with an email + a fully-paid $450 invoice (real Payment row —
    _recalculate flips status to 'paid' at zero balance) + delivery mocks.
    Returns (inv_dict, captured_send_kwargs)."""
    from gdx_dispatch.models.tenant_models import Customer

    captured = {}

    def fake_send(**kwargs):
        captured.update(kwargs)
        return True, "outlook_graph", None

    monkeypatch.setattr(
        "gdx_dispatch.core.transactional_email.send_transactional_email", fake_send
    )
    monkeypatch.setattr(
        "gdx_dispatch.core.pdf_generator.generate_invoice_pdf",
        lambda **kw: b"%PDF-1.4 tiny",
    )

    cust = Customer(
        name="Receipt Customer", email=email,
        phone="555-0402", company_id="tenant-test",
    )
    db.add(cust)
    db.commit()
    db.refresh(cust)

    job = _seed_job(db)
    inv = create_invoice(
        payload=InvoiceCreateIn(
            job_id=job.id, customer_id=cust.id,
            line_items=[{"description": "Opener", "quantity": 1, "unit_price": 450.00}],
        ),
        _=_current_user(), db=db,
    )
    _verify(db, inv)
    record_payment(
        invoice_id=UUID(inv["id"]),
        payload=PaymentCreateIn(amount=450.0, method="check"),
        _=_current_user(), db=db,
    )
    return inv, captured


def test_send_invoice_on_paid_is_a_receipt(tenant_db_session, monkeypatch):
    """2026-08-17 — sending a PAID invoice must read as a receipt: 'Payment
    received' subject + '-paid' PDF name, status stays paid (no AR
    resurrection), and the delivery stamp still moves."""
    inv, captured = _seed_paid_invoice_with_customer(
        tenant_db_session, "receipt@example.com", monkeypatch
    )

    sent = send_invoice(invoice_id=UUID(inv["id"]), _=_current_user(), db=tenant_db_session)

    assert sent["status"] == "paid"
    assert sent["email_sent"] is True
    # Template-driven subject (composer parity): "Invoice INV-x from Co".
    assert captured["subject"].startswith("Payment received — Invoice ")
    assert captured["attachments"][0]["name"].endswith("-paid.pdf")
    assert sent["sent_at"] is not None
    assert sent["sent_via"] == "email"


def test_send_receipt_delivers_the_paid_invoice_for_real(tenant_db_session, monkeypatch):
    """2026-08-17 — POST /send-receipt was a fake-success no-op (issue #220,
    contract class C6): it wrote a payment_receipt_sent audit row and returned
    {"sent": true} with NO email behind it. Pin the honest version: a real
    transactional send goes out with the receipt subject, and the response
    only claims sent on acknowledged delivery."""
    from gdx_dispatch.routers.invoices import send_payment_receipt

    inv, captured = _seed_paid_invoice_with_customer(
        tenant_db_session, "receipt2@example.com", monkeypatch
    )

    out = send_payment_receipt(invoice_id=inv["id"], db=tenant_db_session, _=_current_user())

    assert out["sent"] is True
    assert out["to"] == "receipt2@example.com"
    assert out["pdf_attached"] is True
    # Template-driven subject (composer parity): "Invoice INV-x from Co".
    assert captured["subject"].startswith("Payment received — Invoice ")
    row = tenant_db_session.get(Invoice, UUID(inv["id"]))
    assert row.status == "paid"
    assert row.sent_at is not None


def test_send_receipt_reports_failed_delivery_honestly(tenant_db_session, monkeypatch):
    """A failed send must not claim success — that lie is exactly what the
    old stub did. sent stays False and the delivery stamp does not move."""
    from gdx_dispatch.routers.invoices import send_payment_receipt

    inv, _captured = _seed_paid_invoice_with_customer(
        tenant_db_session, "receipt3@example.com", monkeypatch
    )
    monkeypatch.setattr(
        "gdx_dispatch.core.transactional_email.send_transactional_email",
        lambda **kwargs: (False, None, "no_email_provider"),
    )

    out = send_payment_receipt(invoice_id=inv["id"], db=tenant_db_session, _=_current_user())

    assert out["sent"] is False
    assert out["email_skip_reason"] == "no_email_provider"
    row = tenant_db_session.get(Invoice, UUID(inv["id"]))
    assert row.sent_at is None


def test_send_receipt_refuses_unpaid_invoice(tenant_db_session):
    """Receipts are for settled invoices — an unpaid one goes through /send."""
    from gdx_dispatch.routers.invoices import send_payment_receipt

    job = _seed_job(tenant_db_session)
    inv = create_invoice(
        payload=InvoiceCreateIn(job_id=job.id, customer_id=job.customer_id),
        _=_current_user(), db=tenant_db_session,
    )
    _verify(tenant_db_session, inv)

    with pytest.raises(HTTPException) as exc:
        send_payment_receipt(invoice_id=inv["id"], db=tenant_db_session, _=_current_user())
    assert exc.value.status_code == 409


def test_send_receipt_requires_a_real_payment(tenant_db_session):
    """A credit-memo'd invoice reaches status 'paid' with zero money received
    — "payment received" over a write-off would be a lie, so no Payment rows
    means 422."""
    from gdx_dispatch.routers.invoices import send_payment_receipt

    job = _seed_job(tenant_db_session)
    inv = create_invoice(
        payload=InvoiceCreateIn(job_id=job.id, customer_id=job.customer_id),
        _=_current_user(), db=tenant_db_session,
    )
    invoice_obj = tenant_db_session.get(Invoice, UUID(inv["id"]))
    invoice_obj.status = "paid"
    tenant_db_session.commit()

    with pytest.raises(HTTPException) as exc:
        send_payment_receipt(invoice_id=inv["id"], db=tenant_db_session, _=_current_user())
    assert exc.value.status_code == 422


def test_send_invoice_attaches_the_invoice_pdf(tenant_db_session, monkeypatch):
    """2026-07-20 — the direct /send email must carry the invoice PDF.

    Before this, /send (Billing bulk-send, mobile) emailed an HTML body only;
    a real customer received an invoice email with no PDF attached. Pin that
    the transactional send now gets a generated PDF whose bytes render the
    invoice, and that the response reports pdf_attached."""
    from gdx_dispatch.models.tenant_models import Customer

    captured = {}

    def fake_send(**kwargs):
        captured.update(kwargs)
        return True, "outlook_graph", None

    monkeypatch.setattr(
        "gdx_dispatch.core.transactional_email.send_transactional_email", fake_send
    )

    cust = Customer(
        name="Attach Customer", email="attach@example.com",
        phone="555-0300", company_id="tenant-test",
    )
    tenant_db_session.add(cust)
    tenant_db_session.commit()
    tenant_db_session.refresh(cust)

    job = _seed_job(tenant_db_session, title="Opener replacement")
    inv = create_invoice(
        payload=InvoiceCreateIn(
            job_id=job.id, customer_id=cust.id,
            due_date=date.today() + timedelta(days=30),
            line_items=[{"description": "Opener", "quantity": 1, "unit_price": 450.00}],
        ),
        _=_current_user(), db=tenant_db_session,
    )

    _verify(tenant_db_session, inv)

    sent = send_invoice(invoice_id=UUID(inv["id"]), _=_current_user(), db=tenant_db_session)

    assert sent["email_sent"] is True
    assert sent["pdf_attached"] is True
    (att,) = captured["attachments"]
    assert att["name"] == f"invoice-{inv['invoice_number']}.pdf"
    assert att["content_type"] == "application/pdf"
    import base64 as _b64
    pdf_bytes = _b64.b64decode(att["content_base64"])
    assert pdf_bytes[:4] == b"%PDF"
    import io

    from pypdf import PdfReader
    rendered = "".join(
        p.extract_text() or "" for p in PdfReader(io.BytesIO(pdf_bytes)).pages
    )
    assert inv["invoice_number"] in rendered
    assert "Attach Customer" in rendered


def test_invoice_email_compose_returns_pdf_and_template(tenant_db_session):
    """2026-05-15 — composer payload mirrors estimates: to/subject/body_text +
    base64 PDF that the in-app dialog auto-attaches. Drives the new
    InvoiceDetailView Send button (composer-then-send instead of blind send)."""
    from gdx_dispatch.models.tenant_models import Customer
    from gdx_dispatch.routers.invoices import invoice_email_compose

    cust = Customer(
        name="Counter Customer", email="counter@example.com",
        phone="555-0100", company_id="tenant-test",
    )
    tenant_db_session.add(cust)
    tenant_db_session.commit()
    tenant_db_session.refresh(cust)

    job = Job(
        customer_id=cust.id, title="Spring repair",
        lifecycle_stage="estimate", dispatch_status="unassigned",
        billing_status="unbilled", company_id="tenant-test",
    )
    tenant_db_session.add(job)
    tenant_db_session.commit()
    tenant_db_session.refresh(job)

    # due_date passed explicitly so create_invoice doesn't fall through to
    # resolve_effective_terms (which needs the tenant_settings table that
    # the test fixture doesn't seed).
    inv = create_invoice(
        payload=InvoiceCreateIn(
            job_id=job.id, customer_id=cust.id,
            due_date=date.today() + timedelta(days=30),
            line_items=[{"description": "Spring", "quantity": 1, "unit_price": 75.00}],
        ),
        _=_current_user(), db=tenant_db_session,
    )

    _verify(tenant_db_session, inv)
    payload = invoice_email_compose(
        invoice_id=UUID(inv["id"]), _=_current_user(), db=tenant_db_session,
    )
    assert payload["to"] == ["counter@example.com"]
    assert payload["subject"]
    assert "Spring repair" in payload["body_text"] or inv["invoice_number"] in payload["body_text"]
    assert payload["pdf"]["content_type"] == "application/pdf"
    assert payload["pdf"]["size_bytes"] > 1000  # real PDFs of this fixture are ~3-10KB
    import base64 as _b64
    import io

    from pypdf import PdfReader
    pdf_bytes = _b64.b64decode(payload["pdf"]["content_base64"])
    assert pdf_bytes[:4] == b"%PDF"
    # Audit catch (2026-05-15): magic-bytes alone would pass on a blank PDF
    # skeleton. Decompress the PDF and assert the rendered text actually
    # carries the invoice number + customer name + line item, so a future
    # renderer regression that emits a content-free PDF fails here.
    reader = PdfReader(io.BytesIO(pdf_bytes))
    rendered = "".join(p.extract_text() or "" for p in reader.pages)
    assert inv["invoice_number"] in rendered
    assert "Counter Customer" in rendered
    assert "Spring" in rendered  # line-item description


def test_invoice_email_compose_on_paid_is_receipt_flavored(tenant_db_session):
    """2026-08-17 — composing on a PAID invoice drafts the receipt: thank-you
    subject/body with Paid + $0.00 Balance lines, no 'Pay online' ask, a
    '-paid' PDF name, and the PDF itself carries the PAID badge (extracted
    from the real render, not just the payload)."""
    from gdx_dispatch.models.tenant_models import Customer
    from gdx_dispatch.routers.invoices import invoice_email_compose

    cust = Customer(
        name="Paidup Customer", email="paidup@example.com",
        phone="555-0101", company_id="tenant-test",
    )
    tenant_db_session.add(cust)
    tenant_db_session.commit()
    tenant_db_session.refresh(cust)

    job = _seed_job(tenant_db_session)
    inv = create_invoice(
        payload=InvoiceCreateIn(
            job_id=job.id, customer_id=cust.id,
            due_date=date.today() + timedelta(days=30),
            line_items=[{"description": "Opener install", "quantity": 1, "unit_price": 450.00}],
        ),
        _=_current_user(), db=tenant_db_session,
    )
    _verify(tenant_db_session, inv)
    record_payment(
        invoice_id=UUID(inv["id"]),
        payload=PaymentCreateIn(amount=450.0, method="check"),
        _=_current_user(), db=tenant_db_session,
    )

    payload = invoice_email_compose(
        invoice_id=UUID(inv["id"]), _=_current_user(), db=tenant_db_session,
    )
    assert payload["subject"].startswith("Payment received")
    assert "Thank you for your payment" in payload["body_text"]
    assert "Paid: $450.00" in payload["body_text"]
    assert "Balance Due: $0.00" in payload["body_text"]
    assert "Pay online" not in payload["body_text"]
    assert payload["pdf"]["name"].endswith("-paid.pdf")

    import base64 as _b64
    import io

    from pypdf import PdfReader
    pdf_bytes = _b64.b64decode(payload["pdf"]["content_base64"])
    reader = PdfReader(io.BytesIO(pdf_bytes))
    rendered = "".join(p.extract_text() or "" for p in reader.pages)
    assert "PAID" in rendered
    assert "Paid to Date" in rendered


def test_invoice_email_compose_counter_sale_no_job(tenant_db_session):
    """Counter-sale invoices have no job; compose still returns a PDF and
    the customer's email — body just omits the job_title placeholder."""
    from gdx_dispatch.models.tenant_models import Customer
    from gdx_dispatch.routers.invoices import invoice_email_compose

    cust = Customer(
        name="Walk-In", email="walkin@example.com",
        phone="555-0200", company_id="tenant-test",
    )
    tenant_db_session.add(cust)
    tenant_db_session.commit()
    tenant_db_session.refresh(cust)

    inv = create_invoice(
        payload=InvoiceCreateIn(
            customer_id=cust.id,
            due_date=date.today() + timedelta(days=30),
            line_items=[{"description": "Spring (counter)", "quantity": 2, "unit_price": 35.00}],
        ),
        _=_current_user(), db=tenant_db_session,
    )

    _verify(tenant_db_session, inv)
    payload = invoice_email_compose(
        invoice_id=UUID(inv["id"]), _=_current_user(), db=tenant_db_session,
    )
    assert payload["to"] == ["walkin@example.com"]
    assert payload["pdf"]["size_bytes"] > 0
    # extra_attachments stays empty for invoices regardless of job/no-job
    # (S122 audit catch — Document has no invoice_id, so filtering by job_id
    # would leak every doc on the job to the customer).
    assert payload["extra_attachments"] == []


def test_mark_invoice_sent_flips_status_without_email(tenant_db_session):
    """mark-sent is the post-Outlook (or post-mailto) handoff — flips status
    and stamps sent_at, but DOES NOT trigger the server-side email path."""
    from gdx_dispatch.routers.invoices import mark_invoice_sent

    job = _seed_job(tenant_db_session)
    inv = create_invoice(
        payload=InvoiceCreateIn(job_id=job.id, customer_id=job.customer_id),
        _=_current_user(), db=tenant_db_session,
    )

    _verify(tenant_db_session, inv)
    out = mark_invoice_sent(
        invoice_id=UUID(inv["id"]), _=_current_user(), db=tenant_db_session,
    )
    assert out["status"] == "sent"
    assert out["sent_at"] is not None
    assert out["public_token"]
    # Critically: no email_sent / email_provider keys (those only appear on
    # the server-send path). mark-sent is the manual-channel acknowledgment.
    assert "email_sent" not in out
    # No body → channel defaults to 'manual' (pre-057 callers keep meaning
    # "operator says it went out, channel unknown").
    assert out["sent_via"] == "manual"


def test_mark_invoice_sent_mail_channel_records_paper_delivery(tenant_db_session):
    """The paper path: a draft invoice printed and posted. channel='mail'
    stamps sent_at (the delivery fact) + sent_via so the Billing Unsent tab
    stops counting it — previously the only honest exits were email or
    nothing."""
    from gdx_dispatch.routers.invoices import MarkSentIn, mark_invoice_sent

    job = _seed_job(tenant_db_session)
    inv = create_invoice(
        payload=InvoiceCreateIn(job_id=job.id, customer_id=job.customer_id),
        _=_current_user(), db=tenant_db_session,
    )

    _verify(tenant_db_session, inv)
    out = mark_invoice_sent(
        invoice_id=UUID(inv["id"]), payload=MarkSentIn(channel="mail"),
        _=_current_user(), db=tenant_db_session,
    )
    assert out["status"] == "sent"
    assert out["sent_at"] is not None
    assert out["sent_via"] == "mail"


def test_mark_invoice_sent_on_paid_stamps_delivery_without_status_regress(tenant_db_session):
    """2026-08-17 — "Send Receipt": the composer's post-send handoff lands on
    mark-sent for PAID invoices too. Paid is terminal — regressing to 'sent'
    would resurrect AR (and fire a GL S5 posting) — so the endpoint stamps
    the delivery fact (sent_at + sent_via) and leaves status alone."""
    from gdx_dispatch.routers.invoices import MarkSentIn, mark_invoice_sent

    job = _seed_job(tenant_db_session)
    inv = create_invoice(
        payload=InvoiceCreateIn(job_id=job.id, customer_id=job.customer_id),
        _=_current_user(), db=tenant_db_session,
    )
    invoice_obj = tenant_db_session.get(Invoice, UUID(inv["id"]))
    invoice_obj.status = "paid"
    tenant_db_session.commit()

    out = mark_invoice_sent(
        invoice_id=UUID(inv["id"]), payload=MarkSentIn(channel="email"),
        _=_current_user(), db=tenant_db_session,
    )
    assert out["status"] == "paid"
    assert out["sent_at"] is not None
    assert out["sent_via"] == "email"


def test_mark_invoice_sent_rejects_void(tenant_db_session):
    """Void stays locked — marking a voided invoice sent would resurrect a
    cancelled bill into the delivery-facing columns."""
    from gdx_dispatch.routers.invoices import mark_invoice_sent

    job = _seed_job(tenant_db_session)
    inv = create_invoice(
        payload=InvoiceCreateIn(job_id=job.id, customer_id=job.customer_id),
        _=_current_user(), db=tenant_db_session,
    )
    invoice_obj = tenant_db_session.get(Invoice, UUID(inv["id"]))
    invoice_obj.status = "void"
    tenant_db_session.commit()

    with pytest.raises(HTTPException) as exc:
        mark_invoice_sent(
            invoice_id=UUID(inv["id"]), _=_current_user(), db=tenant_db_session,
        )
    assert exc.value.status_code == 409


def test_add_line_item_updates_subtotal_for_unlocked_invoice(tenant_db_session):
    job = _seed_job(tenant_db_session)
    inv = create_invoice(payload=InvoiceCreateIn(job_id=job.id, customer_id=job.customer_id), _=_current_user(), db=tenant_db_session)

    add_invoice_line(
        invoice_id=UUID(inv["id"]),
        payload=InvoiceLineCreateIn(description="Labor", quantity=2, unit_price=50.0),
        current_user=_current_user(),
        db=tenant_db_session,
    )
    data = get_invoice(UUID(inv["id"]), _=_current_user(), db=tenant_db_session)

    assert len(data["lines"]) == 1
    assert data["subtotal"] == 100.0


def test_finalize_invoice_locks_and_calculates_total(tenant_db_session):
    job = _seed_job(tenant_db_session)
    inv = create_invoice(payload=InvoiceCreateIn(job_id=job.id, customer_id=job.customer_id, tax_amount=5.0), _=_current_user(), db=tenant_db_session)
    add_invoice_line(
        invoice_id=UUID(inv["id"]),
        payload=InvoiceLineCreateIn(description="Part", quantity=1, unit_price=80.0),
        current_user=_current_user(),
        db=tenant_db_session,
    )

    finalized = finalize_invoice(invoice_id=UUID(inv["id"]), _=_current_user(), db=tenant_db_session)

    assert finalized["locked"] is True
    assert finalized["subtotal"] == 80.0
    assert finalized["tax_amount"] == 5.0
    assert finalized["total"] == 85.0


def test_add_line_rejects_locked_invoice(tenant_db_session):
    job = _seed_job(tenant_db_session)
    inv = create_invoice(payload=InvoiceCreateIn(job_id=job.id, customer_id=job.customer_id), _=_current_user(), db=tenant_db_session)
    finalize_invoice(invoice_id=UUID(inv["id"]), _=_current_user(), db=tenant_db_session)

    with pytest.raises(HTTPException) as exc:
        add_invoice_line(
            invoice_id=UUID(inv["id"]),
            payload=InvoiceLineCreateIn(description="Late line", quantity=1, unit_price=10.0),
            current_user=_current_user(),
            db=tenant_db_session,
        )
    assert exc.value.status_code == 409


def test_record_payment_updates_status_paid_when_fully_paid(tenant_db_session):
    job = _seed_job(tenant_db_session)
    inv = create_invoice(payload=InvoiceCreateIn(job_id=job.id, customer_id=job.customer_id), _=_current_user(), db=tenant_db_session)
    add_invoice_line(
        invoice_id=UUID(inv["id"]),
        payload=InvoiceLineCreateIn(description="Service", quantity=1, unit_price=120.0),
        current_user=_current_user(),
        db=tenant_db_session,
    )
    finalize_invoice(invoice_id=UUID(inv["id"]), _=_current_user(), db=tenant_db_session)

    record_payment(
        invoice_id=UUID(inv["id"]),
        payload=PaymentCreateIn(amount=120.0, method="card", date=date.today()),
        _=_current_user(),
        db=tenant_db_session,
    )
    invoice = get_invoice(UUID(inv["id"]), _=_current_user(), db=tenant_db_session)

    assert invoice["status"] == "paid"
    assert invoice["balance_due"] == 0.0


def test_record_payment_validation_rejects_non_positive_amount():
    with pytest.raises(ValidationError):
        PaymentCreateIn(amount=0, method="cash", date=date.today())


def test_record_payment_date_defaults_to_today():
    # 2026-07-06: the /billing dialog posted without `date` and a real
    # check payment 422'd twice. An omitted date must mean "today", not
    # a validation error.
    payload = PaymentCreateIn(amount=448.32, method="check")
    assert payload.date == date.today()


def test_list_payments_for_invoice(tenant_db_session):
    job = _seed_job(tenant_db_session)
    inv = create_invoice(payload=InvoiceCreateIn(job_id=job.id, customer_id=job.customer_id), _=_current_user(), db=tenant_db_session)
    finalize_invoice(invoice_id=UUID(inv["id"]), _=_current_user(), db=tenant_db_session)

    record_payment(
        invoice_id=UUID(inv["id"]),
        payload=PaymentCreateIn(amount=10.0, method="cash", date=(date.today() - timedelta(days=1))),
        _=_current_user(),
        db=tenant_db_session,
    )
    record_payment(
        invoice_id=UUID(inv["id"]),
        payload=PaymentCreateIn(amount=15.0, method="card", date=date.today()),
        _=_current_user(),
        db=tenant_db_session,
    )

    items = list_payments(invoice_id=UUID(inv["id"]), _=_current_user(), db=tenant_db_session)
    assert len(items) == 2
    assert items[0]["amount"] == 10.0
    assert items[1]["amount"] == 15.0


# ─── S115 regression: GET /api/invoices/summary aggregator ──────────────
# (closes test gap from S111 / S113)
def _import_billing_summary():
    """Import here so the import-side schema check above doesn't drag the
    invoices.summary endpoint into test_routes_require_auth_dependency
    counting (it's a separate endpoint with its own permission gate)."""
    from gdx_dispatch.routers.invoices import billing_summary as _bs
    return _bs


def test_billing_summary_response_shape(tenant_db_session):
    """Empty tenant — every key present with correct types."""
    bs = _import_billing_summary()
    res = bs(request=_mock_request(), _=_current_user(), db=tenant_db_session)
    assert set(res.keys()) >= {
        "total_outstanding", "overdue", "paid_this_month",
        "ready_for_billing", "as_of",
    }
    assert isinstance(res["total_outstanding"], (int, float))
    assert isinstance(res["overdue"], (int, float))
    assert isinstance(res["paid_this_month"], (int, float))
    assert isinstance(res["ready_for_billing"], int)
    assert isinstance(res["as_of"], str)


def test_billing_summary_excludes_drafts_from_outstanding(tenant_db_session):
    """S111 fix: Total Outstanding is receivables only — drafts not yet sent
    are work-in-progress, not AR. A draft invoice with $1290 balance must
    NOT appear in the outstanding total."""
    bs = _import_billing_summary()
    job = _seed_job(tenant_db_session)
    # Draft invoice — should be EXCLUDED
    draft = create_invoice(
        payload=InvoiceCreateIn(job_id=job.id, customer_id=job.customer_id, due_date=date.today() + timedelta(days=30)),
        _=_current_user(), db=tenant_db_session,
    )
    add_invoice_line(
        invoice_id=UUID(draft["id"]),
        payload=InvoiceLineCreateIn(description="WIP", quantity=1, unit_price=1290.0),
        current_user=_current_user(), db=tenant_db_session,
    )
    # Sent invoice — should be INCLUDED. force=True: the draft above already
    # bills the job ($>0 draft), and the 2026-07-23 guard 409s a silent
    # second create.
    sent = create_invoice(
        payload=InvoiceCreateIn(job_id=job.id, customer_id=job.customer_id, due_date=date.today() + timedelta(days=30), force=True),
        _=_current_user(), db=tenant_db_session,
    )
    add_invoice_line(
        invoice_id=UUID(sent["id"]),
        payload=InvoiceLineCreateIn(description="Service", quantity=1, unit_price=500.0),
        current_user=_current_user(), db=tenant_db_session,
    )
    _verify(tenant_db_session, sent)
    send_invoice(invoice_id=UUID(sent["id"]), _=_current_user(), db=tenant_db_session)

    res = bs(request=_mock_request(), _=_current_user(), db=tenant_db_session)
    # Outstanding contains the sent ($500) but NOT the draft ($1290).
    assert res["total_outstanding"] == 500.0, (
        f"draft must be excluded — got {res['total_outstanding']}"
    )


def test_billing_summary_ready_for_billing_uses_lifecycle_stage(tenant_db_session):
    """S114: ready_for_billing must filter on Job.lifecycle_stage='completed'
    (not the legacy Job.status varchar). QB-imported jobs have NULL status
    but lifecycle_stage='completed' — they MUST count as ready-for-billing.
    The S114 alignment closed the 4-vs-8 discrepancy on GDX prod."""
    bs = _import_billing_summary()
    # Seed a "QB-imported style" job: no status set, lifecycle_stage=completed
    job_qb = Job(
        customer_id=uuid4(),
        title="QB import - ready",
        lifecycle_stage="completed",
        # status intentionally NULL — mimics QB import shape
        company_id="tenant-test",
    )
    tenant_db_session.add(job_qb)
    tenant_db_session.commit()
    res = bs(request=_mock_request(), _=_current_user(), db=tenant_db_session)
    assert res["ready_for_billing"] == 1, (
        f"QB-imported job (status=NULL, lifecycle_stage=completed) must count "
        f"as ready_for_billing — got {res['ready_for_billing']}"
    )


def test_billing_summary_ready_for_billing_counts_until_invoice_leaves_draft(tenant_db_session):
    """A completed job stays ready_for_billing until an invoice moves PAST
    draft (sent/paid) or the job is marked not billable.

    Closeout-autodraft semantic change (2026-08-07): this test previously
    asserted a priced DRAFT excluded the job. With closeout minting a priced
    draft automatically, that would have emptied the queue at the exact
    moment the draft needs review — so drafts (any total) now keep the job
    in the queue, whose rows carry the draft for a Review action. SENDING
    the invoice is what settles it. Full matrix in
    test_billing_predicates_pr2.py.
    """
    bs = _import_billing_summary()
    job = Job(
        customer_id=uuid4(),
        title="Already invoiced",
        lifecycle_stage="completed",
        company_id="tenant-test",
    )
    tenant_db_session.add(job)
    tenant_db_session.commit()
    created = create_invoice(
        payload=InvoiceCreateIn(
            job_id=job.id,
            customer_id=job.customer_id,
            due_date=date.today() + timedelta(days=30),
            line_items=[{"description": "Spring", "quantity": 1, "unit_price": 250.0}],
        ),
        _=_current_user(), db=tenant_db_session,
    )
    # A priced draft: billing STARTED, not finished — still in the queue.
    res = bs(request=_mock_request(), _=_current_user(), db=tenant_db_session)
    assert res["ready_for_billing"] == 1

    # Sending it settles the job.
    inv = tenant_db_session.get(Invoice, UUID(created["id"]))
    inv.status = "sent"
    tenant_db_session.commit()
    res = bs(request=_mock_request(), _=_current_user(), db=tenant_db_session)
    assert res["ready_for_billing"] == 0

    # And the $0-draft placeholder direction: void the real invoice, attach
    # a lineless $0 draft — the job must come BACK as ready for billing.
    inv.status = "void"
    tenant_db_session.commit()
    create_invoice(
        payload=InvoiceCreateIn(job_id=job.id, customer_id=job.customer_id, due_date=date.today() + timedelta(days=30)),
        _=_current_user(), db=tenant_db_session,
    )
    res = bs(request=_mock_request(), _=_current_user(), db=tenant_db_session)
    assert res["ready_for_billing"] == 1


def test_billing_summary_overdue_uses_due_date_and_balance(tenant_db_session):
    """Overdue = balance_due > 0 AND due_date < today AND not paid/draft/void."""
    bs = _import_billing_summary()
    job = _seed_job(tenant_db_session)
    overdue_inv = create_invoice(
        payload=InvoiceCreateIn(job_id=job.id, customer_id=job.customer_id, due_date=date.today() - timedelta(days=10)),
        _=_current_user(), db=tenant_db_session,
    )
    add_invoice_line(
        invoice_id=UUID(overdue_inv["id"]),
        payload=InvoiceLineCreateIn(description="Late", quantity=1, unit_price=750.0),
        current_user=_current_user(), db=tenant_db_session,
    )
    _verify(tenant_db_session, overdue_inv)
    send_invoice(invoice_id=UUID(overdue_inv["id"]), _=_current_user(), db=tenant_db_session)

    res = bs(request=_mock_request(), _=_current_user(), db=tenant_db_session)
    assert res["overdue"] == 750.0
    assert res["total_outstanding"] == 750.0


def test_list_invoices_filters_overdue(tenant_db_session):
    job = _seed_job(tenant_db_session)
    inv = create_invoice(
        payload=InvoiceCreateIn(job_id=job.id, customer_id=job.customer_id, due_date=(date.today() - timedelta(days=5))),
        _=_current_user(),
        db=tenant_db_session,
    )
    add_invoice_line(
        invoice_id=UUID(inv["id"]),
        payload=InvoiceLineCreateIn(description="Service", quantity=1, unit_price=100.0),
        current_user=_current_user(),
        db=tenant_db_session,
    )
    _verify(tenant_db_session, inv)
    send_invoice(invoice_id=UUID(inv["id"]), _=_current_user(), db=tenant_db_session)

    all_items = list_invoices(request=_mock_request(), status=None, customer_id=None, _=_current_user(), db=tenant_db_session)
    overdue = list_invoices(request=_mock_request(), status="overdue", customer_id=None, _=_current_user(), db=tenant_db_session)

    assert len(all_items) == 1
    assert len(overdue) == 1
    assert overdue[0]["effective_status"] == "overdue"


# ----------------------------------------------------------------------------
# S122 — from_part_ids: invoice POST marks JobPartNeeded.billed_invoice_id
# ----------------------------------------------------------------------------

def _seed_part(db, job_id: str, part_name: str = "Spring", status: str = "received") -> JobPartNeeded:
    from uuid import uuid4

    # Match production's id format (str(uuid4()) — dashed, 36 chars) so the
    # from_part_ids UUID parsing round-trips to the same string we stored.
    part = JobPartNeeded(
        id=str(uuid4()),
        company_id="tenant-test",
        job_id=str(job_id),
        part_name=part_name,
        quantity=1,
        status=status,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def test_create_invoice_with_from_part_ids_marks_parts_billed(tenant_db_session):
    """S122 — passing from_part_ids sets JobPartNeeded.billed_invoice_id."""
    job = _seed_job(tenant_db_session)
    part_a = _seed_part(tenant_db_session, job.id, "Torsion spring", "received")
    part_b = _seed_part(tenant_db_session, job.id, "Cable", "ordered")

    created = create_invoice(
        payload=InvoiceCreateIn(
            job_id=job.id,
            customer_id=job.customer_id,            line_items=[
                {"description": "Torsion spring", "quantity": 1, "unit_price": 80.0},
                {"description": "Cable", "quantity": 1, "unit_price": 25.0},
            ],
            from_part_ids=[part_a.id, part_b.id],
        ),
        _=_current_user(),
        db=tenant_db_session,
    )

    tenant_db_session.refresh(part_a)
    tenant_db_session.refresh(part_b)
    assert str(part_a.billed_invoice_id) == created["id"]
    assert str(part_b.billed_invoice_id) == created["id"]


def test_create_invoice_from_part_ids_rejects_already_billed(tenant_db_session):
    """S122 → PR3-billing-capture semantic change: re-billing a part used to
    be SILENTLY skipped — but the payload lines still carried the amount, so
    the customer quietly double-paid while the stamp no-opped. Now the whole
    second create 409s (stamp-first UPDATE…RETURNING gates the request) and
    the part stays on the first invoice."""
    from sqlalchemy import func as _func
    from sqlalchemy import select

    job = _seed_job(tenant_db_session)
    part = _seed_part(tenant_db_session, job.id, "Roller", "received")

    first = create_invoice(
        payload=InvoiceCreateIn(
            job_id=job.id,
            customer_id=job.customer_id,            line_items=[{"description": "Roller", "quantity": 1, "unit_price": 10.0}],
            from_part_ids=[part.id],
        ),
        _=_current_user(),
        db=tenant_db_session,
    )
    tenant_db_session.refresh(part)
    assert str(part.billed_invoice_id) == first["id"]

    count_before = tenant_db_session.scalar(select(_func.count(Invoice.id)))
    with pytest.raises(HTTPException) as exc_info:
        create_invoice(
            payload=InvoiceCreateIn(
                job_id=job.id,
                customer_id=job.customer_id,                line_items=[{"description": "Roller again", "quantity": 1, "unit_price": 10.0}],
                from_part_ids=[part.id],
            ),
            _=_current_user(),
            db=tenant_db_session,
        )
    assert exc_info.value.status_code == 409
    # No second invoice created; part still points at the first.
    assert tenant_db_session.scalar(select(_func.count(Invoice.id))) == count_before
    tenant_db_session.refresh(part)
    assert str(part.billed_invoice_id) == first["id"]


def test_create_invoice_from_part_ids_rejects_other_jobs_parts(tenant_db_session):
    """S122 → PR3-billing-capture semantic change: a wrong-job part used to
    be silently ignored (invoice created anyway). A caller referencing a part
    that isn't billable on THIS job is a bug or a race — now the request 409s
    and nothing is written; the part is untouched."""
    from sqlalchemy import func as _func
    from sqlalchemy import select

    job_a = _seed_job(tenant_db_session, title="Job A")
    job_b = _seed_job(tenant_db_session, title="Job B")
    part_other = _seed_part(tenant_db_session, job_b.id, "Wrong-job part", "received")

    count_before = tenant_db_session.scalar(select(_func.count(Invoice.id)))
    with pytest.raises(HTTPException) as exc_info:
        create_invoice(
            payload=InvoiceCreateIn(
                job_id=job_a.id,
                customer_id=job_a.customer_id,                line_items=[{"description": "Service", "quantity": 1, "unit_price": 50.0}],
                from_part_ids=[part_other.id],
            ),
            _=_current_user(),
            db=tenant_db_session,
        )
    assert exc_info.value.status_code == 409
    assert tenant_db_session.scalar(select(_func.count(Invoice.id))) == count_before
    tenant_db_session.refresh(part_other)
    assert part_other.billed_invoice_id is None


def test_create_invoice_from_estimate_forwards_margin_pct_snapshot(tenant_db_session):
    """S122-b auditor catch: the engine-resolved tier margin
    (margin_pct_snapshot) must survive the estimate→invoice copy, or the
    invoice loses the original tier-decision provenance and gross-margin
    reporting on invoices drifts from the source estimate."""
    from decimal import Decimal as D

    job = _seed_job(tenant_db_session)
    est = _seed_estimate(tenant_db_session, job.id, D("100.00"))

    # Seed an estimate line with cost + tier-resolved margin snapshot.
    line = EstimateLine(
        estimate_id=est.id,
        description="Spring",
        category="Springs",
        quantity=1,
        unit_price=D("100.00"),
        line_total=D("100.00"),
        sort_order=1,
        cost_snapshot=D("55.00"),
        margin_pct_snapshot=D("0.45"),  # engine resolved 45% from tier
        company_id="tenant-test",
    )
    tenant_db_session.add(line)
    tenant_db_session.commit()

    created = create_invoice(
        payload=InvoiceCreateIn(job_id=job.id, customer_id=job.customer_id, estimate_id=est.id),
        _=_current_user(), db=tenant_db_session,
    )
    full = get_invoice(UUID(created["id"]), _=_current_user(), db=tenant_db_session)
    inv_line = full["lines"][0]
    assert inv_line["category"] == "Springs"
    assert inv_line["cost_snapshot"] == 55.0
    assert inv_line["margin_pct_snapshot"] == 0.45  # the audit catch


def test_create_invoice_line_items_persist_category_cost_margin(tenant_db_session):
    """S122-b — invoice/estimate parity. POST sets category/cost/margin
    on the new line, GET returns them in the serialized response."""
    job = _seed_job(tenant_db_session)
    payload = InvoiceCreateIn(
        job_id=job.id,
        customer_id=job.customer_id,        line_items=[
            {
                "description": "Torsion spring",
                "quantity": 1,
                "unit_price": 80.0,
                "category": "Springs",
                "cost": 35.0,
                "margin_pct_override": 0.45,
            },
            {
                "description": "Service call",
                "quantity": 1,
                "unit_price": 75.0,
                # no category/cost/margin — legal, fields stay NULL
            },
        ],
    )
    created = create_invoice(payload=payload, _=_current_user(), db=tenant_db_session)
    full = get_invoice(UUID(created["id"]), _=_current_user(), db=tenant_db_session)

    lines = sorted(full["lines"], key=lambda l: l["sort_order"])
    assert lines[0]["category"] == "Springs"
    assert lines[0]["cost_snapshot"] == 35.0
    assert lines[0]["margin_pct_override"] == 0.45
    # Second line shouldn't have any of those.
    assert lines[1]["category"] is None
    assert lines[1]["cost_snapshot"] is None
    assert lines[1]["margin_pct_override"] is None


def test_add_invoice_line_persists_estimate_parity_fields(tenant_db_session):
    """S122-b — POST /api/invoices/:id/lines also writes category/cost/margin."""
    from gdx_dispatch.routers.invoices import add_invoice_line

    job = _seed_job(tenant_db_session)
    created = create_invoice(
        payload=InvoiceCreateIn(job_id=job.id, customer_id=job.customer_id),
        _=_current_user(), db=tenant_db_session,
    )

    line = add_invoice_line(
        invoice_id=UUID(created["id"]),
        payload=InvoiceLineCreateIn(
            description="Labor — service call",
            quantity=1,
            unit_price=85.0,
            taxable=False,
            category="Labor",
            cost=0,
            margin_pct_override=0.0,
        ),
        current_user=_current_user(), db=tenant_db_session,
    )
    assert line["category"] == "Labor"
    assert line["taxable"] is False


def test_patch_invoice_line_clears_cost_when_set_to_null(tenant_db_session):
    """Auditor round-2 catch: blanking a cost in InvoiceDetailView's edit
    table sends `cost: null`, and the backend must clear it (not ignore).
    Pin: PATCH with cost=None nulls the column."""
    from gdx_dispatch.routers.invoices import InvoiceLinePatchIn, add_invoice_line, patch_invoice_line

    job = _seed_job(tenant_db_session)
    inv = create_invoice(payload=InvoiceCreateIn(job_id=job.id, customer_id=job.customer_id), _=_current_user(), db=tenant_db_session)
    line = add_invoice_line(
        invoice_id=UUID(inv["id"]),
        payload=InvoiceLineCreateIn(
            description="Spring", quantity=1, unit_price=80.0, cost=35.0, margin_pct_override=0.40,
        ),
        current_user=_current_user(), db=tenant_db_session,
    )
    assert line["cost_snapshot"] == 35.0

    # Operator blanks cost + margin in the edit table — frontend sends nulls.
    patched = patch_invoice_line(
        invoice_id=UUID(inv["id"]),
        line_id=UUID(line["id"]),
        payload=InvoiceLinePatchIn(cost=None, margin_pct_override=None),
        user=_current_user(), db=tenant_db_session,
    )
    assert patched["cost_snapshot"] is None
    assert patched["margin_pct_override"] is None


def test_delete_invoice_line_releases_part_via_line_part_id(tenant_db_session):
    """D-S122-line-removal-unbill — when a line that came from a part is
    deleted, the part's billed_invoice_id is released back to NULL so the
    parts-from-job checklist re-offers it on the next invoice."""
    from gdx_dispatch.routers.invoices import delete_invoice_line

    job = _seed_job(tenant_db_session)
    part = _seed_part(tenant_db_session, job.id, "Bracket", "received")

    created = create_invoice(
        payload=InvoiceCreateIn(
            job_id=job.id,
            customer_id=job.customer_id,            line_items=[{
                "description": "Bracket",
                "quantity": 1,
                "unit_price": 12.0,
                "part_id": part.id,  # line-level part linkage
            }],
        ),
        _=_current_user(), db=tenant_db_session,
    )
    # Part should be billed against this invoice.
    tenant_db_session.refresh(part)
    assert str(part.billed_invoice_id) == created["id"]

    # Find the line ID we just created.
    full = get_invoice(UUID(created["id"]), _=_current_user(), db=tenant_db_session)
    line_id = UUID(full["lines"][0]["id"])
    assert full["lines"][0]["part_id"] == part.id  # round-trip exposes part_id

    # Now delete that line.
    delete_invoice_line(
        invoice_id=UUID(created["id"]),
        line_id=line_id,
        user=_current_user(),
        db=tenant_db_session,
    )

    # Part should be released back to unbilled.
    tenant_db_session.refresh(part)
    assert part.billed_invoice_id is None


def test_create_invoice_without_from_part_ids_leaves_parts_untouched(tenant_db_session):
    """S122 — back-compat: callers that don't pass from_part_ids change nothing."""
    job = _seed_job(tenant_db_session)
    part = _seed_part(tenant_db_session, job.id, "Hinge", "received")

    create_invoice(
        payload=InvoiceCreateIn(
            job_id=job.id,
            customer_id=job.customer_id,            line_items=[{"description": "Hinge", "quantity": 1, "unit_price": 4.0}],
        ),
        _=_current_user(),
        db=tenant_db_session,
    )
    tenant_db_session.refresh(part)
    assert part.billed_invoice_id is None


def test_create_invoice_rejects_estimate_id_with_from_part_ids(tenant_db_session):
    """S122 auditor: estimate_id + from_part_ids together is a 422 — the
    estimate-line copy path ignores inline line_items, but the from_part_ids
    UPDATE would still mark parts billed against an invoice that contains
    none of them. Reject at the contract layer."""
    with pytest.raises(ValidationError):
        InvoiceCreateIn(
            job_id=uuid4(),
            estimate_id=uuid4(),
            from_part_ids=[uuid4()],
        )


def test_soft_delete_invoice_releases_billed_parts(tenant_db_session):
    """S122 auditor: deleting a draft invoice must un-bill any parts pulled
    into it; otherwise the part stays "billed" forever and never re-appears
    in the unbilled pool, stranding it."""
    from gdx_dispatch.routers.invoices import delete_invoice

    job = _seed_job(tenant_db_session)
    part = _seed_part(tenant_db_session, job.id, "Spring", "received")

    created = create_invoice(
        payload=InvoiceCreateIn(
            job_id=job.id,
            customer_id=job.customer_id,            line_items=[{"description": "Spring", "quantity": 1, "unit_price": 50.0}],
            from_part_ids=[part.id],
        ),
        _=_current_user(),
        db=tenant_db_session,
    )
    tenant_db_session.refresh(part)
    assert str(part.billed_invoice_id) == created["id"]

    # Soft-delete the invoice — part should release back to unbilled.
    delete_invoice(
        invoice_id=UUID(created["id"]),
        current_user=_current_user(),
        db=tenant_db_session,
    )
    tenant_db_session.refresh(part)
    assert part.billed_invoice_id is None


def test_void_invoice_releases_billed_parts(tenant_db_session):
    """A void is dead money — `billing_predicates` excludes voided invoices
    from "is this job billed". But the parts it claimed stayed stamped with
    its id, so they read "billed" while no LIVE invoice bills them: gone from
    `parts-needed?unbilled=true`, absent from Ready-for-Billing, unbillable
    forever. Work that was going to be charged silently stops being charged.

    The same reasoning was already applied to the soft-delete path (see
    `test_soft_delete_invoice_releases_billed_parts`) and to
    `void_untouched_autodraft`. Three void-ish paths, and this was the one
    that did not release.
    """
    from gdx_dispatch.routers.invoices import void_invoice

    job = _seed_job(tenant_db_session)
    part = _seed_part(tenant_db_session, job.id, "Torsion spring", "received")

    created = create_invoice(
        payload=InvoiceCreateIn(
            job_id=job.id,
            customer_id=job.customer_id,
            line_items=[{"description": "Torsion spring", "quantity": 1, "unit_price": 120.0}],
            from_part_ids=[part.id],
        ),
        _=_current_user(),
        db=tenant_db_session,
    )
    tenant_db_session.refresh(part)
    assert str(part.billed_invoice_id) == created["id"], "setup: the part must be claimed"

    void_invoice(
        invoice_id=UUID(created["id"]),
        _=_current_user(),
        db=tenant_db_session,
    )
    tenant_db_session.refresh(part)
    assert part.billed_invoice_id is None, "voiding stranded the part"


def test_void_invoice_records_what_it_released(tenant_db_session):
    """Voiding puts work back on the unbilled checklist. "What changed" has to
    include that, not just the invoice total — otherwise the trail cannot
    explain why a part reappeared."""
    from gdx_dispatch.core.audit import AuditLog
    from gdx_dispatch.routers.invoices import void_invoice

    job = _seed_job(tenant_db_session)
    part = _seed_part(tenant_db_session, job.id, "Cable", "received")
    created = create_invoice(
        payload=InvoiceCreateIn(
            job_id=job.id,
            customer_id=job.customer_id,
            line_items=[{"description": "Cable", "quantity": 1, "unit_price": 30.0}],
            from_part_ids=[part.id],
        ),
        _=_current_user(),
        db=tenant_db_session,
    )
    void_invoice(
        invoice_id=UUID(created["id"]),
        _=_current_user(),
        db=tenant_db_session,
    )
    row = (
        tenant_db_session.query(AuditLog)
        .filter(AuditLog.action == "invoice_voided")
        .order_by(AuditLog.created_at.desc())
        .first()
    )
    assert row is not None
    assert row.details["released_parts"] == 1
    assert "released_change_orders" in row.details


def test_voiding_one_invoice_leaves_another_invoices_claims_alone(tenant_db_session):
    """The scoping counterfactual, and the one that would hurt if it broke.

    An earlier version of this test asserted that an unclaimed part is still
    unclaimed after the void — which is true whether or not the release exists,
    so it proved nothing (adversarial review 2026-08-23 called it out). What
    actually needs pinning is the `billed_invoice_id == invoice.id` scope: a
    part claimed by a DIFFERENT, live invoice must survive this void. Widen
    that predicate by accident and voiding any invoice would un-bill work that
    another invoice is still charging for — the office would re-add the line
    and the customer would be billed twice.
    """
    from gdx_dispatch.core.audit import AuditLog
    from gdx_dispatch.routers.invoices import void_invoice

    job = _seed_job(tenant_db_session)
    # Claimed by the invoice that SURVIVES.
    keeper_part = _seed_part(tenant_db_session, job.id, "Roller", "received")
    keeper = create_invoice(
        payload=InvoiceCreateIn(
            job_id=job.id,
            customer_id=job.customer_id,
            line_items=[{"description": "Roller", "quantity": 1, "unit_price": 40.0}],
            from_part_ids=[keeper_part.id],
        ),
        _=_current_user(),
        db=tenant_db_session,
    )
    tenant_db_session.refresh(keeper_part)
    assert str(keeper_part.billed_invoice_id) == keeper["id"], "setup"

    # A second, claim-free invoice on the same job, which is the one voided.
    # `force` clears the double-billing guard — deliberate: both invoices must
    # live on ONE job, or the surviving claim would be scoped out by job id
    # rather than by the invoice id this test exists to pin.
    doomed = create_invoice(
        payload=InvoiceCreateIn(
            job_id=job.id,
            customer_id=job.customer_id,
            line_items=[{"description": "Labor", "quantity": 1, "unit_price": 95.0}],
            force=True,
        ),
        _=_current_user(),
        db=tenant_db_session,
    )
    void_invoice(
        invoice_id=UUID(doomed["id"]),
        _=_current_user(),
        db=tenant_db_session,
    )
    tenant_db_session.refresh(keeper_part)
    assert str(keeper_part.billed_invoice_id) == keeper["id"], (
        "voiding one invoice released a part another invoice is still billing"
    )
    row = (
        tenant_db_session.query(AuditLog)
        .filter(AuditLog.action == "invoice_voided")
        .order_by(AuditLog.created_at.desc())
        .first()
    )
    # Reports zero rather than omitting the key — a missing count reads as
    # "nothing was checked", not "nothing was released".
    assert row.details["released_parts"] == 0
    assert row.details["released_change_orders"] == 0


def test_void_invoice_releases_billed_change_orders(tenant_db_session):
    """The change-order arm, exercised for real.

    `test_void_invoice_records_what_it_released` only asserted the key exists
    in the audit details — which would keep passing if the change-order UPDATE
    matched nothing forever (adversarial review 2026-08-23). An approved CO
    claimed onto a voided invoice is the same money as a stranded part: it
    drops off `change-orders?unbilled=true` while no live invoice charges it.
    """
    from gdx_dispatch.core.audit import AuditLog
    from gdx_dispatch.routers.change_orders import (
        ChangeOrder,
        ChangeOrderIn,
        create_change_order,
    )
    from gdx_dispatch.routers.invoices import void_invoice

    job = _seed_job(tenant_db_session)
    out = create_change_order(
        payload=ChangeOrderIn(
            job_id=str(job.id),
            customer_id=str(job.customer_id),
            title="Extra opener",
            status="approved",
            line_items=[{"description": "Opener unit", "quantity": 1, "unit_price": 300.0}],
        ),
        user=_current_user(),
        db=tenant_db_session,
    )
    co = tenant_db_session.get(ChangeOrder, UUID(out["id"]))

    created = create_invoice(
        payload=InvoiceCreateIn(
            job_id=job.id,
            customer_id=job.customer_id,
            from_change_order_ids=[co.id],
        ),
        _=_current_user(),
        db=tenant_db_session,
    )
    tenant_db_session.refresh(co)
    assert str(co.billed_invoice_id) == created["id"], "setup: the CO must be claimed"

    void_invoice(
        invoice_id=UUID(created["id"]),
        _=_current_user(),
        db=tenant_db_session,
    )
    tenant_db_session.refresh(co)
    assert co.billed_invoice_id is None, "voiding stranded the change order"
    row = (
        tenant_db_session.query(AuditLog)
        .filter(AuditLog.action == "invoice_voided")
        .order_by(AuditLog.created_at.desc())
        .first()
    )
    assert row.details["released_change_orders"] == 1


# ── Tenant-editable invoice / receipt email templates (issue #351) ──────────
#
# _prepare_invoice_email is the one render behind compose, preview, one-click
# and bulk send, and send-receipt. These pin the resolution order: tenant
# template when non-blank, else the platform default — and that the paid
# flavor reads the RECEIPT pair, never the invoice pair.

_TENANT_INVOICE_SUBJECT = "Bill {{invoice_number}} from {{company_name}} — {{job_title}}"
_TENANT_INVOICE_BODY = "Hey {{customer_name}}, {{invoice_number}} is {{total}} (bal {{balance_due}}).{{due_line}}"
_TENANT_RECEIPT_SUBJECT = "Thanks — Invoice {{invoice_number}} from {{company_name}}"
_TENANT_RECEIPT_BODY = "Cheers {{customer_name}}, {{invoice_number}} settled.{{paid_line}}{{balance_line}}"


def _stub_features(monkeypatch, **templates):
    from gdx_dispatch.modules import estimates_features as ef
    features = ef.EstimatesFeatures(**templates)
    monkeypatch.setattr(ef, "get_features", lambda tid: features)
    return features


def _seed_unpaid_invoice(db, *, due: bool = True) -> Invoice:
    from gdx_dispatch.models.tenant_models import Customer

    cust = Customer(
        name="Template Customer", email="tpl@example.com",
        phone="555-0199", company_id="tenant-test",
    )
    db.add(cust)
    db.commit()
    db.refresh(cust)
    job = _seed_job(db, title="Spring swap")
    inv = create_invoice(
        payload=InvoiceCreateIn(
            job_id=job.id, customer_id=cust.id,
            due_date=(date.today() + timedelta(days=14)) if due else None,
            line_items=[{"description": "Torsion spring", "quantity": 1, "unit_price": 320.00}],
        ),
        _=_current_user(), db=db,
    )
    _verify(db, inv)
    return db.get(Invoice, UUID(inv["id"]))


def test_prepare_invoice_email_uses_tenant_invoice_template(tenant_db_session, monkeypatch):
    from gdx_dispatch.routers.invoices import _prepare_invoice_email

    _stub_features(
        monkeypatch,
        invoice_email_subject_template=_TENANT_INVOICE_SUBJECT,
        invoice_email_body_template=_TENANT_INVOICE_BODY,
        receipt_email_subject_template=_TENANT_RECEIPT_SUBJECT,
        receipt_email_body_template=_TENANT_RECEIPT_BODY,
    )
    inv = _seed_unpaid_invoice(tenant_db_session)
    prep = _prepare_invoice_email(tenant_db_session, inv, mint_token=False)

    assert prep["is_paid"] is False
    assert prep["subject"].startswith(f"Bill {inv.invoice_number} from ")
    assert prep["subject"].endswith(" — Spring swap")
    assert prep["body_text"].startswith(
        f"Hey Template Customer, {inv.invoice_number} is $320.00 (bal $320.00)."
    )
    assert "\nDue: " in prep["body_text"], "{{due_line}} must render on an invoice with a due date"
    # The unpaid path must not reach for the receipt pair.
    assert "Cheers" not in prep["body_text"]
    assert not prep["subject"].startswith("Thanks")
    # Placeholders never ship raw.
    assert "{{" not in prep["subject"] and "{{" not in prep["body_text"]


def test_prepare_invoice_email_on_paid_uses_tenant_receipt_template(tenant_db_session, monkeypatch):
    from gdx_dispatch.routers.invoices import _prepare_invoice_email

    _stub_features(
        monkeypatch,
        invoice_email_subject_template=_TENANT_INVOICE_SUBJECT,
        invoice_email_body_template=_TENANT_INVOICE_BODY,
        receipt_email_subject_template=_TENANT_RECEIPT_SUBJECT,
        receipt_email_body_template=_TENANT_RECEIPT_BODY,
    )
    inv = _seed_unpaid_invoice(tenant_db_session)
    record_payment(
        invoice_id=inv.id,
        payload=PaymentCreateIn(amount=320.0, method="check"),
        _=_current_user(), db=tenant_db_session,
    )
    tenant_db_session.refresh(inv)
    assert inv.status == "paid"

    prep = _prepare_invoice_email(tenant_db_session, inv, mint_token=False)
    assert prep["is_paid"] is True
    assert prep["subject"].startswith(f"Thanks — Invoice {inv.invoice_number} from ")
    assert prep["body_text"].startswith(f"Cheers Template Customer, {inv.invoice_number} settled.")
    assert "\nPaid: $320.00" in prep["body_text"]
    assert "\nBalance Due: $0.00" in prep["body_text"]
    # The paid path must not reach for the invoice pair.
    assert "Hey Template Customer" not in prep["body_text"]
    assert not prep["subject"].startswith("Bill ")


def test_prepare_invoice_email_blank_tenant_templates_fall_back_to_defaults(tenant_db_session, monkeypatch):
    """Blank AND whitespace-only both mean "platform default" — a stray space
    in Settings must not blank the customer's email."""
    from gdx_dispatch.routers.invoices import (
        _DEFAULT_INVOICE_SUBJECT_TEMPLATE,
        _DEFAULT_RECEIPT_SUBJECT_TEMPLATE,
        _prepare_invoice_email,
    )

    _stub_features(
        monkeypatch,
        invoice_email_subject_template="   ",
        invoice_email_body_template="",
        receipt_email_subject_template="\n",
        receipt_email_body_template=" ",
    )
    inv = _seed_unpaid_invoice(tenant_db_session)
    prep = _prepare_invoice_email(tenant_db_session, inv, mint_token=False)
    assert prep["subject"].startswith(f"Invoice {inv.invoice_number} from ")
    assert "Please see the attached invoice" in prep["body_text"]
    assert "Total: $320.00" in prep["body_text"]
    # The defaults keep the bounce rung-1 shape ("Invoice <serial> from ").
    assert "Invoice {{invoice_number}} from" in _DEFAULT_INVOICE_SUBJECT_TEMPLATE
    assert "Invoice {{invoice_number}} from" in _DEFAULT_RECEIPT_SUBJECT_TEMPLATE

    record_payment(
        invoice_id=inv.id,
        payload=PaymentCreateIn(amount=320.0, method="check"),
        _=_current_user(), db=tenant_db_session,
    )
    tenant_db_session.refresh(inv)
    prep = _prepare_invoice_email(tenant_db_session, inv, mint_token=False)
    assert prep["subject"].startswith("Payment received — Invoice ")
    assert "Thank you for your payment" in prep["body_text"]


def test_prepare_invoice_email_survives_a_features_read_failure(tenant_db_session, monkeypatch):
    """A settings hiccup must never block an invoice from going out."""
    from gdx_dispatch.modules import estimates_features as ef
    from gdx_dispatch.routers.invoices import _prepare_invoice_email

    def boom(tid):
        raise RuntimeError("control plane unavailable")

    monkeypatch.setattr(ef, "get_features", boom)
    inv = _seed_unpaid_invoice(tenant_db_session)
    prep = _prepare_invoice_email(tenant_db_session, inv, mint_token=False)
    assert prep["subject"].startswith(f"Invoice {inv.invoice_number} from ")
    assert "Please see the attached invoice" in prep["body_text"]


def test_composer_override_still_beats_the_tenant_template(tenant_db_session, monkeypatch):
    """Per-send edits in the composer win over the tenant default — the
    tenant template is the starting copy, not a lock."""
    from gdx_dispatch.routers.invoices import _prepare_invoice_email

    _stub_features(
        monkeypatch,
        invoice_email_subject_template=_TENANT_INVOICE_SUBJECT,
        invoice_email_body_template=_TENANT_INVOICE_BODY,
    )
    inv = _seed_unpaid_invoice(tenant_db_session)
    prep = _prepare_invoice_email(
        tenant_db_session, inv, mint_token=False,
        subject_override="Operator subject", body_text_override="Operator body.",
    )
    assert prep["subject"] == "Operator subject"
    assert prep["body_text"].startswith("Operator body.")


def test_send_invoice_on_paid_honors_tenant_receipt_template(tenant_db_session, monkeypatch):
    """Compose == send (the locked estimate decision, applied to invoices):
    the one-click send of a paid invoice delivers the tenant's receipt copy,
    not the platform default. Goes through send_invoice end to end with the
    transport captured."""
    inv, captured = _seed_paid_invoice_with_customer(
        tenant_db_session, "receipt-tpl@example.com", monkeypatch
    )
    _stub_features(
        monkeypatch,
        receipt_email_subject_template=_TENANT_RECEIPT_SUBJECT,
        receipt_email_body_template=_TENANT_RECEIPT_BODY,
    )
    sent = send_invoice(invoice_id=UUID(inv["id"]), _=_current_user(), db=tenant_db_session)
    assert sent["email_sent"] is True
    assert captured["subject"].startswith("Thanks — Invoice ")
    assert "Cheers Receipt Customer" in captured["html_body"].replace("&nbsp;", " ")
    assert "Thank you for your payment" not in captured["html_body"]

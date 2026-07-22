"""The four office billing paths that silently wasted time (2026-07-21 audit).

1. POST /api/invoices/{id}/pay-link — the office "copy pay link" flow that
   replaced the dead "Pay $X" button (its /api/payments/intent target was a
   stub that never returned a checkout URL).
2. POST /api/payments (ui_compat) — was a silent no-op returning 201; now
   delegates to the canonical record-payment logic.
3. Bulk/mobile "Mark Paid" — now records real payments; these tests pin the
   endpoint they rely on (full-balance payment auto-flips status to paid).
4. send_invoice email — carries a "View & Pay" link only when the link would
   actually work (public base URL + Stripe keys), never a dead link.
"""
from __future__ import annotations

from datetime import date, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.payments import public_pay_url, stripe_configured
from gdx_dispatch.models.tenant_models import Invoice, InvoiceLine, Job, JobPartNeeded, Payment
from gdx_dispatch.modules.proposals.models import Estimate, EstimateLine
from gdx_dispatch.routers.invoices import (
    InvoiceCreateIn,
    InvoiceLineCreateIn,
    add_invoice_line,
    create_invoice,
    get_invoice_pay_link,
    send_invoice,
)
from gdx_dispatch.routers.ui_compat import _PaymentCompatIn, create_payment


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


def _current_user() -> dict[str, str]:
    return {"user_id": "user-1", "tenant_id": "tenant-1", "role": "admin"}


def _seed_job(db) -> Job:
    job = Job(
        customer_id=uuid4(),
        title="Garage repair",
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


def _seed_invoice(db, total: float = 250.0) -> dict:
    job = _seed_job(db)
    inv = create_invoice(
        payload=InvoiceCreateIn(
            job_id=job.id,
            customer_id=job.customer_id,
            due_date=date.today() + timedelta(days=30),
        ),
        _=_current_user(),
        db=db,
    )
    add_invoice_line(
        invoice_id=UUID(inv["id"]),
        payload=InvoiceLineCreateIn(description="Spring replacement", quantity=1, unit_price=total),
        _=_current_user(),
        db=db,
    )
    return inv


# `_seed_invoice` returns the create payload; fetch fresh state via the ORM
def _fresh(db, inv_id: str) -> Invoice:
    return db.get(Invoice, UUID(inv_id))


# ── public_pay_url / stripe_configured ─────────────────────────────────────

def test_public_pay_url_requires_all_three_pieces(monkeypatch):
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    monkeypatch.delenv("GDX_PUBLIC_BASE_URL", raising=False)
    assert stripe_configured() is False
    assert public_pay_url("tok") is None

    monkeypatch.setenv("GDX_PUBLIC_BASE_URL", "https://gdx.example.com")
    assert public_pay_url("tok") is None  # still no Stripe key

    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    assert public_pay_url("tok") == "https://gdx.example.com/pay/tok"
    assert public_pay_url(None) is None
    assert public_pay_url("") is None


# ── POST /api/invoices/{id}/pay-link ───────────────────────────────────────

def test_pay_link_mints_token_and_reports_config(tenant_db_session, monkeypatch):
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    monkeypatch.setenv("GDX_PUBLIC_BASE_URL", "https://gdx.example.com")
    inv = _seed_invoice(tenant_db_session)

    res = get_invoice_pay_link(invoice_id=UUID(inv["id"]), _=_current_user(), db=tenant_db_session)
    assert res["stripe_configured"] is False
    assert res["url"] is None  # never hand out a link that can't charge

    fresh = _fresh(tenant_db_session, inv["id"])
    assert fresh.public_token  # minted even while unconfigured

    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    res2 = get_invoice_pay_link(invoice_id=UUID(inv["id"]), _=_current_user(), db=tenant_db_session)
    assert res2["stripe_configured"] is True
    assert res2["url"] == f"https://gdx.example.com/pay/{fresh.public_token}"

    # Idempotent: a second call must not rotate the token.
    res3 = get_invoice_pay_link(invoice_id=UUID(inv["id"]), _=_current_user(), db=tenant_db_session)
    assert res3["url"] == res2["url"]


def test_pay_link_409s_when_nothing_to_pay(tenant_db_session):
    inv = _seed_invoice(tenant_db_session)
    create_payment(
        payload=_PaymentCompatIn(invoice_id=inv["id"], amount=250.0, method="check"),
        user=_current_user(),
        db=tenant_db_session,
    )
    with pytest.raises(HTTPException) as exc:
        get_invoice_pay_link(invoice_id=UUID(inv["id"]), _=_current_user(), db=tenant_db_session)
    assert exc.value.status_code == 409


# ── POST /api/payments (ui_compat, was a silent no-op) ────────────────────

def test_compat_create_payment_actually_persists(tenant_db_session):
    inv = _seed_invoice(tenant_db_session)

    res = create_payment(
        payload=_PaymentCompatIn(
            invoice_id=inv["id"], amount=100.0, method="check",
            date=date.today().isoformat(), processor_ref="chk 1042",
        ),
        user=_current_user(),
        db=tenant_db_session,
    )
    assert res["amount"] == 100.0

    rows = tenant_db_session.query(Payment).all()
    assert len(rows) == 1
    assert float(rows[0].amount) == 100.0
    assert rows[0].reference == "chk 1042"
    fresh = _fresh(tenant_db_session, inv["id"])
    assert float(fresh.balance_due) == 150.0


def test_compat_create_payment_resolves_invoice_number(tenant_db_session):
    inv = _seed_invoice(tenant_db_session)
    number = _fresh(tenant_db_session, inv["id"]).invoice_number
    assert number

    create_payment(
        payload=_PaymentCompatIn(invoice_id=number, amount=250.0, method="cash"),
        user=_current_user(),
        db=tenant_db_session,
    )
    fresh = _fresh(tenant_db_session, inv["id"])
    assert float(fresh.balance_due) == 0.0
    # Full-balance payment auto-flips status — the semantics bulk/mobile
    # "Mark Paid" now rely on.
    assert fresh.status == "paid"


def test_compat_create_payment_404s_unknown_invoice(tenant_db_session):
    with pytest.raises(HTTPException) as exc:
        create_payment(
            payload=_PaymentCompatIn(invoice_id="NOPE-999", amount=10.0),
            user=_current_user(),
            db=tenant_db_session,
        )
    assert exc.value.status_code == 404


# ── send_invoice email pay link ────────────────────────────────────────────

def _capture_send(monkeypatch):
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
    return captured


def test_send_invoice_email_includes_pay_link_when_configured(tenant_db_session, monkeypatch):
    from gdx_dispatch.models.tenant_models import Customer

    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setenv("GDX_PUBLIC_BASE_URL", "https://gdx.example.com")
    captured = _capture_send(monkeypatch)

    cust = Customer(name="Link Customer", email="link@example.com", phone="555-0100", company_id="tenant-test")
    tenant_db_session.add(cust)
    tenant_db_session.commit()
    tenant_db_session.refresh(cust)

    job = _seed_job(tenant_db_session)
    inv = create_invoice(
        payload=InvoiceCreateIn(job_id=job.id, customer_id=cust.id),
        _=_current_user(), db=tenant_db_session,
    )
    add_invoice_line(
        invoice_id=UUID(inv["id"]),
        payload=InvoiceLineCreateIn(description="Opener install", quantity=1, unit_price=500.0),
        _=_current_user(), db=tenant_db_session,
    )

    sent = send_invoice(invoice_id=UUID(inv["id"]), _=_current_user(), db=tenant_db_session)

    assert sent["email_sent"] is True
    token = _fresh(tenant_db_session, inv["id"]).public_token
    assert f"https://gdx.example.com/pay/{token}" in captured["html_body"]


def test_send_invoice_email_omits_pay_link_when_unconfigured(tenant_db_session, monkeypatch):
    from gdx_dispatch.models.tenant_models import Customer

    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    monkeypatch.setenv("GDX_PUBLIC_BASE_URL", "https://gdx.example.com")
    captured = _capture_send(monkeypatch)

    cust = Customer(name="NoLink Customer", email="nolink@example.com", phone="555-0101", company_id="tenant-test")
    tenant_db_session.add(cust)
    tenant_db_session.commit()
    tenant_db_session.refresh(cust)

    job = _seed_job(tenant_db_session)
    inv = create_invoice(
        payload=InvoiceCreateIn(job_id=job.id, customer_id=cust.id),
        _=_current_user(), db=tenant_db_session,
    )
    add_invoice_line(
        invoice_id=UUID(inv["id"]),
        payload=InvoiceLineCreateIn(description="Opener install", quantity=1, unit_price=500.0),
        _=_current_user(), db=tenant_db_session,
    )

    sent = send_invoice(invoice_id=UUID(inv["id"]), _=_current_user(), db=tenant_db_session)

    assert sent["email_sent"] is True
    assert "/pay/" not in captured["html_body"]  # no dead link, no CTA


# ── GET /pay/{token} page + GET /api/invoices/{id}/email-compose ───────────

def test_pay_page_renders_on_current_starlette(tenant_db_session, monkeypatch):
    """Regression: TemplateResponse must use the (request, name, ctx)
    signature — the old (name, ctx) order raises TypeError on the shipped
    Starlette, which surfaced as a 400 on every customer pay link the day
    Stripe went live."""
    from starlette.requests import Request as StarletteRequest

    from gdx_dispatch.core.payments import pay_invoice

    monkeypatch.setenv("STRIPE_PUBLISHABLE_KEY", "pk_test_page")
    inv = _seed_invoice(tenant_db_session)
    row = _fresh(tenant_db_session, inv["id"])

    req = StarletteRequest(
        {"type": "http", "method": "GET", "path": f"/pay/{row.public_token}", "headers": [], "query_string": b""}
    )
    resp = pay_invoice(invoice_token=row.public_token, request=req, db=tenant_db_session)

    assert resp.status_code == 200
    body = resp.body.decode()
    assert "pk_test_page" in body
    assert str(row.invoice_number) in body


def test_email_compose_includes_pay_link_when_configured(tenant_db_session, monkeypatch):
    """The composer flow (email-compose → /api/outlook/send) is how the
    office actually emails invoices; the draft must carry the pay link so
    the CTA doesn't silently vanish on the path #190 didn't cover."""
    from gdx_dispatch.routers.invoices import invoice_email_compose

    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setenv("GDX_PUBLIC_BASE_URL", "https://gdx.example.com")
    monkeypatch.setattr(
        "gdx_dispatch.core.pdf_generator.generate_invoice_pdf", lambda **_kw: b"%PDF-fake"
    )

    inv = _seed_invoice(tenant_db_session)
    out = invoice_email_compose(invoice_id=UUID(inv["id"]), _=_current_user(), db=tenant_db_session)

    token = _fresh(tenant_db_session, inv["id"]).public_token
    assert f"Pay online: https://gdx.example.com/pay/{token}" in out["body_text"]


def test_email_compose_omits_pay_link_when_unconfigured(tenant_db_session, monkeypatch):
    from gdx_dispatch.routers.invoices import invoice_email_compose

    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    monkeypatch.setenv("GDX_PUBLIC_BASE_URL", "https://gdx.example.com")
    monkeypatch.setattr(
        "gdx_dispatch.core.pdf_generator.generate_invoice_pdf", lambda **_kw: b"%PDF-fake"
    )

    inv = _seed_invoice(tenant_db_session)
    out = invoice_email_compose(invoice_id=UUID(inv["id"]), _=_current_user(), db=tenant_db_session)

    assert "/pay/" not in out["body_text"]  # no dead link in the draft


def test_email_compose_zero_balance_is_a_pure_read(tenant_db_session, monkeypatch):
    """A zero-balance invoice gets no pay line AND no token side effect —
    GET /email-compose must stay a pure read when nothing is chargeable."""
    from gdx_dispatch.routers.invoices import invoice_email_compose

    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setenv("GDX_PUBLIC_BASE_URL", "https://gdx.example.com")
    monkeypatch.setattr(
        "gdx_dispatch.core.pdf_generator.generate_invoice_pdf", lambda **_kw: b"%PDF-fake"
    )

    job = _seed_job(tenant_db_session)
    inv = create_invoice(  # no lines added: total = balance_due = 0
        payload=InvoiceCreateIn(job_id=job.id, customer_id=job.customer_id),
        _=_current_user(),
        db=tenant_db_session,
    )
    # Blank the creation-minted token so the mint branch is actually
    # reachable — otherwise this test passes even against mint-outside-gate
    # code (audit catch 2026-07-21: the assertion was non-discriminating).
    row = _fresh(tenant_db_session, inv["id"])
    row.public_token = ""
    tenant_db_session.commit()

    out = invoice_email_compose(invoice_id=UUID(inv["id"]), _=_current_user(), db=tenant_db_session)

    assert "/pay/" not in out["body_text"]
    assert _fresh(tenant_db_session, inv["id"]).public_token == ""  # no mint on a pure read

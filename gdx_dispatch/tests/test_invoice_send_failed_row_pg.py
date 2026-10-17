"""A failed or skipped invoice send leaves its Email Log row — GDXA-673.

`send_invoice` committed only `if email_sent:`, and `get_db` only closes the
session, so on Postgres the "failed" outbound_emails row staged on the
request's session never landed. GDXA-674 moved that row onto its own session;
these tests drive the route itself and show the route holds on its own too:
with a one-connection pool the own-session write cannot get a connection,
falls back to staging on the caller, and only the route's commit saves it.

Postgres only: pysqlite releases an outermost SAVEPOINT as a commit, so
SQLite shows the row whether or not the route commits.
"""
from __future__ import annotations

import contextlib
import secrets
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models.tenant_models import Customer, Invoice, InvoiceLine, OutboundEmail

COMPANY = "22222222-2222-2222-2222-222222222222"
USER = {"sub": "33333333-3333-3333-3333-333333333333", "tenant_id": COMPANY}


@pytest.fixture
def pg_engine(pg_test_engine, monkeypatch):
    """The fixture's schema dump predates tables the route touches (no
    outbound_emails, no gl_settings), so this per-test clone is rebuilt from
    the ORM — the tenant-plane schema of record."""
    import gdx_dispatch.models  # noqa: F401 — registers every mapper

    monkeypatch.delenv("GDX_ENV", raising=False)
    with pg_test_engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    TenantBase.metadata.create_all(pg_test_engine)
    return pg_test_engine


@pytest.fixture
def provider_refuses(monkeypatch):
    import gdx_dispatch.core.transactional_email as te

    monkeypatch.setattr(te, "_try_outlook_graph", lambda **_kw: (False, "outlook_send_failed"))
    monkeypatch.setattr(te, "_try_smtp", lambda **_kw: (False, "smtp_send_failed"))
    monkeypatch.setattr("gdx_dispatch.core.pdf_generator.generate_invoice_pdf", lambda **_kw: b"%PDF-1.4 tiny")


def _seed_invoice(engine) -> object:
    with sessionmaker(bind=engine)() as s:
        customer = Customer(name="Invoice Customer", email="billing@example.invalid", company_id=COMPANY)
        s.add(customer)
        s.flush()
        inv = Invoice(
            id=uuid4(), customer_id=customer.id, invoice_number=f"INV-{uuid4().hex[:6]}",
            status="draft", subtotal=Decimal("250"), tax_amount=Decimal("0"), total=Decimal("250"),
            balance_due=Decimal("250"), verified_at=datetime.now(UTC),
            public_token=secrets.token_urlsafe(48)[:64], company_id=COMPANY,
        )
        s.add(inv)
        s.flush()
        s.add(InvoiceLine(invoice_id=inv.id, description="Spring", quantity=1, unit_price=Decimal("250"),
                          line_total=Decimal("250"), taxable=False, company_id=COMPANY))
        s.commit()
        return inv.id


def _logged_from_a_fresh_session(engine, invoice_id):
    with sessionmaker(bind=engine)() as s:
        return [
            (r.status, r.skip_reason) for r in s.execute(
                select(OutboundEmail).where(OutboundEmail.entity_id == str(invoice_id))).scalars()
        ]


def _send_like_a_request(engine, invoice_id):
    """What `get_db` does around the route: open, call, close — no commit."""
    from gdx_dispatch.routers.invoices import send_invoice

    db = sessionmaker(bind=engine, autoflush=False)()
    try:
        return send_invoice(invoice_id=invoice_id, _=USER, db=db)
    finally:
        db.close()


def test_postgres_a_failed_invoice_send_leaves_its_row(pg_engine, provider_refuses):
    invoice_id = _seed_invoice(pg_engine)

    out = _send_like_a_request(pg_engine, invoice_id)

    assert out["email_sent"] is False and out["email_skip_reason"] == "smtp_send_failed"
    assert out["status"] == "sent"
    assert _logged_from_a_fresh_session(pg_engine, invoice_id) == [("failed", "smtp_send_failed")]


def test_postgres_the_route_saves_the_row_when_the_own_session_write_falls_back(
    pg_test_db, pg_engine, provider_refuses
):
    """Pool of one: the request's session holds the only connection, so
    `_record_outbound`'s own-session write times out and stages the row on
    the request's session. Pre-fix nothing committed it and this read `[]`."""
    narrow = create_engine(pg_test_db, pool_size=1, max_overflow=0, pool_timeout=0.5)
    try:
        invoice_id = _seed_invoice(narrow)

        out = _send_like_a_request(narrow, invoice_id)

        assert out["email_sent"] is False
        assert _logged_from_a_fresh_session(narrow, invoice_id) == [("failed", "smtp_send_failed")]
    finally:
        narrow.dispose()


def test_postgres_a_dead_transaction_after_the_send_still_answers(
    pg_engine, provider_refuses, monkeypatch
):
    """GDXA-160's measured 500: a swallowed failure inside the PDF attach
    aborts the transaction; nothing refreshed the expired invoice, so
    serializing it ran a SELECT on the dead transaction. Now the route rolls
    back and refreshes; the status flip committed before the send stands, and
    the attempt is still logged (own-session write)."""
    from sqlalchemy.exc import SQLAlchemyError

    import gdx_dispatch.routers.invoices as invoices

    def _attach_swallows_a_failed_read(db, _invoice, _cust, **_kw):
        with contextlib.suppress(SQLAlchemyError):
            db.execute(text("SELECT * FROM no_such_table_gdxa_673"))
        return None

    monkeypatch.setattr(invoices, "_invoice_pdf_attachments", _attach_swallows_a_failed_read)
    invoice_id = _seed_invoice(pg_engine)

    out = _send_like_a_request(pg_engine, invoice_id)

    assert out["email_sent"] is False
    assert out["status"] == "sent"
    assert _logged_from_a_fresh_session(pg_engine, invoice_id) == [("failed", "smtp_send_failed")]


def test_postgres_a_delivered_send_on_a_dead_transaction_still_stamps_sent_at(
    pg_engine, provider_refuses, monkeypatch
):
    """Same dead transaction, but the email went out: the route answers 200,
    and `sent_at` lands on a clean transaction — pre-fix the stamp's commit
    raised a 500 for mail the customer received, and Last Sent stayed blank."""
    from sqlalchemy.exc import SQLAlchemyError

    import gdx_dispatch.core.transactional_email as te
    import gdx_dispatch.routers.invoices as invoices

    monkeypatch.setattr(te, "_try_smtp", lambda **_kw: (True, None))

    def _attach_swallows_a_failed_read(db, _invoice, _cust, **_kw):
        with contextlib.suppress(SQLAlchemyError):
            db.execute(text("SELECT * FROM no_such_table_gdxa_673"))
        return None

    monkeypatch.setattr(invoices, "_invoice_pdf_attachments", _attach_swallows_a_failed_read)
    invoice_id = _seed_invoice(pg_engine)

    out = _send_like_a_request(pg_engine, invoice_id)

    assert out["email_sent"] is True
    with sessionmaker(bind=pg_engine)() as s:
        inv = s.get(Invoice, invoice_id)
        assert inv.sent_at is not None and inv.sent_via == "email"
    assert _logged_from_a_fresh_session(pg_engine, invoice_id) == [("sent", None)]

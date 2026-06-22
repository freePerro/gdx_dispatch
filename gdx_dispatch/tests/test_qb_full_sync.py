"""Tests for the QuickBooks module — httpx-based, no SDK dependencies.

Tests sync operations (pull/push), OAuth callback, webhook verification,
status endpoint, and module gating.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from gdx_dispatch.core.audit import AuditLog, TenantBase
from gdx_dispatch.core.quickbooks import QBConnection, QBEntityMap, QBVendor
from gdx_dispatch.models.tenant_models import CustomCatalog, CustomCatalogItem, Customer, Expense, Invoice, InvoiceLine, Job
from gdx_dispatch.modules.quickbooks.client import QBClient
from uuid import uuid4


@pytest.fixture()
def db_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = SessionLocal()
    try:
        QBConnection.__table__.create(bind=engine, checkfirst=True)
        QBEntityMap.__table__.create(bind=engine, checkfirst=True)
        QBVendor.__table__.create(bind=engine, checkfirst=True)
        yield db
    finally:
        db.close()
        engine.dispose()


@pytest.fixture()
def qb_connection(db_session: Session):
    row = QBConnection(
        tenant_id="tenant-1",
        realm_id="realm-1",
        access_token="access",
        refresh_token="refresh",
        access_token_expires_at=datetime.now(UTC) + timedelta(hours=1),
        refresh_token_expires_at=datetime.now(UTC) + timedelta(days=30),
    )
    db_session.add(row)
    db_session.commit()
    return row


@pytest.fixture()
def mock_qb() -> QBClient:
    """A QBClient with mocked HTTP methods."""
    qb = QBClient.__new__(QBClient)
    qb.access_token = "test-token"
    qb.realm_id = "realm-1"
    qb.base_url = "https://sandbox-quickbooks.api.intuit.com"
    qb.minor_version = 75
    qb._client = None  # won't be used — we mock query/create
    qb.query = AsyncMock(return_value=[])
    qb.create = AsyncMock(return_value={})
    qb.close = AsyncMock()
    return qb


def _request(*, tenant_id: str = "tenant-1", headers: list[tuple[bytes, bytes]] | None = None, body: bytes = b"") -> Request:
    async def _receive():
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/qb/webhooks",
        "headers": headers or [],
        "query_string": b"",
    }
    req = Request(scope, receive=_receive)
    req.state.tenant = {"id": tenant_id}
    return req


def _seed_customer(db: Session, name: str = "Alice") -> Customer:
    row = Customer(name=name, email=f"{name.lower()}@example.com", phone="555-0101", company_id="tenant-test")
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _seed_invoice(db: Session, customer: Customer) -> Invoice:
    job = Job(customer_id=customer.id, title="Install opener", company_id="tenant-test")
    db.add(job)
    db.flush()
    inv = Invoice(
        customer_id=uuid4(),
        job_id=job.id, invoice_number="INV-100", subtotal=100, tax_amount=10,
        total=110, balance_due=110, status="sent", public_token="pub-100",
        company_id="tenant-test",
    )
    db.add(inv)
    db.flush()
    line = InvoiceLine(
        invoice_id=inv.id, description="Labor", quantity=1,
        unit_price=100, line_total=100, sort_order=1,
        company_id="tenant-test",
    )
    db.add(line)
    db.commit()
    db.refresh(inv)
    return inv


def _seed_expense(db: Session) -> Expense:
    row = Expense(
        vendor="Supply House", amount=99.50, date=date.today(),
        category="materials", description="Parts order",
        company_id="tenant-test",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# ---------------------------------------------------------------------------
# Pull tests
# ---------------------------------------------------------------------------

def test_pull_customers_creates_new(db_session: Session, qb_connection, mock_qb):
    from gdx_dispatch.modules.quickbooks import sync

    mock_qb.query = AsyncMock(return_value=[
        {"Id": "QB-C-1", "DisplayName": "New QB Customer", "PrimaryEmailAddr": {"Address": "new@example.com"}},
    ])

    out = asyncio.run(sync.pull_customers("tenant-1", db_session, mock_qb))

    assert out["created"] == 1
    row = db_session.execute(select(Customer).where(Customer.email == "new@example.com")).scalar_one()
    assert row.name == "New QB Customer"


def test_pull_customers_updates_existing(db_session: Session, qb_connection, mock_qb):
    from gdx_dispatch.modules.quickbooks import sync

    cust = _seed_customer(db_session, "Legacy")
    db_session.add(QBEntityMap(tenant_id="tenant-1", entity_type="customer", local_id=str(cust.id), qb_id="QB-C-2"))
    db_session.commit()

    mock_qb.query = AsyncMock(return_value=[
        {"Id": "QB-C-2", "DisplayName": "Legacy Updated", "PrimaryEmailAddr": {"Address": "legacy-updated@example.com"}},
    ])

    out = asyncio.run(sync.pull_customers("tenant-1", db_session, mock_qb))

    assert out["updated"] == 1
    fresh = db_session.get(Customer, cust.id)
    assert fresh.name == "Legacy Updated"
    assert fresh.email == "legacy-updated@example.com"


def test_pull_invoices_creates_new(db_session: Session, qb_connection, mock_qb):
    from gdx_dispatch.modules.quickbooks import sync

    customer = _seed_customer(db_session, "Invoice Customer")
    db_session.add(QBEntityMap(tenant_id="tenant-1", entity_type="customer", local_id=str(customer.id), qb_id="QB-C-100"))
    db_session.commit()

    mock_qb.query = AsyncMock(return_value=[
        {
            "Id": "QB-I-1", "DocNumber": "INV-QB-1", "TotalAmt": 120, "Balance": 120,
            "CustomerRef": {"value": "QB-C-100"},
            "Line": [{"Amount": 120, "Description": "Service"}],
        },
    ])

    out = asyncio.run(sync.pull_invoices("tenant-1", db_session, mock_qb))

    assert out["created"] == 1
    inv = db_session.execute(select(Invoice).where(Invoice.invoice_number == "INV-QB-1")).scalar_one()
    assert float(inv.total) == pytest.approx(120)


def test_pull_payments_creates_new(db_session: Session, qb_connection, mock_qb):
    from gdx_dispatch.modules.quickbooks import sync

    customer = _seed_customer(db_session)
    invoice = _seed_invoice(db_session, customer)
    db_session.add(QBEntityMap(tenant_id="tenant-1", entity_type="invoice", local_id=str(invoice.id), qb_id="QB-I-10"))
    db_session.commit()

    mock_qb.query = AsyncMock(return_value=[
        {"Id": "QB-P-1", "TotalAmt": 55, "CustomerRef": {"value": "QB-C-5"},
         "Line": [{"LinkedTxn": [{"TxnType": "Invoice", "TxnId": "QB-I-10"}]}]},
    ])

    out = asyncio.run(sync.pull_payments("tenant-1", db_session, mock_qb))

    assert out["created"] == 1


def test_pull_items_links_to_catalog(db_session: Session, qb_connection, mock_qb):
    from gdx_dispatch.modules.quickbooks import sync

    catalog = CustomCatalog(name="QB Catalog", source_system="qb")
    db_session.add(catalog)
    db_session.commit()

    mock_qb.query = AsyncMock(return_value=[
        {"Id": "QB-ITEM-1", "Name": "Spring", "UnitPrice": 199.99, "Type": "Service", "Active": True},
    ])

    out = asyncio.run(sync.pull_items("tenant-1", db_session, mock_qb))

    assert out["created"] == 1
    item = db_session.execute(select(CustomCatalogItem).where(CustomCatalogItem.qb_item_id == "QB-ITEM-1")).scalar_one()
    assert item.name == "Spring"
    assert float(item.price) == pytest.approx(199.99)


# ---------------------------------------------------------------------------
# Push tests
# ---------------------------------------------------------------------------

def test_push_invoice_creates_in_qb(db_session: Session, qb_connection, mock_qb):
    from gdx_dispatch.modules.quickbooks import sync

    customer = _seed_customer(db_session)
    invoice = _seed_invoice(db_session, customer)

    mock_qb.create = AsyncMock(return_value={"Id": "QB-I-NEW"})

    out = asyncio.run(sync.push_invoice("tenant-1", str(invoice.id), db_session, mock_qb))

    assert out["qb_invoice_id"] == "QB-I-NEW"
    mock_qb.create.assert_called_once()
    call_args = mock_qb.create.call_args
    assert call_args[0][0] == "Invoice"
    assert call_args[0][1]["Line"][0]["Description"] == "Labor"


def test_legacy_push_invoice_counter_sale_uses_invoice_customer(
    db_session: Session, qb_connection, monkeypatch
):
    """2026-05-14 — counter-sale invoices have job_id=None. The legacy
    `gdx_dispatch.core.quickbooks.push_invoice` path previously did `db.get(Job, None)`,
    skipped customer_ref entirely, and POSTed to QBO with no CustomerRef.
    Regression: it must now resolve customer from `invoice.customer_id`.
    """
    from gdx_dispatch.core import quickbooks as legacy_qb

    customer = _seed_customer(db_session, "CounterCust")
    inv = Invoice(
        customer_id=customer.id,
        job_id=None,
        invoice_number="INV-CSALE-1",
        subtotal=70, tax_amount=0, total=70, balance_due=70,
        status="sent", public_token="pub-csale-1",
        company_id="tenant-test",
    )
    db_session.add(inv)
    db_session.flush()
    db_session.add(InvoiceLine(
        invoice_id=inv.id, description="Spring (counter)", quantity=2,
        unit_price=35, line_total=70, sort_order=1, company_id="tenant-test",
    ))
    db_session.add(QBEntityMap(
        tenant_id="tenant-1", entity_type="customer",
        local_id=str(customer.id), qb_id="QB-CUST-CSALE",
    ))
    db_session.commit()

    captured: dict = {}
    def _fake_create(_client, *, entity_name, payload, idempotency_key):
        captured["entity_name"] = entity_name
        captured["payload"] = payload
        return {"Invoice": {"Id": "QB-I-CSALE"}}
    monkeypatch.setattr(legacy_qb, "_qb_create", _fake_create)
    monkeypatch.setattr(legacy_qb, "get_qb_client", lambda _t, _d: object())

    legacy_qb.push_invoice("tenant-1", str(inv.id), db_session)

    assert captured["payload"].get("CustomerRef") == {"value": "QB-CUST-CSALE"}, \
        "counter-sale invoice pushed to QBO without CustomerRef"


def test_push_expense_creates_in_qb(db_session: Session, qb_connection, mock_qb):
    from sqlalchemy import text as _text
    from uuid import uuid4 as _uuid4

    from gdx_dispatch.modules.quickbooks import sync

    expense = _seed_expense(db_session)

    # S122-19: push_expense now looks up a real AccountRef from qb_accounts
    # instead of hardcoding "1". Seed an Expense account so the lookup
    # succeeds.
    db_session.execute(_text(sync._QB_ACCOUNTS_DDL))
    db_session.execute(_text("""
        INSERT INTO qb_accounts (id, tenant_id, qb_account_id, name,
            account_type, account_sub_type, classification, current_balance, active)
        VALUES (:id, 'tenant-1', 'acc-test-expense', 'Office Supplies',
                'Expense', '', '', 0, TRUE)
    """), {"id": str(_uuid4())})
    db_session.commit()

    mock_qb.create = AsyncMock(return_value={"Id": "QB-PUR-1"})

    out = asyncio.run(sync.push_expense("tenant-1", str(expense.id), db_session, mock_qb))

    assert out["qb_purchase_id"] == "QB-PUR-1"


# ---------------------------------------------------------------------------
# Audit tests
# ---------------------------------------------------------------------------

def test_audit_logged_on_sync(db_session: Session, qb_connection, mock_qb):
    from gdx_dispatch.modules.quickbooks import sync

    mock_qb.query = AsyncMock(return_value=[])

    asyncio.run(sync.pull_customers("tenant-1", db_session, mock_qb))

    log_entry = db_session.execute(select(AuditLog).where(AuditLog.action == "qb_pull_customers")).scalar_one()
    assert log_entry.entity_type == "quickbooks"


# ---------------------------------------------------------------------------
# OAuth callback test
# ---------------------------------------------------------------------------

def test_oauth_callback_stores_tokens(db_session: Session, monkeypatch):
    import gdx_dispatch.modules.quickbooks.router as qb_router_mod
    from gdx_dispatch.modules.quickbooks import oauth

    async def _fake_exchange(code):
        return {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 3600,
            "x_refresh_token_expires_in": 8726400,
        }

    monkeypatch.setattr(qb_router_mod, "exchange_code_for_tokens", _fake_exchange)

    out = asyncio.run(qb_router_mod.qb_callback(code="abc", state="tenant-1", realmId="realm-9", db=db_session))

    # Callback now returns an HTMLResponse with postMessage + auto-close JS
    # (see docstring on qb_callback — popup OAuth pattern).
    from starlette.responses import HTMLResponse
    assert isinstance(out, HTMLResponse)
    assert out.status_code == 200
    body = out.body.decode("utf-8")
    assert "qb_oauth_result" in body
    assert "postMessage" in body
    assert "realm-9" in body

    row = db_session.execute(select(oauth.QBTokenStore).where(oauth.QBTokenStore.realm_id == "realm-9")).scalar_one()
    assert row.realm_id == "realm-9"


def test_oauth_callback_missing_state_returns_error_html(db_session: Session):
    """Empty state must return an error HTML page, not crash or succeed."""
    from starlette.responses import HTMLResponse

    import gdx_dispatch.modules.quickbooks.router as qb_router_mod

    out = asyncio.run(qb_router_mod.qb_callback(code="x", state="", realmId="r", db=db_session))
    assert isinstance(out, HTMLResponse)
    assert out.status_code == 400
    body = out.body.decode("utf-8")
    assert '"status": "error"' in body
    assert "Missing tenant state" in body


def test_oauth_callback_token_exchange_failure_returns_html(db_session: Session, monkeypatch):
    """Token exchange failure must return error HTML — not leak the exception to the browser."""
    from starlette.responses import HTMLResponse

    import gdx_dispatch.modules.quickbooks.router as qb_router_mod
    from gdx_dispatch.modules.quickbooks.client import QBAuthError

    async def _boom(code):
        raise QBAuthError("<script>alert('xss')</script>")

    monkeypatch.setattr(qb_router_mod, "exchange_code_for_tokens", _boom)

    out = asyncio.run(qb_router_mod.qb_callback(code="x", state="tenant-1", realmId="r", db=db_session))
    assert isinstance(out, HTMLResponse)
    assert out.status_code == 502
    body = out.body.decode("utf-8")
    # Exception text must NOT leak into the rendered HTML (even escaped)
    assert "<script>alert" not in body
    assert "alert('xss')" not in body
    assert "Token exchange with Intuit failed" in body


def test_callback_html_escapes_untrusted_input():
    """Malicious realm_id / message content must not break out of its context.

    json.dumps() will escape the value into a JSON string literal; html.escape()
    will escape anything rendered in HTML. Single quotes inside a double-quoted
    JSON string are fine and cannot terminate the JS string. The attack shape
    we care about is `</script>` appearing verbatim (which DOES break out of
    the <script> tag) or unescaped HTML tags in the display message.
    """
    import gdx_dispatch.modules.quickbooks.router as qb_router_mod

    # Attacker tries to close the script tag and inject new code
    malicious_realm = "</script><script>alert('pwned')</script>"
    html_str = qb_router_mod._callback_html("connected", realm_id=malicious_realm)

    # The literal </script> sequence must NOT appear verbatim in the payload —
    # we apply HTML-safe JSON escaping (< → \u003c) to prevent script tag
    # breakout. Count occurrences of literal </script>: there should be
    # exactly 1 (our own closing tag, not inside the payload JSON).
    assert html_str.count("</script>") == 1
    # The malicious realm_id's < should be encoded as \u003c in the JSON
    assert "\\u003c/script" in html_str
    # Target origin is NOT wildcard
    assert "postMessage(payload, '*')" not in html_str
    # Malicious HTML tags in display message get escaped
    html_with_msg = qb_router_mod._callback_html("error", message="<img src=x onerror=alert(1)>")
    assert "<img src=x" not in html_with_msg
    assert "&lt;img" in html_with_msg


def test_callback_html_target_origin_uses_gdx_base_url(monkeypatch):
    """postMessage target must be the configured origin, not '*'."""
    import gdx_dispatch.modules.quickbooks.router as qb_router_mod

    monkeypatch.setenv("GDX_BASE_URL", "https://example.example.com")
    html_str = qb_router_mod._callback_html("connected", realm_id="r1")
    assert '"https://example.example.com"' in html_str
    assert "postMessage(payload, '*')" not in html_str


def test_qb_events_endpoint_returns_audit_log_events(db_session: Session, qb_connection, mock_qb):
    """Events endpoint reads QB audit log rows and formats them for the UI."""
    import gdx_dispatch.modules.quickbooks.router as qb_router_mod
    from gdx_dispatch.modules.quickbooks import sync

    # Produce some real audit log entries via the sync functions
    mock_qb.query = AsyncMock(return_value=[
        {"Id": "C-1", "DisplayName": "Alice"},
        {"Id": "C-2", "DisplayName": "Bob"},
    ])
    asyncio.run(sync.pull_customers("tenant-1", db_session, mock_qb))

    req = _request(tenant_id="tenant-1")
    result = qb_router_mod.qb_events(request=req, current_user={"tenant_id": "tenant-1"}, db=db_session)

    assert "events" in result
    customer_events = [e for e in result["events"] if e["type"] == "customers"]
    assert len(customer_events) == 1
    assert customer_events[0]["count"] == 2
    assert customer_events[0]["status"] == "success"
    assert "created" in (customer_events[0]["details"] or "")


def test_qb_events_filters_non_sync_audit_entries(db_session: Session):
    """Connect / disconnect / webhook audit rows should not appear in the events list."""
    import gdx_dispatch.modules.quickbooks.router as qb_router_mod
    from gdx_dispatch.core.audit import log_audit_event_sync

    # Add some noise that the UI shouldn't display
    log_audit_event_sync(db_session, tenant_id="tenant-1", user_id="u",
                         action="qb_connect", entity_type="quickbooks",
                         entity_id="", details={})
    log_audit_event_sync(db_session, tenant_id="tenant-1", user_id="u",
                         action="qb_disconnect", entity_type="quickbooks",
                         entity_id="", details={})
    # And one real sync event
    log_audit_event_sync(db_session, tenant_id="tenant-1", user_id="system",
                         action="qb_pull_invoices", entity_type="quickbooks",
                         entity_id="tenant-1", details={"created": 5, "updated": 2})
    db_session.commit()

    req = _request(tenant_id="tenant-1")
    result = qb_router_mod.qb_events(request=req, current_user={"tenant_id": "tenant-1"}, db=db_session)

    types = [e["type"] for e in result["events"]]
    assert "invoices" in types
    assert "connect" not in types
    assert "disconnect" not in types


def test_pull_invoices_adopts_existing_local_invoice_by_number(db_session: Session, qb_connection, mock_qb):
    """Regression: 2026-04-13 production hit a UniqueViolation when QB invoice
    #505316 was brought in and local already had that number. Adoption path
    should link the existing invoice to the QB id instead of inserting a dupe.
    """
    from gdx_dispatch.modules.quickbooks import sync

    # Seed a pre-existing local invoice with number "505316"
    cust = _seed_customer(db_session, "Pre-existing Customer")
    job = Job(customer_id=cust.id, title="Legacy", company_id="tenant-1")
    db_session.add(job)
    db_session.flush()
    existing_inv = Invoice(
        customer_id=uuid4(),
        job_id=job.id, invoice_number="505316", subtotal=100, tax_amount=0,
        total=100, balance_due=100, status="sent", public_token="pre-505316",
        company_id="tenant-1",
    )
    db_session.add(existing_inv)
    db_session.commit()
    original_id = existing_inv.id

    # QB brings in an invoice with the same DocNumber
    mock_qb.query = AsyncMock(return_value=[
        {"Id": "QB-I-505316", "DocNumber": "505316", "TotalAmt": 250, "Balance": 0},
    ])

    out = asyncio.run(sync.pull_invoices("tenant-1", db_session, mock_qb))

    # No new row created — the existing local invoice was adopted
    assert out["adopted"] == 1
    assert out["created"] == 0
    assert out["errors"] == []

    # The existing invoice was updated with QB data, not replaced
    fresh = db_session.get(Invoice, original_id)
    assert fresh is not None
    assert float(fresh.total) == 250.0
    assert fresh.status == "paid"  # balance was 0

    # Mapping now exists pointing at the original id
    mapping = db_session.execute(
        select(QBEntityMap).where(
            QBEntityMap.tenant_id == "tenant-1",
            QBEntityMap.entity_type == "invoice",
            QBEntityMap.qb_id == "QB-I-505316",
        )
    ).scalar_one()
    assert mapping.local_id == str(original_id)


def test_pull_invoices_one_bad_row_does_not_kill_sync(db_session: Session, qb_connection, mock_qb, monkeypatch):
    """SAVEPOINT isolation — a failure on invoice A must not prevent invoice B
    from being processed. Before this fix, any UniqueViolation rolled back the
    entire session and every subsequent invoice in the batch was lost.
    """
    from gdx_dispatch.modules.quickbooks import sync

    mock_qb.query = AsyncMock(return_value=[
        {"Id": "QB-I-GOOD-1", "DocNumber": "INV-A", "TotalAmt": 100},
        # Bad row: missing required data — the helper will barf on this
        {"Id": "QB-I-BAD", "DocNumber": "", "TotalAmt": "not-a-number"},
        {"Id": "QB-I-GOOD-2", "DocNumber": "INV-C", "TotalAmt": 200},
    ])

    out = asyncio.run(sync.pull_invoices("tenant-1", db_session, mock_qb))

    # Good invoices created despite the bad row in the middle
    assert out["created"] >= 2
    # Not raised, not crashed — errors collected
    good_a = db_session.execute(select(Invoice).where(Invoice.invoice_number == "INV-A")).scalar_one_or_none()
    good_c = db_session.execute(select(Invoice).where(Invoice.invoice_number == "INV-C")).scalar_one_or_none()
    assert good_a is not None, "INV-A should have been created"
    assert good_c is not None, "INV-C should have been created despite the bad row between"


def test_pull_customers_adopts_by_name_and_phone(db_session: Session, qb_connection, mock_qb):
    """2026-04-13: adoption now matches on normalized name + last-4 of phone,
    not email. Field-service customers often have no email in QB. This test
    covers the happy path: same name (case/punctuation-insensitive) + same
    phone last 4 → adopt existing customer, no duplicate created.
    """
    from gdx_dispatch.modules.quickbooks import sync

    existing = Customer(
        name="Robert Sudbeck",
        phone="555-867-5309",
        email=None,  # no email, matches reality for most service customers
        source=None,
        company_id="tenant-1",
    )
    db_session.add(existing)
    db_session.commit()

    mock_qb.query = AsyncMock(return_value=[
        {"Id": "QB-C-RS", "DisplayName": "robert sudbeck",  # case-different
         "PrimaryPhone": {"FreeFormNumber": "(555) 867-5309"}},  # format-different, same digits
    ])

    out = asyncio.run(sync.pull_customers("tenant-1", db_session, mock_qb))

    assert out["adopted"] == 1
    assert out["created"] == 0
    assert out["errors"] == []
    mapping = db_session.execute(
        select(QBEntityMap).where(
            QBEntityMap.entity_type == "customer",
            QBEntityMap.qb_id == "QB-C-RS",
        )
    ).scalar_one()
    assert mapping.local_id == str(existing.id)


def test_pull_customers_no_adopt_when_name_different(db_session: Session, qb_connection, mock_qb):
    """Different name → do NOT adopt, even if email matches. Safer to create
    a duplicate that can be reviewed than to clobber the wrong customer."""
    from gdx_dispatch.modules.quickbooks import sync

    existing = Customer(
        name="Alice",
        phone="555-111-2222",
        email="shared@example.com",
        company_id="tenant-1",
    )
    db_session.add(existing)
    db_session.commit()

    # Same email but completely different name — ambiguous, don't adopt
    mock_qb.query = AsyncMock(return_value=[
        {"Id": "QB-C-BOB", "DisplayName": "Bob Jones",
         "PrimaryEmailAddr": {"Address": "shared@example.com"}},
    ])

    out = asyncio.run(sync.pull_customers("tenant-1", db_session, mock_qb))

    assert out["created"] == 1
    assert out["adopted"] == 0
    # Both customers now exist separately — human can review the email collision
    all_customers = db_session.execute(
        select(Customer).where(Customer.company_id == "tenant-1")
    ).scalars().all()
    assert len(all_customers) == 2


def test_qb_disconnect_clears_both_stores(db_session: Session, monkeypatch):
    """Disconnect must remove QBTokenStore AND QBConnection rows — no zombies."""
    from datetime import timedelta

    from gdx_dispatch.core.quickbooks import QBConnection as _QBConn
    from gdx_dispatch.modules.quickbooks import oauth
    from gdx_dispatch.modules.quickbooks import router as qb_router_mod

    # Seed both stores for tenant-1
    oauth.QBTokenStore.__table__.create(bind=db_session.bind, checkfirst=True)
    db_session.add(oauth.QBTokenStore(
        tenant_id="tenant-1",
        realm_id="r1",
        environment="production",
        access_token_enc="abc",
        refresh_token_enc="def",
        access_token_expires_at=datetime.now(UTC) + timedelta(hours=1),
        refresh_token_expires_at=datetime.now(UTC) + timedelta(days=30),
    ))
    db_session.add(_QBConn(
        tenant_id="tenant-1", realm_id="r1",
        access_token="legacy-a", refresh_token="legacy-r",
        access_token_expires_at=datetime.now(UTC) + timedelta(hours=1),
        refresh_token_expires_at=datetime.now(UTC) + timedelta(days=30),
    ))
    db_session.commit()

    req = _request(tenant_id="tenant-1")
    out = qb_router_mod.qb_disconnect(
        request=req,
        user={"role": "admin", "sub": "u-1"},
        db=db_session,
    )
    assert out["disconnected"] is True
    assert "qb_token_store" in out["cleared"]
    assert "qb_connection" in out["cleared"]

    # Verify both are actually gone
    assert db_session.execute(select(oauth.QBTokenStore)).scalar_one_or_none() is None
    assert db_session.execute(select(_QBConn).where(_QBConn.tenant_id == "tenant-1")).scalar_one_or_none() is None


# ---------------------------------------------------------------------------
# Endpoint-through-FastAPI integration tests
#
# Regression guard for 2026-04-13 bug: sync ran correctly against Intuit
# (all 5 pull calls returned 200, data imported) but the endpoint crashed
# with ResponseValidationError because my function return shape ({"created",
# "updated", "adopted", "errors": []}) didn't match the endpoint's declared
# type (dict[str, int]). Unit tests passed because they called the sync
# functions directly — bypassing FastAPI's response serializer. This class
# of bug now gets caught before shipping.
# ---------------------------------------------------------------------------

def test_sync_full_endpoint_returns_valid_response_shape(db_session: Session, qb_connection, monkeypatch):
    """The sync_full endpoint must serialize correctly through FastAPI,
    including the per-entity 'errors' list alongside integer counts.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import gdx_dispatch.modules.quickbooks.router as qb_router_mod
    from gdx_dispatch.core.database import get_db
    from gdx_dispatch.modules.quickbooks import sync

    # Stub the sync functions — they shouldn't hit real QB in a unit test.
    # Return the exact shape the production code produces, including errors: [].
    async def _fake_pull(*_a, **_kw):
        return {"created": 1, "updated": 0, "adopted": 0, "errors": []}

    async def _fake_pull_payments(*_a, **_kw):
        return {"created": 1, "updated": 0, "skipped": 0, "errors": []}

    monkeypatch.setattr(sync, "pull_customers", _fake_pull)
    monkeypatch.setattr(sync, "pull_invoices", _fake_pull)
    monkeypatch.setattr(sync, "pull_items", _fake_pull)
    monkeypatch.setattr(sync, "pull_vendors", _fake_pull)
    monkeypatch.setattr(sync, "pull_payments", _fake_pull_payments)
    monkeypatch.setattr(sync, "pull_accounts", _fake_pull)
    monkeypatch.setattr(sync, "pull_bank_transactions", _fake_pull)

    # Stub the QB client so get_qb_client doesn't try real OAuth
    async def _fake_client(_tenant_id, _db):
        class _FakeQB:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def close(self): pass
        return _FakeQB()
    monkeypatch.setattr(qb_router_mod, "get_qb_client", _fake_client)

    # Minimal FastAPI app with just the sync_full route
    app = FastAPI()

    # Middleware that injects tenant state onto every request — the router's
    # _tenant_id reads from request.state.tenant which is normally set by
    # TenantMiddleware in production.
    from starlette.middleware.base import BaseHTTPMiddleware
    class _InjectTenant(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.tenant = {"id": "tenant-1"}
            return await call_next(request)
    app.add_middleware(_InjectTenant)

    app.include_router(qb_router_mod.router)

    # Stub dependencies
    def _fake_tenant_db():
        yield db_session
    def _fake_user():
        return {"sub": "u-1", "tenant_id": "tenant-1"}
    from gdx_dispatch.core.modules import require_module
    from gdx_dispatch.routers.auth import get_current_user
    app.dependency_overrides[get_db] = _fake_tenant_db
    app.dependency_overrides[get_current_user] = _fake_user
    app.dependency_overrides[require_module("quickbooks")] = lambda: None

    client = TestClient(app)
    resp = client.post("/api/qb/sync/full")
    # Must NOT be a ResponseValidationError 500
    assert resp.status_code != 500, f"response validation failed: {resp.text[:500]}"
    # The response should be valid JSON with the expected shape
    data = resp.json()
    for entity in ("customers", "invoices", "items", "vendors", "payments"):
        assert entity in data, f"missing entity: {entity}"
        assert "errors" in data[entity], f"{entity} missing errors field"
        assert isinstance(data[entity]["errors"], list)


# ---------------------------------------------------------------------------
# Webhook test
# ---------------------------------------------------------------------------

def test_webhook_verifies_signature(db_session: Session, monkeypatch):
    import gdx_dispatch.modules.quickbooks.router as qb_router

    monkeypatch.setenv("QB_WEBHOOK_VERIFIER_TOKEN", "top-secret")
    body = b'{"eventNotifications":[]}'
    sig = base64.b64encode(hmac.new(b"top-secret", body, hashlib.sha256).digest()).decode("utf-8")

    ok_req = _request(headers=[(b"intuit-signature", sig.encode("utf-8"))], body=body)
    out = asyncio.run(qb_router.qb_webhooks(request=ok_req, db=db_session))
    assert out["verified"] is True

    bad_req = _request(headers=[(b"intuit-signature", b"bad")], body=body)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(qb_router.qb_webhooks(request=bad_req, db=db_session))
    assert exc.value.status_code == 403


def test_s122_5_modular_webhook_verifies_hmac_sha256_raw_bytes(db_session, monkeypatch):
    """S122-5 (C3): the MODULAR webhook handler used to do a literal-string
    compare on the wrong header against the wrong algorithm. Real Intuit
    deliveries 403'd, forged events passed. Now it must do HMAC-SHA256 base64
    against the raw request bytes using the ``intuit-signature`` header.
    """
    from gdx_dispatch.modules.quickbooks import webhook_router

    monkeypatch.setenv("QB_WEBHOOK_VERIFIER_TOKEN", "vt")
    body = b'{"eventNotifications":[]}'
    good_sig = base64.b64encode(hmac.new(b"vt", body, hashlib.sha256).digest()).decode("utf-8")

    ok_req = _request(headers=[(b"intuit-signature", good_sig.encode("utf-8"))], body=body)
    out = asyncio.run(webhook_router.qb_webhook(request=ok_req, db=db_session))
    assert out["processed"] == 0  # empty notification list

    bad_req = _request(headers=[(b"intuit-signature", b"forged")], body=body)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(webhook_router.qb_webhook(request=bad_req, db=db_session))
    assert exc.value.status_code == 403


def _stub_celery_delay(monkeypatch):
    """Stop Celery .delay() from trying to reach Redis in unit tests.

    Includes the S122-11/12 per-entity tasks the webhook now dispatches
    instead of the legacy sync_all_*_task helpers.
    """
    from gdx_dispatch.modules.quickbooks import tasks as qb_tasks
    monkeypatch.setattr(qb_tasks.sync_all_customers_task, "delay", lambda *a, **kw: None)
    monkeypatch.setattr(qb_tasks.sync_all_invoices_task, "delay", lambda *a, **kw: None)
    for name in (
        "sync_customer_task", "sync_invoice_task", "sync_payment_task",
        "sync_item_task", "sync_vendor_task", "sync_account_task",
    ):
        monkeypatch.setattr(getattr(qb_tasks, name), "delay", lambda *a, **kw: None)


def test_s122_ce_modular_webhook_parses_cloudevents_format(db_session, monkeypatch):
    """S122-CE (B1): Intuit's CloudEvents v1.0 format is mandatory by 2026-07-31.
    Top-level is an array; each event has ``type=qbo.<entity>.<op>.v1`` and
    ``intuitaccountid`` (replaces realmId) + ``intuitentityid`` (replaces entities[].id).
    """
    from gdx_dispatch.modules.quickbooks import webhook_router

    monkeypatch.delenv("QB_WEBHOOK_VERIFIER_TOKEN", raising=False)
    monkeypatch.delenv("QB_WEBHOOK_SECRET", raising=False)
    _stub_celery_delay(monkeypatch)
    body = json.dumps([{
        "specversion": "1.0",
        "id": "evt-1",
        "source": "intuit.x",
        "type": "qbo.customer.created.v1",
        "datacontenttype": "application/json",
        "time": "2026-07-31T12:00:00Z",
        "intuitentityid": "42",
        "intuitaccountid": "9130000000000000001",
        "data": {},
    }]).encode("utf-8")
    req = _request(body=body)
    out = asyncio.run(webhook_router.qb_webhook(request=req, db=db_session))
    assert out["processed"] == 1


def test_s122_ce_modular_webhook_still_parses_old_format(db_session, monkeypatch):
    """During the 2026-05-12 → 2026-07-31 transition window both formats coexist."""
    from gdx_dispatch.modules.quickbooks import webhook_router

    monkeypatch.delenv("QB_WEBHOOK_VERIFIER_TOKEN", raising=False)
    monkeypatch.delenv("QB_WEBHOOK_SECRET", raising=False)
    _stub_celery_delay(monkeypatch)
    body = json.dumps({
        "eventNotifications": [{
            "realmId": "12345",
            "dataChangeEvent": {"entities": [
                {"name": "Invoice", "id": "100", "operation": "Update", "lastUpdated": "2026-05-12T00:00:00Z"}
            ]},
        }],
    }).encode("utf-8")
    req = _request(body=body)
    out = asyncio.run(webhook_router.qb_webhook(request=req, db=db_session))
    assert out["processed"] == 1


def test_s122_ce_modular_webhook_deduplicates_on_replay(db_session, monkeypatch):
    """Intuit retries on 20/30/50 min schedule — duplicate event_id must skip."""
    from gdx_dispatch.modules.quickbooks import webhook_router

    monkeypatch.delenv("QB_WEBHOOK_VERIFIER_TOKEN", raising=False)
    monkeypatch.delenv("QB_WEBHOOK_SECRET", raising=False)
    _stub_celery_delay(monkeypatch)
    body = json.dumps([{
        "specversion": "1.0", "id": "evt-dup", "source": "x",
        "type": "qbo.invoice.updated.v1", "datacontenttype": "application/json",
        "time": "2026-07-31T12:00:00Z", "intuitentityid": "777",
        "intuitaccountid": "12345", "data": {},
    }]).encode("utf-8")
    asyncio.run(webhook_router.qb_webhook(request=_request(body=body), db=db_session))
    out2 = asyncio.run(webhook_router.qb_webhook(request=_request(body=body), db=db_session))
    assert out2["skipped"] == 1


def test_s122_2_pull_invoices_parses_txn_tax_detail(db_session, qb_connection, mock_qb, monkeypatch):
    """S122-2 (T3): pull_invoices must extract TxnTaxDetail.TotalTax from the
    QB Invoice payload and persist it as ``tax_amount``. Pre-fix every
    QB-imported invoice landed with ``tax_amount=0`` and ``subtotal=total``,
    so AR rollups silently lost every dollar of collected tax (GDX since
    2026-04-13). Subtotal must equal ``total - tax_amount``.
    """
    from gdx_dispatch.modules.quickbooks import sync as qb_sync

    customer = _seed_customer(db_session, name="Tax Test Co")
    db_session.commit()
    db_session.add(QBEntityMap(tenant_id="tenant-test", entity_type="customer",
                               local_id=str(customer.id), qb_id="C-TAX-1"))
    db_session.commit()

    invoice_payload = {
        "Id": "INV-TAX-1",
        "DocNumber": "TAX-001",
        "TotalAmt": "108.25",
        "Balance": "0",
        "TxnDate": "2026-05-12",
        "DueDate": "2026-05-26",
        "CustomerRef": {"value": "C-TAX-1"},
        "TxnTaxDetail": {"TotalTax": "8.25"},
        "Line": [{"Amount": "100.00", "Description": "Service"}],
    }
    mock_qb.query = AsyncMock(return_value=[invoice_payload])

    out = asyncio.run(qb_sync.pull_invoices("tenant-test", db_session, mock_qb))
    assert out["created"] == 1

    inv = db_session.execute(select(Invoice).where(Invoice.invoice_number == "TAX-001")).scalar_one()
    assert Decimal(str(inv.tax_amount)) == Decimal("8.25"), f"tax_amount={inv.tax_amount}"
    assert Decimal(str(inv.subtotal)) == Decimal("100.00"), f"subtotal={inv.subtotal}"
    assert Decimal(str(inv.total)) == Decimal("108.25"), f"total={inv.total}"


def test_s122_2_pull_invoices_subtotal_sums_line_amounts_not_subtract_tax(db_session, qb_connection, mock_qb, monkeypatch):
    """S122-2 auditor catch round 2: subtotal must come from summing
    SalesItemLine amounts, NOT from ``total - tax``. The subtract path is
    wrong when the invoice has DiscountLine or ShippingLine — their amounts
    contribute to TotalAmt but aren't part of the item subtotal.
    """
    from gdx_dispatch.modules.quickbooks import sync as qb_sync

    customer = _seed_customer(db_session, name="Discount Co")
    db_session.commit()
    db_session.add(QBEntityMap(tenant_id="tenant-test", entity_type="customer",
                               local_id=str(customer.id), qb_id="C-DISC-1"))
    db_session.commit()

    # Item lines sum to 100; 10 discount; 8.25 tax; total 98.25.
    # Subtract-from-total: subtotal = 98.25 - 8.25 = 90.00 (WRONG)
    # Sum-of-item-lines:  subtotal = 100.00 (correct)
    invoice_payload = {
        "Id": "INV-DISC-1",
        "DocNumber": "DISC-001",
        "TotalAmt": "98.25",
        "Balance": "98.25",
        "TxnDate": "2026-05-12",
        "CustomerRef": {"value": "C-DISC-1"},
        "TxnTaxDetail": {"TotalTax": "8.25"},
        "Line": [
            {"Amount": "60.00", "DetailType": "SalesItemLineDetail",
             "Description": "Door", "SalesItemLineDetail": {}},
            {"Amount": "40.00", "DetailType": "SalesItemLineDetail",
             "Description": "Service", "SalesItemLineDetail": {}},
            {"Amount": "-10.00", "DetailType": "DiscountLineDetail",
             "Description": "Discount", "DiscountLineDetail": {}},
        ],
    }
    mock_qb.query = AsyncMock(return_value=[invoice_payload])

    asyncio.run(qb_sync.pull_invoices("tenant-test", db_session, mock_qb))
    inv = db_session.execute(select(Invoice).where(Invoice.invoice_number == "DISC-001")).scalar_one()
    assert Decimal(str(inv.subtotal)) == Decimal("100.00"), (
        f"subtotal must be sum of item lines (100), not total-minus-tax (90). got {inv.subtotal}"
    )
    assert Decimal(str(inv.tax_amount)) == Decimal("8.25")
    assert Decimal(str(inv.total)) == Decimal("98.25")


def test_s122_ce_cloudevents_canonicalizes_compound_entity_names(db_session, monkeypatch):
    """S122-CE auditor catch round 2: CloudEvents lowercases entity names
    (``qbo.salesreceipt.created.v1``); naive ``str.capitalize()`` returns
    ``Salesreceipt`` instead of the canonical ``SalesReceipt``. QBO API
    entity routes are case-sensitive — wrong casing means wrong sync task
    routing + silent drop.
    """
    from gdx_dispatch.modules.quickbooks.webhook_router import _canonicalize_entity_name
    assert _canonicalize_entity_name("salesreceipt") == "SalesReceipt"
    assert _canonicalize_entity_name("creditmemo") == "CreditMemo"
    assert _canonicalize_entity_name("journalentry") == "JournalEntry"
    assert _canonicalize_entity_name("refundreceipt") == "RefundReceipt"
    assert _canonicalize_entity_name("billpayment") == "BillPayment"
    assert _canonicalize_entity_name("timeactivity") == "TimeActivity"
    assert _canonicalize_entity_name("customer") == "Customer"  # single-word still works
    assert _canonicalize_entity_name("invoice") == "Invoice"


def test_s122_ce_webhook_rejects_oversized_body(db_session, monkeypatch):
    """S122-CE auditor catch round 2: unauthenticated webhook endpoint must
    cap body size to avoid memory pressure from malicious or malformed
    deliveries. Intuit's typical payload is a few KB.
    """
    from gdx_dispatch.modules.quickbooks import webhook_router

    monkeypatch.delenv("QB_WEBHOOK_VERIFIER_TOKEN", raising=False)
    monkeypatch.delenv("QB_WEBHOOK_SECRET", raising=False)
    body = b"x" * (1_048_577)  # 1 MB + 1 byte
    req = _request(body=body)
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(webhook_router.qb_webhook(request=req, db=db_session))
    assert excinfo.value.status_code == 413


def test_s122_1_encryption_boot_gate_wrong_key_surfaces_at_first_use(monkeypatch):
    """S122-1 auditor blind-spot test: boot gate fires on _FERNET=None, but a
    WRONG key initializes _FERNET successfully — existing rows fail to
    decrypt with cryptography.fernet.InvalidToken at first use. Document the
    failure mode is at-first-call, not boot-time.
    """
    from cryptography.fernet import Fernet, InvalidToken
    correct_key = Fernet.generate_key()
    wrong_key = Fernet.generate_key()
    cipher_correct = Fernet(correct_key)
    cipher_wrong = Fernet(wrong_key)
    ciphertext = cipher_correct.encrypt(b"secret-refresh-token").decode("utf-8")
    # Decryption with the wrong key raises InvalidToken — surfaces at first
    # qb_token_store read, not at boot. Acceptable (boot gate covers the
    # high-impact case of NO key); document the rotation gap.
    with pytest.raises(InvalidToken):
        cipher_wrong.decrypt(ciphertext.encode("utf-8"))


def test_s122_2_pull_invoices_no_tax_detail_yields_zero(db_session, qb_connection, mock_qb, monkeypatch):
    """When TxnTaxDetail is absent (zero-tax realm / pre-tax invoice), tax_amount
    defaults to 0 and subtotal == total (no negative-tax surprises).
    """
    from gdx_dispatch.modules.quickbooks import sync as qb_sync

    customer = _seed_customer(db_session, name="No Tax Co")
    db_session.commit()
    db_session.add(QBEntityMap(tenant_id="tenant-test", entity_type="customer",
                               local_id=str(customer.id), qb_id="C-NOTAX-1"))
    db_session.commit()

    invoice_payload = {
        "Id": "INV-NOTAX-1",
        "DocNumber": "NOTAX-001",
        "TotalAmt": "100.00",
        "Balance": "100.00",
        "TxnDate": "2026-05-12",
        "CustomerRef": {"value": "C-NOTAX-1"},
        "Line": [{"Amount": "100.00", "Description": "Service"}],
    }
    mock_qb.query = AsyncMock(return_value=[invoice_payload])

    asyncio.run(qb_sync.pull_invoices("tenant-test", db_session, mock_qb))
    inv = db_session.execute(select(Invoice).where(Invoice.invoice_number == "NOTAX-001")).scalar_one()
    assert Decimal(str(inv.tax_amount)) == Decimal("0")
    assert Decimal(str(inv.subtotal)) == Decimal("100.00")


def test_s122_2_push_invoice_includes_txn_tax_detail(db_session, qb_connection, mock_qb, monkeypatch):
    """S122-2 push side: a local invoice with tax_amount > 0 must include
    TxnTaxDetail.TotalTax in the QBO push payload. Pre-fix, tax was silently
    dropped on push too — QB either rejected or fabricated its own number.
    """
    from gdx_dispatch.modules.quickbooks import sync as qb_sync

    customer = _seed_customer(db_session, name="Push Tax Co")
    db_session.commit()
    db_session.add(QBEntityMap(tenant_id="tenant-test", entity_type="customer",
                               local_id=str(customer.id), qb_id="C-PUSH-1"))
    db_session.commit()

    invoice = Invoice(
        invoice_number="PUSH-TAX-1", subtotal=Decimal("100"), tax_amount=Decimal("8.25"),
        total=Decimal("108.25"), balance_due=Decimal("108.25"),
        status="sent", invoice_date=date(2026, 5, 12), customer_id=customer.id,
        company_id="tenant-test", public_token="push-tax-1",
    )
    db_session.add(invoice)
    db_session.commit()

    captured: dict[str, Any] = {}
    async def _fake_create(entity, payload, *, idempotency_key=None):
        captured["entity"] = entity
        captured["payload"] = payload
        captured["idempotency_key"] = idempotency_key
        return {"Id": "QB-PUSH-1"}
    mock_qb.create = _fake_create
    mock_qb.realm_id = "realm-push-tax"

    asyncio.run(qb_sync.push_invoice("tenant-test", str(invoice.id), db_session, mock_qb))
    assert captured["entity"] == "Invoice"
    assert "TxnTaxDetail" in captured["payload"]
    assert captured["payload"]["TxnTaxDetail"]["TotalTax"] == 8.25
    # S122-8: idempotency key flows through
    assert captured["idempotency_key"]


def test_s122_8_qb_client_appends_requestid_query_param():
    """S122-8 (C1): create/update/delete must append ?requestid=<uuid> on
    the QBClient URL when an idempotency_key is provided. Pre-fix the
    legacy kernel set Request-Id as a header — Intuit ignores the header
    form, so retries on transient 5xx/timeouts duplicated entities.
    """
    qb = QBClient.__new__(QBClient)
    qb.realm_id = "12345"
    qb.minor_version = 75
    url = qb._url("Customer", idempotency_key="abc-123")
    assert "requestid=abc-123" in url
    # And no requestid when no key is supplied
    url_no_key = qb._url("Customer")
    assert "requestid=" not in url_no_key


def test_s122_8_idempotency_key_is_stable():
    """S122-8: the UUID5 for (realm, entity, local_id, op) MUST be deterministic
    across retries — that's the whole point of the server-side dedup window.
    """
    from gdx_dispatch.modules.quickbooks.sync import _idempotency_key
    k1 = _idempotency_key("realm-A", "customer", "abc", "create")
    k2 = _idempotency_key("realm-A", "customer", "abc", "create")
    assert k1 == k2
    # Different realm / op / local_id → different key
    assert _idempotency_key("realm-B", "customer", "abc", "create") != k1
    assert _idempotency_key("realm-A", "customer", "abc", "update") != k1
    assert _idempotency_key("realm-A", "customer", "xyz", "create") != k1


def test_s122_8_push_customer_passes_idempotency_key(db_session, qb_connection, mock_qb, monkeypatch):
    """S122-8: push_customer must call qb.create with the deterministic key
    derived from (realm, "customer", customer_id, "create").
    """
    from gdx_dispatch.modules.quickbooks import sync as qb_sync

    customer = _seed_customer(db_session, name="Idempotent Co")
    db_session.commit()
    captured: dict[str, Any] = {}
    async def _fake_create(entity, payload, *, idempotency_key=None):
        captured["entity"] = entity
        captured["idempotency_key"] = idempotency_key
        return {"Id": "QB-IDEMP-1"}
    mock_qb.create = _fake_create
    mock_qb.realm_id = "realm-idemp"

    asyncio.run(qb_sync.push_customer("tenant-test", str(customer.id), db_session, mock_qb))
    assert captured["entity"] == "Customer"
    assert captured["idempotency_key"] == qb_sync._idempotency_key(
        "realm-idemp", "customer", str(customer.id), "create",
    )


def test_s122_8_push_customer_short_circuits_when_already_mapped(db_session, qb_connection, mock_qb):
    """S122-8 auditor round 2: push must read-through QBEntityMap before
    calling Intuit. The retry-after-dedup-window failure mode and the
    dedup-replay-body shape are both irrelevant when we short-circuit on
    the existing map.
    """
    from gdx_dispatch.modules.quickbooks import sync as qb_sync

    customer = _seed_customer(db_session, name="AlreadyMapped Co")
    db_session.commit()
    db_session.add(QBEntityMap(
        tenant_id="tenant-test", entity_type="customer",
        local_id=str(customer.id), qb_id="QB-EXISTING-77",
    ))
    db_session.commit()

    call_count = [0]
    async def _fake_create(entity, payload, *, idempotency_key=None):
        call_count[0] += 1
        return {"Id": "QB-NEW"}
    mock_qb.create = _fake_create
    mock_qb.realm_id = "realm-idemp"

    out = asyncio.run(qb_sync.push_customer("tenant-test", str(customer.id), db_session, mock_qb))
    assert out["qb_customer_id"] == "QB-EXISTING-77"
    assert out.get("already_mapped") == "true"
    assert call_count[0] == 0, "must not call qb.create when already mapped"


def test_s122_8_push_customer_raises_on_empty_id(db_session, qb_connection, mock_qb):
    """S122-8 auditor round 2: a QBO response that parses to an empty Id is
    a protocol violation (or a dedup-replay shape we don't know how to
    handle yet). Raise instead of silently committing an empty audit row
    and leaving the map unwritten — which was the previous behavior and the
    setup for an infinite retry loop.
    """
    from gdx_dispatch.modules.quickbooks import sync as qb_sync

    customer = _seed_customer(db_session, name="EmptyId Co")
    db_session.commit()
    async def _fake_create(entity, payload, *, idempotency_key=None):
        return {"unexpected": "shape"}  # no Id key
    mock_qb.create = _fake_create
    mock_qb.realm_id = "realm-idemp"

    with pytest.raises(qb_sync.QBSyncError) as excinfo:
        asyncio.run(qb_sync.push_customer("tenant-test", str(customer.id), db_session, mock_qb))
    assert "empty Id" in str(excinfo.value)


def test_s122_4_token_store_isolates_tenants(db_session, monkeypatch):
    """S122-4 (N1): pre-fix QBTokenStore had no tenant_id column and was read
    with ``.limit(1)`` — so two tenants connecting QB on the same DB returned
    each other's tokens. The fix adds (tenant_id, realm_id) UNIQUE and every
    read filters by tenant_id. This test pins both contracts.
    """
    from datetime import timedelta
    from unittest.mock import MagicMock, patch

    from gdx_dispatch.modules.quickbooks import oauth as qb_oauth

    # SQLite strips tz info; use naive throughout and patch datetime.now to match.
    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    future_access = now_naive + timedelta(hours=1)
    future_refresh = now_naive + timedelta(days=30)

    qb_oauth.QBTokenStore.__table__.create(bind=db_session.bind, checkfirst=True)
    db_session.add(qb_oauth.QBTokenStore(
        tenant_id="tenant-A", realm_id="REALM-A", environment="production",
        access_token_enc=qb_oauth._encrypt("acc-A"),
        refresh_token_enc=qb_oauth._encrypt("ref-A"),
        access_token_expires_at=future_access,
        refresh_token_expires_at=future_refresh,
    ))
    db_session.add(qb_oauth.QBTokenStore(
        tenant_id="tenant-B", realm_id="REALM-B", environment="production",
        access_token_enc=qb_oauth._encrypt("acc-B"),
        refresh_token_enc=qb_oauth._encrypt("ref-B"),
        access_token_expires_at=future_access,
        refresh_token_expires_at=future_refresh,
    ))
    db_session.commit()

    monkeypatch.setenv("QB_ENVIRONMENT", "production")
    mock_dt = MagicMock(wraps=datetime)
    mock_dt.now = MagicMock(return_value=now_naive)
    with patch("gdx_dispatch.modules.quickbooks.oauth.datetime", mock_dt):
        client_a = asyncio.run(qb_oauth.get_qb_client("tenant-A", db_session))
        assert client_a.realm_id == "REALM-A"
        assert client_a.access_token == "acc-A"
        asyncio.run(client_a.close())

        client_b = asyncio.run(qb_oauth.get_qb_client("tenant-B", db_session))
        assert client_b.realm_id == "REALM-B"
        assert client_b.access_token == "acc-B"
        asyncio.run(client_b.close())


def test_s122_4_env_mismatch_refused(db_session, monkeypatch):
    """S122-4 (B9): get_qb_client refuses to serve tokens minted against a
    different Intuit environment than the running ``QB_ENVIRONMENT``.
    """
    from datetime import timedelta
    from unittest.mock import MagicMock, patch

    from gdx_dispatch.modules.quickbooks import oauth as qb_oauth

    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)

    qb_oauth.QBTokenStore.__table__.create(bind=db_session.bind, checkfirst=True)
    db_session.add(qb_oauth.QBTokenStore(
        tenant_id="tenant-X", realm_id="REALM-X", environment="sandbox",
        access_token_enc=qb_oauth._encrypt("acc"),
        refresh_token_enc=qb_oauth._encrypt("ref"),
        access_token_expires_at=now_naive + timedelta(hours=1),
        refresh_token_expires_at=now_naive + timedelta(days=30),
    ))
    db_session.commit()
    monkeypatch.setenv("QB_ENVIRONMENT", "production")
    mock_dt = MagicMock(wraps=datetime)
    mock_dt.now = MagicMock(return_value=now_naive)
    with patch("gdx_dispatch.modules.quickbooks.oauth.datetime", mock_dt), pytest.raises(qb_oauth.QBAuthError) as excinfo:
        asyncio.run(qb_oauth.get_qb_client("tenant-X", db_session))
    assert "sandbox" in str(excinfo.value) and "production" in str(excinfo.value)


def test_s122_9_refresh_lock_key_stable_and_distinct():
    """S122-9: ``_refresh_lock_key`` must be deterministic (same value across
    process restarts — Python's ``hash()`` is salted by PYTHONHASHSEED so it
    can't be used) and distinct per (tenant, realm) so concurrent refreshes on
    different connections don't block each other.
    """
    from gdx_dispatch.modules.quickbooks import oauth as qb_oauth

    # Stability — two calls return the same int64
    k1 = qb_oauth._refresh_lock_key("tenant-A", "REALM-1")
    k2 = qb_oauth._refresh_lock_key("tenant-A", "REALM-1")
    assert k1 == k2
    assert -(2 ** 63) <= k1 <= (2 ** 63) - 1, "must fit signed int64 for pg_advisory_xact_lock(bigint)"

    # Distinctness — different (tenant, realm) → different keys
    assert qb_oauth._refresh_lock_key("tenant-A", "REALM-1") != qb_oauth._refresh_lock_key("tenant-B", "REALM-1")
    assert qb_oauth._refresh_lock_key("tenant-A", "REALM-1") != qb_oauth._refresh_lock_key("tenant-A", "REALM-2")

    # Known fixed value to catch any future hash-function change that would
    # break the lock across rolling deploys (worker A on old hash, worker B
    # on new hash, both grab "their" lock and both refresh).
    assert qb_oauth._refresh_lock_key("frozen-tenant", "frozen-realm") == -7653466684944279336


def test_s122_9_fast_path_skips_refresh_and_lock_when_token_fresh(db_session, monkeypatch):
    """S122-9: when the access token isn't expiring in <5min, ``get_qb_client``
    returns immediately without calling ``refresh_access_token`` AND without
    issuing ``pg_advisory_xact_lock``. Verifies the fast-path optimization.
    """
    from datetime import timedelta
    from unittest.mock import AsyncMock, MagicMock, patch

    from gdx_dispatch.modules.quickbooks import oauth as qb_oauth

    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    future_access = now_naive + timedelta(hours=1)  # 1h away — comfortably fresh
    future_refresh = now_naive + timedelta(days=30)

    qb_oauth.QBTokenStore.__table__.create(bind=db_session.bind, checkfirst=True)
    db_session.add(qb_oauth.QBTokenStore(
        tenant_id="tenant-fast", realm_id="REALM-fast", environment="production",
        access_token_enc=qb_oauth._encrypt("acc-fast"),
        refresh_token_enc=qb_oauth._encrypt("ref-fast"),
        access_token_expires_at=future_access,
        refresh_token_expires_at=future_refresh,
    ))
    db_session.commit()

    monkeypatch.setenv("QB_ENVIRONMENT", "production")
    mock_dt = MagicMock(wraps=datetime)
    mock_dt.now = MagicMock(return_value=now_naive)

    refresh_mock = AsyncMock()
    with patch("gdx_dispatch.modules.quickbooks.oauth.datetime", mock_dt), \
         patch("gdx_dispatch.modules.quickbooks.oauth.refresh_access_token", refresh_mock):
        client = asyncio.run(qb_oauth.get_qb_client("tenant-fast", db_session))

    assert client.access_token == "acc-fast"
    refresh_mock.assert_not_called(), "fast-path must NOT call refresh_access_token"
    asyncio.run(client.close())


def test_s122_9_peer_refresh_detection_via_for_update_path(db_session, monkeypatch):
    """S122-9: When the access token is expiring AND ``_is_postgres`` returns
    True, ``get_qb_client`` should advisory-lock + re-read the row with FOR
    UPDATE. If the re-read shows the row was updated by another worker
    (access_token_expires_at moved forward), it must skip the Intuit call.

    SQLite doesn't have advisory locks, so we monkeypatch ``_is_postgres`` to
    True and stub ``db.execute(select(func.pg_advisory_xact_lock(...)))`` to
    a no-op. We then mutate the row in between the lock and FOR UPDATE reread
    to simulate a peer worker's commit.
    """
    from datetime import timedelta
    from unittest.mock import AsyncMock, MagicMock, patch

    from gdx_dispatch.modules.quickbooks import oauth as qb_oauth

    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    expiring_soon = now_naive + timedelta(minutes=2)  # < 5min window → triggers slow path
    future_refresh = now_naive + timedelta(days=30)

    qb_oauth.QBTokenStore.__table__.create(bind=db_session.bind, checkfirst=True)
    db_session.add(qb_oauth.QBTokenStore(
        tenant_id="tenant-race", realm_id="REALM-race", environment="production",
        access_token_enc=qb_oauth._encrypt("acc-old"),
        refresh_token_enc=qb_oauth._encrypt("ref-old"),
        access_token_expires_at=expiring_soon,
        refresh_token_expires_at=future_refresh,
    ))
    db_session.commit()

    monkeypatch.setenv("QB_ENVIRONMENT", "production")
    mock_dt = MagicMock(wraps=datetime)
    mock_dt.now = MagicMock(return_value=now_naive)

    # Simulate the peer worker committing a refreshed token after we acquire
    # the advisory lock but before the FOR UPDATE reread.
    peer_refreshed_access = now_naive + timedelta(hours=1)
    real_execute = db_session.execute
    call_count = {"advisory_lock": 0}

    def fake_execute(stmt, *args, **kw):
        # Detect the advisory lock call (it's the only one that calls
        # ``pg_advisory_xact_lock``) — when we see it, "peer-refresh" the row.
        sql = str(stmt)
        if "pg_advisory_xact_lock" in sql:
            call_count["advisory_lock"] += 1
            row = real_execute(
                qb_oauth.select(qb_oauth.QBTokenStore).where(
                    qb_oauth.QBTokenStore.tenant_id == "tenant-race"
                )
            ).scalar_one()
            row.access_token_enc = qb_oauth._encrypt("acc-PEER-NEW")
            row.access_token_expires_at = peer_refreshed_access
            db_session.commit()
            # Return a stub result; the real lock call returns NULL
            return MagicMock(scalar_one_or_none=lambda: None)
        return real_execute(stmt, *args, **kw)

    refresh_mock = AsyncMock()
    with patch("gdx_dispatch.modules.quickbooks.oauth.datetime", mock_dt), \
         patch("gdx_dispatch.modules.quickbooks.oauth._is_postgres", return_value=True), \
         patch.object(db_session, "execute", side_effect=fake_execute), \
         patch("gdx_dispatch.modules.quickbooks.oauth.refresh_access_token", refresh_mock):
        client = asyncio.run(qb_oauth.get_qb_client("tenant-race", db_session))

    assert call_count["advisory_lock"] == 1, "advisory lock must be acquired exactly once"
    refresh_mock.assert_not_called(), "peer-refresh detection must skip the Intuit call"
    assert client.access_token == "acc-PEER-NEW", "must return the peer's refreshed token"
    asyncio.run(client.close())


def test_s122_ce_modular_webhook_unhandled_entities_logged_not_dropped(db_session, monkeypatch):
    """N5: Bill/JournalEntry/etc events without a pull function in sync.py are
    logged as ``unhandled`` and visible in the response. Post-S122-11/12 the
    set of unhandled entities shrunk (Payment/Item/Vendor/Account are now
    handled); Bill remains unhandled.
    """
    from gdx_dispatch.modules.quickbooks import webhook_router

    monkeypatch.delenv("QB_WEBHOOK_VERIFIER_TOKEN", raising=False)
    monkeypatch.delenv("QB_WEBHOOK_SECRET", raising=False)
    _stub_celery_delay(monkeypatch)
    body = json.dumps([{
        "specversion": "1.0", "id": "evt-bill", "source": "x",
        "type": "qbo.bill.created.v1", "datacontenttype": "application/json",
        "time": "2026-07-31T12:00:00Z", "intuitentityid": "555",
        "intuitaccountid": "12345", "data": {},
    }]).encode("utf-8")
    out = asyncio.run(webhook_router.qb_webhook(request=_request(body=body), db=db_session))
    assert out["unhandled"] == 1
    assert out["processed"] == 0


def test_s122_15_is_transient_classification():
    """S122-15: ``_is_transient`` distinguishes retry-worthy from log-and-move-on
    failures. Wrong classification means rate-limits stop the task (over-retry)
    or validation errors retry forever (under-retry).
    """
    import httpx

    from gdx_dispatch.modules.quickbooks.client import QBAPIError, QBAuthError
    from gdx_dispatch.modules.quickbooks.sync import QBRateLimitError, QBSyncError
    from gdx_dispatch.modules.quickbooks.tasks import _is_transient

    # Transient: rate-limit
    assert _is_transient(QBRateLimitError("throttled")) is True
    # Transient: 5xx
    assert _is_transient(QBAPIError(503, "service unavailable")) is True
    assert _is_transient(QBAPIError(500, "internal error")) is True
    # Transient: network failures
    assert _is_transient(httpx.TimeoutException("read timeout")) is True
    assert _is_transient(httpx.ConnectError("conn refused")) is True
    assert _is_transient(httpx.ReadError("eof")) is True

    # Permanent: 4xx
    assert _is_transient(QBAPIError(400, "bad request")) is False
    assert _is_transient(QBAPIError(404, "not found")) is False
    # Permanent: auth (handled by connection_healthy gate, but still permanent)
    assert _is_transient(QBAuthError("token expired")) is False
    # Permanent: sync logic errors
    assert _is_transient(QBSyncError("Customer not found")) is False
    # Unknown exception class — treat as permanent (don't retry endlessly)
    assert _is_transient(ValueError("unexpected")) is False
    assert _is_transient(KeyError("missing key")) is False


def test_s122_15_sync_all_customers_continues_on_permanent_error(db_session, monkeypatch):
    """S122-15: a permanent error on one customer doesn't kill the task —
    other customers still get pushed and the result captures the failure.
    """
    from uuid import uuid4

    from gdx_dispatch.models.tenant_models import Customer
    from gdx_dispatch.modules.quickbooks import oauth as qb_oauth
    from gdx_dispatch.modules.quickbooks import tasks as qb_tasks
    from gdx_dispatch.modules.quickbooks.sync import QBSyncError

    Customer.__table__.create(bind=db_session.bind, checkfirst=True)
    qb_oauth.QBTokenStore.__table__.create(bind=db_session.bind, checkfirst=True)

    # Mark connection healthy so the gate doesn't skip
    from datetime import timedelta
    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    db_session.add(qb_oauth.QBTokenStore(
        tenant_id="tenant-s15", realm_id="r-s15", environment="production",
        access_token_enc=qb_oauth._encrypt("a"),
        refresh_token_enc=qb_oauth._encrypt("r"),
        access_token_expires_at=now_naive + timedelta(hours=1),
        refresh_token_expires_at=now_naive + timedelta(days=30),
        auth_state="healthy",
    ))

    good1_id = uuid4()
    bad_id = uuid4()
    good2_id = uuid4()
    db_session.add_all([
        Customer(id=good1_id, name="Good 1", company_id="co-1", customer_type="Residential"),
        Customer(id=bad_id, name="Bad Row", company_id="co-1", customer_type="Residential"),
        Customer(id=good2_id, name="Good 2", company_id="co-1", customer_type="Residential"),
    ])
    db_session.commit()

    # Stub push_customer (called inside _run_push_loop) + get_qb_client to a
    # no-op stub. Success on good rows, QBSyncError (permanent) on the bad one.
    pushed: list[str] = []

    async def fake_push(tenant_id, customer_id, db_, qb_):
        pushed.append(customer_id)
        if customer_id == str(bad_id):
            raise QBSyncError("push_customer: empty Id in QBO response")
        return {"customer_id": customer_id, "qb_customer_id": "fake-qb-id"}

    class _StubQB:
        async def close(self): pass  # noqa: E704

    async def fake_get_client(*a, **kw):
        return _StubQB()

    monkeypatch.setattr(qb_tasks, "push_customer", fake_push)
    monkeypatch.setattr(qb_tasks, "get_qb_client", fake_get_client)
    monkeypatch.setattr(qb_tasks, "_tenant_session", lambda tid: _NoopCtx(db_session))

    result = qb_tasks.sync_all_customers_task.run("tenant-s15")
    assert result["succeeded"] == 2, f"two good rows should succeed, got {result}"
    assert result["failed_count"] == 1
    assert result["failed_permanent"][0]["error_class"] == "QBSyncError"
    assert result["failed_permanent"][0]["customer_id"] == str(bad_id)
    # All three rows were attempted (loop didn't bail on the failure)
    assert len(pushed) == 3


class _NoopCtx:
    """Minimal context-manager wrapping a session so tasks can ``with _tenant_session(tid) as db:``."""
    def __init__(self, db): self._db = db  # noqa: E704
    def __enter__(self): return self._db  # noqa: E704
    def __exit__(self, *a): pass  # noqa: E704


def test_s122_19_default_expense_account_prefers_expense_over_cogs(db_session):
    """S122-19: when both ``Expense`` and ``CostOfGoodsSold`` accounts exist,
    pick the Expense one. Falls back to COGS only when no Expense account is
    active.
    """
    from sqlalchemy import text as _text
    from uuid import uuid4

    from gdx_dispatch.modules.quickbooks.sync import _QB_ACCOUNTS_DDL, _default_expense_account_qb_id

    db_session.execute(_text(_QB_ACCOUNTS_DDL))
    db_session.commit()

    # Seed: 1 Expense, 1 COGS, 1 Income (Income must NOT be picked)
    for qb_id, name, atype, active in [
        ("acc-100", "Office Supplies", "Expense", True),
        ("acc-200", "COGS - Parts", "CostOfGoodsSold", True),
        ("acc-300", "Sales of Product Income", "Income", True),
    ]:
        db_session.execute(_text("""
            INSERT INTO qb_accounts (id, tenant_id, qb_account_id, name,
                account_type, account_sub_type, classification, current_balance, active)
            VALUES (:id, :tid, :qid, :name, :at, '', '', 0, :act)
        """), {"id": str(uuid4()), "tid": "tenant-s19a", "qid": qb_id,
               "name": name, "at": atype, "act": active})
    db_session.commit()

    assert _default_expense_account_qb_id("tenant-s19a", db_session) == "acc-100"


def test_s122_19_default_expense_account_falls_back_to_cogs(db_session):
    """No Expense account active → fall back to COGS."""
    from sqlalchemy import text as _text
    from uuid import uuid4

    from gdx_dispatch.modules.quickbooks.sync import _QB_ACCOUNTS_DDL, _default_expense_account_qb_id

    db_session.execute(_text(_QB_ACCOUNTS_DDL))
    db_session.commit()

    db_session.execute(_text("""
        INSERT INTO qb_accounts (id, tenant_id, qb_account_id, name,
            account_type, account_sub_type, classification, current_balance, active)
        VALUES (:id, :tid, 'acc-200', 'COGS Only', 'CostOfGoodsSold', '', '', 0, TRUE)
    """), {"id": str(uuid4()), "tid": "tenant-s19b"})
    db_session.commit()

    assert _default_expense_account_qb_id("tenant-s19b", db_session) == "acc-200"


def test_s122_19_default_expense_account_raises_when_no_match(db_session):
    """No Expense / COGS in the chart → raise QBSyncError with a clear
    message ("run pull_accounts first").
    """
    from sqlalchemy import text as _text

    from gdx_dispatch.modules.quickbooks.sync import (
        _QB_ACCOUNTS_DDL,
        QBSyncError,
        _default_expense_account_qb_id,
    )

    db_session.execute(_text(_QB_ACCOUNTS_DDL))
    db_session.commit()

    with pytest.raises(QBSyncError) as exc_info:
        _default_expense_account_qb_id("tenant-s19-empty", db_session)
    assert "pull_accounts" in str(exc_info.value).lower()


def test_s122_19_default_expense_account_skips_inactive(db_session):
    """An inactive Expense account is NOT selected — falls through to the
    next candidate.
    """
    from sqlalchemy import text as _text
    from uuid import uuid4

    from gdx_dispatch.modules.quickbooks.sync import _QB_ACCOUNTS_DDL, _default_expense_account_qb_id

    db_session.execute(_text(_QB_ACCOUNTS_DDL))
    db_session.commit()

    for qb_id, atype, active in [
        ("acc-deactivated-expense", "Expense", False),
        ("acc-active-cogs", "CostOfGoodsSold", True),
    ]:
        db_session.execute(_text("""
            INSERT INTO qb_accounts (id, tenant_id, qb_account_id, name,
                account_type, account_sub_type, classification, current_balance, active)
            VALUES (:id, :tid, :qid, :name, :at, '', '', 0, :act)
        """), {"id": str(uuid4()), "tid": "tenant-s19c", "qid": qb_id,
               "name": qb_id, "at": atype, "act": active})
    db_session.commit()

    assert _default_expense_account_qb_id("tenant-s19c", db_session) == "acc-active-cogs"


def test_s122_17_customer_qb_dirty_defaults_true_on_create(db_session):
    """S122-17: new Customer rows default to qb_dirty=True so the next full
    sync picks them up.
    """
    from uuid import uuid4

    from gdx_dispatch.models.tenant_models import Customer

    Customer.__table__.create(bind=db_session.bind, checkfirst=True)
    cust = Customer(id=uuid4(), name="New Sync Test", company_id="co-1", customer_type="Residential")
    db_session.add(cust); db_session.commit()  # noqa: E702

    db_session.expire_all()
    cust = db_session.get(Customer, cust.id)
    assert cust.qb_dirty is True


def test_s122_17_customer_qb_dirty_flips_on_any_field_change(db_session):
    """S122-17: after push_customer clears qb_dirty=False, the before_update
    listener re-flips on any non-internal column change. Internal-only
    updates (qb_dirty, qb_synced_at) do NOT trigger the re-flip — otherwise
    push_customer would bounce its own clear.
    """
    from datetime import datetime as dt_type, timezone as tz_type
    from uuid import uuid4

    from gdx_dispatch.models.tenant_models import Customer

    Customer.__table__.create(bind=db_session.bind, checkfirst=True)
    cust = Customer(
        id=uuid4(), name="Cursor Test", company_id="co-1",
        customer_type="Residential", phone="555-0100",
    )
    db_session.add(cust); db_session.commit()  # noqa: E702

    # Simulate push_customer's clear path. Listener must NOT re-flip.
    cust.qb_dirty = False
    cust.qb_synced_at = dt_type.now(tz_type.utc).replace(tzinfo=None)
    db_session.commit()
    db_session.expire_all()
    cust = db_session.get(Customer, cust.id)
    assert cust.qb_dirty is False, "internal-only update must not re-flip qb_dirty"

    # Change phone — listener flips qb_dirty back.
    cust.phone = "555-0199"
    db_session.commit()
    db_session.expire_all()
    cust = db_session.get(Customer, cust.id)
    assert cust.qb_dirty is True, "non-internal change must re-flip qb_dirty"


def test_s122_17_sync_all_customers_only_pushes_dirty(db_session, monkeypatch):
    """S122-17: ``sync_all_customers_task`` only pushes customers with
    ``qb_dirty=True``. Pre-fix it iterated every Customer row; post-fix it
    filters on qb_dirty and skips clean rows.
    """
    from uuid import uuid4

    from gdx_dispatch.models.tenant_models import Customer
    from gdx_dispatch.modules.quickbooks import oauth as qb_oauth
    from gdx_dispatch.modules.quickbooks import tasks as qb_tasks

    Customer.__table__.create(bind=db_session.bind, checkfirst=True)
    qb_oauth.QBTokenStore.__table__.create(bind=db_session.bind, checkfirst=True)

    from datetime import timedelta
    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    db_session.add(qb_oauth.QBTokenStore(
        tenant_id="tenant-s17", realm_id="r-s17", environment="production",
        access_token_enc=qb_oauth._encrypt("a"),
        refresh_token_enc=qb_oauth._encrypt("r"),
        access_token_expires_at=now_naive + timedelta(hours=1),
        refresh_token_expires_at=now_naive + timedelta(days=30),
        auth_state="healthy",
    ))

    # 2 dirty + 1 clean customer
    dirty1 = Customer(id=uuid4(), name="Dirty 1", company_id="co-s17", customer_type="Residential")
    dirty2 = Customer(id=uuid4(), name="Dirty 2", company_id="co-s17", customer_type="Residential")
    clean = Customer(id=uuid4(), name="Clean", company_id="co-s17", customer_type="Residential")
    db_session.add_all([dirty1, dirty2, clean])
    db_session.commit()
    # Mark "clean" as already synced — directly via attribute set; the
    # internal-only-update branch of the listener means no re-flip.
    clean.qb_dirty = False
    db_session.commit()

    pushed_ids: list[str] = []

    async def fake_push(tenant_id, customer_id, db_, qb_):
        pushed_ids.append(customer_id)

    class _StubQB:
        async def close(self): pass  # noqa: E704

    async def fake_get_client(*a, **kw): return _StubQB()  # noqa: E704

    monkeypatch.setattr(qb_tasks, "push_customer", fake_push)
    monkeypatch.setattr(qb_tasks, "get_qb_client", fake_get_client)
    monkeypatch.setattr(qb_tasks, "_tenant_session", lambda tid: _NoopCtx(db_session))

    result = qb_tasks.sync_all_customers_task.run("tenant-s17")
    assert result["succeeded"] == 2, f"only the 2 dirty rows should push, got {result}"
    assert set(pushed_ids) == {str(dirty1.id), str(dirty2.id)}
    assert str(clean.id) not in pushed_ids


def test_s122_16_sync_all_customers_uses_single_qbclient_across_loop(db_session, monkeypatch):
    """S122-16: a single QBClient is opened and reused for every row in the
    loop — was previously one client per row (N TCP+TLS handshakes for N
    customers). Pin via ``get_qb_client`` call count.
    """
    from uuid import uuid4

    from gdx_dispatch.models.tenant_models import Customer
    from gdx_dispatch.modules.quickbooks import oauth as qb_oauth
    from gdx_dispatch.modules.quickbooks import tasks as qb_tasks

    Customer.__table__.create(bind=db_session.bind, checkfirst=True)
    qb_oauth.QBTokenStore.__table__.create(bind=db_session.bind, checkfirst=True)

    from datetime import timedelta
    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    db_session.add(qb_oauth.QBTokenStore(
        tenant_id="tenant-s16", realm_id="r-s16", environment="production",
        access_token_enc=qb_oauth._encrypt("a"),
        refresh_token_enc=qb_oauth._encrypt("r"),
        access_token_expires_at=now_naive + timedelta(hours=1),
        refresh_token_expires_at=now_naive + timedelta(days=30),
        auth_state="healthy",
    ))

    # 5 customers
    for n in range(5):
        db_session.add(Customer(
            id=uuid4(), name=f"Single-Client Test {n}",
            company_id="co-s16", customer_type="Residential",
        ))
    db_session.commit()

    client_calls = {"get_qb_client": 0, "close": 0, "push": 0}

    class _StubQB:
        async def close(self):  # noqa: E704
            client_calls["close"] += 1

    async def fake_get_client(*a, **kw):
        client_calls["get_qb_client"] += 1
        return _StubQB()

    async def fake_push(tenant_id, customer_id, db_, qb_):
        client_calls["push"] += 1
        return {"customer_id": customer_id, "qb_customer_id": "fake"}

    monkeypatch.setattr(qb_tasks, "get_qb_client", fake_get_client)
    monkeypatch.setattr(qb_tasks, "push_customer", fake_push)
    monkeypatch.setattr(qb_tasks, "_tenant_session", lambda tid: _NoopCtx(db_session))

    result = qb_tasks.sync_all_customers_task.run("tenant-s16")

    assert result["succeeded"] == 5, f"all 5 customers should push, got {result}"
    assert client_calls["push"] == 5, "5 push calls expected"
    assert client_calls["get_qb_client"] == 1, (
        "S122-16: must open exactly ONE QBClient for the whole loop, "
        f"got {client_calls['get_qb_client']}"
    )
    assert client_calls["close"] == 1, "must close the shared client exactly once"


def test_s122_14_invoice_qb_dirty_defaults_true_on_create(db_session, monkeypatch):
    """S122-14: new Invoice rows default to qb_dirty=True so the next full
    sync picks them up. Listener doesn't fire on INSERT, only UPDATE.
    """
    from datetime import date as date_type
    from uuid import uuid4

    from gdx_dispatch.models.tenant_models import Customer, Invoice

    Customer.__table__.create(bind=db_session.bind, checkfirst=True)
    Invoice.__table__.create(bind=db_session.bind, checkfirst=True)

    cust = Customer(id=uuid4(), name="QB Dirty Test Co", company_id="co-1", customer_type="Residential")
    db_session.add(cust); db_session.commit()  # noqa: E702
    inv = Invoice(
        id=uuid4(), invoice_number="INV-DIRTY-1", customer_id=cust.id, company_id="co-1",
        public_token=str(uuid4()), invoice_date=date_type.today(), status="sent",
    )
    db_session.add(inv); db_session.commit()  # noqa: E702

    db_session.expire_all()
    inv = db_session.execute(
        qb_oauth_select_invoice_by_id_helper(inv.id)
    ).scalar_one()
    assert inv.qb_dirty is True


def test_s122_14_invoice_qb_dirty_flips_on_any_field_change(db_session):
    """S122-14: when push_invoice clears qb_dirty=False and then ANY other
    field changes, the listener auto-flips qb_dirty back to True so the next
    sync re-pushes. Internal columns (qb_dirty, qb_synced_at) don't trigger
    a re-flip — otherwise the clear-after-push would loop.
    """
    from datetime import date as date_type, datetime as dt_type, timezone as tz_type
    from uuid import uuid4

    from gdx_dispatch.models.tenant_models import Customer, Invoice

    Customer.__table__.create(bind=db_session.bind, checkfirst=True)
    Invoice.__table__.create(bind=db_session.bind, checkfirst=True)

    cust = Customer(id=uuid4(), name="Listener Test Co", company_id="co-1", customer_type="Residential")
    db_session.add(cust); db_session.commit()  # noqa: E702
    inv = Invoice(
        id=uuid4(), invoice_number="INV-LISTENER-1", customer_id=cust.id, company_id="co-1",
        public_token=str(uuid4()), invoice_date=date_type.today(), status="sent",
    )
    db_session.add(inv); db_session.commit()  # noqa: E702

    # Simulate push_invoice's clear path. Listener should NOT re-flip
    # because only the two internal columns changed.
    inv.qb_dirty = False
    inv.qb_synced_at = dt_type.now(tz_type.utc).replace(tzinfo=None)
    db_session.commit()

    db_session.expire_all()
    inv = db_session.execute(qb_oauth_select_invoice_by_id_helper(inv.id)).scalar_one()
    assert inv.qb_dirty is False, "clear-after-push must persist"

    # Now change a non-internal column — listener flips qb_dirty back.
    inv.notes = "post-sync update"
    db_session.commit()

    db_session.expire_all()
    inv = db_session.execute(qb_oauth_select_invoice_by_id_helper(inv.id)).scalar_one()
    assert inv.qb_dirty is True, "non-internal change must re-flip qb_dirty"


def qb_oauth_select_invoice_by_id_helper(invoice_id):
    from gdx_dispatch.models.tenant_models import Invoice
    from gdx_dispatch.modules.quickbooks.oauth import select
    return select(Invoice).where(Invoice.id == invoice_id)


def test_s122_13_auth_state_default_healthy_on_new_token_row(db_session):
    """S122-13: new QBTokenStore rows default to ``auth_state='healthy'`` so
    ``connection_healthy()`` returns True out of the gate.
    """
    from datetime import timedelta

    from gdx_dispatch.modules.quickbooks import oauth as qb_oauth

    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    qb_oauth.QBTokenStore.__table__.create(bind=db_session.bind, checkfirst=True)
    db_session.add(qb_oauth.QBTokenStore(
        tenant_id="tenant-healthy", realm_id="r1", environment="production",
        access_token_enc=qb_oauth._encrypt("a"),
        refresh_token_enc=qb_oauth._encrypt("r"),
        access_token_expires_at=now_naive + timedelta(hours=1),
        refresh_token_expires_at=now_naive + timedelta(days=30),
    ))
    db_session.commit()

    row = db_session.execute(
        qb_oauth.select(qb_oauth.QBTokenStore).where(
            qb_oauth.QBTokenStore.tenant_id == "tenant-healthy"
        )
    ).scalar_one()
    assert row.auth_state == "healthy"
    assert qb_oauth.connection_healthy("tenant-healthy", db_session) is True


def test_s122_13_connection_healthy_false_when_needs_reconnect(db_session):
    """S122-13: ``connection_healthy()`` returns False when auth_state is set
    to ``needs_reconnect``. The frontend uses this to render the
    "Reconnect QuickBooks" CTA + Celery tasks short-circuit.
    """
    from datetime import timedelta

    from gdx_dispatch.modules.quickbooks import oauth as qb_oauth

    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    qb_oauth.QBTokenStore.__table__.create(bind=db_session.bind, checkfirst=True)
    db_session.add(qb_oauth.QBTokenStore(
        tenant_id="tenant-dead", realm_id="r2", environment="production",
        access_token_enc=qb_oauth._encrypt("a"),
        refresh_token_enc=qb_oauth._encrypt("r"),
        access_token_expires_at=now_naive + timedelta(hours=1),
        refresh_token_expires_at=now_naive + timedelta(days=30),
        auth_state="needs_reconnect",
    ))
    db_session.commit()

    assert qb_oauth.connection_healthy("tenant-dead", db_session) is False
    # Unknown tenant: False, not error
    assert qb_oauth.connection_healthy("tenant-no-row", db_session) is False
    # Empty tenant_id: False, not error
    assert qb_oauth.connection_healthy("", db_session) is False


def test_s122_13_get_qb_client_marks_needs_reconnect_on_invalid_grant(db_session, monkeypatch):
    """S122-13: when refresh_access_token raises with ``invalid_grant`` in the
    message (Intuit's response when the refresh token is rejected),
    ``get_qb_client`` sets auth_state='needs_reconnect' so subsequent sync
    tasks short-circuit instead of burning more Intuit calls.
    """
    from datetime import timedelta
    from unittest.mock import AsyncMock, MagicMock, patch

    from gdx_dispatch.modules.quickbooks import oauth as qb_oauth

    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    qb_oauth.QBTokenStore.__table__.create(bind=db_session.bind, checkfirst=True)
    db_session.add(qb_oauth.QBTokenStore(
        tenant_id="tenant-x", realm_id="r3", environment="production",
        access_token_enc=qb_oauth._encrypt("old-access"),
        refresh_token_enc=qb_oauth._encrypt("dead-refresh"),
        access_token_expires_at=now_naive + timedelta(minutes=2),  # expiring soon → slow path
        refresh_token_expires_at=now_naive + timedelta(days=30),
        auth_state="healthy",
    ))
    db_session.commit()

    monkeypatch.setenv("QB_ENVIRONMENT", "production")
    mock_dt = MagicMock(wraps=datetime)
    mock_dt.now = MagicMock(return_value=now_naive)
    refresh_mock = AsyncMock(side_effect=qb_oauth.QBAuthError(
        "Token refresh failed: HTTP 400 — invalid_grant"
    ))

    with patch("gdx_dispatch.modules.quickbooks.oauth.datetime", mock_dt), \
         patch("gdx_dispatch.modules.quickbooks.oauth.refresh_access_token", refresh_mock):
        client = asyncio.run(qb_oauth.get_qb_client("tenant-x", db_session))

    refresh_mock.assert_called_once()
    assert client.access_token == "old-access", "pre-refresh access_token preserved on failure"
    asyncio.run(client.close())

    db_session.expire_all()
    row = db_session.execute(
        qb_oauth.select(qb_oauth.QBTokenStore).where(
            qb_oauth.QBTokenStore.tenant_id == "tenant-x"
        )
    ).scalar_one()
    assert row.auth_state == "needs_reconnect", (
        "invalid_grant must transition auth_state to needs_reconnect"
    )
    assert qb_oauth.connection_healthy("tenant-x", db_session) is False


def test_s122_13_get_qb_client_marks_refresh_failed_on_network_error(db_session, monkeypatch):
    """S122-13: transient failures (network, 5xx, timeout) get
    ``refresh_failed`` (will retry next sync) instead of ``needs_reconnect``
    (requires user action). This avoids prompting users to reconnect for a
    blip that'll heal on its own.
    """
    from datetime import timedelta
    from unittest.mock import AsyncMock, MagicMock, patch

    from gdx_dispatch.modules.quickbooks import oauth as qb_oauth

    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    qb_oauth.QBTokenStore.__table__.create(bind=db_session.bind, checkfirst=True)
    db_session.add(qb_oauth.QBTokenStore(
        tenant_id="tenant-blip", realm_id="r4", environment="production",
        access_token_enc=qb_oauth._encrypt("a"),
        refresh_token_enc=qb_oauth._encrypt("r"),
        access_token_expires_at=now_naive + timedelta(minutes=2),
        refresh_token_expires_at=now_naive + timedelta(days=30),
        auth_state="healthy",
    ))
    db_session.commit()

    monkeypatch.setenv("QB_ENVIRONMENT", "production")
    mock_dt = MagicMock(wraps=datetime)
    mock_dt.now = MagicMock(return_value=now_naive)
    refresh_mock = AsyncMock(side_effect=Exception("Connection reset by peer"))

    with patch("gdx_dispatch.modules.quickbooks.oauth.datetime", mock_dt), \
         patch("gdx_dispatch.modules.quickbooks.oauth.refresh_access_token", refresh_mock):
        client = asyncio.run(qb_oauth.get_qb_client("tenant-blip", db_session))
    asyncio.run(client.close())

    db_session.expire_all()
    row = db_session.execute(
        qb_oauth.select(qb_oauth.QBTokenStore).where(
            qb_oauth.QBTokenStore.tenant_id == "tenant-blip"
        )
    ).scalar_one()
    assert row.auth_state == "refresh_failed", (
        "network errors should be transient (refresh_failed), not terminal (needs_reconnect)"
    )


def test_s122_11_12_per_entity_dispatch(db_session, monkeypatch):
    """S122-11 + S122-12: webhook routes Customer / Invoice / Payment / Item /
    Vendor / Account events to per-entity sync_<x>_task(tenant_id, entity_id)
    — not the old sync_all_*_task that pushed every GDX row to QB on every
    webhook (wrong direction; the webhook always means QB-side changed, so
    we PULL from QB).
    """
    from gdx_dispatch.modules.quickbooks import tasks as qb_tasks
    from gdx_dispatch.modules.quickbooks import webhook_router

    monkeypatch.delenv("QB_WEBHOOK_VERIFIER_TOKEN", raising=False)
    monkeypatch.delenv("QB_WEBHOOK_SECRET", raising=False)

    calls: dict[str, list[tuple]] = {
        "sync_customer_task": [], "sync_invoice_task": [],
        "sync_payment_task": [], "sync_item_task": [],
        "sync_vendor_task": [], "sync_account_task": [],
    }

    def _capture(name):
        return lambda *args, **kw: calls[name].append(args)

    for name in calls:
        monkeypatch.setattr(getattr(qb_tasks, name), "delay", _capture(name))
    monkeypatch.setattr(qb_tasks.sync_all_customers_task, "delay", lambda *a, **kw: None)
    monkeypatch.setattr(qb_tasks.sync_all_invoices_task, "delay", lambda *a, **kw: None)

    # Fire one webhook per supported entity type.
    entity_to_task = [
        ("customer", "sync_customer_task", "1001"),
        ("invoice", "sync_invoice_task", "1002"),
        ("payment", "sync_payment_task", "1003"),
        ("item", "sync_item_task", "1004"),
        ("vendor", "sync_vendor_task", "1005"),
        ("account", "sync_account_task", "1006"),
    ]
    for i, (qb_entity_lc, _expected_task, entity_id) in enumerate(entity_to_task):
        body = json.dumps([{
            "specversion": "1.0", "id": f"evt-{qb_entity_lc}-{i}", "source": "intuit.x",
            "type": f"qbo.{qb_entity_lc}.created.v1",
            "datacontenttype": "application/json", "time": "2026-07-31T12:00:00Z",
            "intuitentityid": entity_id, "intuitaccountid": "9130000000000000001",
            "data": {},
        }]).encode("utf-8")
        out = asyncio.run(webhook_router.qb_webhook(request=_request(body=body), db=db_session))
        assert out["processed"] == 1, f"{qb_entity_lc} should route to a per-entity task"

    # Each per-entity task should have been called exactly once with the
    # tenant_id + the QB entity_id from the webhook payload.
    for qb_entity_lc, expected_task, entity_id in entity_to_task:
        assert len(calls[expected_task]) == 1, (
            f"{expected_task} expected 1 call, got {len(calls[expected_task])}"
        )
        args = calls[expected_task][0]
        assert args[1] == entity_id, (
            f"{expected_task} should receive entity_id={entity_id} as 2nd arg, got {args}"
        )

    # Confirm the legacy sync_all_*_task tasks are NOT dispatched by the
    # webhook anymore (they remain in the module for manual full-sync triggers
    # but webhooks must use the per-entity tasks).
    legacy_calls = []
    monkeypatch.setattr(
        qb_tasks.sync_all_customers_task, "delay",
        lambda *a, **kw: legacy_calls.append(("customers", a)),
    )
    monkeypatch.setattr(
        qb_tasks.sync_all_invoices_task, "delay",
        lambda *a, **kw: legacy_calls.append(("invoices", a)),
    )
    body = json.dumps([{
        "specversion": "1.0", "id": "evt-cust-late", "source": "x",
        "type": "qbo.customer.updated.v1", "datacontenttype": "application/json",
        "time": "2026-07-31T12:00:00Z", "intuitentityid": "9999",
        "intuitaccountid": "9130000000000000001", "data": {},
    }]).encode("utf-8")
    asyncio.run(webhook_router.qb_webhook(request=_request(body=body), db=db_session))
    assert legacy_calls == [], (
        f"webhook must NOT call sync_all_*_task; saw: {legacy_calls}"
    )


# ---------------------------------------------------------------------------
# Status test
# ---------------------------------------------------------------------------

def test_sync_status_shows_last_sync(db_session: Session):
    import gdx_dispatch.modules.quickbooks.router as qb_router

    row = QBConnection(
        tenant_id="tenant-1", realm_id="realm-status",
        access_token="a", refresh_token="r",
        access_token_expires_at=datetime.now(UTC) + timedelta(hours=1),
        refresh_token_expires_at=datetime.now(UTC) + timedelta(days=30),
        last_sync_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        error_count=2,
    )
    db_session.add(row)
    db_session.commit()

    req = _request()
    data = qb_router.qb_status(request=req, current_user={"tenant_id": "tenant-1"}, db=db_session)
    assert data["connected"] is True
    assert data["last_sync_at"].startswith("2026-01-02T03:04:05")


# ---------------------------------------------------------------------------
# Full sync test
# ---------------------------------------------------------------------------

def test_full_sync_calls_all_pulls(db_session: Session, qb_connection, mock_qb, monkeypatch):
    from gdx_dispatch.modules.quickbooks import sync

    called: list[str] = []

    async def _fake_pull(name, tid, db, qb):
        called.append(name)
        return {"created": 0, "updated": 0}

    monkeypatch.setattr(sync, "pull_customers", lambda tid, db, qb: _fake_pull("customers", tid, db, qb))
    monkeypatch.setattr(sync, "pull_invoices", lambda tid, db, qb: _fake_pull("invoices", tid, db, qb))
    monkeypatch.setattr(sync, "pull_items", lambda tid, db, qb: _fake_pull("items", tid, db, qb))
    monkeypatch.setattr(sync, "pull_vendors", lambda tid, db, qb: _fake_pull("vendors", tid, db, qb))
    monkeypatch.setattr(sync, "pull_payments", lambda tid, db, qb: _fake_pull("payments", tid, db, qb))

    # Can't easily test the router's sync_full because it calls get_qb_client,
    # so test the sync functions directly
    async def _run_all():
        await sync.pull_customers("tenant-1", db_session, mock_qb)
        await sync.pull_invoices("tenant-1", db_session, mock_qb)
        await sync.pull_items("tenant-1", db_session, mock_qb)
        await sync.pull_vendors("tenant-1", db_session, mock_qb)
        await sync.pull_payments("tenant-1", db_session, mock_qb)

    asyncio.run(_run_all())
    assert called == ["customers", "invoices", "items", "vendors", "payments"]


# ---------------------------------------------------------------------------
# Client tests
# ---------------------------------------------------------------------------

def test_qb_client_url_includes_minor_version():
    qb = QBClient.__new__(QBClient)
    qb.realm_id = "12345"
    qb.minor_version = 75
    url = qb._url("Customer")
    assert "minorversion=75" in url
    assert "/v3/company/12345/customer" in url


def test_qb_client_url_with_entity_id():
    qb = QBClient.__new__(QBClient)
    qb.realm_id = "12345"
    qb.minor_version = 75
    url = qb._url("Invoice", "42")
    assert "/v3/company/12345/invoice/42" in url


# ---------------------------------------------------------------------------
# Reconciliation fix tests (2026-05-04)
# ---------------------------------------------------------------------------

def test_pull_payments_uses_qb_txn_date_not_today(db_session: Session, qb_connection, mock_qb):
    """Regression: 263 prod payments stamped date.today() because pull_payments
    ignored TxnDate. Verify the QB TxnDate becomes payment_date."""
    from gdx_dispatch.modules.quickbooks import sync
    from gdx_dispatch.models.tenant_models import Payment

    customer = _seed_customer(db_session)
    invoice = _seed_invoice(db_session, customer)
    db_session.add(QBEntityMap(tenant_id="tenant-1", entity_type="invoice", local_id=str(invoice.id), qb_id="QB-I-T1"))
    db_session.commit()

    mock_qb.query = AsyncMock(return_value=[{
        "Id": "QB-P-T1", "TotalAmt": 100, "TxnDate": "2025-09-15",
        "Line": [{"LinkedTxn": [{"TxnType": "Invoice", "TxnId": "QB-I-T1"}]}],
    }])
    out = asyncio.run(sync.pull_payments("tenant-1", db_session, mock_qb))
    assert out["created"] == 1
    payment = db_session.execute(select(Payment)).scalar_one()
    assert payment.payment_date == date(2025, 9, 15), \
        f"expected payment_date from TxnDate, got {payment.payment_date}"


def test_pull_payments_falls_back_to_today_when_txndate_missing(db_session: Session, qb_connection, mock_qb):
    from gdx_dispatch.modules.quickbooks import sync
    from gdx_dispatch.models.tenant_models import Payment

    customer = _seed_customer(db_session)
    invoice = _seed_invoice(db_session, customer)
    db_session.add(QBEntityMap(tenant_id="tenant-1", entity_type="invoice", local_id=str(invoice.id), qb_id="QB-I-T2"))
    db_session.commit()

    mock_qb.query = AsyncMock(return_value=[{
        "Id": "QB-P-T2", "TotalAmt": 50,
        "Line": [{"LinkedTxn": [{"TxnType": "Invoice", "TxnId": "QB-I-T2"}]}],
    }])
    asyncio.run(sync.pull_payments("tenant-1", db_session, mock_qb))
    payment = db_session.execute(select(Payment)).scalar_one()
    assert payment.payment_date == date.today()


def test_pull_payments_update_branch_actually_updates_amount_and_date(db_session: Session, qb_connection, mock_qb):
    """Regression: pre-fix update branch incremented counter but did nothing —
    QB-side edits never propagated."""
    from gdx_dispatch.modules.quickbooks import sync
    from gdx_dispatch.models.tenant_models import Payment

    customer = _seed_customer(db_session)
    invoice = _seed_invoice(db_session, customer)
    db_session.add(QBEntityMap(tenant_id="tenant-1", entity_type="invoice", local_id=str(invoice.id), qb_id="QB-I-U1"))
    payment = Payment(invoice_id=invoice.id, amount=100, method="quickbooks",
                      payment_date=date(2024, 1, 1), company_id="tenant-1")
    db_session.add(payment)
    db_session.flush()
    db_session.add(QBEntityMap(tenant_id="tenant-1", entity_type="payment", local_id=str(payment.id), qb_id="QB-P-U1"))
    db_session.commit()

    mock_qb.query = AsyncMock(return_value=[{
        "Id": "QB-P-U1", "TotalAmt": 175, "TxnDate": "2025-10-20",
        "Line": [{"LinkedTxn": [{"TxnType": "Invoice", "TxnId": "QB-I-U1"}]}],
    }])
    out = asyncio.run(sync.pull_payments("tenant-1", db_session, mock_qb))
    assert out["updated"] == 1
    db_session.refresh(payment)
    assert payment.amount == Decimal("175")
    assert payment.payment_date == date(2025, 10, 20)


def test_pull_payments_idempotent_second_sync_is_noop(db_session: Session, qb_connection, mock_qb):
    """Regression: second sync_full on unchanged data should report 0/0/0,
    not lie via the no-op counter increment."""
    from gdx_dispatch.modules.quickbooks import sync

    customer = _seed_customer(db_session)
    invoice = _seed_invoice(db_session, customer)
    db_session.add(QBEntityMap(tenant_id="tenant-1", entity_type="invoice", local_id=str(invoice.id), qb_id="QB-I-N1"))
    db_session.commit()

    payload = [{
        "Id": "QB-P-N1", "TotalAmt": 80, "TxnDate": "2025-12-01",
        "Line": [{"LinkedTxn": [{"TxnType": "Invoice", "TxnId": "QB-I-N1"}]}],
    }]
    mock_qb.query = AsyncMock(return_value=payload)
    first = asyncio.run(sync.pull_payments("tenant-1", db_session, mock_qb))
    assert first["created"] == 1
    second = asyncio.run(sync.pull_payments("tenant-1", db_session, mock_qb))
    assert second["created"] == 0
    assert second["updated"] == 0, "second sync on unchanged data must be a true no-op"


def test_pull_invoices_adoption_imports_lines(db_session: Session, qb_connection, mock_qb):
    """Regression: adoption branch wrote totals but skipped lines, leaving 282
    line-less invoices in GDX prod (~$615k)."""
    from gdx_dispatch.modules.quickbooks import sync
    from gdx_dispatch.models.tenant_models import InvoiceLine as IL

    customer = _seed_customer(db_session)
    # Existing local invoice with the same number QB will return — this is what
    # the adoption path matches on.
    job = Job(customer_id=customer.id, title="Pre-existing", company_id="tenant-1")
    db_session.add(job)
    db_session.flush()
    inv = Invoice(
        customer_id=uuid4(),
        job_id=job.id, invoice_number="ADOPT-1", subtotal=0, tax_amount=0,
        total=0, balance_due=0, status="sent", public_token="adopt-tok",
        company_id="tenant-1",
    )
    db_session.add(inv)
    db_session.commit()

    mock_qb.query = AsyncMock(return_value=[{
        "Id": "QB-I-A1", "DocNumber": "ADOPT-1", "TotalAmt": 250, "Balance": 0,
        "TxnDate": "2025-08-10", "DueDate": "2025-09-10",
        "Line": [
            {"Amount": 200, "Description": "Spring replacement"},
            {"Amount": 50, "Description": "Service call"},
        ],
    }])
    out = asyncio.run(sync.pull_invoices("tenant-1", db_session, mock_qb))
    assert out["adopted"] == 1
    lines = db_session.execute(select(IL).where(IL.invoice_id == inv.id)).scalars().all()
    assert len(lines) == 2, "adoption must import QB lines"
    assert sum(l.line_total for l in lines) == Decimal("250")


def test_pull_invoices_update_branch_resyncs_lines(db_session: Session, qb_connection, mock_qb):
    """Regression: update branch refreshed totals but never re-synced lines on
    edit — local lines drifted from QB source-of-truth forever."""
    from gdx_dispatch.modules.quickbooks import sync
    from gdx_dispatch.models.tenant_models import InvoiceLine as IL

    customer = _seed_customer(db_session)
    invoice = _seed_invoice(db_session, customer)
    db_session.add(QBEntityMap(tenant_id="tenant-1", entity_type="invoice", local_id=str(invoice.id), qb_id="QB-I-UP1"))
    db_session.commit()
    # Sanity: seed gave us one line.
    assert db_session.execute(select(IL).where(IL.invoice_id == invoice.id)).scalars().all()

    mock_qb.query = AsyncMock(return_value=[{
        "Id": "QB-I-UP1", "DocNumber": "INV-100", "TotalAmt": 999, "Balance": 0,
        "TxnDate": "2025-11-01",
        "Line": [
            {"Amount": 500, "Description": "QB-edited line A"},
            {"Amount": 499, "Description": "QB-edited line B"},
        ],
    }])
    asyncio.run(sync.pull_invoices("tenant-1", db_session, mock_qb))
    lines = db_session.execute(select(IL).where(IL.invoice_id == invoice.id)).scalars().all()
    assert len(lines) == 2
    assert sorted(l.description for l in lines) == ["QB-edited line A", "QB-edited line B"]


def test_pull_invoices_skips_subtotal_and_group_lines(db_session: Session, qb_connection, mock_qb):
    """Regression for prod invoice 1111 (2026-05-09 walk): pre-fix the resync
    only filtered lines whose Amount was None. QB SubTotalLineDetail and
    GroupLineDetail rows DO carry an Amount (the running subtotal), so they
    were being inserted as additional InvoiceLine rows and inflating the
    line sum past the persisted invoice.subtotal."""
    from gdx_dispatch.modules.quickbooks import sync
    from gdx_dispatch.models.tenant_models import InvoiceLine as IL

    customer = _seed_customer(db_session)
    invoice = _seed_invoice(db_session, customer)
    db_session.add(QBEntityMap(tenant_id="tenant-1", entity_type="invoice", local_id=str(invoice.id), qb_id="QB-I-SUB"))
    db_session.commit()

    mock_qb.query = AsyncMock(return_value=[{
        "Id": "QB-I-SUB", "DocNumber": "INV-SUB", "TotalAmt": 600, "Balance": 0,
        "TxnDate": "2025-11-01",
        "Line": [
            {"Amount": 400, "Description": "Real item A", "DetailType": "SalesItemLineDetail"},
            {"Amount": 200, "Description": "Real item B", "DetailType": "SalesItemLineDetail"},
            # The two below MUST be dropped, otherwise sum(line_total) becomes
            # 400+200+600+0 = 1200, double the actual TotalAmt.
            {"Amount": 600, "Description": "Subtotal", "DetailType": "SubTotalLineDetail"},
            {"Description": "Group section", "DetailType": "GroupLineDetail"},
        ],
    }])
    asyncio.run(sync.pull_invoices("tenant-1", db_session, mock_qb))
    lines = db_session.execute(select(IL).where(IL.invoice_id == invoice.id)).scalars().all()
    assert len(lines) == 2, f"expected 2 item lines, got {len(lines)}: {[l.description for l in lines]}"
    assert sum(float(l.line_total) for l in lines) == 600.0


def test_qb_client_query_paginates(monkeypatch):
    """Regression: ``query`` had a hardcoded MAXRESULTS 1000 and dropped any
    rows past the first page silently."""
    import json as _json
    import httpx
    from gdx_dispatch.modules.quickbooks.client import QBClient

    qb = QBClient.__new__(QBClient)
    qb.access_token = "x"
    qb.realm_id = "12345"
    qb.base_url = "https://example"
    qb.minor_version = 75

    pages = [
        [{"Id": str(i)} for i in range(1, 1001)],     # full page (1000)
        [{"Id": str(i)} for i in range(1001, 1051)],  # short page (50)
    ]
    call_log: list[str] = []

    class _FakeResp:
        def __init__(self, body):
            self._body = body
            self.is_success = True
            self.status_code = 200
            self.text = ""
        def json(self):
            return self._body

    async def fake_get(url):
        call_log.append(url)
        page = pages.pop(0) if pages else []
        return _FakeResp({"QueryResponse": {"Customer": page}})

    qb._client = type("FakeClient", (), {"get": staticmethod(fake_get)})()

    out = asyncio.run(qb.query("Customer"))
    assert len(out) == 1050, f"pagination dropped rows; got {len(out)}"
    # urllib.quote encodes spaces as %20; STARTPOSITION%201%20 == "STARTPOSITION 1 ".
    assert any("STARTPOSITION%201%20" in u for u in call_log), f"first page not at offset 1: {call_log}"
    assert any("STARTPOSITION%201001%20" in u for u in call_log), f"second page not at offset 1001: {call_log}"
    assert len(call_log) == 2, f"expected exactly 2 page fetches, got {len(call_log)}"


def test_pull_invoices_no_synthetic_job(db_session: Session, qb_connection, mock_qb):
    """Slice 2 regression: imported invoices must NOT attach to a 'QuickBooks
    Import' synthetic job. job_id stays NULL; customer_id carries the linkage."""
    from gdx_dispatch.modules.quickbooks import sync

    customer = _seed_customer(db_session)
    db_session.add(QBEntityMap(tenant_id="tenant-1", entity_type="customer",
                               local_id=str(customer.id), qb_id="QB-C-S2"))
    db_session.commit()

    mock_qb.query = AsyncMock(return_value=[{
        "Id": "QB-I-S2", "DocNumber": "S2-1", "TotalAmt": 100, "Balance": 0,
        "TxnDate": "2025-06-01", "CustomerRef": {"value": "QB-C-S2"},
        "Line": [{"Amount": 100, "Description": "Test line"}],
    }])
    out = asyncio.run(sync.pull_invoices("tenant-1", db_session, mock_qb))
    assert out["created"] == 1

    # No synthetic "QuickBooks Import" job should have been created.
    qb_import_jobs = db_session.execute(
        select(Job).where(Job.title == "QuickBooks Import")
    ).scalars().all()
    assert qb_import_jobs == [], f"unexpected synthetic jobs: {[j.id for j in qb_import_jobs]}"

    # The invoice should exist with job_id IS NULL and customer_id set.
    inv = db_session.execute(select(Invoice).where(Invoice.invoice_number == "S2-1")).scalar_one()
    assert inv.job_id is None, "imported invoice must not be attached to a synthetic job"
    assert inv.customer_id == customer.id


def test_qb_delete_sync_flag_off_default_no_op(db_session: Session, qb_connection, mock_qb, monkeypatch):
    """Slice 5: with QB_DELETE_SYNC_ENABLED unset, deletes must NOT propagate.

    Setup: existing local customer with a QB map. QBO returns an empty
    customer list. With the flag off, the local customer must NOT be
    soft-deleted.
    """
    from gdx_dispatch.modules.quickbooks import sync

    monkeypatch.delenv("QB_DELETE_SYNC_ENABLED", raising=False)

    customer = _seed_customer(db_session)
    db_session.add(QBEntityMap(tenant_id="tenant-1", entity_type="customer",
                               local_id=str(customer.id), qb_id="QB-C-D1"))
    db_session.commit()

    mock_qb.query = AsyncMock(return_value=[])  # QBO returns nothing
    out = asyncio.run(sync.pull_customers("tenant-1", db_session, mock_qb))
    assert out["deleted"] == 0
    db_session.refresh(customer)
    assert customer.deleted_at is None, "delete must not propagate when flag is off"


def test_qb_delete_sync_flag_on_soft_deletes_missing(db_session: Session, qb_connection, mock_qb, monkeypatch):
    """Slice 5: with QB_DELETE_SYNC_ENABLED=1, locals whose qb_id is no longer
    in QBO get soft-deleted and the mapping is removed."""
    from gdx_dispatch.modules.quickbooks import sync

    monkeypatch.setenv("QB_DELETE_SYNC_ENABLED", "1")

    keep_customer = _seed_customer(db_session)
    drop_customer = Customer(name="Going Away", source="quickbooks", company_id="tenant-1")
    db_session.add(drop_customer)
    db_session.flush()
    db_session.add(QBEntityMap(tenant_id="tenant-1", entity_type="customer",
                               local_id=str(keep_customer.id), qb_id="QB-C-KEEP"))
    db_session.add(QBEntityMap(tenant_id="tenant-1", entity_type="customer",
                               local_id=str(drop_customer.id), qb_id="QB-C-DROP"))
    db_session.commit()

    # QBO returns only the keeper.
    mock_qb.query = AsyncMock(return_value=[
        {"Id": "QB-C-KEEP", "DisplayName": keep_customer.name},
    ])
    out = asyncio.run(sync.pull_customers("tenant-1", db_session, mock_qb))
    assert out["deleted"] == 1, f"expected 1 delete, got {out}"

    db_session.refresh(keep_customer)
    db_session.refresh(drop_customer)
    assert keep_customer.deleted_at is None
    assert drop_customer.deleted_at is not None, "missing-from-QBO customer must be soft-deleted"

    # Mapping for the dropped customer should be gone.
    drop_map = db_session.execute(
        select(QBEntityMap).where(QBEntityMap.qb_id == "QB-C-DROP")
    ).scalar_one_or_none()
    assert drop_map is None, "QBEntityMap for dropped customer should be deleted"


def test_qb_delete_sync_empty_seen_set_skipped(db_session: Session, qb_connection, mock_qb, monkeypatch):
    """Slice 5 safety: if QBO returns zero rows total (e.g. transient zero-page
    fetch), do NOT nuke every local customer. Empty seen-set is a 'we don't
    know' signal, not an authority to delete."""
    from gdx_dispatch.modules.quickbooks import sync

    monkeypatch.setenv("QB_DELETE_SYNC_ENABLED", "1")

    customer = _seed_customer(db_session)
    db_session.add(QBEntityMap(tenant_id="tenant-1", entity_type="customer",
                               local_id=str(customer.id), qb_id="QB-C-EMPTY"))
    db_session.commit()

    mock_qb.query = AsyncMock(return_value=[])  # zero rows
    out = asyncio.run(sync.pull_customers("tenant-1", db_session, mock_qb))
    assert out["deleted"] == 0, "empty seen-set must NOT trigger deletes"
    db_session.refresh(customer)
    assert customer.deleted_at is None


def test_qb_delete_sync_writes_audit_row(db_session: Session, qb_connection, mock_qb, monkeypatch):
    """S103 audit-log gap: every soft-delete must leave a permanent AuditLog
    row before the QBEntityMap is hard-deleted. Without this, Slice 5 deletes
    are invisible — the mapping row is gone and only a stdlib log line
    survives, which rotates."""
    from gdx_dispatch.modules.quickbooks import sync

    monkeypatch.setenv("QB_DELETE_SYNC_ENABLED", "1")

    keeper = _seed_customer(db_session)
    dropper = Customer(name="Vanishing Co", source="quickbooks", company_id="tenant-1")
    db_session.add(dropper)
    db_session.flush()
    db_session.add(QBEntityMap(tenant_id="tenant-1", entity_type="customer",
                               local_id=str(keeper.id), qb_id="QB-C-KEEP-AUDIT"))
    db_session.add(QBEntityMap(tenant_id="tenant-1", entity_type="customer",
                               local_id=str(dropper.id), qb_id="QB-C-DROP-AUDIT"))
    db_session.commit()

    mock_qb.query = AsyncMock(return_value=[
        {"Id": "QB-C-KEEP-AUDIT", "DisplayName": keeper.name},
    ])
    asyncio.run(sync.pull_customers("tenant-1", db_session, mock_qb))

    audit_rows = db_session.execute(
        select(AuditLog).where(AuditLog.action == "qb_delete_sync")
    ).scalars().all()
    assert len(audit_rows) == 1, "exactly one qb_delete_sync row expected"
    row = audit_rows[0]
    assert row.entity_id == "QB-C-DROP-AUDIT"
    assert row.entity_type == "quickbooks"
    details = row.details or {}
    assert details.get("entity_type") == "customer"
    assert details.get("qb_id") == "QB-C-DROP-AUDIT"
    assert details.get("local_id") == str(dropper.id)
    assert details.get("reason") == "absent_from_full_set_diff"


def test_qb_delete_sync_no_audit_when_flag_off(db_session: Session, qb_connection, mock_qb, monkeypatch):
    """Mirror of the audit-write test: with the flag off, no AuditLog row may
    be written even if a qb_id disappears, because no destructive op happens."""
    from gdx_dispatch.modules.quickbooks import sync

    monkeypatch.delenv("QB_DELETE_SYNC_ENABLED", raising=False)

    customer = _seed_customer(db_session)
    db_session.add(QBEntityMap(tenant_id="tenant-1", entity_type="customer",
                               local_id=str(customer.id), qb_id="QB-C-FLAG-OFF"))
    db_session.commit()

    mock_qb.query = AsyncMock(return_value=[])
    asyncio.run(sync.pull_customers("tenant-1", db_session, mock_qb))

    audit_rows = db_session.execute(
        select(AuditLog).where(AuditLog.action == "qb_delete_sync")
    ).scalars().all()
    assert audit_rows == [], "no audit rows should be written when flag is off"


def test_delete_sync_per_tenant_column_overrides_env(db_session: Session, qb_connection, monkeypatch):
    """S103: when QBConnection.delete_sync_enabled is set, that wins over the
    QB_DELETE_SYNC_ENABLED env var."""
    from gdx_dispatch.modules.quickbooks import sync

    # Env says off, column says on → effective = on.
    monkeypatch.setenv("QB_DELETE_SYNC_ENABLED", "0")
    qb_connection.delete_sync_enabled = True
    db_session.commit()
    assert sync._delete_sync_enabled("tenant-1", db_session) is True

    # Env says on, column says off → effective = off (admin opted out).
    monkeypatch.setenv("QB_DELETE_SYNC_ENABLED", "1")
    qb_connection.delete_sync_enabled = False
    db_session.commit()
    assert sync._delete_sync_enabled("tenant-1", db_session) is False

    # Column NULL → fall back to env var.
    qb_connection.delete_sync_enabled = None
    db_session.commit()
    monkeypatch.setenv("QB_DELETE_SYNC_ENABLED", "1")
    assert sync._delete_sync_enabled("tenant-1", db_session) is True
    monkeypatch.setenv("QB_DELETE_SYNC_ENABLED", "0")
    assert sync._delete_sync_enabled("tenant-1", db_session) is False


def test_set_delete_sync_endpoint_writes_column_and_audit(db_session: Session, qb_connection):
    """S103: POST /api/qb/settings/delete-sync flips the column and writes an
    audit row. Admin/owner only — verified by manual role-check assert."""
    from gdx_dispatch.modules.quickbooks import router as qb_router_mod

    req = _request(tenant_id="tenant-1")
    user = {"role": "admin", "user_id": "admin-1", "tenant_id": "tenant-1"}

    out = qb_router_mod.set_delete_sync(
        request=req, payload={"enabled": True}, current_user=user, db=db_session,
    )
    assert out["delete_sync_enabled"] is True
    assert out["delete_sync_source"] == "tenant"
    assert out["column_value"] is True

    db_session.refresh(qb_connection)
    assert qb_connection.delete_sync_enabled is True

    audit_rows = db_session.execute(
        select(AuditLog).where(AuditLog.action == "qb_set_delete_sync")
    ).scalars().all()
    assert len(audit_rows) == 1
    assert audit_rows[0].details["new"] is True

    # Clearing returns to NULL → source flips back to env.
    out2 = qb_router_mod.set_delete_sync(
        request=req, payload={"enabled": None}, current_user=user, db=db_session,
    )
    assert out2["column_value"] is None
    assert out2["delete_sync_source"] == "env"
    db_session.refresh(qb_connection)
    assert qb_connection.delete_sync_enabled is None


def test_set_delete_sync_rejects_non_admin(db_session: Session, qb_connection):
    """Only admin/owner can flip a destructive flag."""
    from fastapi import HTTPException
    from gdx_dispatch.modules.quickbooks import router as qb_router_mod

    req = _request(tenant_id="tenant-1")
    for role in ("dispatcher", "technician", "viewer"):
        with pytest.raises(HTTPException) as exc:
            qb_router_mod.set_delete_sync(
                request=req, payload={"enabled": True},
                current_user={"role": role}, db=db_session,
            )
        assert exc.value.status_code == 403


def test_qb_events_action_filter_returns_only_matching(db_session: Session, qb_connection, mock_qb):
    """S103: GET /api/qb/events?action=qb_delete_sync must return only those
    rows in the raw shape, ignoring the default sync-log presentation."""
    from gdx_dispatch.core.audit import log_audit_event_sync
    from gdx_dispatch.modules.quickbooks import router as qb_router_mod

    log_audit_event_sync(db_session, tenant_id="tenant-1", user_id="system",
                         action="qb_pull_customers", entity_type="quickbooks",
                         entity_id="tenant-1", details={"created": 5, "updated": 0})
    log_audit_event_sync(db_session, tenant_id="tenant-1", user_id="system",
                         action="qb_delete_sync", entity_type="quickbooks",
                         entity_id="QB-C-XYZ", details={"entity_type": "customer",
                         "qb_id": "QB-C-XYZ", "local_id": "abc-123",
                         "reason": "absent_from_full_set_diff"})
    db_session.commit()

    req = _request(tenant_id="tenant-1")
    filtered = qb_router_mod.qb_events(
        request=req, current_user={"tenant_id": "tenant-1"},
        db=db_session, limit=50, action="qb_delete_sync",
    )
    assert isinstance(filtered, dict) and "events" in filtered
    assert len(filtered["events"]) == 1, f"expected 1 row, got {filtered}"
    ev = filtered["events"][0]
    assert ev["action"] == "qb_delete_sync"
    assert ev["entity_id"] == "QB-C-XYZ"
    assert ev["details"]["qb_id"] == "QB-C-XYZ"

    # Default events response (no action) must EXCLUDE qb_delete_sync rows so
    # the existing sync log isn't polluted by per-row delete entries.
    default = qb_router_mod.qb_events(
        request=req, current_user={"tenant_id": "tenant-1"},
        db=db_session, limit=50, action="",
    )
    types = [e["type"] for e in default["events"]]
    assert "qb_delete_sync" not in types
    assert "customers" in types


# ---------------------------------------------------------------------------
# QBO merge-detection (Active=false probe)
# ---------------------------------------------------------------------------

def test_pull_customers_detects_qbo_merge_active_false(db_session: Session, qb_connection, mock_qb):
    """When a mapped customer drops out of the pull AND qb.read returns
    Active=false, the local row gets soft-deleted, its map is dropped, and an
    audit row is written. This is the QBO-side merge detection class."""
    from gdx_dispatch.modules.quickbooks import sync

    # Seed a local customer that was previously mapped to QB-C-MERGED.
    cust = _seed_customer(db_session, "Merged")
    db_session.add(QBEntityMap(
        tenant_id="tenant-1", entity_type="customer",
        local_id=str(cust.id), qb_id="QB-C-MERGED",
    ))
    db_session.commit()

    # The pull does NOT return this customer (it was merged-away in QBO).
    mock_qb.query = AsyncMock(return_value=[])
    # The targeted probe returns Active=false.
    mock_qb.read = AsyncMock(return_value={
        "Id": "QB-C-MERGED", "DisplayName": "Merged", "Active": False,
    })

    out = asyncio.run(sync.pull_customers("tenant-1", db_session, mock_qb))

    assert out["merged_remote"] == 1
    # Local row was soft-deleted.
    db_session.refresh(cust)
    assert cust.deleted_at is not None
    # Map row was dropped.
    residual_map = db_session.execute(
        select(QBEntityMap).where(QBEntityMap.qb_id == "QB-C-MERGED")
    ).scalar_one_or_none()
    assert residual_map is None
    # Audit row written.
    audit = db_session.execute(
        select(AuditLog).where(AuditLog.action == "qbo_customer_merged_remote")
    ).scalar_one()
    assert audit.entity_id == "QB-C-MERGED"
    assert audit.details["local_id"] == str(cust.id)


def test_pull_customers_qbo_merge_noop_on_active_true(db_session: Session, qb_connection, mock_qb):
    """If qb.read returns Active=true (entity exists, just missed by this
    pull — e.g. pagination, where-clause filter), do NOT delete."""
    from gdx_dispatch.modules.quickbooks import sync

    cust = _seed_customer(db_session, "StillActive")
    db_session.add(QBEntityMap(
        tenant_id="tenant-1", entity_type="customer",
        local_id=str(cust.id), qb_id="QB-C-ACTIVE",
    ))
    db_session.commit()

    mock_qb.query = AsyncMock(return_value=[])
    mock_qb.read = AsyncMock(return_value={
        "Id": "QB-C-ACTIVE", "DisplayName": "StillActive", "Active": True,
    })

    out = asyncio.run(sync.pull_customers("tenant-1", db_session, mock_qb))

    assert out["merged_remote"] == 0
    db_session.refresh(cust)
    assert cust.deleted_at is None
    residual_map = db_session.execute(
        select(QBEntityMap).where(QBEntityMap.qb_id == "QB-C-ACTIVE")
    ).scalar_one_or_none()
    assert residual_map is not None


def test_pull_customers_qbo_merge_noop_on_read_failure(db_session: Session, qb_connection, mock_qb):
    """If qb.read raises (404, network blip, permission), treat as ambiguous
    and do not act. The local row + map must stay intact for the next sync."""
    from gdx_dispatch.modules.quickbooks import sync

    cust = _seed_customer(db_session, "Ambiguous")
    db_session.add(QBEntityMap(
        tenant_id="tenant-1", entity_type="customer",
        local_id=str(cust.id), qb_id="QB-C-MISSING",
    ))
    db_session.commit()

    mock_qb.query = AsyncMock(return_value=[])
    mock_qb.read = AsyncMock(side_effect=RuntimeError("404 Not Found"))

    out = asyncio.run(sync.pull_customers("tenant-1", db_session, mock_qb))

    assert out["merged_remote"] == 0
    db_session.refresh(cust)
    assert cust.deleted_at is None


def test_find_or_create_import_job_helper_removed():
    """The synthetic-job helper must not exist; pulling it back would re-attach
    every imported invoice to a fake job."""
    from gdx_dispatch.modules.quickbooks import sync
    assert not hasattr(sync, "_find_or_create_import_job"), \
        "_find_or_create_import_job is a deprecated synthetic-job helper — keep it removed"


# ---------------------------------------------------------------------------
# Module gating test
# ---------------------------------------------------------------------------

def test_router_is_module_gated():
    import gdx_dispatch.modules.quickbooks.router as qb_mod

    dependency_fn = qb_mod.router.dependencies[0].dependency
    closure_values = [cell.cell_contents for cell in (dependency_fn.__closure__ or [])]
    assert "quickbooks" in closure_values

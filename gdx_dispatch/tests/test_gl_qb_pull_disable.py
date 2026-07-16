"""GL Phase 1 (S9) — QB money-pull disable under the ledger flag (spec §5.4).

Plan gates: with ``ledger_posting_enabled`` on, the four money-mutating pull
paths (pull_invoices, pull_payments, _resync_invoice_lines,
_apply_qbo_deletes for invoice/payment) fail loudly BEFORE touching QBO or
local rows; non-money pulls (customers/items/vendors/accounts/banking) are
untouched; the disable state surfaces on /qb status + dashboard; the webhook
dispatcher suppresses Invoice/Payment pull tasks instead of enqueueing a
permanently-failing task per GDX→QBO push echo; the dead legacy
core/quickbooks.py pulls are gone.

House pattern: endpoint functions called directly with a session + a
starlette Request carrying ``state.tenant`` (same as test_qb_full_sync.py).
"""
from __future__ import annotations

import asyncio
import secrets
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.quickbooks import QBConnection, QBEntityMap, QBVendor
from gdx_dispatch.models.tenant_models import Customer, Invoice, InvoiceLine, Job
from gdx_dispatch.modules.ledger.service import ensure_gl_seed
from gdx_dispatch.modules.quickbooks import sync
from gdx_dispatch.modules.quickbooks.client import QBClient
from gdx_dispatch.modules.quickbooks.sync import QBPullDisabledError
from gdx_dispatch.modules.quickbooks.webhook_models import QBWebhookEvent

TENANT = "tenant-1"


@pytest.fixture()
def db(monkeypatch) -> Session:
    monkeypatch.delenv("GDX_ENV", raising=False)
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = SessionLocal()
    try:
        QBConnection.__table__.create(bind=engine, checkfirst=True)
        QBEntityMap.__table__.create(bind=engine, checkfirst=True)
        QBVendor.__table__.create(bind=engine, checkfirst=True)
        QBWebhookEvent.__table__.create(bind=engine, checkfirst=True)
        ensure_gl_seed(session, TENANT)
        session.commit()
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def mock_qb() -> QBClient:
    qb = QBClient.__new__(QBClient)
    qb.access_token = "test-token"
    qb.realm_id = "realm-1"
    qb.base_url = "https://sandbox-quickbooks.api.intuit.com"
    qb.minor_version = 75
    qb._client = None
    qb.query = AsyncMock(return_value=[])
    qb.create = AsyncMock(return_value={})
    qb.close = AsyncMock()
    return qb


def _set_flag(db: Session, on: bool) -> None:
    settings = ensure_gl_seed(db, TENANT)
    settings.ledger_posting_enabled = on
    db.commit()


def _request(*, tenant_id: str = TENANT) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/qb/status",
        "headers": [],
        "query_string": b"",
    }
    req = Request(scope)
    req.state.tenant = {"id": tenant_id}
    return req


def _seed_invoice_with_line(db: Session) -> Invoice:
    customer = Customer(name="Alice", company_id=TENANT)
    db.add(customer)
    db.flush()
    job = Job(customer_id=customer.id, title="Install opener", company_id=TENANT)
    db.add(job)
    db.flush()
    inv = Invoice(
        id=uuid4(),
        customer_id=customer.id,
        invoice_number=f"INV-{uuid4().hex[:8].upper()}",
        status="sent",
        subtotal=Decimal("100.00"),
        tax_amount=Decimal("0.00"),
        total=Decimal("100.00"),
        public_token=secrets.token_urlsafe(48)[:64],
        company_id=TENANT,
    )
    db.add(inv)
    db.flush()
    db.add(
        InvoiceLine(
            invoice_id=inv.id,
            description="Spring",
            quantity=1,
            unit_price=Decimal("100.00"),
            line_total=Decimal("100.00"),
            company_id=TENANT,
        )
    )
    db.commit()
    return inv


# ---------------------------------------------------------------------------
# The four §5.4 gates
# ---------------------------------------------------------------------------

def test_pull_invoices_blocked_before_any_qbo_call(db, mock_qb):
    _set_flag(db, True)
    with pytest.raises(QBPullDisabledError):
        asyncio.run(sync.pull_invoices(TENANT, db, mock_qb))
    assert mock_qb.query.await_count == 0


def test_pull_payments_blocked_before_any_qbo_call(db, mock_qb):
    _set_flag(db, True)
    with pytest.raises(QBPullDisabledError):
        asyncio.run(sync.pull_payments(TENANT, db, mock_qb))
    assert mock_qb.query.await_count == 0


def test_resync_invoice_lines_blocked_and_lines_survive(db):
    inv = _seed_invoice_with_line(db)
    _set_flag(db, True)
    with pytest.raises(QBPullDisabledError):
        sync._resync_invoice_lines(inv.id, [{"Amount": 5}], TENANT, db)
    db.rollback()
    lines = db.execute(
        select(InvoiceLine).where(InvoiceLine.invoice_id == inv.id)
    ).scalars().all()
    assert len(lines) == 1  # the gate fired before the bulk delete


def test_apply_qbo_deletes_gates_money_entities_only(db):
    _set_flag(db, True)
    for entity in ("invoice", "payment"):
        with pytest.raises(QBPullDisabledError):
            sync._apply_qbo_deletes(TENANT, entity, {"qb-1"}, db)
    # Non-money entity types stay functional (delete sync itself is off →
    # returns 0, but crucially does not raise).
    assert sync._apply_qbo_deletes(TENANT, "customer", {"qb-1"}, db) == 0


def test_pulls_run_normally_with_flag_off(db, mock_qb):
    _set_flag(db, False)
    out = asyncio.run(sync.pull_invoices(TENANT, db, mock_qb))
    assert out["created"] == 0
    assert mock_qb.query.await_count == 1
    out = asyncio.run(sync.pull_payments(TENANT, db, mock_qb))
    assert out["created"] == 0


# ---------------------------------------------------------------------------
# Router surfacing
# ---------------------------------------------------------------------------

def test_qb_status_surfaces_money_pulls_disabled(db):
    from gdx_dispatch.modules.quickbooks.router import qb_status

    out = qb_status(request=_request(), current_user={"sub": "t"}, db=db)
    assert out["money_pulls_disabled"] is False
    assert out["money_pulls_disabled_reason"] is None

    _set_flag(db, True)
    out = qb_status(request=_request(), current_user={"sub": "t"}, db=db)
    assert out["money_pulls_disabled"] is True
    assert "book of record" in out["money_pulls_disabled_reason"]


def test_qb_dashboard_surfaces_money_pulls_disabled(db):
    from gdx_dispatch.modules.quickbooks.router import qb_dashboard

    _set_flag(db, True)
    out = qb_dashboard(request=_request(), current_user={"sub": "t"}, db=db)
    assert out["money_pulls_disabled"] is True


def test_sync_invoices_endpoint_409_when_ledger_on(db, mock_qb, monkeypatch):
    from gdx_dispatch.modules.quickbooks import router as qb_router

    async def _fake_client(tenant_id, session):
        return mock_qb

    monkeypatch.setattr(qb_router, "get_qb_client", _fake_client)
    _set_flag(db, True)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            qb_router.sync_invoices(request=_request(), current_user={"sub": "t"}, db=db)
        )
    assert exc.value.status_code == 409
    assert "book of record" in exc.value.detail


def test_sync_full_reports_disabled_slots_and_keeps_mirror_pulls(db, mock_qb, monkeypatch):
    from gdx_dispatch.modules.quickbooks import router as qb_router

    async def _fake_client(tenant_id, session):
        return mock_qb

    monkeypatch.setattr(qb_router, "get_qb_client", _fake_client)
    _set_flag(db, True)
    out = asyncio.run(
        qb_router.sync_full(request=_request(), current_user={"sub": "t"}, db=db)
    )
    assert out["invoices"]["disabled"] == "ledger_posting_enabled"
    assert "book of record" in out["invoices"]["detail"]
    assert out["payments"]["disabled"] == "ledger_posting_enabled"
    # Non-money pulls still ran and returned real counts.
    assert "created" in out["customers"]
    assert "created" in out["items"]


# ---------------------------------------------------------------------------
# Webhook dispatcher suppression
# ---------------------------------------------------------------------------

def _dispatched(monkeypatch) -> list[str]:
    """Stub every per-entity task's .delay and record which fired."""
    from gdx_dispatch.modules.quickbooks import tasks as qb_tasks

    fired: list[str] = []
    for name in (
        "sync_customer_task", "sync_invoice_task", "sync_payment_task",
        "sync_item_task", "sync_vendor_task", "sync_account_task",
    ):
        monkeypatch.setattr(
            getattr(qb_tasks, name), "delay",
            lambda *a, _n=name, **kw: fired.append(_n),
        )
    return fired


def test_webhook_suppresses_money_pull_dispatch_when_ledger_on(db, monkeypatch):
    from gdx_dispatch.modules.quickbooks import webhook_router

    monkeypatch.setenv("GDX_TENANT_ID", TENANT)
    monkeypatch.delenv("QB_WEBHOOK_VERIFIER_TOKEN", raising=False)
    monkeypatch.delenv("QB_WEBHOOK_SECRET", raising=False)
    fired = _dispatched(monkeypatch)
    _set_flag(db, True)

    import json

    body = json.dumps({
        "eventNotifications": [{
            "realmId": "realm-1",
            "dataChangeEvent": {"entities": [
                {"name": "Invoice", "id": "101", "operation": "Update"},
                {"name": "Payment", "id": "202", "operation": "Create"},
                {"name": "Customer", "id": "303", "operation": "Update"},
            ]},
        }]
    }).encode()

    async def _receive():
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http", "method": "POST", "path": "/api/qb/webhooks",
        "headers": [], "query_string": b"",
    }
    req = Request(scope, receive=_receive)
    req.state.tenant = {"id": TENANT}

    out = asyncio.run(webhook_router.qb_webhook(request=req, db=db))
    assert out["suppressed_ledger_on"] == 2
    assert fired == ["sync_customer_task"]


def test_webhook_dispatches_money_pulls_when_flag_off(db, monkeypatch):
    from gdx_dispatch.modules.quickbooks import webhook_router

    monkeypatch.setenv("GDX_TENANT_ID", TENANT)
    monkeypatch.delenv("QB_WEBHOOK_VERIFIER_TOKEN", raising=False)
    monkeypatch.delenv("QB_WEBHOOK_SECRET", raising=False)
    fired = _dispatched(monkeypatch)
    _set_flag(db, False)

    import json

    body = json.dumps({
        "eventNotifications": [{
            "realmId": "realm-1",
            "dataChangeEvent": {"entities": [
                {"name": "Invoice", "id": "101", "operation": "Update"},
            ]},
        }]
    }).encode()

    async def _receive():
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http", "method": "POST", "path": "/api/qb/webhooks",
        "headers": [], "query_string": b"",
    }
    req = Request(scope, receive=_receive)
    req.state.tenant = {"id": TENANT}

    out = asyncio.run(webhook_router.qb_webhook(request=req, db=db))
    assert out["suppressed_ledger_on"] == 0
    assert fired == ["sync_invoice_task"]


# ---------------------------------------------------------------------------
# Dead legacy pulls stay dead
# ---------------------------------------------------------------------------

def test_legacy_core_quickbooks_pulls_are_gone():
    from gdx_dispatch.core import quickbooks as legacy

    assert not hasattr(legacy, "pull_invoices")
    assert not hasattr(legacy, "pull_payments")
    assert not hasattr(legacy, "_find_or_create_import_job")

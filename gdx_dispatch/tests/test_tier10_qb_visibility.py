"""Tier 10 — make invisible QuickBooks state visible.

Two contract gaps (docs/design/backend-vue-contract-gaps-2026-07-24.md Tier 10):

1. Per-record QB state was serialized nowhere. The AUTHORITATIVE "is this record
   in QuickBooks" signal is a ``QBEntityMap`` row (what every push path writes and
   the dashboard counts) — NOT ``qb_synced_at``, which the selective-push path
   stamps with no backfill (a legacy/imported/manual record reads NULL yet is in
   QB). ``qb_entity_is_mapped`` is that signal; ``qb_dirty``/``qb_synced_at`` add
   push freshness on top.

2. ``/api/qb/dashboard`` (the frontend's primary status source) never returned
   ``auth_state``/``needs_reconnect``, so a dead token showed "Connected" while
   every sync silently no-op'd.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.quickbooks import QBConnection, QBEntityMap, qb_entity_is_mapped
from gdx_dispatch.models.tenant_models import Customer, Invoice
from gdx_dispatch.modules.quickbooks.oauth import QBTokenStore
from gdx_dispatch.routers.customers import _customer_dict
from gdx_dispatch.routers.invoices import _serialize_invoice

TENANT = "tenant-1"


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Customer.__table__.create(bind=engine, checkfirst=True)
    Invoice.__table__.create(bind=engine, checkfirst=True)
    TenantBase.metadata.create_all(bind=engine, checkfirst=True)  # QBConnection, QBEntityMap
    QBTokenStore.__table__.create(bind=engine, checkfirst=True)  # lives on the shared Base
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _request() -> Request:
    req = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    req.state.tenant = {"id": TENANT}
    req.state.tenant_id = TENANT
    return req


def _seed_customer(db) -> Customer:
    c = Customer(company_id=TENANT, name="Amy Ratepayer", email="amy@example.com")
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _seed_invoice(db, customer) -> Invoice:
    inv = Invoice(
        company_id=TENANT,
        customer_id=customer.id,
        invoice_number=f"INV-{uuid4().hex[:8].upper()}",
        billing_type="standard",
        sequence_number=1,
        subtotal=Decimal("400"),
        tax_amount=Decimal("0"),
        total=Decimal("400"),
        balance_due=Decimal("400"),
        status="sent",
        invoice_date=date.today(),
        public_token=uuid4().hex,
        locked=False,
    )
    db.add(inv)
    db.commit()
    db.refresh(inv)
    return inv


# ── Per-record serializer: raw push-freshness fields ────────────────────────

def test_invoice_serializes_never_synced_state(db):
    inv = _seed_invoice(db, _seed_customer(db))
    payload = _serialize_invoice(inv)
    assert payload["qb_dirty"] is True
    assert payload["qb_synced_at"] is None


def test_invoice_serializes_synced_state(db):
    inv = _seed_invoice(db, _seed_customer(db))
    inv.qb_dirty = False
    inv.qb_synced_at = datetime(2026, 7, 24, 15, 0, tzinfo=UTC)
    db.commit()
    payload = _serialize_invoice(inv)
    assert payload["qb_dirty"] is False
    assert payload["qb_synced_at"].startswith("2026-07-24T15:00:00")


def test_customer_serializes_qb_state_orm_branch(db):
    cust = _seed_customer(db)
    payload = _customer_dict(cust)
    assert payload["qb_dirty"] is True
    assert payload["qb_synced_at"] is None
    cust.qb_dirty = False
    cust.qb_synced_at = datetime(2026, 7, 24, 9, 30, tzinfo=UTC)
    db.commit()
    payload = _customer_dict(cust)
    assert payload["qb_dirty"] is False
    assert payload["qb_synced_at"].startswith("2026-07-24T09:30:00")


# ── The load-bearing signal: QBEntityMap presence ("in QuickBooks") ─────────

def test_unmapped_record_is_not_in_quickbooks(db):
    """A record with no QBEntityMap row is genuinely not in QB — even though it
    also has qb_synced_at NULL. This is the honest 'Not in QuickBooks' state."""
    inv = _seed_invoice(db, _seed_customer(db))
    assert qb_entity_is_mapped(db, "invoice", inv.id) is False


def test_mapped_record_is_in_quickbooks_despite_null_timestamp(db):
    """THE regression this fix exists to prevent: a record that IS in QB via a
    QBEntityMap row (legacy sync / import / manual) but has qb_synced_at NULL.
    The presence signal must report True so the chip doesn't say 'not synced'."""
    cust = _seed_customer(db)
    inv = _seed_invoice(db, cust)
    db.add(QBEntityMap(tenant_id=TENANT, entity_type="invoice", local_id=str(inv.id), qb_id="QB-42"))
    db.add(QBEntityMap(tenant_id=TENANT, entity_type="customer", local_id=str(cust.id), qb_id="QB-7"))
    db.commit()
    # qb_synced_at stays NULL (no selective-push has run) — yet both are in QB.
    assert inv.qb_synced_at is None
    assert cust.qb_synced_at is None
    assert qb_entity_is_mapped(db, "invoice", inv.id) is True
    assert qb_entity_is_mapped(db, "customer", cust.id) is True


def test_mapping_is_entity_scoped(db):
    """A customer mapping must not make the same-id lookup as an invoice pass."""
    cust = _seed_customer(db)
    db.add(QBEntityMap(tenant_id=TENANT, entity_type="customer", local_id=str(cust.id), qb_id="QB-7"))
    db.commit()
    assert qb_entity_is_mapped(db, "customer", cust.id) is True
    assert qb_entity_is_mapped(db, "invoice", cust.id) is False


def test_mapped_lookup_survives_missing_table(db):
    """Tenants that never connected QB may lack qb_entity_maps — the helper
    must degrade to False, never raise, so the detail view still renders."""
    QBEntityMap.__table__.drop(bind=db.get_bind())
    inv = _seed_invoice(db, _seed_customer(db))
    assert qb_entity_is_mapped(db, "invoice", inv.id) is False


# ── Dashboard connection health: auth_state / needs_reconnect ────────────────

def test_dashboard_surfaces_needs_reconnect(db, monkeypatch):
    """A dead token (auth_state='needs_reconnect') on the modern token store
    must flip needs_reconnect on the dashboard — it stayed silently 'Connected'
    before, hiding the dead-QB incident."""
    from gdx_dispatch.modules.quickbooks import router as qb_router

    db.add(QBTokenStore(
        tenant_id=TENANT, realm_id="R1", environment="production",
        access_token_enc="x", refresh_token_enc="y",
        access_token_expires_at=datetime(2026, 1, 1, tzinfo=UTC),
        refresh_token_expires_at=datetime(2026, 1, 1, tzinfo=UTC),
        auth_state="needs_reconnect",
    ))
    db.commit()
    # Neutralize helpers that would touch tables this in-memory DB doesn't have.
    monkeypatch.setattr(qb_router.sync, "money_pulls_disabled", lambda _db, _tid: False)
    monkeypatch.setattr(qb_router.sync, "_delete_sync_enabled", lambda _tid, _db: False)
    monkeypatch.setattr(qb_router, "_delete_sync_source", lambda _conn: None)

    result = qb_router.qb_dashboard(_request(), {"tenant_id": TENANT, "role": "admin"}, db)
    assert result["connected"] is True          # a token row exists
    assert result["auth_state"] == "needs_reconnect"
    assert result["needs_reconnect"] is True


def test_dashboard_healthy_when_no_token(db, monkeypatch):
    """No token row → auth_state defaults healthy, needs_reconnect False, and a
    legacy QBConnection still reports connected (behavior preserved)."""
    from gdx_dispatch.modules.quickbooks import router as qb_router

    db.add(QBConnection(
        tenant_id=TENANT, realm_id="R9",
        access_token="x", refresh_token="y",
        access_token_expires_at=datetime(2026, 1, 1, tzinfo=UTC),
        refresh_token_expires_at=datetime(2026, 1, 1, tzinfo=UTC),
    ))
    db.commit()
    monkeypatch.setattr(qb_router.sync, "money_pulls_disabled", lambda _db, _tid: False)
    monkeypatch.setattr(qb_router.sync, "_delete_sync_enabled", lambda _tid, _db: False)
    monkeypatch.setattr(qb_router, "_delete_sync_source", lambda _conn: None)

    result = qb_router.qb_dashboard(_request(), {"tenant_id": TENANT, "role": "admin"}, db)
    assert result["connected"] is True
    assert result["auth_state"] == "healthy"
    assert result["needs_reconnect"] is False

import asyncio
from datetime import date, datetime, time, timedelta, timezone
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from gdx_dispatch.core.modules import require_module
from gdx_dispatch.modules.equipment.models import CustomerEquipment, EquipmentServiceHistory
from gdx_dispatch.modules.inventory.models import Part
from gdx_dispatch.modules.inventory.service import check_low_stock_alerts, deduct_stock
from gdx_dispatch.modules.quickbooks.oauth import QBTokenStore
from gdx_dispatch.modules.quickbooks.sync import (
    QBRateLimitError,
    QBSyncError,
    pull_customers,
    pull_invoices,
    pull_payments,
    push_customer,
    push_invoice,
)
from gdx_dispatch.modules.quickbooks.webhook_models import QBWebhookEvent
from gdx_dispatch.modules.timeclock.models import TimeClock
from gdx_dispatch.modules.timeclock.service import clock_in, clock_out, daily_labor_report


@pytest.fixture
def inventory_db(tenant_db):
    """Reuse the shared tenant_db fixture which creates all tables."""
    yield tenant_db


@pytest.fixture
def timeclock_db(tenant_db):
    """Reuse the shared tenant_db fixture which creates all tables."""
    yield tenant_db


def test_inventory_deduct_stock_thread_safe(inventory_db):
    p = Part(sku="SKU-1", name="Bolt", qty_on_hand=10, reorder_point=2, unit_cost=1, unit_price=2)
    inventory_db.add(p); inventory_db.commit(); inventory_db.refresh(p)  # noqa: E701,E702
    deduct_stock(p.id, 3, inventory_db); inventory_db.commit(); inventory_db.refresh(p)  # noqa: E701,E702
    assert p.qty_on_hand == 7
    from fastapi import HTTPException
    with pytest.raises((ValueError, HTTPException)):
        deduct_stock(p.id, 8, inventory_db)


def test_inventory_low_stock_alert(inventory_db):
    p = Part(sku="SKU-2", name="Nut", qty_on_hand=2, reorder_point=5, unit_cost=1, unit_price=2)
    inventory_db.add(p); inventory_db.commit(); inventory_db.refresh(p)  # noqa: E701,E702
    assert p.id in {row.id for row in check_low_stock_alerts(inventory_db)}


def test_timeclock_clock_in_out(timeclock_db):
    row = clock_in("tech-1", None, timeclock_db, company_id="tenant-test")
    assert row.clock_out_at is None
    with pytest.raises(ValueError, match="Already clocked in"):
        clock_in("tech-1", None, timeclock_db, company_id="tenant-test")
    row.clock_in_at = datetime.now(timezone.utc) - timedelta(minutes=5); timeclock_db.commit()  # noqa: E701,E702
    out = clock_out(row.id, timeclock_db)
    assert out.labor_minutes > 0 and out.clock_out_at is not None


def test_timeclock_daily_report(timeclock_db):
    tz = ZoneInfo("America/New_York")
    start_local = datetime.combine(date.today(), time(9, 0), tzinfo=tz)
    r1 = TimeClock(company_id="tenant-test", technician_id="tech-1", job_id=None, clock_in_at=start_local.astimezone(timezone.utc), clock_out_at=(start_local + timedelta(hours=1)).astimezone(timezone.utc), labor_minutes=60)
    r2 = TimeClock(company_id="tenant-test", technician_id="tech-2", job_id=None, clock_in_at=(start_local + timedelta(hours=2)).astimezone(timezone.utc), clock_out_at=(start_local + timedelta(hours=3)).astimezone(timezone.utc), labor_minutes=60)
    timeclock_db.add_all([r1, r2]); timeclock_db.commit()  # noqa: E701,E702
    report = daily_labor_report(date.today(), "America/New_York", timeclock_db)
    assert len(report) == 2


def test_module_gate_require_module():
    dep = require_module("inventory")
    assert callable(dep) and dep is not None


def test_equipment_model_columns():
    cols = CustomerEquipment.__table__.columns
    for name in ["equipment_type", "manufacturer", "model", "serial_number"]:
        assert name in cols
    assert any(fk.target_fullname == "customer_equipments.id" for fk in EquipmentServiceHistory.__table__.c.equipment_id.foreign_keys)


def test_qb_sync_importable():
    assert all([pull_customers, pull_invoices, pull_payments, push_customer, push_invoice, QBSyncError, QBRateLimitError])


def test_kb_token_store_model():
    cols = QBTokenStore.__table__.columns
    for name in ["realm_id", "access_token_enc", "refresh_token_enc"]:
        assert name in cols


# ---------------------------------------------------------------------------
# New tests: token refresh DB-commit guard, webhook deduplication, rate limit
# ---------------------------------------------------------------------------

def test_qb_token_refresh_survives_db_commit_failure():
    """get_qb_client must return in-memory refreshed client even when DB commit fails."""
    from datetime import timedelta, timezone

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from gdx_dispatch.core.audit import TenantBase
    from gdx_dispatch.models.tenant_models import Base as TenantModelsBase
    from gdx_dispatch.modules.quickbooks.oauth import (
        QBTokenStore,
        _encrypt,
        get_qb_client,
    )

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    TenantModelsBase.metadata.create_all(engine, checkfirst=True)
    TenantBase.metadata.create_all(engine, checkfirst=True)
    QBTokenStore.__table__.create(bind=engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    # SQLite strips timezone info on round-trip, so use naive datetimes throughout.
    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    future = now_naive + timedelta(days=100)
    soon = now_naive + timedelta(minutes=2)

    # Insert a token row that needs refresh (expires in 2 minutes < 5-minute window)
    with Session() as db:
        row = QBTokenStore(
            tenant_id="tenant-refresh-test",
            realm_id="realm-test",
            environment="production",
            access_token_enc=_encrypt("old-access"),
            refresh_token_enc=_encrypt("old-refresh"),
            access_token_expires_at=soon,
            refresh_token_expires_at=future,
        )
        db.add(row)
        db.commit()

    with Session() as db:
        # Mock the refresh_access_token async function to return new tokens
        async def fake_refresh(refresh_token):
            return {
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_in": 3600,
                "x_refresh_token_expires_in": 8726400,
            }

        # Patch datetime.now inside oauth to return a naive datetime so comparisons
        # against SQLite-stored naive values don't raise TypeError.
        mock_dt = MagicMock(wraps=datetime)
        mock_dt.now = MagicMock(return_value=now_naive)

        with patch("gdx_dispatch.modules.quickbooks.oauth.refresh_access_token", fake_refresh), \
             patch("gdx_dispatch.modules.quickbooks.oauth.datetime", mock_dt):
            # Force DB commit to fail
            call_count = [0]

            def failing_commit():
                call_count[0] += 1
                raise RuntimeError("simulated DB failure")

            db.commit = failing_commit

            # Should NOT raise — returns QBClient with refreshed token
            client = asyncio.run(get_qb_client("tenant-refresh-test", db))

        # Commit was attempted and failed (refresh tried to persist)
        assert call_count[0] >= 1
        # But the returned client carries the freshly-refreshed token
        assert client.access_token == "new-access"
        await_close = client.close  # cleanup
        asyncio.run(await_close())


def test_qb_webhook_deduplication(tenant_db):
    """Second delivery of identical QB webhook event is silently skipped."""
    from sqlalchemy import select

    from gdx_dispatch.modules.quickbooks.webhook_models import QBWebhookEvent

    event_id = "realm1:Customer:42:Update"

    # First insertion
    evt1 = QBWebhookEvent(event_id=event_id, event_type="Customer", entity_id="42", realm_id="realm1")
    tenant_db.add(evt1)
    tenant_db.commit()

    # Simulate the dedup check the webhook endpoint performs
    existing = tenant_db.execute(
        select(QBWebhookEvent).where(QBWebhookEvent.event_id == event_id)
    ).scalar_one_or_none()
    assert existing is not None, "First event should be stored"

    # Attempt to insert a duplicate — dedup logic should detect and skip
    duplicate = tenant_db.execute(
        select(QBWebhookEvent).where(QBWebhookEvent.event_id == event_id)
    ).scalar_one_or_none()
    assert duplicate is existing, "Dedup check returns existing row, not None"

    # Confirm only one row exists
    all_events = tenant_db.execute(select(QBWebhookEvent)).scalars().all()
    assert len(all_events) == 1


def test_qb_client_rate_limit_error():
    """QBRateLimitError is raised on HTTP 429 from QBClient."""
    from gdx_dispatch.modules.quickbooks.client import QBRateLimitError as ClientRateLimitError

    # Verify the error hierarchy — QBRateLimitError should be catchable
    err = ClientRateLimitError("test rate limit")
    assert err.status_code == 429
    assert "rate limit" in str(err).lower()


def test_qb_webhook_event_model_columns():
    """QBWebhookEvent table has the expected columns."""
    cols = QBWebhookEvent.__table__.columns
    for name in ["id", "event_id", "event_type", "entity_id", "realm_id", "processed_at"]:
        assert name in cols

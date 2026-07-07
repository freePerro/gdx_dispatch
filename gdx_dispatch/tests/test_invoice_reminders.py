"""Tests for the invoice_reminders router."""
from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.invoice_reminders import router


def _make_client(tenant_id: str = "tenant-test") -> TestClient:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)

    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    setup = Session()
    setup.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS tenant_module_grants (
                id TEXT PRIMARY KEY, tenant_id TEXT, module_key TEXT,
                granted_at TEXT, created_at TEXT, expires_at TEXT
            )
            """
        )
    )
    setup.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS company_module_grants (
                id TEXT PRIMARY KEY, company_id TEXT, module_key TEXT,
                granted_at TEXT, created_at TEXT, expires_at TEXT,
                UNIQUE(company_id, module_key)
            )
            """
        )
    )
    setup.execute(
        text(
            """
            INSERT OR IGNORE INTO tenant_module_grants (id, tenant_id, module_key, granted_at, created_at)
            VALUES (:id, :tid, 'invoices', datetime('now'), datetime('now'))
            """
        ),
        {"id": f"g1-{tenant_id}", "tid": tenant_id},
    )
    setup.execute(
        text(
            """
            INSERT OR IGNORE INTO company_module_grants (id, company_id, module_key, granted_at, created_at)
            VALUES (:id, :tid, 'invoices', datetime('now'), datetime('now'))
            """
        ),
        {"id": f"g2-{tenant_id}", "tid": tenant_id},
    )
    setup.commit()
    setup.close()

    def _override_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()

    @app.middleware("http")
    async def inject_tenant(request, call_next):
        request.state.tenant = {"id": tenant_id}
        return await call_next(request)

    app.include_router(router)
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "user-1",
        "sub": "user-1",
        "role": "admin",
        "tenant_id": tenant_id,
    }

    tc = TestClient(app, raise_server_exceptions=True)
    tc._engine = engine  # type: ignore[attr-defined]
    return tc


@pytest.fixture()
def client():
    tc = _make_client()
    yield tc
    tc.app.dependency_overrides.clear()
    tc._engine.dispose()  # type: ignore[attr-defined]


def test_get_settings_creates_default(client: TestClient):
    r = client.get("/api/invoice-reminders/settings")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["enabled"] is True
    assert data["schedule_days"] == [7, 14, 30]
    assert "invoice_number" in data["subject_template"]
    assert data["company_id"] == "tenant-test"


def test_update_settings(client: TestClient):
    payload = {
        "enabled": False,
        "schedule_days": [3, 10, 21, 45],
        "subject_template": "Reminder {invoice_number}",
        "body_template": "Hi {customer_name}, you owe ${amount_due}.",
    }
    r = client.post("/api/invoice-reminders/settings", json=payload)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["enabled"] is False
    assert data["schedule_days"] == [3, 10, 21, 45]
    assert data["subject_template"] == "Reminder {invoice_number}"

    # Settings are persisted and returned from GET too.
    r2 = client.get("/api/invoice-reminders/settings").json()
    assert r2["enabled"] is False
    assert r2["schedule_days"] == [3, 10, 21, 45]


def test_schedule_days_validation(client: TestClient):
    bad = {
        "enabled": True,
        "schedule_days": [-1, 500],
        "subject_template": "x",
        "body_template": "y",
    }
    r = client.post("/api/invoice-reminders/settings", json=bad)
    assert r.status_code == 422




def _seed_invoice_row(tc: TestClient, invoice_id: str) -> None:
    """PR6: email reminders now resolve the real invoice (and 404 on ghosts
    instead of minting orphan log rows) — seed a minimal sent invoice."""
    from sqlalchemy.orm import sessionmaker as _sm

    from gdx_dispatch.models.tenant_models import Invoice as _Inv

    Session = _sm(bind=tc._engine, autoflush=False, autocommit=False)  # type: ignore[attr-defined]
    db = Session()
    try:
        from datetime import date as _date
        from datetime import timedelta as _td
        from decimal import Decimal as _D
        db.add(_Inv(
            id=UUID(invoice_id),
            company_id="tenant-test",
            customer_id=uuid4(),
            invoice_number=f"INV-{invoice_id[:8]}",
            billing_type="standard",
            sequence_number=1,
            subtotal=_D("100"),
            tax_amount=_D("0"),
            total=_D("100"),
            balance_due=_D("100"),
            status="sent",
            invoice_date=_date.today() - _td(days=20),
            due_date=_date.today() - _td(days=10),
            public_token=invoice_id.replace("-", ""),
            locked=False,
        ))
        db.commit()
    finally:
        db.close()

def test_send_reminder_creates_payment_reminder_row(client: TestClient):
    invoice_id = str(uuid4())
    _seed_invoice_row(client, invoice_id)
    r = client.post(
        f"/api/invoices/{invoice_id}/send-reminder",
        json={"channel": "email", "stage": "friendly", "notes": "First touch"},
    )
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["invoice_id"] == invoice_id
    assert data["stage"] == "friendly"
    assert data["channel"] == "email"
    # PR6: the email path now reports delivery honestly — no SMTP/customer
    # in this fixture, so the note carries the skip reason.
    assert data["notes"].startswith("First touch")
    assert "[skipped:" in data["notes"]
    assert data["sent"] is False
    assert data["sent_at"] is not None


def test_reminder_history(client: TestClient):
    invoice_id = str(uuid4())
    _seed_invoice_row(client, invoice_id)
    client.post(
        f"/api/invoices/{invoice_id}/send-reminder",
        json={"channel": "email", "stage": "friendly"},
    )
    client.post(
        f"/api/invoices/{invoice_id}/send-reminder",
        json={"channel": "sms", "stage": "first_reminder"},
    )
    r = client.get(f"/api/invoices/{invoice_id}/reminder-history")
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == 2
    # Newest-first ordering
    assert rows[0]["stage"] in ("friendly", "first_reminder")
    stages = [row["stage"] for row in rows]
    assert set(stages) == {"friendly", "first_reminder"}


def test_preview_renders_placeholders(client: TestClient):
    r = client.post(
        "/api/invoice-reminders/preview",
        json={
            "invoice_number": "INV-123",
            "customer_name": "Jane Homeowner",
            "amount_due": 450.5,
            "days_overdue": 14,
            "due_date": "2026-03-22",
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["subject"] == "Payment reminder for invoice INV-123"
    assert "Jane Homeowner" in data["body"]
    assert "INV-123" in data["body"]
    assert "450.50" in data["body"]
    assert "14" in data["body"]
    assert "2026-03-22" in data["body"]


def test_tenant_scope():
    c1 = _make_client(tenant_id="tenant-a")
    c2 = _make_client(tenant_id="tenant-b")
    try:
        c1.post(
            "/api/invoice-reminders/settings",
            json={
                "enabled": False,
                "schedule_days": [1, 2, 3],
                "subject_template": "A subject {invoice_number}",
                "body_template": "A body",
            },
        )
        # Tenant B still sees defaults (its own row)
        r2 = c2.get("/api/invoice-reminders/settings").json()
        assert r2["enabled"] is True
        assert r2["schedule_days"] == [7, 14, 30]
        assert r2["company_id"] == "tenant-b"

        # Tenant A retains its customized settings
        r1 = c1.get("/api/invoice-reminders/settings").json()
        assert r1["enabled"] is False
        assert r1["schedule_days"] == [1, 2, 3]
        assert r1["company_id"] == "tenant-a"
    finally:
        c1.app.dependency_overrides.clear()
        c2.app.dependency_overrides.clear()
        c1._engine.dispose()  # type: ignore[attr-defined]
        c2._engine.dispose()  # type: ignore[attr-defined]

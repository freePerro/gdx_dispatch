from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID

import jwt
import pytest


def _mock_request(tenant_id="test-tenant"):
    r = MagicMock()
    r.state.tenant = {"id": tenant_id}
    r.client.host = "127.0.0.1"
    return r
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models.tenant_models import Customer, Document, Invoice, Job
from gdx_dispatch.modules.customer_portal.models import CustomerUser
from gdx_dispatch.modules.equipment.models import CustomerEquipment
from gdx_dispatch.routers import portal as portal_router
from uuid import uuid4


@pytest.fixture()
def tenant_db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    TenantBase.metadata.create_all(bind=engine, checkfirst=True)
    Customer.__table__.create(bind=engine, checkfirst=True)
    CustomerUser.__table__.create(bind=engine, checkfirst=True)
    Job.__table__.create(bind=engine, checkfirst=True)
    Invoice.__table__.create(bind=engine, checkfirst=True)
    Document.__table__.create(bind=engine, checkfirst=True)
    CustomerEquipment.__table__.create(bind=engine, checkfirst=True)

    db = Session()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def _seed_customer_data(db):
    customer_a = Customer(name="Customer A", email="a@example.com", company_id="tenant-test")
    customer_b = Customer(name="Customer B", email="b@example.com", company_id="tenant-test")
    db.add_all([customer_a, customer_b])
    db.commit()
    db.refresh(customer_a)
    db.refresh(customer_b)

    user_a = CustomerUser(customer_id=customer_a.id, email="a@example.com", is_active=True)
    user_b = CustomerUser(customer_id=customer_b.id, email="b@example.com", is_active=True)
    db.add_all([user_a, user_b])
    db.commit()
    db.refresh(user_a)
    db.refresh(user_b)

    job_a = Job(customer_id=customer_a.id, title="A Job", lifecycle_stage="scheduled", dispatch_status="assigned", company_id="tenant-test")
    job_b = Job(customer_id=customer_b.id, title="B Job", lifecycle_stage="scheduled", dispatch_status="assigned", company_id="tenant-test")
    db.add_all([job_a, job_b])
    db.commit()
    db.refresh(job_a)
    db.refresh(job_b)

    inv_a = Invoice(
        customer_id=uuid4(),
        job_id=job_a.id,
        invoice_number="INV-A",
        subtotal=100,
        tax_amount=0,
        total=100,
        balance_due=100,
        status="sent",
        public_token="pub-a",
        company_id="tenant-test",
    )
    inv_b = Invoice(
        customer_id=uuid4(),
        job_id=job_b.id,
        invoice_number="INV-B",
        subtotal=200,
        tax_amount=0,
        total=200,
        balance_due=200,
        status="sent",
        public_token="pub-b",
        company_id="tenant-test",
    )
    db.add_all([inv_a, inv_b])

    doc_a = Document(filename="a.pdf", original_name="a.pdf", file_size=1, customer_id=customer_a.id)
    doc_b = Document(filename="b.pdf", original_name="b.pdf", file_size=1, customer_id=customer_b.id)
    db.add_all([doc_a, doc_b])

    equip_a = CustomerEquipment(customer_id=customer_a.id, equipment_type="garage_door", manufacturer="LiftMaster")
    equip_b = CustomerEquipment(customer_id=customer_b.id, equipment_type="opener", manufacturer="Genie")
    db.add_all([equip_a, equip_b])

    db.commit()
    return {
        "customer_a_id": customer_a.id,
        "customer_b_id": customer_b.id,
        "user_a_id": user_a.id,
        "user_b_id": user_b.id,
        "user_a_email": user_a.email,
        "inv_a_id": inv_a.id,
        "inv_b_id": inv_b.id,
        "job_a_id": job_a.id,
        "job_b_id": job_b.id,
    }


def _principal(user_id: UUID, customer_id: UUID) -> portal_router.PortalPrincipal:
    return portal_router.PortalPrincipal(user_id=user_id, customer_id=customer_id, role="customer")


def _issue_token(user_id: UUID, customer_id: UUID) -> str:
    user = SimpleNamespace(id=user_id, customer_id=customer_id)
    return portal_router.issue_customer_access_token(user)


def test_module_gate_requires_customer_portal():
    dep_calls = [d.dependency for d in portal_router.router.dependencies]
    from gdx_dispatch.core.modules import require_module

    assert require_module("customer_portal") in dep_calls


def test_login_sends_email(tenant_db_session, monkeypatch):
    seeded = _seed_customer_data(tenant_db_session)

    sent: list[dict] = []

    def _fake_send(to_email: str, magic_link: str) -> None:
        sent.append({"to": to_email, "link": magic_link})

    monkeypatch.setattr(portal_router, "send_portal_magic_link_email", _fake_send)

    body = portal_router.portal_login(
        payload=portal_router.PortalLoginIn(email=seeded["user_a_email"]),
        request=_mock_request(),
        db=tenant_db_session,
    )
    assert body["ok"] is True
    assert len(sent) == 1
    assert sent[0]["to"] == seeded["user_a_email"]


def test_verify_valid_token_returns_jwt(tenant_db_session):
    seeded = _seed_customer_data(tenant_db_session)

    user = tenant_db_session.get(CustomerUser, seeded["user_a_id"])
    user.portal_token = "valid-token"
    user.portal_token_expires_at = datetime.now(UTC) + timedelta(minutes=10)
    tenant_db_session.commit()

    payload = portal_router.portal_verify(token="valid-token", request=_mock_request(), db=tenant_db_session)
    assert payload["token_type"] == "bearer"
    claims = jwt.decode(payload["access_token"], portal_router.VERIFY_KEY, algorithms=[portal_router.ALG])
    assert claims["role"] == "customer"
    assert claims["customer_id"] == str(seeded["customer_a_id"])


def test_verify_expired_token_returns_401(tenant_db_session):
    seeded = _seed_customer_data(tenant_db_session)

    user = tenant_db_session.get(CustomerUser, seeded["user_a_id"])
    user.portal_token = "expired-token"
    user.portal_token_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    tenant_db_session.commit()

    with pytest.raises(Exception) as exc:
        portal_router.portal_verify(token="expired-token", request=_mock_request(), db=tenant_db_session)
    assert getattr(exc.value, "status_code", None) == 401


def test_dashboard_shows_customer_data(tenant_db_session):
    seeded = _seed_customer_data(tenant_db_session)
    principal = _principal(seeded["user_a_id"], seeded["customer_a_id"])

    body = portal_router.portal_dashboard(principal=principal, db=tenant_db_session)
    assert body["customer_id"] == str(seeded["customer_a_id"])
    assert body["counts"]["jobs"] == 1
    assert body["counts"]["invoices"] == 1
    assert body["counts"]["equipment"] == 1


def test_customer_cant_see_other_customers_data(tenant_db_session):
    seeded = _seed_customer_data(tenant_db_session)
    principal = _principal(seeded["user_a_id"], seeded["customer_a_id"])

    jobs = portal_router.portal_jobs(principal=principal, db=tenant_db_session)
    invoices = portal_router.portal_invoices(principal=principal, db=tenant_db_session)
    documents = portal_router.portal_documents(principal=principal, db=tenant_db_session)

    job_ids = {row["id"] for row in jobs}
    invoice_ids = {row["id"] for row in invoices}
    document_customers = {row["customer_id"] for row in documents}

    assert str(seeded["job_a_id"]) in job_ids
    assert str(seeded["job_b_id"]) not in job_ids
    assert str(seeded["inv_a_id"]) in invoice_ids
    assert str(seeded["inv_b_id"]) not in invoice_ids
    assert document_customers == {str(seeded["customer_a_id"])}


def test_jobs_history_endpoint_filters_to_customer(tenant_db_session):
    seeded = _seed_customer_data(tenant_db_session)
    principal = _principal(seeded["user_a_id"], seeded["customer_a_id"])

    rows = portal_router.portal_jobs(principal=principal, db=tenant_db_session)
    assert len(rows) == 1


def test_invoices_endpoint_includes_payment_status(tenant_db_session):
    seeded = _seed_customer_data(tenant_db_session)
    principal = _principal(seeded["user_a_id"], seeded["customer_a_id"])

    row = portal_router.portal_invoices(principal=principal, db=tenant_db_session)[0]
    assert row["status"] == "sent"
    assert row["payment_status"] == "unpaid"


def test_equipment_endpoint_filters_to_customer(tenant_db_session):
    seeded = _seed_customer_data(tenant_db_session)
    principal = _principal(seeded["user_a_id"], seeded["customer_a_id"])

    rows = portal_router.portal_equipment(principal=principal, db=tenant_db_session)
    assert len(rows) == 1
    assert rows[0]["customer_id"] == str(seeded["customer_a_id"])


def test_booking_creates_request(tenant_db_session):
    seeded = _seed_customer_data(tenant_db_session)
    principal = _principal(seeded["user_a_id"], seeded["customer_a_id"])

    payload = portal_router.BookingIn(
        requested_date=datetime.fromisoformat("2030-04-01T10:00:00+00:00"),
        service_type="maintenance",
        notes="Need tune-up",
    )
    out = portal_router.portal_booking(payload=payload, request=_mock_request(), principal=principal, db=tenant_db_session)
    assert out["status"] == "requested"

    row = tenant_db_session.execute(
        text("SELECT customer_id, service_type, notes FROM portal_booking_requests WHERE id = :id"),
        {"id": out["id"]},
    ).mappings().first()
    assert row is not None
    assert row["customer_id"] == str(seeded["customer_a_id"])
    assert row["service_type"] == "maintenance"


def test_pay_invoice_creates_payment_intent(tenant_db_session, monkeypatch):
    seeded = _seed_customer_data(tenant_db_session)
    principal = _principal(seeded["user_a_id"], seeded["customer_a_id"])

    calls: list[dict] = []

    def _fake_create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(id="pi_123", client_secret="secret_123", status="requires_payment_method")

    monkeypatch.setattr("gdx_dispatch.routers.portal.stripe.PaymentIntent.create", _fake_create)

    body = portal_router.portal_invoice_pay(
        invoice_id=seeded["inv_a_id"],
        principal=principal,
        db=tenant_db_session,
    )
    assert body["payment_intent_id"] == "pi_123"
    assert body["client_secret"] == "secret_123"
    assert len(calls) == 1
    assert calls[0]["amount"] == 10000


def test_message_creates_record(tenant_db_session):
    seeded = _seed_customer_data(tenant_db_session)
    principal = _principal(seeded["user_a_id"], seeded["customer_a_id"])

    out = portal_router.portal_message(
        payload=portal_router.MessageIn(subject="Question", message="Can someone call me?"),
        request=_mock_request(),
        principal=principal,
        db=tenant_db_session,
    )
    assert out["status"] == "sent"

    row = tenant_db_session.execute(
        text("SELECT customer_id, subject FROM portal_messages WHERE id = :id"),
        {"id": out["id"]},
    ).mappings().first()
    assert row is not None
    assert row["customer_id"] == str(seeded["customer_a_id"])
    assert row["subject"] == "Question"


def test_verify_token_cannot_be_reused(tenant_db_session):
    seeded = _seed_customer_data(tenant_db_session)

    user = tenant_db_session.get(CustomerUser, seeded["user_a_id"])
    user.portal_token = "one-time-token"
    user.portal_token_expires_at = datetime.now(UTC) + timedelta(minutes=10)
    tenant_db_session.commit()

    first = portal_router.portal_verify(token="one-time-token", request=_mock_request(), db=tenant_db_session)
    assert first["token_type"] == "bearer"

    with pytest.raises(Exception) as exc:
        portal_router.portal_verify(token="one-time-token", request=_mock_request(), db=tenant_db_session)
    assert getattr(exc.value, "status_code", None) == 401


def test_get_current_customer_accepts_valid_customer_jwt(tenant_db_session):
    seeded = _seed_customer_data(tenant_db_session)
    token = _issue_token(seeded["user_a_id"], seeded["customer_a_id"])

    principal = portal_router.get_current_portal_customer(token=token, db=tenant_db_session)
    assert principal.role == "customer"
    assert principal.customer_id == seeded["customer_a_id"]

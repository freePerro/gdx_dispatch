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
    r.base_url = "http://testserver/"
    return r
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models.tenant_models import AppSettings, Customer, Document, Invoice, Job
from gdx_dispatch.modules.customer_portal.models import CustomerUser
from gdx_dispatch.modules.equipment.models import CustomerEquipment
from gdx_dispatch.modules.proposals.models import Estimate, EstimateLine
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
    AppSettings.__table__.create(bind=engine, checkfirst=True)
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


def _get_user(db, user_id):
    return db.execute(select(CustomerUser).where(CustomerUser.id == user_id)).scalar_one()


def _decode_portal_jwt(token: str) -> dict:
    return jwt.decode(token, portal_router.VERIFY_KEY, algorithms=[portal_router.ALG])


def test_module_gate_requires_customer_portal():
    dep_calls = [d.dependency for d in portal_router.router.dependencies]
    from gdx_dispatch.core.modules import require_module

    assert require_module("customer_portal") in dep_calls


def _fake_email_capture(monkeypatch):
    sent: list[dict] = []

    def _fake_send(db, tenant_id, to_email, magic_link, **kwargs):
        sent.append({"to": to_email, "link": magic_link, "tenant_id": tenant_id, **kwargs})
        return True, None

    monkeypatch.setattr(portal_router, "send_portal_magic_link_email", _fake_send)
    return sent


def test_login_sends_email(tenant_db_session, monkeypatch):
    seeded = _seed_customer_data(tenant_db_session)
    sent = _fake_email_capture(monkeypatch)

    body = portal_router.portal_login(
        payload=portal_router.PortalLoginIn(email=seeded["user_a_email"]),
        request=_mock_request(),
        db=tenant_db_session,
    )
    assert body["ok"] is True
    assert len(sent) == 1
    assert sent[0]["to"] == seeded["user_a_email"]
    # The emailed link must land on the SPA portal page, not a bare API route.
    assert "/customer-portal?token=" in sent[0]["link"]
    assert sent[0]["link"].startswith("http://testserver")


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


# ---------------------------------------------------------------------------
# Estimates (customer-facing)
# ---------------------------------------------------------------------------

def _seed_estimate(db, customer_id, status="sent", number=None):
    est = Estimate(
        customer_id=customer_id,
        estimate_number=number or f"EST-{uuid4().hex[:8]}",
        label="New garage door",
        total=1500,
        status=status,
        public_token=uuid4().hex,
        company_id="tenant-test",
    )
    db.add(est)
    db.commit()
    db.refresh(est)
    return est


def test_estimates_show_only_customer_visible_statuses(tenant_db_session):
    seeded = _seed_customer_data(tenant_db_session)
    sent = _seed_estimate(tenant_db_session, seeded["customer_a_id"], status="sent")
    _seed_estimate(tenant_db_session, seeded["customer_a_id"], status="draft")
    _seed_estimate(tenant_db_session, seeded["customer_b_id"], status="sent")
    principal = _principal(seeded["user_a_id"], seeded["customer_a_id"])

    rows = portal_router.portal_estimates(principal=principal, db=tenant_db_session)
    assert [row["id"] for row in rows] == [str(sent.id)]
    assert rows[0]["status"] == "sent"


def test_rejected_estimate_stays_visible_and_reads_as_sent(tenant_db_session):
    """'rejected' = the estimate EMAIL bounced (bounce detector,
    2026-08-13). The customer's portal must keep showing the estimate —
    a detector false positive must not vanish it mid-decision — and the
    internal bookkeeping word must not leak (it reads as "we rejected
    YOU"), so the serializer presents it as plain 'sent'."""
    seeded = _seed_customer_data(tenant_db_session)
    est = _seed_estimate(tenant_db_session, seeded["customer_a_id"], status="rejected")
    principal = _principal(seeded["user_a_id"], seeded["customer_a_id"])

    rows = portal_router.portal_estimates(principal=principal, db=tenant_db_session)
    assert [row["id"] for row in rows] == [str(est.id)]
    assert rows[0]["status"] == "sent"


def test_rejected_estimate_accept_self_heals(tenant_db_session):
    """An accept from 'rejected' is the strongest proof the customer DID
    receive the estimate — it must succeed, not 409/404."""
    seeded = _seed_customer_data(tenant_db_session)
    est = _seed_estimate(tenant_db_session, seeded["customer_a_id"], status="rejected")
    principal = _principal(seeded["user_a_id"], seeded["customer_a_id"])

    body = portal_router.portal_estimate_accept(
        estimate_id=est.id, request=_mock_request(), principal=principal, db=tenant_db_session
    )
    assert body["status"] == "accepted"

    tenant_db_session.refresh(est)
    assert est.status == "accepted"


def test_rejected_estimate_decline_works(tenant_db_session):
    seeded = _seed_customer_data(tenant_db_session)
    est = _seed_estimate(tenant_db_session, seeded["customer_a_id"], status="rejected")
    principal = _principal(seeded["user_a_id"], seeded["customer_a_id"])

    body = portal_router.portal_estimate_decline(
        estimate_id=est.id,
        request=_mock_request(),
        payload=portal_router.DeclineEstimateIn(reason="Went another way"),
        principal=principal,
        db=tenant_db_session,
    )
    assert body["status"] == "declined"


def test_estimate_accept_marks_accepted(tenant_db_session):
    seeded = _seed_customer_data(tenant_db_session)
    est = _seed_estimate(tenant_db_session, seeded["customer_a_id"], status="sent")
    principal = _principal(seeded["user_a_id"], seeded["customer_a_id"])

    body = portal_router.portal_estimate_accept(
        estimate_id=est.id, request=_mock_request(), principal=principal, db=tenant_db_session
    )
    assert body["status"] == "accepted"
    assert body["accepted_at"] is not None

    with pytest.raises(Exception) as exc:
        portal_router.portal_estimate_accept(
            estimate_id=est.id, request=_mock_request(), principal=principal, db=tenant_db_session
        )
    assert getattr(exc.value, "status_code", None) == 409


def test_portal_accept_asks_then_pay_click_mints_deposit(tenant_db_session, monkeypatch):
    """2026-08-18 (Doug): acceptance only ASKS for the deposit — no invoice
    exists until the customer actually moves to pay online (accept-time
    minting left phantom 'sent' invoices like INV-000341 inflating A/R).
    The pay endpoint mints once; a second click lands on the same invoice."""
    import gdx_dispatch.modules.estimates_features as features_module

    # deposit_ask_for late-imports get_features from the source module.
    monkeypatch.setattr(
        features_module,
        "get_features",
        lambda tenant_id: SimpleNamespace(deposit_pct=50, hide_line_prices=False),
    )
    seeded = _seed_customer_data(tenant_db_session)
    est = _seed_estimate(tenant_db_session, seeded["customer_a_id"], status="sent")
    tenant_db_session.add(EstimateLine(
        estimate_id=est.id, description="New door", quantity=1,
        unit_price=1500, line_total=1500, sort_order=1, company_id="tenant-test",
    ))
    tenant_db_session.commit()
    principal = _principal(seeded["user_a_id"], seeded["customer_a_id"])

    def _deposit_rows():
        return tenant_db_session.execute(
            select(Invoice).where(
                Invoice.estimate_id == est.id, Invoice.billing_type == "deposit"
            )
        ).scalars().all()

    body = portal_router.portal_estimate_accept(
        estimate_id=est.id, request=_mock_request(), principal=principal, db=tenant_db_session
    )
    assert "deposit" not in body
    ask_amount = body["deposit_ask"]["amount"]
    assert body["deposit_ask"]["pct"] == 50 and ask_amount > 0
    assert _deposit_rows() == []  # an ask is a request, not a receivable

    out = portal_router.portal_estimate_deposit_pay(
        estimate_id=est.id, request=_mock_request(), principal=principal, db=tenant_db_session
    )
    assert out["deposit"]["amount"] == pytest.approx(ask_amount)
    assert len(_deposit_rows()) == 1

    out2 = portal_router.portal_estimate_deposit_pay(
        estimate_id=est.id, request=_mock_request(), principal=principal, db=tenant_db_session
    )
    assert out2["deposit"]["existing"] is True
    assert out2["deposit"]["invoice_number"] == out["deposit"]["invoice_number"]
    assert len(_deposit_rows()) == 1

    # With a live still-owed invoice, the serializer serves IT, not the ask.
    detail = portal_router.portal_estimates(principal=principal, db=tenant_db_session)[0]
    assert detail["deposit"]["invoice_number"] == out["deposit"]["invoice_number"]
    assert "deposit_ask" not in detail


def test_portal_deposit_pay_gates(tenant_db_session, monkeypatch):
    """Not accepted → 409; accepted with no deposit percent → 404. Neither
    leaves an invoice row behind."""
    import gdx_dispatch.modules.estimates_features as features_module

    monkeypatch.setattr(
        features_module,
        "get_features",
        lambda tenant_id: SimpleNamespace(deposit_pct=0, hide_line_prices=False),
    )
    seeded = _seed_customer_data(tenant_db_session)
    est = _seed_estimate(tenant_db_session, seeded["customer_a_id"], status="sent")
    principal = _principal(seeded["user_a_id"], seeded["customer_a_id"])

    with pytest.raises(Exception) as exc:
        portal_router.portal_estimate_deposit_pay(
            estimate_id=est.id, request=_mock_request(), principal=principal, db=tenant_db_session
        )
    assert getattr(exc.value, "status_code", None) == 409

    est.status = "accepted"
    tenant_db_session.commit()
    with pytest.raises(Exception) as exc:
        portal_router.portal_estimate_deposit_pay(
            estimate_id=est.id, request=_mock_request(), principal=principal, db=tenant_db_session
        )
    assert getattr(exc.value, "status_code", None) == 404
    assert tenant_db_session.execute(
        select(Invoice).where(
            Invoice.estimate_id == est.id, Invoice.billing_type == "deposit"
        )
    ).scalars().all() == []


def test_estimate_decline_records_reason(tenant_db_session):
    seeded = _seed_customer_data(tenant_db_session)
    est = _seed_estimate(tenant_db_session, seeded["customer_a_id"], status="sent")
    principal = _principal(seeded["user_a_id"], seeded["customer_a_id"])

    body = portal_router.portal_estimate_decline(
        estimate_id=est.id,
        request=_mock_request(),
        payload=portal_router.DeclineEstimateIn(reason="Too expensive"),
        principal=principal,
        db=tenant_db_session,
    )
    assert body["status"] == "declined"
    tenant_db_session.refresh(est)
    assert est.declined_reason == "Too expensive"


def test_portal_decision_alerts_the_office(tenant_db_session):
    """Same gap as the emailed public link (2026-08-17): a portal accept or
    decline was invisible to the office until someone reopened the estimate.
    Both must land a broadcast bell notification — user_id NULL (every office
    user), category "estimate" (drawer deep-link)."""
    from gdx_dispatch.models.tenant_models import Notification

    seeded = _seed_customer_data(tenant_db_session)
    principal = _principal(seeded["user_a_id"], seeded["customer_a_id"])

    est = _seed_estimate(tenant_db_session, seeded["customer_a_id"], status="sent")
    portal_router.portal_estimate_accept(
        estimate_id=est.id, request=_mock_request(), principal=principal, db=tenant_db_session
    )
    rows = tenant_db_session.execute(select(Notification)).scalars().all()
    assert len(rows) == 1
    assert rows[0].user_id is None
    assert rows[0].category == "estimate"
    assert rows[0].title == "Estimate accepted"
    assert "Customer A" in rows[0].message
    assert est.estimate_number in rows[0].message

    est2 = _seed_estimate(tenant_db_session, seeded["customer_a_id"], status="sent")
    portal_router.portal_estimate_decline(
        estimate_id=est2.id,
        request=_mock_request(),
        payload=portal_router.DeclineEstimateIn(reason="Too expensive"),
        principal=principal,
        db=tenant_db_session,
    )
    rows = tenant_db_session.execute(select(Notification)).scalars().all()
    assert sorted(r.title for r in rows) == ["Estimate accepted", "Estimate declined"]
    declined = next(r for r in rows if r.title == "Estimate declined")
    assert '"Too expensive"' in declined.message


def _seed_estimate_with_lines(db, customer_id, status="sent"):
    est = _seed_estimate(db, customer_id, status=status)
    db.add_all([
        EstimateLine(estimate_id=est.id, description="16x7 insulated door", quantity=1, unit_price=2450, line_total=2450, sort_order=1, company_id="tenant-test"),
        EstimateLine(estimate_id=est.id, description="Haul away", quantity=1, unit_price=150, line_total=150, sort_order=2, company_id="tenant-test"),
    ])
    est.total = 2600
    db.commit()
    db.refresh(est)
    return est


def test_estimate_detail_includes_lines_and_totals(tenant_db_session):
    seeded = _seed_customer_data(tenant_db_session)
    est = _seed_estimate_with_lines(tenant_db_session, seeded["customer_a_id"])
    principal = _principal(seeded["user_a_id"], seeded["customer_a_id"])

    body = portal_router.portal_estimate_detail(estimate_id=est.id, request=_mock_request(), principal=principal, db=tenant_db_session)
    assert [line["description"] for line in body["lines"]] == ["16x7 insulated door", "Haul away"]
    assert body["totals"]["subtotal"] == 2600.0
    assert body["totals"]["tax_unavailable"] is False
    # Card total and breakdown total come from one computation — must agree.
    assert body["total"] == body["totals"]["total"]
    assert body["totals"]["total"] == pytest.approx(2600.0 + body["totals"]["tax"] - body["totals"]["discount"])

    principal_b = _principal(seeded["user_b_id"], seeded["customer_b_id"])
    with pytest.raises(Exception) as exc:
        portal_router.portal_estimate_detail(estimate_id=est.id, request=_mock_request(), principal=principal_b, db=tenant_db_session)
    assert getattr(exc.value, "status_code", None) == 404


def test_estimate_detail_lists_only_renderable_image_attachments(tenant_db_session):
    seeded = _seed_customer_data(tenant_db_session)
    est = _seed_estimate_with_lines(tenant_db_session, seeded["customer_a_id"])
    photo = Document(filename="stored-door.png", original_name="door.png", file_size=10, content_type="image/png", estimate_id=est.id)
    pdf = Document(filename="stored-spec.pdf", original_name="spec.pdf", file_size=10, content_type="application/pdf", estimate_id=est.id)
    # HEIC passes the staff upload allow-list but browsers can't render it —
    # it must not be advertised to the portal.
    heic = Document(filename="stored-photo.heic", original_name="photo.heic", file_size=10, content_type="image/heic", estimate_id=est.id)
    tenant_db_session.add_all([photo, pdf, heic])
    tenant_db_session.commit()
    principal = _principal(seeded["user_a_id"], seeded["customer_a_id"])

    body = portal_router.portal_estimate_detail(estimate_id=est.id, request=_mock_request(), principal=principal, db=tenant_db_session)
    assert [img["original_name"] for img in body["images"]] == ["door.png"]
    assert body["images"][0]["url"] == f"/portal/estimates/{est.id}/attachments/{photo.id}"


def test_estimate_attachment_streams_image_and_isolates_customers(tenant_db_session, monkeypatch, tmp_path):
    seeded = _seed_customer_data(tenant_db_session)
    est = _seed_estimate_with_lines(tenant_db_session, seeded["customer_a_id"])
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
    file_dir = tmp_path / "test-tenant" / "estimate" / str(est.id)
    file_dir.mkdir(parents=True)
    (file_dir / "stored-door.png").write_bytes(b"\x89PNG fake")

    photo = Document(filename="stored-door.png", original_name="door.png", file_size=9, content_type="image/png", estimate_id=est.id)
    tenant_db_session.add(photo)
    tenant_db_session.commit()

    principal = _principal(seeded["user_a_id"], seeded["customer_a_id"])
    resp = portal_router.portal_estimate_attachment(
        estimate_id=est.id, document_id=photo.id, request=_mock_request(), principal=principal, db=tenant_db_session
    )
    assert resp.path == str(file_dir / "stored-door.png")
    assert resp.media_type == "image/png"

    principal_b = _principal(seeded["user_b_id"], seeded["customer_b_id"])
    with pytest.raises(Exception) as exc:
        portal_router.portal_estimate_attachment(
            estimate_id=est.id, document_id=photo.id, request=_mock_request(), principal=principal_b, db=tenant_db_session
        )
    assert getattr(exc.value, "status_code", None) == 404


def test_estimate_detail_degraded_totals_are_flagged(tenant_db_session, monkeypatch):
    seeded = _seed_customer_data(tenant_db_session)
    est = _seed_estimate_with_lines(tenant_db_session, seeded["customer_a_id"])
    principal = _principal(seeded["user_a_id"], seeded["customer_a_id"])

    def _boom(*_a, **_k):
        raise RuntimeError("tax engine down")

    monkeypatch.setattr("gdx_dispatch.modules.proposals.totals.compute_estimate_totals", _boom)

    body = portal_router.portal_estimate_detail(estimate_id=est.id, request=_mock_request(), principal=principal, db=tenant_db_session)
    # Degraded path: pre-tax subtotal shown, but flagged so the UI can say
    # "final total may differ" instead of presenting it as authoritative.
    assert body["totals"]["tax_unavailable"] is True
    assert body["totals"]["total"] == 2600.0
    assert body["total"] == body["totals"]["total"]


def test_estimate_detail_strips_line_prices_when_hidden(tenant_db_session):
    # hide_line_prices is a customer-facing privacy control. The portal is a
    # JSON API, so per-line prices must be ABSENT from the payload — not merely
    # hidden in the template — or they'd leak in the raw network response.
    seeded = _seed_customer_data(tenant_db_session)
    est = _seed_estimate_with_lines(tenant_db_session, seeded["customer_a_id"])
    est.hide_line_prices = True  # per-estimate override wins over the tenant default
    tenant_db_session.commit()
    principal = _principal(seeded["user_a_id"], seeded["customer_a_id"])

    body = portal_router.portal_estimate_detail(
        estimate_id=est.id, request=_mock_request(), principal=principal, db=tenant_db_session
    )
    assert body["hide_line_prices"] is True
    # Descriptions + quantities still show; per-line prices are stripped.
    assert [line["description"] for line in body["lines"]] == ["16x7 insulated door", "Haul away"]
    for line in body["lines"]:
        assert "unit_price" not in line
        assert "line_total" not in line
    # The grand total is NOT hidden — only the per-line breakdown is.
    assert body["total"] == body["totals"]["total"]


# --- Password login (opt-in; magic-link stays as onboarding + forgot-password) ---

def test_password_login_succeeds_and_issues_customer_jwt(tenant_db_session):
    seeded = _seed_customer_data(tenant_db_session)
    user = _get_user(tenant_db_session, seeded["user_a_id"])
    user.password_hash = portal_router._hash_portal_password("hunter2-strong")
    tenant_db_session.commit()

    out = portal_router.portal_password_login(
        payload=portal_router.PortalPasswordLoginIn(email="A@Example.com", password="hunter2-strong"),
        request=_mock_request(),
        db=tenant_db_session,
    )
    assert out["token_type"] == "bearer"
    claims = _decode_portal_jwt(out["access_token"])
    assert claims["role"] == "customer"
    assert claims["sub"] == str(seeded["user_a_id"])
    assert claims["customer_id"] == str(seeded["customer_a_id"])


def test_password_login_wrong_password_is_401(tenant_db_session):
    seeded = _seed_customer_data(tenant_db_session)
    user = _get_user(tenant_db_session, seeded["user_a_id"])
    user.password_hash = portal_router._hash_portal_password("correct-horse")
    tenant_db_session.commit()

    with pytest.raises(Exception) as exc:
        portal_router.portal_password_login(
            payload=portal_router.PortalPasswordLoginIn(email="a@example.com", password="wrong"),
            request=_mock_request(),
            db=tenant_db_session,
        )
    assert getattr(exc.value, "status_code", None) == 401


def test_password_login_unknown_email_matches_bad_password_401(tenant_db_session):
    # Anti-enumeration: unknown email and wrong password return the identical 401.
    _seed_customer_data(tenant_db_session)
    with pytest.raises(Exception) as exc:
        portal_router.portal_password_login(
            payload=portal_router.PortalPasswordLoginIn(email="nobody@example.com", password="whatever"),
            request=_mock_request(),
            db=tenant_db_session,
        )
    assert getattr(exc.value, "status_code", None) == 401
    assert exc.value.detail == "Invalid email or password"


def test_password_login_rejected_when_no_password_set(tenant_db_session):
    # Seeded users have password_hash=None — password login must not work until set.
    _seed_customer_data(tenant_db_session)
    with pytest.raises(Exception) as exc:
        portal_router.portal_password_login(
            payload=portal_router.PortalPasswordLoginIn(email="a@example.com", password="anything"),
            request=_mock_request(),
            db=tenant_db_session,
        )
    assert getattr(exc.value, "status_code", None) == 401


def test_password_login_remember_extends_token_ttl(tenant_db_session):
    seeded = _seed_customer_data(tenant_db_session)
    user = _get_user(tenant_db_session, seeded["user_a_id"])
    user.password_hash = portal_router._hash_portal_password("remember-me-pw")
    tenant_db_session.commit()

    short = portal_router.portal_password_login(
        payload=portal_router.PortalPasswordLoginIn(email="a@example.com", password="remember-me-pw", remember=False),
        request=_mock_request(), db=tenant_db_session,
    )
    long = portal_router.portal_password_login(
        payload=portal_router.PortalPasswordLoginIn(email="a@example.com", password="remember-me-pw", remember=True),
        request=_mock_request(), db=tenant_db_session,
    )
    exp_short = _decode_portal_jwt(short["access_token"])["exp"]
    exp_long = _decode_portal_jwt(long["access_token"])["exp"]
    # remember=True must outlive the default 1h session by a wide margin (30d).
    assert exp_long - exp_short > 60 * 60 * 24


def test_set_password_then_password_login_roundtrip(tenant_db_session):
    # Set a password from an authenticated (e.g. magic-link) session, then use it.
    seeded = _seed_customer_data(tenant_db_session)
    principal = _principal(seeded["user_a_id"], seeded["customer_a_id"])

    out = portal_router.portal_set_password(
        payload=portal_router.PortalSetPasswordIn(new_password="brand-new-pass"),
        request=_mock_request(), principal=principal, db=tenant_db_session,
    )
    assert out["ok"] is True

    login = portal_router.portal_password_login(
        payload=portal_router.PortalPasswordLoginIn(email="a@example.com", password="brand-new-pass"),
        request=_mock_request(), db=tenant_db_session,
    )
    assert _decode_portal_jwt(login["access_token"])["sub"] == str(seeded["user_a_id"])


def test_estimate_accept_cannot_cross_customers(tenant_db_session):
    seeded = _seed_customer_data(tenant_db_session)
    est_b = _seed_estimate(tenant_db_session, seeded["customer_b_id"], status="sent")
    principal_a = _principal(seeded["user_a_id"], seeded["customer_a_id"])

    with pytest.raises(Exception) as exc:
        portal_router.portal_estimate_accept(
            estimate_id=est_b.id, request=_mock_request(), principal=principal_a, db=tenant_db_session
        )
    assert getattr(exc.value, "status_code", None) == 404


def test_context_returns_company_and_customer(tenant_db_session):
    seeded = _seed_customer_data(tenant_db_session)
    tenant_db_session.add(
        AppSettings(company_name="Garage Door Xperts", phone="(218) 555-0100", email="office@gdx.test", address="123 Main St")
    )
    tenant_db_session.commit()
    principal = _principal(seeded["user_a_id"], seeded["customer_a_id"])

    body = portal_router.portal_context(principal=principal, db=tenant_db_session)
    assert body["company"]["name"] == "Garage Door Xperts"
    assert body["company"]["phone"] == "(218) 555-0100"
    assert body["customer"]["name"] == "Customer A"


# ---------------------------------------------------------------------------
# Staff management (/api/portal)
# ---------------------------------------------------------------------------

_STAFF = {"sub": "staff-user-1"}


def test_admin_list_reports_portal_state(tenant_db_session):
    seeded = _seed_customer_data(tenant_db_session)
    no_portal = Customer(name="Customer C", email="c@example.com", company_id="tenant-test")
    tenant_db_session.add(no_portal)
    tenant_db_session.commit()

    entries = portal_router.portal_admin_list(_=_STAFF, db=tenant_db_session)
    by_id = {e["id"]: e for e in entries}
    assert by_id[str(seeded["customer_a_id"])]["portal_enabled"] is True
    assert by_id[str(no_portal.id)]["portal_enabled"] is False
    assert by_id[str(no_portal.id)]["email"] == "c@example.com"


def test_admin_toggle_disable_and_reenable(tenant_db_session):
    seeded = _seed_customer_data(tenant_db_session)

    body = portal_router.portal_admin_toggle(
        customer_id=seeded["customer_a_id"],
        payload=portal_router.PortalToggleIn(portal_enabled=False),
        request=_mock_request(),
        staff=_STAFF,
        db=tenant_db_session,
    )
    assert body["portal_enabled"] is False
    user = tenant_db_session.get(CustomerUser, seeded["user_a_id"])
    assert user.is_active is False

    body = portal_router.portal_admin_toggle(
        customer_id=seeded["customer_a_id"],
        payload=portal_router.PortalToggleIn(portal_enabled=True),
        request=_mock_request(),
        staff=_STAFF,
        db=tenant_db_session,
    )
    assert body["portal_enabled"] is True
    tenant_db_session.refresh(user)
    assert user.is_active is True


def test_admin_toggle_enable_requires_email(tenant_db_session):
    _seed_customer_data(tenant_db_session)
    no_email = Customer(name="No Email", company_id="tenant-test")
    tenant_db_session.add(no_email)
    tenant_db_session.commit()

    with pytest.raises(Exception) as exc:
        portal_router.portal_admin_toggle(
            customer_id=no_email.id,
            payload=portal_router.PortalToggleIn(portal_enabled=True),
            request=_mock_request(),
            staff=_STAFF,
            db=tenant_db_session,
        )
    assert getattr(exc.value, "status_code", None) == 400


def test_admin_invite_creates_user_and_link(tenant_db_session, monkeypatch):
    _seed_customer_data(tenant_db_session)
    sent = _fake_email_capture(monkeypatch)
    newcomer = Customer(name="Newcomer", email="new@example.com", company_id="tenant-test")
    tenant_db_session.add(newcomer)
    tenant_db_session.commit()

    body = portal_router.portal_admin_invite(
        payload=portal_router.PortalInviteIn(customer_id=newcomer.id),
        request=_mock_request(),
        staff=_STAFF,
        db=tenant_db_session,
    )
    assert body["ok"] is True
    assert body["invite_sent"] is True
    assert "/customer-portal?token=" in body["magic_link"]
    assert len(sent) == 1

    user = tenant_db_session.execute(
        select(CustomerUser).where(CustomerUser.customer_id == newcomer.id)
    ).scalar_one()
    assert user.is_active is True
    assert user.portal_token is not None
    # Invite links get the long TTL, not the 15-minute login TTL.
    expires_at = portal_router._normalize_dt(user.portal_token_expires_at)
    assert expires_at - datetime.now(UTC) > timedelta(days=1)

    # The emailed token must round-trip through verify.
    payload = portal_router.portal_verify(token=user.portal_token, request=_mock_request(), db=tenant_db_session)
    assert payload["token_type"] == "bearer"


def test_login_does_not_clobber_pending_invite(tenant_db_session, monkeypatch):
    seeded = _seed_customer_data(tenant_db_session)
    sent = _fake_email_capture(monkeypatch)

    user = tenant_db_session.get(CustomerUser, seeded["user_a_id"])
    user.portal_token = "pending-invite-token"
    user.portal_token_expires_at = datetime.now(UTC) + timedelta(days=5)
    tenant_db_session.commit()

    portal_router.portal_login(
        payload=portal_router.PortalLoginIn(email=seeded["user_a_email"]),
        request=_mock_request(),
        db=tenant_db_session,
    )
    tenant_db_session.refresh(user)
    # The public login endpoint must re-send the still-valid invite token,
    # not rotate it out from under the customer.
    assert user.portal_token == "pending-invite-token"
    assert "pending-invite-token" in sent[0]["link"]


def test_login_survives_duplicate_emails(tenant_db_session, monkeypatch):
    seeded = _seed_customer_data(tenant_db_session)
    _fake_email_capture(monkeypatch)
    dup = CustomerUser(customer_id=seeded["customer_b_id"], email=seeded["user_a_email"], is_active=True)
    tenant_db_session.add(dup)
    tenant_db_session.commit()

    body = portal_router.portal_login(
        payload=portal_router.PortalLoginIn(email=seeded["user_a_email"]),
        request=_mock_request(),
        db=tenant_db_session,
    )
    assert body["ok"] is True


def test_login_send_failure_still_returns_ok(tenant_db_session, monkeypatch):
    seeded = _seed_customer_data(tenant_db_session)

    def _fake_send_fail(db, tenant_id, to_email, magic_link, **kwargs):
        return False, "smtp_not_configured"

    monkeypatch.setattr(portal_router, "send_portal_magic_link_email", _fake_send_fail)

    body = portal_router.portal_login(
        payload=portal_router.PortalLoginIn(email=seeded["user_a_email"]),
        request=_mock_request(),
        db=tenant_db_session,
    )
    # Anti-enumeration: the caller can't learn whether delivery worked.
    assert body["ok"] is True


def test_admin_toggle_disable_deactivates_duplicates(tenant_db_session):
    seeded = _seed_customer_data(tenant_db_session)
    dup = CustomerUser(customer_id=seeded["customer_a_id"], email="a2@example.com", is_active=True, portal_token="tok")
    tenant_db_session.add(dup)
    tenant_db_session.commit()

    portal_router.portal_admin_toggle(
        customer_id=seeded["customer_a_id"],
        payload=portal_router.PortalToggleIn(portal_enabled=False),
        request=_mock_request(),
        staff=_STAFF,
        db=tenant_db_session,
    )
    rows = tenant_db_session.execute(
        select(CustomerUser).where(CustomerUser.customer_id == seeded["customer_a_id"])
    ).scalars().all()
    assert len(rows) == 2
    assert all(row.is_active is False and row.portal_token is None for row in rows)


# ─────────────────────────────────────────────────────────────────────────────
# Staff-side portal status for the customer page (2026-08-24)
#
# The customer page's Portal tab used to read `/api/customers/{id}/portal-account`,
# a ui_compat shim that returned a hardcoded {"exists": false, "account": null}.
# It reported "no portal account" for EVERY customer — including, on prod, the
# one customer who actually had one. These pin the replacement,
# `GET /api/portal/{customer_id}`, against the behaviour that was wrong.
# ─────────────────────────────────────────────────────────────────────────────


def _portal_status(db, customer_id):
    """Call the staff status endpoint with its DB dependency satisfied."""
    return portal_router.portal_admin_status(
        customer_id=customer_id,
        _={"sub": "staff-1"},
        db=db,
    )


def test_portal_status_reports_an_existing_account_as_enabled(tenant_db_session):
    """The defect this replaces: a real account rendered as 'no account'."""
    db = tenant_db_session
    customer = Customer(name="Has Portal", email="has@example.com", company_id="tenant-test")
    db.add(customer)
    db.commit()
    db.refresh(customer)
    db.add(CustomerUser(customer_id=customer.id, email="has@example.com", is_active=True))
    db.commit()

    out = _portal_status(db, customer.id)

    assert out["portal_enabled"] is True, (
        "a customer with an active CustomerUser must report enabled — the old shim "
        "hardcoded exists=false here"
    )
    assert out["email"] == "has@example.com"
    assert out["last_login"] is None  # never signed in — surfaced, not hidden


def test_portal_status_reports_no_account_as_disabled(tenant_db_session):
    """Counterfactual to the test above: absence must NOT read as enabled."""
    db = tenant_db_session
    customer = Customer(name="No Portal", email="none@example.com", company_id="tenant-test")
    db.add(customer)
    db.commit()
    db.refresh(customer)

    out = _portal_status(db, customer.id)

    assert out["portal_enabled"] is False
    # Falls back to the customer record's address so the invite dialog can
    # prefill something useful.
    assert out["email"] == "none@example.com"


def test_portal_status_deactivated_account_is_not_enabled(tenant_db_session):
    """Turning access off must actually read as off — this is what the tab's
    'Turn Off Portal Access' button produces via PATCH."""
    db = tenant_db_session
    customer = Customer(name="Was Portal", email="was@example.com", company_id="tenant-test")
    db.add(customer)
    db.commit()
    db.refresh(customer)
    db.add(CustomerUser(customer_id=customer.id, email="was@example.com", is_active=False))
    db.commit()

    assert _portal_status(db, customer.id)["portal_enabled"] is False


def test_portal_status_picks_the_same_row_the_writer_picks(tenant_db_session):
    """customer_users has no unique constraint on customer_id. The reader must
    tie-break the way `_get_or_create_customer_user` does (active first, then
    newest) or the tab reports on a different row than Enable/Disable touches."""
    db = tenant_db_session
    customer = Customer(name="Dupes", email="dupe@example.com", company_id="tenant-test")
    db.add(customer)
    db.commit()
    db.refresh(customer)
    db.add(CustomerUser(customer_id=customer.id, email="stale@example.com", is_active=False))
    db.commit()
    db.add(CustomerUser(customer_id=customer.id, email="live@example.com", is_active=True))
    db.commit()

    out = _portal_status(db, customer.id)

    assert out["portal_enabled"] is True
    assert out["email"] == "live@example.com", "must prefer the ACTIVE duplicate"

    writer_pick = portal_router._get_or_create_customer_user(db, customer, "live@example.com")
    assert writer_pick.email == "live@example.com", "reader and writer must agree"


def test_portal_status_surfaces_a_live_signin_link_expiry(tenant_db_session):
    """Prod's one real invite expired unused after 7 days and nothing said so.
    The tab now shows the expiry, so the operator can tell a dead link from a
    live one.

    Named for the SIGN-IN LINK, not the invite: `portal_token` serves both a
    staff invite (7 days) and a customer's own self-service link (15 minutes),
    and nothing distinguishes them in the column."""
    db = tenant_db_session
    customer = Customer(name="Invited", email="inv@example.com", company_id="tenant-test")
    db.add(customer)
    db.commit()
    db.refresh(customer)
    expires = datetime.now(UTC) + timedelta(days=3)
    db.add(CustomerUser(
        customer_id=customer.id,
        email="inv@example.com",
        is_active=True,
        portal_token="tok-abc",
        portal_token_expires_at=expires,
    ))
    db.commit()

    assert _portal_status(db, customer.id)["signin_link_expires_at"] is not None


def test_portal_status_reports_no_expiry_when_the_token_is_gone(tenant_db_session):
    """Counterfactual: an expiry timestamp with no token must not render as a
    live invite — turning access off clears the token but may leave the stamp."""
    db = tenant_db_session
    customer = Customer(name="Revoked", email="rev@example.com", company_id="tenant-test")
    db.add(customer)
    db.commit()
    db.refresh(customer)
    db.add(CustomerUser(
        customer_id=customer.id,
        email="rev@example.com",
        is_active=True,
        portal_token=None,
        portal_token_expires_at=datetime.now(UTC) + timedelta(days=3),
    ))
    db.commit()

    assert _portal_status(db, customer.id)["signin_link_expires_at"] is None


def test_portal_status_404s_for_an_unknown_customer(tenant_db_session):
    import fastapi

    with pytest.raises(fastapi.HTTPException) as exc:
        _portal_status(tenant_db_session, uuid4())
    assert exc.value.status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# Route-level gates on the staff portal endpoints.
#
# Added 2026-08-24 after an adversarial review deleted
# `Depends(require_permission("customers.read_all"))` from portal_admin_status
# and the seven tests above STILL PASSED — they call the handler function
# directly, so the dependency stack (permission gate, module gate, route
# registration, /info-vs-/{customer_id} arbitration) was entirely unproven.
#
# These assert on the REGISTERED ROUTE, so they fail if a gate is removed.
# ─────────────────────────────────────────────────────────────────────────────


def _staff_portal_route(method: str, path: str):
    """Find a registered staff-portal route, recursing FastAPI's lazy
    _IncludedRouter wrappers (a flat app.routes read sees ~8 entries)."""
    from fastapi.routing import _IncludedRouter

    from gdx_dispatch.app import app

    def walk(routes, prefix=""):
        for rt in routes:
            if isinstance(rt, _IncludedRouter):
                yield from walk(rt.original_router.routes, prefix + getattr(rt.include_context, "prefix", ""))
            elif hasattr(rt, "path"):
                yield prefix + rt.path, rt

    for full_path, route in walk(app.routes):
        if full_path == path and method in (getattr(route, "methods", None) or set()):
            return route
    return None


def _dependency_names(route) -> set[str]:
    out = set()
    for dep in getattr(route, "dependencies", []) or []:
        call = getattr(dep, "dependency", None)
        out.add(getattr(call, "__qualname__", "") or getattr(call, "__name__", ""))
    for dep in getattr(getattr(route, "dependant", None), "dependencies", []) or []:
        call = getattr(dep, "call", None)
        out.add(getattr(call, "__qualname__", "") or getattr(call, "__name__", ""))
    return out


def test_portal_status_route_is_registered():
    route = _staff_portal_route("GET", "/api/portal/{customer_id}")
    assert route is not None, "GET /api/portal/{customer_id} is not registered"
    assert route.endpoint.__name__ == "portal_admin_status"


def test_portal_status_route_carries_a_permission_gate():
    """Counterfactual target: delete the require_permission dependency from
    portal_admin_status and THIS fails, where the direct-call tests do not."""
    route = _staff_portal_route("GET", "/api/portal/{customer_id}")
    names = _dependency_names(route)
    assert any("require_permission" in n for n in names), (
        f"GET /api/portal/{{customer_id}} has no require_permission dependency; saw {sorted(names)}"
    )


def test_portal_write_routes_carry_a_permission_gate():
    for method, path in [("PATCH", "/api/portal/{customer_id}"), ("POST", "/api/portal/invite")]:
        route = _staff_portal_route(method, path)
        assert route is not None, f"{method} {path} is not registered"
        names = _dependency_names(route)
        assert any("require_permission" in n for n in names), f"{method} {path} is ungated"


def test_literal_info_route_wins_over_the_customer_id_param():
    """`/info` is a literal sharing a prefix with `/{customer_id}`. If the param
    route were registered first it would swallow it (and 422 on the UUID parse).
    The status endpoint's docstring asserts this ordering; pin it."""
    from fastapi.routing import _IncludedRouter

    from gdx_dispatch.app import app

    def walk(routes, prefix=""):
        for rt in routes:
            if isinstance(rt, _IncludedRouter):
                yield from walk(rt.original_router.routes, prefix + getattr(rt.include_context, "prefix", ""))
            elif hasattr(rt, "path"):
                yield prefix + rt.path, rt

    order = [p for p, _ in walk(app.routes) if p.startswith("/api/portal")]
    assert "/api/portal/info" in order and "/api/portal/{customer_id}" in order
    assert order.index("/api/portal/info") < order.index("/api/portal/{customer_id}"), (
        "GET /api/portal/info must be registered before /{customer_id} or the param route captures it"
    )


def test_retired_portal_account_shim_is_gone():
    """The customer page and the mobile customer page both used to call
    `/api/customers/{id}/portal-account`. Both were repointed; the shim was
    deleted. If anything re-registers it, that is a regression to the lying
    read (`{"exists": false}` for every customer)."""
    for method in ("GET", "POST", "DELETE"):
        assert _staff_portal_route(method, "/api/customers/{customer_id}/portal-account") is None

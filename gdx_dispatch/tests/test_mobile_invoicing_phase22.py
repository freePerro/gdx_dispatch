"""Sprint tech_mobile Phase 2.2 — On-Site Invoicing tests."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models import tenant_models  # noqa: F401
from gdx_dispatch.modules.proposals import models as proposals_models  # noqa: F401
from gdx_dispatch.routers import mobile_invoicing, mobile_quoting

_TEST_USER = {"user_id": "user-1", "role": "technician", "tenant_id": "tenant-a"}


def _as_json(response) -> dict:
    return json.loads(response.body)


def _request(tenant_id: str = "tenant-a") -> Request:
    req = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    req.state.tenant = {"id": tenant_id}
    req.state.tenant_id = tenant_id
    return req


@pytest.fixture()
def session_factory(tmp_path):
    db_file = tmp_path / "mobile_invoicing_test.sqlite3"
    engine = create_engine(
        f"sqlite:///{db_file}",
        connect_args={"check_same_thread": False},
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    yield SessionLocal
    engine.dispose()


def _seed(SessionLocal) -> dict[str, str]:
    db = SessionLocal()
    now = datetime.now(UTC)
    job_id = str(uuid4())
    customer_id = str(uuid4())
    try:
        db.execute(
            text(
                """
                INSERT INTO customers (id, name, phone, email, address, company_id)
                VALUES (:id, 'Acme Customer', '555-1111', 'a@example.com', '123 Main', 'tenant-a')
                """
            ),
            {"id": customer_id},
        )
        db.execute(
            text(
                """
                INSERT INTO technicians (id, company_id, user_id, active, created_at)
                VALUES ('tech-1', 'tenant-a', 'user-1', 1, :created_at)
                """
            ),
            {"created_at": now},
        )
        db.execute(
            text(
                """
                INSERT INTO jobs (
                    id, company_id, customer_id, title, dispatch_status,
                    assigned_to, scheduled_at, created_at, completed_at
                ) VALUES (
                    :id, 'tenant-a', :customer_id, 'Garage Door Repair', 'done',
                    'user-1', :now, :now, :now
                )
                """
            ),
            {"id": job_id, "customer_id": customer_id, "now": now},
        )
        db.commit()
        return {"job_id": job_id, "customer_id": customer_id}
    finally:
        db.close()


def _build_and_accept(SessionLocal, seed: dict[str, str]) -> str:
    """Helper — build + accept a quote on the seeded job, return estimate_id."""
    db = SessionLocal()
    try:
        build = mobile_quoting.build_quote(
            job_id=seed["job_id"],
            payload=mobile_quoting.BuildQuoteIn(service="spring_replacement"),
            request=_request(), current_user=_TEST_USER, db=db,
        )
        quote = _as_json(build)
        better = next(t for t in quote["tiers"] if t["tier_name"] == "better")
        sig = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB"
        mobile_quoting.accept_quote(
            estimate_id=quote["id"],
            payload=mobile_quoting.AcceptQuoteIn(
                chosen_tier_id=better["id"],
                signature_data=sig,
                signed_by="John Customer",
            ),
            request=_request(), current_user=_TEST_USER, db=db,
        )
        return quote["id"]
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Financial summary
# ---------------------------------------------------------------------------


def test_financial_summary_no_quote_no_invoice(session_factory):
    seed = _seed(session_factory)
    db = session_factory()
    try:
        resp = mobile_invoicing.job_financial_summary(
            job_id=seed["job_id"], request=_request(), current_user=_TEST_USER, db=db,
        )
        body = _as_json(resp)
        assert body["accepted_quote"] is None
        assert body["invoices"] == []
        assert body["payment_status"] == "no_invoice"
    finally:
        db.close()


def test_financial_summary_after_accepted_quote(session_factory):
    seed = _seed(session_factory)
    _build_and_accept(session_factory, seed)
    db = session_factory()
    try:
        resp = mobile_invoicing.job_financial_summary(
            job_id=seed["job_id"], request=_request(), current_user=_TEST_USER, db=db,
        )
        body = _as_json(resp)
        assert body["accepted_quote"] is not None
        assert body["accepted_quote"]["total"] > 0
        assert body["payment_status"] == "no_invoice"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Create invoice
# ---------------------------------------------------------------------------


def test_create_invoice_from_accepted_quote(session_factory):
    seed = _seed(session_factory)
    estimate_id = _build_and_accept(session_factory, seed)
    db = session_factory()
    try:
        with patch("gdx_dispatch.routers.mobile_invoicing._send_invoice_email") as send_mock:
            resp = mobile_invoicing.mobile_create_invoice(
                job_id=seed["job_id"],
                payload=mobile_invoicing.CreateInvoiceIn(
                    estimate_id=estimate_id, send_email=True,
                ),
                request=_request(), current_user=_TEST_USER, db=db,
            )
        assert resp.status_code == 201, resp.body
        body = _as_json(resp)
        assert body["status"] == "sent"
        assert body["total"] > 0
        assert len(body["lines"]) >= 1
        # Invoice carries the BETTER tier total (~$439 from spring better preset)
        # — proves we filtered to the chosen tier, not all 3.
        assert body["total"] < 800  # would be much higher if all 3 tiers' lines copied
        send_mock.assert_called_once()
    finally:
        db.close()


def test_create_invoice_unauthorized_404(session_factory):
    seed = _seed(session_factory)
    db = session_factory()
    try:
        resp = mobile_invoicing.mobile_create_invoice(
            job_id=seed["job_id"],
            payload=mobile_invoicing.CreateInvoiceIn(send_email=False),
            request=_request(),
            current_user={"user_id": "other", "tenant_id": "tenant-a"},
            db=db,
        )
        assert resp.status_code == 404
    finally:
        db.close()


def test_create_invoice_unaccepted_estimate_409(session_factory):
    seed = _seed(session_factory)
    db = session_factory()
    try:
        # Build a quote but DON'T accept.
        build = mobile_quoting.build_quote(
            job_id=seed["job_id"],
            payload=mobile_quoting.BuildQuoteIn(service="tune_up"),
            request=_request(), current_user=_TEST_USER, db=db,
        )
        quote = _as_json(build)
        resp = mobile_invoicing.mobile_create_invoice(
            job_id=seed["job_id"],
            payload=mobile_invoicing.CreateInvoiceIn(
                estimate_id=quote["id"], send_email=False,
            ),
            request=_request(), current_user=_TEST_USER, db=db,
        )
        assert resp.status_code == 409
    finally:
        db.close()


def test_create_invoice_without_estimate(session_factory):
    seed = _seed(session_factory)
    db = session_factory()
    try:
        with patch("gdx_dispatch.routers.mobile_invoicing._send_invoice_email"):
            resp = mobile_invoicing.mobile_create_invoice(
                job_id=seed["job_id"],
                payload=mobile_invoicing.CreateInvoiceIn(send_email=False),
                request=_request(), current_user=_TEST_USER, db=db,
            )
        assert resp.status_code == 201
        body = _as_json(resp)
        # No estimate → empty lines, $0 totals; office can fill in via /api/invoices.
        assert body["total"] == 0.0
        assert body["lines"] == []
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Send / send-receipt
# ---------------------------------------------------------------------------


def test_send_invoice_marks_sent(session_factory):
    seed = _seed(session_factory)
    estimate_id = _build_and_accept(session_factory, seed)
    db = session_factory()
    try:
        with patch("gdx_dispatch.routers.mobile_invoicing._send_invoice_email"):
            create = mobile_invoicing.mobile_create_invoice(
                job_id=seed["job_id"],
                payload=mobile_invoicing.CreateInvoiceIn(
                    estimate_id=estimate_id, send_email=False,
                ),
                request=_request(), current_user=_TEST_USER, db=db,
            )
            inv = _as_json(create)
            assert inv["status"] == "draft"  # not sent
            send = mobile_invoicing.mobile_send_invoice(
                invoice_id=inv["id"],
                request=_request(), current_user=_TEST_USER, db=db,
            )
        assert send.status_code == 200
        body = _as_json(send)
        assert body["status"] == "sent"
        assert body["sent_at"] is not None
    finally:
        db.close()


def test_send_receipt_no_payment_404(session_factory):
    seed = _seed(session_factory)
    estimate_id = _build_and_accept(session_factory, seed)
    db = session_factory()
    try:
        with patch("gdx_dispatch.routers.mobile_invoicing._send_invoice_email"):
            create = mobile_invoicing.mobile_create_invoice(
                job_id=seed["job_id"],
                payload=mobile_invoicing.CreateInvoiceIn(
                    estimate_id=estimate_id, send_email=False,
                ),
                request=_request(), current_user=_TEST_USER, db=db,
            )
        inv = _as_json(create)
        resp = mobile_invoicing.mobile_send_receipt(
            invoice_id=inv["id"],
            payload=mobile_invoicing.SendReceiptIn(),
            request=_request(), current_user=_TEST_USER, db=db,
        )
        assert resp.status_code == 404
        assert "no payment" in _as_json(resp)["detail"].lower()
    finally:
        db.close()

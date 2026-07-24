"""Tier 2 contract-gap fixes — backend halves of the new UI doors
(docs/design/backend-vue-contract-gaps-2026-07-24.md).

Covers:
  2.1 — credit memo accepts raw status "overdue" (the prime target)
  2.2 — filing a claim bumps the linked warranty; claim PATCH resolves;
        malformed claim ids 404 instead of erroring on PG
  2.3 — job dependency guards (self/unknown), enriched list, new DELETE
  2.4 — receipts serializer carries promoted_expense_id
  2.5 — per-invoice reminder ledger roundtrip over HTTP
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models import tenant_models  # noqa: F401 — registers tables
from gdx_dispatch.models.tenant_models import (
    Customer,
    Invoice,
    InvoiceLine,
    Job,
    Warranty,
)

TENANT = "tenant-test"


def _engine_and_sessions():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    return engine, sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _make_app(routers: list, role: str = "admin"):
    from gdx_dispatch.core.auth import get_current_user as core_gcu
    from gdx_dispatch.core.database import get_db as core_get_db
    from gdx_dispatch.routers.auth import get_current_user as routers_gcu

    engine, SessionLocal = _engine_and_sessions()
    app = FastAPI()

    @app.middleware("http")
    async def _tenant_shim(request, call_next):
        request.state.tenant = {"id": TENANT, "slug": TENANT}
        return await call_next(request)

    for r in routers:
        app.include_router(r)

    def _override_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    user = {"sub": "u-1", "user_id": "u-1", "role": role, "tenant_id": TENANT, "email": "u@x"}
    app.dependency_overrides[core_get_db] = _override_db
    app.dependency_overrides[routers_gcu] = lambda: user
    app.dependency_overrides[core_gcu] = lambda: user
    return app, SessionLocal, engine


# ── 2.2 warranty claims ────────────────────────────────────────────────────


def _seed_warranty(db: Session) -> Warranty:
    w = Warranty(
        id=str(uuid4()),
        job_id=str(uuid4()),
        customer_id=str(uuid4()),
        description="LiftMaster opener — 5yr parts",
        start_date=date(2026, 1, 1),
        end_date=date(2031, 1, 1),
        status="active",
        claim_count=0,
    )
    db.add(w)
    db.commit()
    db.refresh(w)
    return w


def test_file_claim_bumps_linked_warranty():
    from gdx_dispatch.routers import warranty_claims

    app, SessionLocal, engine = _make_app([warranty_claims.router])
    try:
        db = SessionLocal()
        w = _seed_warranty(db)
        wid = w.id
        cid = w.customer_id
        db.close()

        client = TestClient(app)
        r = client.post(
            "/api/warranty-claims",
            json={
                "warranty_id": wid,
                "customer_id": cid,
                "claim_notes": "Spring snapped inside coverage window",
            },
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["status"] == "filed"
        assert body["warranty_id"] == wid

        db = SessionLocal()
        w2 = db.get(Warranty, wid)
        assert w2.claim_count == 1
        assert w2.status == "claimed"
        assert w2.last_claim_at is not None
        assert "Spring snapped" in (w2.last_claim_notes or "")
        db.close()

        # Claims list shows it; PATCH to approved stamps resolved_at
        listed = client.get("/api/warranty-claims").json()
        assert len(listed) == 1
        patched = client.patch(
            f"/api/warranty-claims/{body['id']}",
            json={"status": "approved", "resolution": "Manufacturer shipped a new spring"},
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["resolved_at"] is not None

        # Malformed id → clean 404, not a PG binding error
        assert client.patch("/api/warranty-claims/not-a-uuid", json={"status": "pending"}).status_code == 404
        assert client.get("/api/warranty-claims/not-a-uuid").status_code == 404
    finally:
        engine.dispose()


# ── 2.3 job dependencies ───────────────────────────────────────────────────


def _seed_job(db: Session, title: str) -> Job:
    j = Job(id=uuid4(), company_id=TENANT, title=title, status="Scheduled")
    db.add(j)
    db.commit()
    db.refresh(j)
    return j


def test_job_dependency_lifecycle():
    from gdx_dispatch.routers import jobs as jobs_router

    app, SessionLocal, engine = _make_app([jobs_router.router])
    try:
        db = SessionLocal()
        a = _seed_job(db, "Install door")
        b = _seed_job(db, "Pour concrete pad")
        a_id, b_id, b_title = str(a.id), str(b.id), b.title
        db.close()

        client = TestClient(app)

        # Guards: self-dependency and unknown target refuse cleanly
        assert client.post(f"/api/jobs/{a_id}/dependencies", json={"depends_on_job_id": a_id}).status_code == 422
        assert client.post(f"/api/jobs/{a_id}/dependencies", json={"depends_on_job_id": str(uuid4())}).status_code == 422

        r = client.post(f"/api/jobs/{a_id}/dependencies", json={"depends_on_job_id": b_id})
        assert r.status_code == 201, r.text
        dep_id = r.json()["id"]

        # Enriched list: the UI renders a name, not a UUID
        listed = client.get(f"/api/jobs/{a_id}/dependencies").json()
        assert len(listed) == 1
        assert listed[0]["depends_on_title"] == b_title
        assert listed[0]["depends_on_job_id"] == b_id

        # DELETE (new route — add-only would make mis-clicks permanent blockers)
        assert client.delete(f"/api/jobs/{a_id}/dependencies/{dep_id}").status_code == 200
        assert client.get(f"/api/jobs/{a_id}/dependencies").json() == []
        assert client.delete(f"/api/jobs/{a_id}/dependencies/{dep_id}").status_code == 404
    finally:
        engine.dispose()


# ── 2.1 credit memo on an overdue invoice ──────────────────────────────────


def test_credit_memo_accepts_overdue_status():
    from gdx_dispatch.routers.invoices import CreditMemoIn, issue_credit_memo

    engine, SessionLocal = _engine_and_sessions()
    db = SessionLocal()
    try:
        cust = Customer(id=uuid4(), company_id=TENANT, name="Acme")
        db.add(cust)
        inv = Invoice(
            id=uuid4(),
            company_id=TENANT,
            customer_id=cust.id,
            invoice_number="INV-T2-1",
            public_token=uuid4().hex,
            subtotal=100.0,
            tax_amount=0.0,
            total=100.0,
            balance_due=100.0,
            status="overdue",
        )
        db.add(inv)
        db.add(
            InvoiceLine(
                id=uuid4(),
                invoice_id=inv.id,
                company_id=TENANT,
                description="Service call",
                quantity=1,
                unit_price=100.0,
                line_total=100.0,
                taxable=False,
                sort_order=1,
            )
        )
        db.commit()
        db.refresh(inv)

        out = issue_credit_memo(
            str(inv.id),
            CreditMemoIn(amount=40, reason="goodwill"),
            db=db,
            _={"sub": "u-1", "role": "admin"},
        )
        assert out["credit_amount"] == 40.0
        assert out["balance_due"] == pytest.approx(60.0)
    finally:
        db.close()
        engine.dispose()


# ── 2.4 receipts serializer carries promoted_expense_id ────────────────────


def test_receipt_serializer_carries_promotion():
    from gdx_dispatch.models.tenant_models import JobReceipt
    from gdx_dispatch.routers.job_hazards_receipts import _receipt_to_response

    expense_id = uuid4()
    r = JobReceipt(
        id=uuid4(),
        job_id=uuid4(),
        vendor="Home Depot",
        amount=Decimal("42.50"),
        created_at=datetime.now(UTC),
        promoted_expense_id=expense_id,
    )
    out = _receipt_to_response(r)
    assert out.promoted_expense_id == str(expense_id)


# ── 2.5 per-invoice reminder ledger over HTTP ──────────────────────────────


def test_reminder_ledger_roundtrip():
    from gdx_dispatch.routers import collections as collections_router

    app, SessionLocal, engine = _make_app([collections_router.router])
    try:
        invoice_id = str(uuid4())
        client = TestClient(app)

        r = client.post(
            "/api/collections/reminders",
            json={
                "invoice_id": invoice_id,
                "customer_name": "Acme",
                "stage": "final_notice",
                "channel": "phone",
                "notes": "spoke with owner",
                "promised_payment_date": "2026-08-01",
            },
        )
        assert r.status_code == 201, r.text
        rid = r.json()["id"]
        assert r.json()["promised_payment_date"].startswith("2026-08-01")

        listed = client.get(f"/api/collections/reminders?invoice_id={invoice_id}").json()
        assert len(listed) == 1
        assert listed[0]["stage"] == "final_notice"

        # An unknown stage refuses
        bad = client.post(
            "/api/collections/reminders",
            json={"invoice_id": invoice_id, "stage": "shouting"},
        )
        assert bad.status_code == 422

        assert client.delete(f"/api/collections/reminders/{rid}").status_code == 204
        assert client.get(f"/api/collections/reminders?invoice_id={invoice_id}").json() == []
    finally:
        engine.dispose()


# ── The real /api/collections queue (replaces the ui_compat shim) ──────────


def test_collections_queue_derives_from_overdue_invoices():
    from gdx_dispatch.routers import collections as collections_router

    app, SessionLocal, engine = _make_app([collections_router.router])
    try:
        db = SessionLocal()
        cust = Customer(id=uuid4(), company_id=TENANT, name="Slow Payer LLC")
        db.add(cust)
        overdue = Invoice(
            id=uuid4(), company_id=TENANT, customer_id=cust.id,
            invoice_number="INV-OD-1", public_token=uuid4().hex,
            subtotal=500.0, tax_amount=0.0, total=500.0, balance_due=500.0,
            status="sent", due_date=date(2026, 6, 1),
        )
        current = Invoice(
            id=uuid4(), company_id=TENANT, customer_id=cust.id,
            invoice_number="INV-OK-1", public_token=uuid4().hex,
            subtotal=100.0, tax_amount=0.0, total=100.0, balance_due=100.0,
            status="sent", due_date=date(2099, 1, 1),
        )
        paid = Invoice(
            id=uuid4(), company_id=TENANT, customer_id=cust.id,
            invoice_number="INV-PD-1", public_token=uuid4().hex,
            subtotal=50.0, tax_amount=0.0, total=50.0, balance_due=0.0,
            status="paid", due_date=date(2026, 6, 1),
        )
        db.add_all([overdue, current, paid])
        db.commit()
        overdue_id = str(overdue.id)
        db.close()

        client = TestClient(app)
        body = client.get("/api/collections").json()
        assert body["total"] == 1
        row = body["items"][0]
        assert row["invoice_number"] == "INV-OD-1"
        assert row["customer"] == "Slow Payer LLC"
        assert row["amount_due"] == 500.0
        assert row["days_overdue"] > 0
        assert row["status"] == "active"
        assert row["id"] == overdue_id  # PATCH path + reminders ledger key

        # Pause → dunning_paused; contact note → reminder-ledger row
        assert client.patch(f"/api/collections/{overdue_id}", json={"status": "paused"}).status_code == 200
        assert client.get("/api/collections").json()["items"][0]["status"] == "paused"
        client.patch(
            f"/api/collections/{overdue_id}",
            json={"status": "active", "last_contact": "2026-07-24", "note": "left voicemail", "contact_type": "phone"},
        )
        ledger = client.get(f"/api/collections/reminders?invoice_id={overdue_id}").json()
        assert len(ledger) == 1
        assert ledger[0]["notes"] == "left voicemail"
        # And the queue reflects the contact as last_contact
        assert client.get("/api/collections").json()["items"][0]["last_contact"] is not None

        # "resolved" refuses loudly — there is no fake write-off
        assert client.patch(f"/api/collections/{overdue_id}", json={"status": "resolved"}).status_code == 409
        # Bulk send refuses honestly while dunning is off
        assert client.post("/api/collections/send-reminders", json={}).status_code == 409
    finally:
        engine.dispose()


def test_reminders_reject_malformed_invoice_id():
    from gdx_dispatch.routers import collections as collections_router

    app, SessionLocal, engine = _make_app([collections_router.router])
    try:
        client = TestClient(app)
        # GET must refuse, not silently return every tenant reminder
        assert client.get("/api/collections/reminders?invoice_id=INV-123").status_code == 422
        assert client.post(
            "/api/collections/reminders", json={"invoice_id": "INV-123", "stage": "friendly"}
        ).status_code == 422
    finally:
        engine.dispose()


# ── finalize gate + dependency cycle guard ─────────────────────────────────


def test_finalize_refuses_draft_and_void():
    from gdx_dispatch.routers.invoices import finalize_invoice

    engine, SessionLocal = _engine_and_sessions()
    db = SessionLocal()
    try:
        from fastapi import HTTPException

        cust = Customer(id=uuid4(), company_id=TENANT, name="Draft Co")
        db.add(cust)
        inv = Invoice(
            id=uuid4(), company_id=TENANT, customer_id=cust.id,
            invoice_number="INV-DR-1",
            public_token=uuid4().hex, subtotal=0.0, tax_amount=0.0,
            total=0.0, balance_due=0.0, status="draft",
        )
        db.add(inv)
        db.commit()
        with pytest.raises(HTTPException) as exc:
            finalize_invoice(inv.id, _={"sub": "u-1"}, db=db)
        assert exc.value.status_code == 409
    finally:
        db.close()
        engine.dispose()


def test_dependency_direct_cycle_refused():
    from gdx_dispatch.routers import jobs as jobs_router

    app, SessionLocal, engine = _make_app([jobs_router.router])
    try:
        db = SessionLocal()
        a = _seed_job(db, "A")
        b = _seed_job(db, "B")
        a_id, b_id = str(a.id), str(b.id)
        db.close()

        client = TestClient(app)
        assert client.post(f"/api/jobs/{a_id}/dependencies", json={"depends_on_job_id": b_id}).status_code == 201
        r = client.post(f"/api/jobs/{b_id}/dependencies", json={"depends_on_job_id": a_id})
        assert r.status_code == 422
        assert "deadlock" in r.json()["detail"]
    finally:
        engine.dispose()

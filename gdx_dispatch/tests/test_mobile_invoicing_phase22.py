"""Sprint tech_mobile Phase 2.2 — On-Site Invoicing tests."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import patch
from uuid import UUID, uuid4

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


def test_financial_summary_reports_real_parts_and_labor(session_factory):
    """The summary must carry actual numbers, not silently-zero ones.

    Found by driving a real phone: this endpoint 500'd for EVERY job in
    production, so the invoice dialog rendered blank and offered to "Generate
    empty invoice" — a tech could not show a customer their bill at all. Three
    separate bugs, and the tests never saw any of them because they only
    asserted the response SHAPE and ran on SQLite, where all three are benign:

      * `parts_needed` has never existed (it is `job_parts_needed`), and it has
        no `estimated_cost`/`deleted_at` either. On SQLite the try/except just
        swallowed it; on Postgres a failed statement ABORTS the transaction, so
        every later query in the request died with InFailedSqlTransaction.
      * labor used `julianday()` — SQLite-only, absent on Postgres — against
        `timeclock_entries`, which is neither the right name
        (`timeclock_entries_router`) nor the right table (that one is the
        DAY-level shift and has no job_id). So labor read 0 on prod, always.

    Asserting the VALUES catches all of it on SQLite too.
    """
    from gdx_dispatch.models.tenant_models import JobPartNeeded, TimeEntry

    seed = _seed(session_factory)
    db = session_factory()
    try:
        # 90 minutes of attested, closed labor -> 1.5h.
        db.add(TimeEntry(
            id=uuid4(),
            company_id="tenant-a",
            job_id=UUID(seed["job_id"]),
            tech_id="tech-1",
            user_id="user-1",
            clock_in=datetime.now(UTC),
            clock_out=datetime.now(UTC),
            duration_minutes=90,
            entry_type="work",
            created_at=datetime.now(UTC),
        ))
        # 2 x $45 of parts the tech recorded.
        db.add(JobPartNeeded(
            id=str(uuid4()),
            company_id="tenant-a",
            job_id=seed["job_id"],
            part_name="Torsion spring",
            quantity=2,
            unit_price=45,
            source="closeout",
            created_at=datetime.now(UTC),
        ))
        db.commit()

        body = _as_json(mobile_invoicing.job_financial_summary(
            job_id=seed["job_id"], request=_request(), current_user=_TEST_USER, db=db,
        ))
        assert body["labor_hours"] == 1.5, "labor silently read zero"
        assert body["parts_cost"] == 90.0, "parts silently read zero"
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


def test_create_invoice_inherits_hide_line_prices(session_factory):
    """The truck path must snapshot the estimate's effective "total-only"
    display onto the invoice, same as the office path — otherwise a tech
    generating the invoice re-exposes per-line prices the customer never saw."""
    from gdx_dispatch.models.tenant_models import Invoice
    from gdx_dispatch.modules.proposals.models import Estimate

    seed = _seed(session_factory)
    estimate_id = _build_and_accept(session_factory, seed)
    db = session_factory()
    try:
        # ORM update, not raw SQL — SQLite stores Uuid PKs as 32-char hex,
        # so a raw `WHERE id = :dashed_str` silently matches zero rows.
        est = db.get(Estimate, UUID(estimate_id))
        assert est is not None
        est.hide_line_prices = True
        db.commit()
        with patch("gdx_dispatch.routers.mobile_invoicing._send_invoice_email"):
            resp = mobile_invoicing.mobile_create_invoice(
                job_id=seed["job_id"],
                payload=mobile_invoicing.CreateInvoiceIn(
                    estimate_id=estimate_id, send_email=False,
                ),
                request=_request(), current_user=_TEST_USER, db=db,
            )
        assert resp.status_code == 201, resp.body
        body = _as_json(resp)
        invoice = db.get(Invoice, UUID(body["id"]))
        assert invoice is not None
        assert bool(invoice.hide_line_prices) is True
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


# ---------------------------------------------------------------------------
# PR1-billing-capture (2026-07-07) — mobile paths honor billing guards
# ---------------------------------------------------------------------------


def test_create_invoice_zero_price_line_blocked_by_policy(session_factory, monkeypatch):
    """The mobile create path bypassed the F-75 zero-price policy — a tenant
    with block_zero_price_on_invoice ON still got $0 lines from the truck.
    Now it 422s and writes NOTHING (the flushed invoice rolls back)."""
    from decimal import Decimal
    from uuid import UUID

    from gdx_dispatch.modules.catalog_policy.service import CatalogPolicy

    seed = _seed(session_factory)
    db = session_factory()
    try:
        est = proposals_models.Estimate(
            job_id=UUID(seed["job_id"]),
            customer_id=UUID(seed["customer_id"]),
            estimate_number=f"EST-{uuid4().hex[:8]}",
            label="Zero-line estimate",
            proposal_mode=False,
            total=Decimal("100.00"),
            status="accepted",
            public_token=uuid4().hex,
            company_id="tenant-a",
        )
        db.add(est)
        db.commit()
        db.refresh(est)
        # Raw SQL with DASHED estimate_id: the mobile plain-copy path queries
        # `WHERE estimate_id = :eid` with str(estimate.id) (dashed), while the
        # ORM stores UUIDs as 32-hex on SQLite — the same dual-format quirk
        # the tier branch works around. Seed in the format the query reads.
        now = datetime.now(UTC)
        for desc, price, order in (
            ("Labor", "100.00", 1),
            ("Hardware (included)", "0", 2),
        ):
            db.execute(
                text(
                    """
                    INSERT INTO estimate_lines
                        (id, estimate_id, description, quantity, unit_price,
                         line_total, sort_order, company_id, created_at)
                    VALUES (:id, :eid, :d, 1, :p, :p, :s, 'tenant-a', :now)
                    """
                ),
                {"id": str(uuid4()), "eid": str(est.id), "d": desc,
                 "p": price, "s": order, "now": now},
            )
        db.commit()

        monkeypatch.setattr(
            "gdx_dispatch.modules.catalog_policy.get_policy",
            lambda tid: CatalogPolicy(block_zero_price_on_invoice=True),
        )
        resp = mobile_invoicing.mobile_create_invoice(
            job_id=seed["job_id"],
            payload=mobile_invoicing.CreateInvoiceIn(
                estimate_id=str(est.id), send_email=False,
            ),
            request=_request(), current_user=_TEST_USER, db=db,
        )
        assert resp.status_code == 422, resp.body
        assert "zero-price" in _as_json(resp)["detail"]
        count = db.execute(text("SELECT COUNT(*) FROM invoices")).scalar()
        assert count == 0, "blocked mobile invoice must roll back entirely"
    finally:
        db.close()


def test_mobile_send_invoice_void_409_no_email(session_factory):
    """Audit catch: the desktop /send got a void guard but the mobile
    re-send still EMAILED voided invoices to customers."""
    from uuid import UUID as _U

    from sqlalchemy import select as _select

    from gdx_dispatch.models.tenant_models import Invoice as _Invoice

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
        inv_id = _as_json(create)["id"]
        inv = db.execute(_select(_Invoice).where(_Invoice.id == _U(inv_id))).scalar_one()
        inv.status = "void"
        db.commit()

        with patch("gdx_dispatch.routers.mobile_invoicing._send_invoice_email") as send_mock:
            resp = mobile_invoicing.mobile_send_invoice(
                invoice_id=inv_id, request=_request(),
                current_user=_TEST_USER, db=db,
            )
        assert resp.status_code == 409
        send_mock.assert_not_called()
        db.refresh(inv)
        assert inv.status == "void"
    finally:
        db.close()

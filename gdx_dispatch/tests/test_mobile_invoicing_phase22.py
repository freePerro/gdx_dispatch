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
                    lifecycle_stage, assigned_to, scheduled_at, created_at,
                    completed_at
                ) VALUES (
                    :id, 'tenant-a', :customer_id, 'Garage Door Repair', 'done',
                    'completed', 'user-1', :now, :now, :now
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
        # Plan §11 (audit): a tech-created invoice is NEVER auto-sent — it
        # lands as a draft awaiting office verification even when the caller
        # asked to send. send_email is deferred, not honored.
        assert body["status"] == "draft"
        assert body["awaiting_verification"] is True
        assert body["total"] > 0
        assert len(body["lines"]) >= 1
        # Invoice carries the BETTER tier total (~$439 from spring better preset)
        # — proves we filtered to the chosen tier, not all 3.
        assert body["total"] < 800  # would be much higher if all 3 tiers' lines copied
        send_mock.assert_not_called()  # nothing reaches the customer unverified
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

            # Plan §11: an UNVERIFIED tech-created invoice must not send —
            # the office reviews the hours first. 409 with the awaiting flag.
            blocked = mobile_invoicing.mobile_send_invoice(
                invoice_id=inv["id"],
                request=_request(), current_user=_TEST_USER, db=db,
            )
            assert blocked.status_code == 409
            assert _as_json(blocked).get("awaiting_verification") is True

            # Office verifies → send proceeds.
            from datetime import UTC as _UTC
            from datetime import datetime as _dt

            from gdx_dispatch.models.tenant_models import Invoice as _Inv
            row = db.get(_Inv, __import__("uuid").UUID(inv["id"]))
            row.verified_at = _dt.now(_UTC)
            row.verified_by_user_id = "office-user"
            db.commit()

            send = mobile_invoicing.mobile_send_invoice(
                invoice_id=inv["id"],
                request=_request(), current_user=_TEST_USER, db=db,
            )
        assert send.status_code == 200
        body = _as_json(send)
        assert body["status"] == "sent"
        assert body["sent_at"] is not None
        # Migration 057: the mobile send is a server-side email send, so the
        # delivery channel must be recorded alongside the stamp (audit catch
        # 2026-08-05 — this was the one send path that skipped sent_via).
        row = db.get(_Inv, __import__("uuid").UUID(inv["id"]))
        db.refresh(row)
        assert row.sent_via == "email"
    finally:
        db.close()


def test_send_invoice_gl_posting_names_the_tech(session_factory, monkeypatch):
    """GDXA-616: the draft→sent flip on the mobile send posts the P1 issuance
    entry when GL posting is on. It passed no actor, so the journal entry
    named nobody (created_by NULL) — a money posting with no acting user."""
    from sqlalchemy import select

    from gdx_dispatch.models.tenant_models import Invoice as _Inv
    from gdx_dispatch.modules.ledger.models import GlJournalEntry
    from gdx_dispatch.modules.ledger.service import ensure_gl_seed

    monkeypatch.delenv("GDX_ENV", raising=False)
    seed = _seed(session_factory)
    estimate_id = _build_and_accept(session_factory, seed)
    db = session_factory()
    try:
        ensure_gl_seed(db, "tenant-a").ledger_posting_enabled = True
        db.commit()
        with patch("gdx_dispatch.routers.mobile_invoicing._send_invoice_email"):
            inv = _as_json(mobile_invoicing.mobile_create_invoice(
                job_id=seed["job_id"],
                payload=mobile_invoicing.CreateInvoiceIn(estimate_id=estimate_id, send_email=False),
                request=_request(), current_user=_TEST_USER, db=db,
            ))
            row = db.get(_Inv, UUID(inv["id"]))
            row.verified_at = datetime.now(UTC)
            row.verified_by_user_id = "office-user"
            db.commit()
            send = mobile_invoicing.mobile_send_invoice(
                invoice_id=inv["id"], request=_request(), current_user=_TEST_USER, db=db,
            )
        assert send.status_code == 200
        # A fresh database: every entry in it came from this send.
        entries = db.scalars(select(GlJournalEntry)).all()
        assert entries, "the mobile send posted no GL entry — the test proves nothing"
        assert [e.created_by for e in entries] == [_TEST_USER["user_id"]] * len(entries)
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

        # §11: receipts email the customer too — unverified refuses first.
        blocked = mobile_invoicing.mobile_send_receipt(
            invoice_id=inv["id"],
            payload=mobile_invoicing.SendReceiptIn(),
            request=_request(), current_user=_TEST_USER, db=db,
        )
        assert blocked.status_code == 409
        assert _as_json(blocked).get("awaiting_verification") is True

        from datetime import UTC as _UTC
        from datetime import datetime as _dt

        from gdx_dispatch.models.tenant_models import Invoice as _Inv
        row = db.get(_Inv, __import__("uuid").UUID(inv["id"]))
        row.verified_at = _dt.now(_UTC)
        db.commit()

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


def test_truck_create_audits_the_deposit_it_voids(session_factory):
    """#696. The truck path runs the same deposit netting as the office create;
    the deposit it voids carries its own row, naming the tech who did it and
    the invoice that superseded it."""
    from sqlalchemy import select

    from gdx_dispatch.core.audit import AuditLog
    from gdx_dispatch.models.tenant_models import Invoice
    from gdx_dispatch.modules.deposits import create_deposit_invoice
    from gdx_dispatch.modules.proposals.models import Estimate

    seed = _seed(session_factory)
    estimate_id = _build_and_accept(session_factory, seed)
    db = session_factory()
    try:
        est = db.get(Estimate, UUID(estimate_id))
        dep = create_deposit_invoice(
            db, estimate=est, amount=100.0, tenant_id="tenant-a",
            actor="user-1", source="test",
        )
        dep_id = dep.id
        resp = mobile_invoicing.mobile_create_invoice(
            job_id=seed["job_id"],
            payload=mobile_invoicing.CreateInvoiceIn(estimate_id=estimate_id),
            request=_request(), current_user=_TEST_USER, db=db,
        )
        assert resp.status_code == 201, resp.body
        final = _as_json(resp)
        db.expire_all()
        assert db.get(Invoice, dep_id).status == "void"
        rows = db.execute(
            select(AuditLog).where(
                AuditLog.entity_id == str(dep_id), AuditLog.action == "invoice_voided"
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].user_id == "user-1"
        assert rows[0].details["via"] == "deposit_superseded"
        assert rows[0].details["final_invoice_id"] == final["id"]
    finally:
        db.close()


def test_truck_create_supersedes_the_deposit_atomically_on_a_fresh_engine(session_factory, monkeypatch):
    """#696. The truck's create voids the job's unpaid deposit and now writes an
    invoice_voided row on it, mid-create. The first audit write on an engine
    initializes the guard — on SQLite that commits whatever is pending. The
    handler primes the audit table before staging anything, so when its one
    commit fails, the deposit, the new invoice and the row all go together.
    Deleting that prime turns this red."""
    from sqlalchemy import select
    from sqlalchemy.exc import OperationalError

    import gdx_dispatch.core.audit as audit_mod
    import gdx_dispatch.modules.deposits as deposits_pkg
    from gdx_dispatch.core.audit import AuditLog
    from gdx_dispatch.models.tenant_models import Invoice
    from gdx_dispatch.modules.deposits import create_deposit_invoice
    from gdx_dispatch.modules.proposals.models import Estimate

    seed = _seed(session_factory)
    estimate_id = _build_and_accept(session_factory, seed)
    db = session_factory()
    try:
        est = db.get(Estimate, UUID(estimate_id))
        dep = create_deposit_invoice(
            db, estimate=est, amount=100.0, tenant_id="tenant-a",
            actor="user-1", source="test",
        )
        dep_id = dep.id
        assert dep.status == "sent"
        # The deposit was made by "another worker": this request's engine has
        # never initialized the audit guard.
        audit_mod._AUDIT_GUARD_INITIALIZED.discard(db.get_bind())

        netted = {"done": False}
        real_apply = deposits_pkg.apply_deposits_to_final

        def apply_then_flag(*args, **kwargs):
            out = real_apply(*args, **kwargs)
            netted["done"] = True
            return out

        real_commit = db.commit

        def commit_fails_after_netting():
            if netted["done"]:
                raise OperationalError("COMMIT", {}, Exception("connection lost"))
            return real_commit()

        monkeypatch.setattr(deposits_pkg, "apply_deposits_to_final", apply_then_flag)
        monkeypatch.setattr(db, "commit", commit_fails_after_netting)
        with pytest.raises(OperationalError):
            mobile_invoicing.mobile_create_invoice(
                job_id=seed["job_id"],
                payload=mobile_invoicing.CreateInvoiceIn(estimate_id=estimate_id),
                request=_request(), current_user=_TEST_USER, db=db,
            )
        assert netted["done"], "the create never reached deposit netting"
        monkeypatch.undo()
        db.rollback()
        db.expire_all()

        assert db.get(Invoice, dep_id).status == "sent"
        finals = db.execute(
            select(Invoice).where(Invoice.billing_type != "deposit")
        ).scalars().all()
        assert finals == []
        assert db.execute(
            select(AuditLog).where(
                AuditLog.entity_id == str(dep_id), AuditLog.action == "invoice_voided"
            )
        ).scalars().all() == []
    finally:
        db.close()


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


def test_invoice_from_closeout_service_lane_prices_hourly(session_factory):
    """Plan §8 (audit: the router assembly had ZERO test coverage). A Service
    Call job with a closeout and no estimate → hourly labor line by the
    decided math, draft + awaiting verification (never auto-sent)."""
    from uuid import UUID as _U

    from gdx_dispatch.models.pricing_engine import PricingSettings
    from gdx_dispatch.models.tenant_models import JobCloseout

    seed = _seed(session_factory)
    db = session_factory()
    try:
        db.execute(
            text("UPDATE jobs SET job_type = 'Service Call' WHERE id = :j"),
            {"j": seed["job_id"]},
        )
        db.add(PricingSettings())  # defaults 100/100
        db.add(JobCloseout(
            id=uuid4(),
            job_id=_U(seed["job_id"]),
            parts_used=[],
            no_parts_used=True,
            hours_worked=3.0,
            techs_on_site=2,
            closed_by_user_id="user-1",
            closed_at=datetime.now(UTC),
        ))
        db.commit()

        resp = mobile_invoicing.mobile_create_invoice(
            job_id=seed["job_id"],
            payload=mobile_invoicing.CreateInvoiceIn(send_email=True),
            request=_request(), current_user=_TEST_USER, db=db,
        )
        assert resp.status_code == 201, resp.body
        body = _as_json(resp)
        # 3.0 h × 2 techs = 6.0 man-hours → $600 (Doug's crew example).
        assert float(body["total"]) == 600.0, body["total"]
        assert body["status"] == "draft"
        assert body["awaiting_verification"] is True
        labor = [ln for ln in body["lines"] if "man-hours" in (ln["description"] or "")]
        assert len(labor) == 1
        assert "6.00 man-hours" in labor[0]["description"]
    finally:
        db.close()


def test_invoice_from_closeout_office_lane_leaves_labor_unpriced(session_factory):
    """A non-service, non-install job (e.g. QB Import) with a closeout: labor
    is NOT priced (office lane), the invoice is a $0 draft, and — crucially —
    it is NOT auto-sent, so the §11 gate holds it off the customer and the
    job stays in Ready-for-Billing rather than silently billed at $0."""
    from uuid import UUID as _U

    from gdx_dispatch.models.tenant_models import JobCloseout

    seed = _seed(session_factory)
    db = session_factory()
    try:
        db.execute(
            text("UPDATE jobs SET job_type = 'QB Import' WHERE id = :j"),
            {"j": seed["job_id"]},
        )
        db.add(JobCloseout(
            id=uuid4(),
            job_id=_U(seed["job_id"]),
            parts_used=[],
            no_parts_used=True,
            hours_worked=3.0,
            techs_on_site=1,
            closed_by_user_id="user-1",
            closed_at=datetime.now(UTC),
        ))
        db.commit()

        resp = mobile_invoicing.mobile_create_invoice(
            job_id=seed["job_id"],
            payload=mobile_invoicing.CreateInvoiceIn(send_email=True),
            request=_request(), current_user=_TEST_USER, db=db,
        )
        assert resp.status_code == 201, resp.body
        body = _as_json(resp)
        assert float(body["total"]) == 0.0
        assert body["status"] == "draft", "a $0 office-lane invoice must NOT auto-send"
        assert body["awaiting_verification"] is True
        labor = [ln for ln in body.get("lines", []) if "man-hours" in (ln.get("description") or "")]
        assert not labor, "office lane must not price labor"
    finally:
        db.close()


def test_invoice_install_lane_flat_price_no_double_labor(session_factory):
    """Plan §8 install lane end-to-end (audit: the router install branch had
    no test). Install job + matrix pick + hours>0 → ONE flat labor line
    (never flat + hourly — the lanes are mutually exclusive), draft."""
    import datetime as _dt
    from decimal import Decimal as _D
    from uuid import UUID as _U
    from uuid import uuid4 as _u4

    from gdx_dispatch.models.labor_pricing import LaborPriceItem
    from gdx_dispatch.models.tenant_models import JobCloseout

    seed = _seed(session_factory)
    db = session_factory()
    try:
        LaborPriceItem.__table__.create(bind=db.get_bind(), checkfirst=True)
        item_id = _u4()
        db.add(LaborPriceItem(
            id=item_id, description="16x7 Sectional Install", service_type="install",
            flat_price=_D("650"), assumed_man_hours=_D("6.5"), default_crew_size=1,
            min_wall_clock_minutes=15, active=True, effective_from=_dt.date(2026, 1, 1),
            sort_order=1,
        ))
        db.execute(
            text("UPDATE jobs SET job_type = 'Installation' WHERE id = :j"),
            {"j": seed["job_id"]},
        )
        db.add(JobCloseout(
            id=_u4(), job_id=_U(seed["job_id"]), parts_used=[], no_parts_used=True,
            hours_worked=8.0, techs_on_site=2,  # hours + crew that WOULD hourly-bill
            labor_matrix_item_id=str(item_id),
            closed_by_user_id="user-1", closed_at=datetime.now(UTC),
        ))
        db.commit()

        resp = mobile_invoicing.mobile_create_invoice(
            job_id=seed["job_id"],
            payload=mobile_invoicing.CreateInvoiceIn(send_email=False),
            request=_request(), current_user=_TEST_USER, db=db,
        )
        assert resp.status_code == 201, resp.body
        body = _as_json(resp)
        # Flat $650, NOT 8h×2×$100 — the install lane ignores hours for price.
        assert float(body["total"]) == 650.0, body["total"]
        install_lines = [ln for ln in body["lines"] if "Install —" in (ln["description"] or "")]
        hourly_lines = [ln for ln in body["lines"] if "man-hours" in (ln["description"] or "")]
        assert len(install_lines) == 1
        assert not hourly_lines, "install lane must not ALSO add an hourly line"
    finally:
        db.close()


def test_line_built_tier_truck_invoice_is_recalc_stable(session_factory):
    """Quality-review catch (2026-08-14): the truck's single all-taxable
    summary line made a LINE-BUILT tier with Labor grow on the first office
    _recalculate_invoice (record_payment, any line edit) — the seeded tax
    excluded labor, the recalc re-taxed the whole package. The truck now
    copies the tier's lines with per-line taxable flags, so the recalc
    reproduces the seeded tax instead of silently growing the total."""
    from decimal import Decimal

    from gdx_dispatch.models.tenant_models import Invoice
    from gdx_dispatch.modules.proposals.models import ProposalTierLine
    from gdx_dispatch.routers.invoices import _recalculate_invoice

    seed = _seed(session_factory)

    # Build the quote, then make the "better" tier LINE-BUILT (a labor line
    # + materials) BEFORE the customer accepts. tax_rate on the estimate so
    # the invoice runs rate-mode — the unstable configuration.
    db = session_factory()
    try:
        build = mobile_quoting.build_quote(
            job_id=seed["job_id"],
            payload=mobile_quoting.BuildQuoteIn(service="spring_replacement"),
            request=_request(), current_user=_TEST_USER, db=db,
        )
        quote = _as_json(build)
        better = next(t for t in quote["tiers"] if t["tier_name"] == "better")
        from uuid import UUID as _PyUUID
        db.add(ProposalTierLine(
            tier_id=_PyUUID(better["id"]), description="Install labor", category="Labor",
            quantity=1, unit_price=Decimal("2000.00"), line_total=Decimal("2000.00"),
            sort_order=1, company_id="tenant-a",
        ))
        db.add(ProposalTierLine(
            tier_id=_PyUUID(better["id"]), description="Opener + hardware",
            quantity=1, unit_price=Decimal("6000.00"), line_total=Decimal("6000.00"),
            sort_order=2, company_id="tenant-a",
        ))
        from gdx_dispatch.modules.proposals.models import Estimate as _Est
        est_row = db.get(_Est, _PyUUID(quote["id"]))
        est_row.tax_rate = Decimal("0.07375")
        db.commit()

        sig = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB"
        mobile_quoting.accept_quote(
            estimate_id=quote["id"],
            payload=mobile_quoting.AcceptQuoteIn(
                chosen_tier_id=better["id"], signature_data=sig, signed_by="John Customer",
            ),
            request=_request(), current_user=_TEST_USER, db=db,
        )
        # The truck accept books the LINE sum, not the preset tier price.
        db.refresh(est_row)
        assert float(est_row.total) == pytest.approx(8000.0)

        with patch("gdx_dispatch.routers.mobile_invoicing._send_invoice_email"):
            resp = mobile_invoicing.mobile_create_invoice(
                job_id=seed["job_id"],
                payload=mobile_invoicing.CreateInvoiceIn(estimate_id=quote["id"]),
                request=_request(), current_user=_TEST_USER, db=db,
            )
        assert resp.status_code == 201, resp.body
        body = _as_json(resp)
        assert len(body["lines"]) == 2  # the tier's lines, not one summary line
        total_before = float(body["total"])
        # Labor excluded: tax on 6000 only → 8000 + 442.50.
        assert total_before == pytest.approx(8442.50)

        # THE pin: an office recalc must not change the total.
        inv = db.get(Invoice, _PyUUID(body["id"]))
        _recalculate_invoice(inv, db)
        db.commit()
        db.refresh(inv)
        assert float(inv.total) == pytest.approx(total_before)
    finally:
        db.close()


def test_line_built_tier_truck_invoice_carries_the_discount(session_factory):
    """Gate-audit catch: the seeded tax subtracts est.discount, so the lines
    must carry the M7 discount line too — without it the truck over-billed a
    discounted line-built proposal by exactly the discount."""
    from decimal import Decimal
    from uuid import UUID as _PyUUID

    from gdx_dispatch.modules.proposals.models import Estimate as _Est
    from gdx_dispatch.modules.proposals.models import ProposalTierLine

    seed = _seed(session_factory)
    db = session_factory()
    try:
        build = mobile_quoting.build_quote(
            job_id=seed["job_id"],
            payload=mobile_quoting.BuildQuoteIn(service="spring_replacement"),
            request=_request(), current_user=_TEST_USER, db=db,
        )
        quote = _as_json(build)
        better = next(t for t in quote["tiers"] if t["tier_name"] == "better")
        db.add(ProposalTierLine(
            tier_id=_PyUUID(better["id"]), description="Opener + hardware",
            quantity=1, unit_price=Decimal("6000.00"), line_total=Decimal("6000.00"),
            sort_order=1, company_id="tenant-a",
        ))
        est_row = db.get(_Est, _PyUUID(quote["id"]))
        est_row.discount = Decimal("500.00")
        db.commit()

        sig = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB"
        mobile_quoting.accept_quote(
            estimate_id=quote["id"],
            payload=mobile_quoting.AcceptQuoteIn(
                chosen_tier_id=better["id"], signature_data=sig, signed_by="John Customer",
            ),
            request=_request(), current_user=_TEST_USER, db=db,
        )
        with patch("gdx_dispatch.routers.mobile_invoicing._send_invoice_email"):
            resp = mobile_invoicing.mobile_create_invoice(
                job_id=seed["job_id"],
                payload=mobile_invoicing.CreateInvoiceIn(estimate_id=quote["id"]),
                request=_request(), current_user=_TEST_USER, db=db,
            )
        assert resp.status_code == 201, resp.body
        body = _as_json(resp)
        descs = [ln["description"] for ln in body["lines"]]
        assert "Discount" in descs
        # 6000 − 500 discount, no tax configured on this estimate.
        assert float(body["total"]) == pytest.approx(5500.0)
    finally:
        db.close()


def test_line_built_tier_zero_price_line_blocked_by_policy(session_factory):
    """Gate-audit catch: the tier-lines branch must enforce the F-75
    zero-price block like both sibling branches."""
    from decimal import Decimal
    from uuid import UUID as _PyUUID

    from gdx_dispatch.modules.proposals.models import ProposalTierLine

    seed = _seed(session_factory)
    db = session_factory()
    try:
        build = mobile_quoting.build_quote(
            job_id=seed["job_id"],
            payload=mobile_quoting.BuildQuoteIn(service="spring_replacement"),
            request=_request(), current_user=_TEST_USER, db=db,
        )
        quote = _as_json(build)
        better = next(t for t in quote["tiers"] if t["tier_name"] == "better")
        db.add(ProposalTierLine(
            tier_id=_PyUUID(better["id"]), description="Mystery item",
            quantity=1, unit_price=Decimal("0.00"), line_total=Decimal("0.00"),
            sort_order=1, company_id="tenant-a",
        ))
        db.commit()

        sig = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB"
        mobile_quoting.accept_quote(
            estimate_id=quote["id"],
            payload=mobile_quoting.AcceptQuoteIn(
                chosen_tier_id=better["id"], signature_data=sig, signed_by="John Customer",
            ),
            request=_request(), current_user=_TEST_USER, db=db,
        )
        with patch("gdx_dispatch.routers.mobile_invoicing._send_invoice_email"), \
             patch("gdx_dispatch.modules.catalog_policy.get_policy") as pol:
            pol.return_value.block_zero_price_on_invoice = True
            resp = mobile_invoicing.mobile_create_invoice(
                job_id=seed["job_id"],
                payload=mobile_invoicing.CreateInvoiceIn(estimate_id=quote["id"]),
                request=_request(), current_user=_TEST_USER, db=db,
            )
        assert resp.status_code == 422, resp.body
    finally:
        db.close()


def _receipt_ready_invoice(session_factory, db):
    """Verified invoice + one recorded payment + a customer with an email."""
    seed = _seed(session_factory)
    estimate_id = _build_and_accept(session_factory, seed)
    with patch("gdx_dispatch.routers.mobile_invoicing._send_invoice_email"):
        create = mobile_invoicing.mobile_create_invoice(
            job_id=seed["job_id"],
            payload=mobile_invoicing.CreateInvoiceIn(estimate_id=estimate_id, send_email=False),
            request=_request(), current_user=_TEST_USER, db=db,
        )
    inv = _as_json(create)
    from datetime import UTC as _UTC
    from datetime import datetime as _dt
    from uuid import UUID as _UUID

    from gdx_dispatch.models.tenant_models import Customer as _Cust
    from gdx_dispatch.models.tenant_models import Invoice as _Inv
    from gdx_dispatch.models.tenant_models import Payment as _Pay
    row = db.get(_Inv, _UUID(inv["id"]))
    row.verified_at = _dt.now(_UTC)
    # The raw-SQL seed stores a dashed uuid string SQLite ORM lookups can't
    # match — attach an ORM-created customer so resolve_recipient finds it.
    cust = _Cust(name="Payer Person", email="payer@example.com", company_id="tenant-a")
    db.add(cust)
    db.flush()
    row.customer_id = cust.id
    db.add(_Pay(invoice_id=row.id, amount=50, method="cash",
                company_id=str(row.company_id or "t1")))
    db.commit()
    return inv


def test_send_receipt_reports_real_outcome_not_fake_success(session_factory, monkeypatch):
    """Audit find 2026-08-18: the old path discarded the send result and
    answered a hardcoded {"sent": true} (and was SMTP-only — guaranteed
    non-delivery on a tenant with no email_settings row, i.e. prod). The
    endpoint must now surface the REAL outcome."""
    import gdx_dispatch.core.transactional_email as te
    db = session_factory()
    try:
        inv = _receipt_ready_invoice(session_factory, db)

        monkeypatch.setattr(
            te, "send_transactional_email",
            lambda **kw: (False, None, "no_email_provider_connected"),
        )
        resp = mobile_invoicing.mobile_send_receipt(
            invoice_id=inv["id"], payload=mobile_invoicing.SendReceiptIn(),
            request=_request(), current_user=_TEST_USER, db=db,
        )
        assert resp.status_code == 200
        body = _as_json(resp)
        assert body["sent"] is False
        assert body["skip_reason"] == "no_email_provider_connected"
    finally:
        db.close()


def test_send_receipt_branded_body_and_pipeline(session_factory, monkeypatch):
    """The receipt rides the unified pipeline (Outlook-capable, audited) with
    a branded body carrying the remaining balance."""
    import gdx_dispatch.core.transactional_email as te
    captured = {}

    def _fake(**kw):
        captured.update(kw)
        return (True, "outlook_graph", None)

    db = session_factory()
    try:
        inv = _receipt_ready_invoice(session_factory, db)
        monkeypatch.setattr(te, "send_transactional_email", _fake)
        resp = mobile_invoicing.mobile_send_receipt(
            invoice_id=inv["id"], payload=mobile_invoicing.SendReceiptIn(),
            request=_request(), current_user=_TEST_USER, db=db,
        )
        assert resp.status_code == 200
        assert _as_json(resp)["sent"] is True
        assert captured["to_email"] == "payer@example.com"
        assert captured["entity_type"] == "invoice"
        html = captured["html_body"]
        assert "Payment received" in html
        assert "$50.00" in html
        # Branded shell, not the old three bare <p> tags.
        assert "<table role=\"presentation\"" in html
    finally:
        db.close()


def test_truck_invoice_is_dated_the_shop_day_not_the_utc_day(session_factory):
    """#444: 04:30 UTC on 14 Sep is 23:30 on the 13th in America/Chicago, and
    already the 14th in UTC and in the New York fallback. A truck
    invoice written that evening was dated the 14th."""
    from datetime import date, timedelta

    from freezegun import freeze_time

    # Imported before the freeze: the route lazily imports routers.mobile, and
    # a FastAPI router built under freezegun sees `FakeDate` in its annotations
    # and refuses it — the test then fails only when it runs first in its shard.
    import gdx_dispatch.routers.mobile  # noqa: F401
    from gdx_dispatch.models.tenant_models import AppSettings

    seed = _seed(session_factory)
    db = session_factory()
    try:
        db.add(AppSettings(timezone="America/Chicago"))
        db.commit()
        with freeze_time("2026-09-14 04:30:00"), patch("gdx_dispatch.routers.mobile_invoicing._send_invoice_email"):
            resp = mobile_invoicing.mobile_create_invoice(
                job_id=seed["job_id"],
                payload=mobile_invoicing.CreateInvoiceIn(send_email=False),
                request=_request(), current_user=_TEST_USER, db=db,
            )
        assert resp.status_code == 201
        body = _as_json(resp)
        assert body["invoice_date"] == date(2026, 9, 13).isoformat()
        assert body["due_date"] == (date(2026, 9, 13) + timedelta(days=30)).isoformat()
    finally:
        db.close()


def test_mobile_send_pdf_renders_sent_with_pay_link(session_factory, monkeypatch):
    """The truck's first send attached a PDF built while the row was still a
    draft: "Payment Status: Draft", no pay link, beside an email body that
    carried the link. The flip now precedes the render,
    as on the desktop /send. Only the provider and the PDF engine are faked;
    the payload the PDF is built from is the real one."""
    from gdx_dispatch.core import payments, pdf_generator
    from gdx_dispatch.models.tenant_models import Invoice as _Inv

    monkeypatch.setenv("GDX_PUBLIC_BASE_URL", "https://pay.example.test")
    monkeypatch.setattr(payments, "stripe_configured", lambda: True)
    rendered: dict = {}

    def _fake_pdf(invoice_data, tenant_branding, template_config=None):
        rendered.update(invoice_data)
        return b"%PDF-1.4 test"

    monkeypatch.setattr(pdf_generator, "generate_invoice_pdf", _fake_pdf)
    sent: dict = {}

    def _fake_send(**kwargs):
        sent.update(kwargs)
        return True, "test-provider", None

    seed = _seed(session_factory)
    estimate_id = _build_and_accept(session_factory, seed)
    db = session_factory()
    try:
        inv = _as_json(mobile_invoicing.mobile_create_invoice(
            job_id=seed["job_id"],
            payload=mobile_invoicing.CreateInvoiceIn(estimate_id=estimate_id, send_email=False),
            request=_request(), current_user=_TEST_USER, db=db,
        ))
        # The seed's customer is raw-SQL inserted with a dashed id, which
        # SQLite's dashless Uuid column never matches — give the invoice an
        # ORM customer with an address to mail.
        from gdx_dispatch.models.tenant_models import Customer as _Cust

        cust = _Cust(id=uuid4(), company_id="tenant-a", name="Acme", email="a@example.com")
        db.add(cust)
        row = db.get(_Inv, UUID(inv["id"]))
        row.customer_id = cust.id
        row.verified_at = datetime.now(UTC)
        row.verified_by_user_id = "office-user"
        db.commit()
        assert row.status == "draft"

        with patch("gdx_dispatch.core.transactional_email.send_transactional_email", _fake_send):
            send = mobile_invoicing.mobile_send_invoice(
                invoice_id=inv["id"], request=_request(), current_user=_TEST_USER, db=db,
            )
        assert send.status_code == 200
        db.refresh(row)
        assert row.status == "sent" and row.public_token
        assert sent.get("attachments"), "no PDF was attached — the test proves nothing"
        assert rendered["status"] == "sent"
        assert rendered["status_label"] == "Unpaid"
        assert rendered["pay_url"] == f"https://pay.example.test/pay/{row.public_token}"
        # The PDF's link and the email body's link are the same token.
        assert rendered["pay_url"] in sent["html_body"]
    finally:
        db.close()


def test_mobile_send_dead_transaction_keeps_flip_and_audit(session_factory, monkeypatch):
    """The flip is committed before the email now, so it must carry its audit
    row in the same commit: `_send_invoice_email` can swallow a failure that
    leaves the transaction dead (its docstring). Before the audit row moved,
    that left a committed `sent` naming nobody. The tail rolls back and
    recovers, as the desktop /send does (GDXA-673)."""
    from sqlalchemy import select

    from gdx_dispatch.core.audit import AuditLog
    from gdx_dispatch.models.tenant_models import Invoice as _Inv

    def _poisoning_send(db, invoice, *, tenant_id, user_id=None, **_kw):
        # A swallowed flush failure: the session is left needing rollback,
        # and the caller is told only "not delivered".
        try:
            db.add(_Inv(id=uuid4(), company_id="tenant-a"))  # NOT NULLs unset
            db.flush()
        except Exception:
            pass
        return False

    seed = _seed(session_factory)
    estimate_id = _build_and_accept(session_factory, seed)
    db = session_factory()
    try:
        with patch("gdx_dispatch.routers.mobile_invoicing._send_invoice_email"):
            inv = _as_json(mobile_invoicing.mobile_create_invoice(
                job_id=seed["job_id"],
                payload=mobile_invoicing.CreateInvoiceIn(estimate_id=estimate_id, send_email=False),
                request=_request(), current_user=_TEST_USER, db=db,
            ))
        row = db.get(_Inv, UUID(inv["id"]))
        row.verified_at = datetime.now(UTC)
        row.verified_by_user_id = "office-user"
        db.commit()
        monkeypatch.setattr(mobile_invoicing, "_send_invoice_email", _poisoning_send)
        send = mobile_invoicing.mobile_send_invoice(
            invoice_id=inv["id"], request=_request(), current_user=_TEST_USER, db=db,
        )
        assert send.status_code == 200
        assert _as_json(send)["email_sent"] is False
    finally:
        db.close()

    fresh = session_factory()
    try:
        row = fresh.get(_Inv, UUID(inv["id"]))
        assert row.status == "sent"
        assert row.sent_at is None  # nothing was delivered
        audits = fresh.scalars(select(AuditLog).where(
            AuditLog.entity_id == inv["id"], AuditLog.action == "mobile_invoice_sent",
        )).all()
        assert len(audits) == 1 and audits[0].user_id == _TEST_USER["user_id"]
    finally:
        fresh.close()


def _verified_invoice_with_customer(session_factory, db) -> str:
    """A verified draft invoice whose customer has an address to mail."""
    from gdx_dispatch.models.tenant_models import Customer as _Cust
    from gdx_dispatch.models.tenant_models import Invoice as _Inv

    seed = _seed(session_factory)
    estimate_id = _build_and_accept(session_factory, seed)
    inv = _as_json(mobile_invoicing.mobile_create_invoice(
        job_id=seed["job_id"],
        payload=mobile_invoicing.CreateInvoiceIn(estimate_id=estimate_id, send_email=False),
        request=_request(), current_user=_TEST_USER, db=db,
    ))
    # The raw-SQL seed customer has a dashed id SQLite's Uuid column never
    # matches — give the invoice an ORM customer.
    cust = _Cust(id=uuid4(), company_id="tenant-a", name="Acme", email="a@example.com")
    db.add(cust)
    row = db.get(_Inv, UUID(inv["id"]))
    row.customer_id = cust.id
    row.verified_at = datetime.now(UTC)
    row.verified_by_user_id = "office-user"
    db.commit()
    return inv["id"]


def test_mobile_double_send_mails_once_and_audits_once(session_factory, monkeypatch):
    """GDXA-1278: a retried send from the truck mailed the customer twice and
    wrote two `mobile_invoice_sent` rows — the desktop /send's `recently_sent`
    guard was missing here, and the truck's send was not even tagged with its
    invoice, so no guard could see it. Real pipeline: only the SMTP hop is
    faked, so the outbound_emails row the guard reads is really written."""
    from sqlalchemy import select

    import gdx_dispatch.core.transactional_email as te
    from gdx_dispatch.core.audit import AuditLog
    from gdx_dispatch.models.tenant_models import OutboundEmail

    smtp_calls: list[dict] = []

    def _fake_smtp(**kw):
        smtp_calls.append(kw)
        return True, None

    monkeypatch.setattr(te, "_try_smtp", _fake_smtp)
    db = session_factory()
    try:
        inv_id = _verified_invoice_with_customer(session_factory, db)
        first = mobile_invoicing.mobile_send_invoice(
            invoice_id=inv_id, request=_request(), current_user=_TEST_USER, db=db,
        )
        assert first.status_code == 200
        assert _as_json(first)["email_sent"] is True
        second = mobile_invoicing.mobile_send_invoice(
            invoice_id=inv_id, request=_request(), current_user=_TEST_USER, db=db,
        )
        assert second.status_code == 200
        body = _as_json(second)
        assert body["email_sent"] is False
        assert body["email_skip_reason"] == "duplicate_send_suppressed"
        assert body["status"] == "sent"

        assert len(smtp_calls) == 1
        rows = db.scalars(select(OutboundEmail).where(OutboundEmail.entity_id == inv_id)).all()
        assert [(r.entity_type, r.kind, r.status) for r in rows] == [("invoice", "document", "sent")]
        audits = db.scalars(select(AuditLog).where(
            AuditLog.entity_id == inv_id, AuditLog.action == "mobile_invoice_sent",
        )).all()
        assert len(audits) == 1
    finally:
        db.close()


def test_mobile_double_receipt_mails_once(session_factory, monkeypatch):
    """GDXA-1278 sweep: /send-receipt had the same gap as /send. The second
    tap inside the window is suppressed, mails nothing and says so."""
    import gdx_dispatch.core.transactional_email as te

    smtp_calls: list[dict] = []

    def _fake_smtp(**kw):
        smtp_calls.append(kw)
        return True, None

    monkeypatch.setattr(te, "_try_smtp", _fake_smtp)
    db = session_factory()
    try:
        inv = _receipt_ready_invoice(session_factory, db)
        bodies = [
            _as_json(mobile_invoicing.mobile_send_receipt(
                invoice_id=inv["id"], payload=mobile_invoicing.SendReceiptIn(),
                request=_request(), current_user=_TEST_USER, db=db,
            ))
            for _ in range(2)
        ]
        assert bodies[0]["sent"] is True
        assert bodies[1]["sent"] is False
        assert bodies[1]["skip_reason"] == "duplicate_send_suppressed"
        assert len(smtp_calls) == 1
    finally:
        db.close()


def test_mobile_send_after_receipt_is_not_suppressed(session_factory, monkeypatch):
    """The guard is kind-scoped, as on the desktop: a receipt does not block
    the invoice email of an unpaid invoice."""
    import gdx_dispatch.core.transactional_email as te

    monkeypatch.setattr(te, "_try_smtp", lambda **kw: (True, None))
    db = session_factory()
    try:
        inv = _receipt_ready_invoice(session_factory, db)
        receipt = _as_json(mobile_invoicing.mobile_send_receipt(
            invoice_id=inv["id"], payload=mobile_invoicing.SendReceiptIn(),
            request=_request(), current_user=_TEST_USER, db=db,
        ))
        assert receipt["sent"] is True
        send = _as_json(mobile_invoicing.mobile_send_invoice(
            invoice_id=inv["id"], request=_request(), current_user=_TEST_USER, db=db,
        ))
        assert send["email_sent"] is True
        assert "email_skip_reason" not in send
    finally:
        db.close()


def test_mobile_receipt_for_a_second_payment_is_not_suppressed(session_factory, monkeypatch):
    """GDXA-1278 audit: a truck receipt is per PAYMENT. The guard must stop
    the same payment's receipt twice, never payment B's just after A's."""
    import gdx_dispatch.core.transactional_email as te
    from gdx_dispatch.models.tenant_models import Invoice as _Inv
    from gdx_dispatch.models.tenant_models import Payment as _Pay

    smtp_calls: list[dict] = []

    def _fake_smtp(**kw):
        smtp_calls.append(kw)
        return True, None

    monkeypatch.setattr(te, "_try_smtp", _fake_smtp)
    db = session_factory()
    try:
        inv = _receipt_ready_invoice(session_factory, db)
        first = _as_json(mobile_invoicing.mobile_send_receipt(
            invoice_id=inv["id"], payload=mobile_invoicing.SendReceiptIn(),
            request=_request(), current_user=_TEST_USER, db=db,
        ))
        assert first["sent"] is True
        row = db.get(_Inv, UUID(inv["id"]))
        second_payment = _Pay(invoice_id=row.id, amount=25, method="check",
                              company_id=str(row.company_id or "t1"))
        db.add(second_payment)
        db.commit()
        second = _as_json(mobile_invoicing.mobile_send_receipt(
            invoice_id=inv["id"],
            payload=mobile_invoicing.SendReceiptIn(payment_id=str(second_payment.id)),
            request=_request(), current_user=_TEST_USER, db=db,
        ))
        assert second["payment_id"] == str(second_payment.id)
        assert second["sent"] is True, second
        assert len(smtp_calls) == 2
    finally:
        db.close()


# ── picked attachments on the truck's re-send (2026-10-16) ─────────────


def _verified_mobile_invoice(session_factory, db):
    """The same invoice as ``_verified_invoice_with_customer``, as its row."""
    from gdx_dispatch.models.tenant_models import Invoice as _Inv

    return db.get(_Inv, UUID(_verified_invoice_with_customer(session_factory, db)))


def _photo_ref(n_bytes=40):
    import base64

    from gdx_dispatch.core.email_attachments import AttachmentRef

    return AttachmentRef(kind="upload", name="garage.pdf", content_type="application/pdf",
                         content_base64=base64.b64encode(b"x" * n_bytes).decode())


def test_mobile_send_carries_the_techs_picks_beside_the_pdf(session_factory, monkeypatch):
    from sqlalchemy import select

    from gdx_dispatch.core import pdf_generator
    from gdx_dispatch.core.audit import AuditLog

    monkeypatch.setattr(pdf_generator, "generate_invoice_pdf", lambda *a, **k: b"%PDF-1.4 test")
    sent: dict = {}
    db = session_factory()
    try:
        row = _verified_mobile_invoice(session_factory, db)
        with patch("gdx_dispatch.core.transactional_email.send_transactional_email",
                   lambda **kw: sent.update(kw) or (True, "test-provider", None)):
            send = mobile_invoicing.mobile_send_invoice(
                invoice_id=str(row.id), request=_request(),
                payload=mobile_invoicing.MobileSendInvoiceIn(attachment_refs=[_photo_ref()]),
                current_user=_TEST_USER, db=db,
            )
        assert send.status_code == 200
        assert _as_json(send)["pdf_attached"] is True
        names = [a["name"] for a in sent["attachments"]]
        assert len(names) == 2 and names[0].endswith(".pdf") and names[1] == "garage.pdf"
        (audit,) = db.scalars(select(AuditLog).where(
            AuditLog.entity_id == str(row.id), AuditLog.action == "mobile_invoice_sent",
        )).all()
        assert audit.details["attachments"][0]["name"] == "garage.pdf"
    finally:
        db.close()


def test_mobile_send_pdf_gives_way_to_the_picks(session_factory, monkeypatch):
    import gdx_dispatch.core.transactional_email as tx
    from gdx_dispatch.core import pdf_generator

    monkeypatch.setattr(pdf_generator, "generate_invoice_pdf", lambda *a, **k: b"%PDF" + b"p" * 60)
    monkeypatch.setattr(tx, "MAX_ATTACHMENT_BYTES", 100)
    sent: dict = {}
    db = session_factory()
    try:
        row = _verified_mobile_invoice(session_factory, db)
        with patch("gdx_dispatch.core.transactional_email.send_transactional_email",
                   lambda **kw: sent.update(kw) or (True, "test-provider", None)):
            send = mobile_invoicing.mobile_send_invoice(
                invoice_id=str(row.id), request=_request(),
                payload=mobile_invoicing.MobileSendInvoiceIn(attachment_refs=[_photo_ref(50)]),
                current_user=_TEST_USER, db=db,
            )
        assert [a["name"] for a in sent["attachments"]] == ["garage.pdf"]
        # The tech is told the PDF was left out (the dialog warns on this).
        assert _as_json(send)["pdf_attached"] is False
    finally:
        db.close()


def test_mobile_send_bad_pick_flips_nothing(session_factory):
    from fastapi import HTTPException

    from gdx_dispatch.core.email_attachments import AttachmentRef

    db = session_factory()
    try:
        row = _verified_mobile_invoice(session_factory, db)
        with (
            patch("gdx_dispatch.routers.mobile_invoicing._send_invoice_email") as mailer,
            pytest.raises(HTTPException) as exc,
        ):
            mobile_invoicing.mobile_send_invoice(
                invoice_id=str(row.id), request=_request(),
                payload=mobile_invoicing.MobileSendInvoiceIn(
                    attachment_refs=[AttachmentRef(kind="document", id=str(uuid4()))]),
                current_user=_TEST_USER, db=db,
            )
        assert exc.value.status_code == 404
        assert not mailer.called
        db.refresh(row)
        assert row.status == "draft"
    finally:
        db.close()


def test_mobile_resend_with_picks_inside_the_window_is_suppressed(session_factory, monkeypatch):
    """GDXA-1278 guard x picked files: a re-send with new files straight after
    a delivered send mails nothing (the picks included), audits nothing, and
    says why. The guard runs before the picks load, so even a pick that would
    404 is answered as the suppressed repeat."""
    from sqlalchemy import select

    import gdx_dispatch.core.transactional_email as te
    from gdx_dispatch.core.audit import AuditLog
    from gdx_dispatch.core.email_attachments import AttachmentRef

    smtp_calls: list[dict] = []

    def _fake_smtp(**kw):
        smtp_calls.append(kw)
        return True, None

    monkeypatch.setattr(te, "_try_smtp", _fake_smtp)
    db = session_factory()
    try:
        inv_id = _verified_invoice_with_customer(session_factory, db)
        first = _as_json(mobile_invoicing.mobile_send_invoice(
            invoice_id=inv_id, request=_request(), current_user=_TEST_USER, db=db,
        ))
        assert first["email_sent"] is True
        for refs in ([_photo_ref()], [AttachmentRef(kind="document", id=str(uuid4()))]):
            again = mobile_invoicing.mobile_send_invoice(
                invoice_id=inv_id, request=_request(),
                payload=mobile_invoicing.MobileSendInvoiceIn(attachment_refs=refs),
                current_user=_TEST_USER, db=db,
            )
            assert again.status_code == 200
            body = _as_json(again)
            assert body["email_sent"] is False
            assert body["email_skip_reason"] == "duplicate_send_suppressed"
        assert len(smtp_calls) == 1
        audits = db.scalars(select(AuditLog).where(
            AuditLog.entity_id == inv_id, AuditLog.action == "mobile_invoice_sent",
        )).all()
        assert len(audits) == 1
    finally:
        db.close()


# GDXA-1369: the truck send goes through the tech's own mailbox, as the desktop
# /send goes through the office user's. The ids are real UUIDs
# here because send_transactional_email skips every Outlook rung on an id that
# is not one, which is how the old user_id=None call went unnoticed. Only the
# provider hops are faked; the outbound_emails row is really written.
_TECH_UUID = "11111111-1111-4111-8111-111111111111"
_OFFICE_UUID = "22222222-2222-4222-8222-222222222222"
_TENANT_UUID = "33333333-3333-4333-8333-333333333333"


def _send_as_uuid_tech(session_factory, monkeypatch, outlook):
    """Send a verified invoice as a tech whose ids are UUIDs; returns the
    response body, the Outlook calls and the invoice's outbound_emails rows."""
    from sqlalchemy import select

    import gdx_dispatch.core.transactional_email as te
    from gdx_dispatch.models.tenant_models import OutboundEmail

    calls: list[str] = []

    def _fake_outlook(**kw):
        calls.append(str(kw["user_id"]))
        return outlook(str(kw["user_id"]))

    smtp_calls: list[dict] = []
    monkeypatch.setattr(te, "_try_outlook_graph", _fake_outlook)
    monkeypatch.setattr(te, "_designated_sender_user_id", lambda _db: _OFFICE_UUID)
    monkeypatch.setattr(te, "_try_smtp", lambda **kw: (smtp_calls.append(kw), (False, "smtp_not_configured"))[1])
    # The ownership gate is not under test; the UUID tech owns no seeded job.
    monkeypatch.setattr(mobile_invoicing, "_job_belongs_to_tech", lambda *a, **k: True)
    tech = {"user_id": _TECH_UUID, "role": "technician", "tenant_id": _TENANT_UUID}
    db = session_factory()
    try:
        inv_id = _verified_invoice_with_customer(session_factory, db)
        body = _as_json(mobile_invoicing.mobile_send_invoice(
            invoice_id=inv_id, request=_request(_TENANT_UUID), current_user=tech, db=db,
        ))
        rows = db.scalars(select(OutboundEmail).where(OutboundEmail.entity_id == inv_id)).all()
        return body, calls, smtp_calls, [(r.initiator_kind, r.initiator_ref, r.status) for r in rows]
    finally:
        db.close()


def test_mobile_send_goes_from_the_techs_mailbox(session_factory, monkeypatch):
    body, calls, smtp_calls, rows = _send_as_uuid_tech(
        session_factory, monkeypatch, lambda uid: (True, None),
    )
    assert body["email_sent"] is True
    assert calls == [_TECH_UUID]
    assert smtp_calls == []
    assert rows == [("user", _TECH_UUID, "sent")]


def test_mobile_send_without_a_tech_mailbox_does_not_use_the_office_mailbox(session_factory, monkeypatch):
    """A tech with no connected mailbox goes on to SMTP, as the desktop /send
    does — never through the office's "Send as" mailbox, which this call site
    does not opt into (outbound_emails cannot record which mailbox carried
    it). The row still names the tech."""
    body, calls, smtp_calls, rows = _send_as_uuid_tech(
        session_factory, monkeypatch,
        lambda uid: (True, None) if uid == _OFFICE_UUID else (False, "outlook_not_connected"),
    )
    assert body["email_sent"] is False
    assert calls == [_TECH_UUID]
    assert len(smtp_calls) == 1
    assert [(k, ref) for k, ref, _ in rows] == [("user", _TECH_UUID)]


def test_mobile_send_expired_tech_mailbox_does_not_fall_back(session_factory, monkeypatch):
    """An expired connection is the tech's to reconnect — no silent office
    send (send_transactional_email's rule); it reports non-delivery."""
    body, calls, smtp_calls, rows = _send_as_uuid_tech(
        session_factory, monkeypatch, lambda uid: (False, "outlook_reconnect_required"),
    )
    assert body["email_sent"] is False
    assert calls == [_TECH_UUID]
    assert len(smtp_calls) == 1
    assert [(k, ref) for k, ref, _ in rows] == [("user", _TECH_UUID)]

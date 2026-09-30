"""GET /api/invoices/{id}/activity — the invoice's trail, on the invoice.

Pins: the whitelist (patch noise and payments stay out), scoping by invoice
(another invoice's rows never leak in), payment reminders come from the
payment_reminders table — including the automatic sweep, which writes no
audit row — and the customer-view summary counts every view, not only the
ones that fit on the first page.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import AuditLog, TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import Customer, Invoice, PaymentReminder, User
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.invoices import router

TENANT = "tenant-test"
NOW = datetime(2026, 9, 29, 18, 0, tzinfo=UTC)


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    setup = Session()
    setup.execute(text(
        "CREATE TABLE IF NOT EXISTS company_module_grants (id TEXT PRIMARY KEY, company_id TEXT, "
        "module_key TEXT, granted_at TEXT, created_at TEXT, expires_at TEXT, UNIQUE(company_id, module_key))"
    ))
    setup.execute(text(
        "INSERT OR IGNORE INTO company_module_grants (id, company_id, module_key, granted_at, created_at) "
        "VALUES ('g1', :t, 'invoices', datetime('now'), datetime('now'))"
    ), {"t": TENANT})
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
        request.state.tenant = {"id": TENANT}
        return await call_next(request)

    app.include_router(router)
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "user-1", "role": "admin", "tenant_id": TENANT,
    }
    tc = TestClient(app, raise_server_exceptions=True)
    tc.session_factory = Session
    yield tc
    app.dependency_overrides.clear()
    engine.dispose()


def _db(client):
    return client.session_factory()


def _mk_invoice(client, *, deleted=False) -> str:
    db = _db(client)
    try:
        c = Customer(id=uuid4(), name="Pat Payer", email="pat@example.com", company_id=TENANT)
        db.add(c)
        db.commit()
        inv = Invoice(
            id=uuid4(), customer_id=c.id, invoice_number=f"INV-{uuid4().hex[:6]}",
            billing_type="standard", sequence_number=1,
            subtotal=Decimal("100"), tax_amount=0, total=Decimal("100"),
            balance_due=Decimal("100"), status="sent",
            invoice_date=date(2026, 9, 1), due_date=date(2026, 9, 15), company_id=TENANT,
            created_at=NOW - timedelta(days=30), public_token=uuid4().hex,
            deleted_at=NOW if deleted else None,
        )
        db.add(inv)
        db.commit()
        return str(inv.id)
    finally:
        db.close()


def _audit(client, *, entity_id, action, user_id="user-1", details=None, at=NOW,
           entity_type="invoice"):
    """tenant_id=None, the way several invoice writers actually record."""
    db = _db(client)
    try:
        db.add(AuditLog(
            tenant_id=None, user_id=user_id, action=action, entity_type=entity_type,
            entity_id=str(entity_id), details=details or {}, created_at=at,
        ))
        db.commit()
    finally:
        db.close()


def _reminder(client, *, invoice_id, sent_by, notes, channel="email", stage="friendly", at=NOW):
    db = _db(client)
    try:
        from uuid import UUID
        db.add(PaymentReminder(
            invoice_id=UUID(invoice_id), stage=stage, channel=channel,
            sent_at=at, sent_by=sent_by, notes=notes, created_at=at,
        ))
        db.commit()
    finally:
        db.close()


def _mk_user(client, name="Pat Office") -> str:
    db = _db(client)
    try:
        u = User(email=f"{uuid4().hex[:6]}@example.com", name=name, role="admin",
                 company_id=TENANT, password_hash="x")
        db.add(u)
        db.commit()
        return str(u.id)
    finally:
        db.close()


def test_404_for_unknown_invoice(client):
    assert client.get(f"/api/invoices/{uuid4()}/activity").status_code == 404


def test_404_for_soft_deleted_invoice(client):
    inv = _mk_invoice(client, deleted=True)
    assert client.get(f"/api/invoices/{inv}/activity").status_code == 404


def test_exclusions_scope_and_order(client):
    """Patch noise and payment movements stay out; another invoice's rows never
    appear; a row with the same id under another entity_type never appears;
    newest first."""
    inv = _mk_invoice(client)
    other = _mk_invoice(client)
    _audit(client, entity_id=inv, action="invoice_created", at=NOW - timedelta(days=3))
    _audit(client, entity_id=inv, action="patch_invoice", at=NOW - timedelta(days=2))
    _audit(client, entity_id=inv, action="invoice_sent", at=NOW - timedelta(days=1))
    _audit(client, entity_id=inv, action="payment_recorded", at=NOW - timedelta(hours=5))
    _audit(client, entity_id=inv, action="invoice_viewed_by_customer",
           user_id="customer", at=NOW - timedelta(hours=2))
    _audit(client, entity_id=other, action="invoice_sent", at=NOW - timedelta(hours=1))
    _audit(client, entity_id=inv, action="invoice_sent", entity_type="estimate",
           at=NOW - timedelta(minutes=30))

    body = client.get(f"/api/invoices/{inv}/activity").json()
    assert [i["action"] for i in body["items"]] == [
        "invoice_viewed_by_customer", "invoice_sent", "invoice_created",
    ]
    assert body["total"] == 3
    assert body["items"][0]["label"] == "Viewed by customer"
    assert body["items"][0]["user_name"] == "Customer"


def test_reminders_come_from_the_reminder_table(client):
    """The automatic sweep writes a payment_reminders row and no audit row;
    the panel must still show it, named as the system. A skip reads as a skip,
    and a manual phone log is not claimed as something the system sent."""
    inv = _mk_invoice(client)
    staff = _mk_user(client, name="Robin Desk")
    _audit(client, entity_id=inv, action="invoice_sent", at=NOW - timedelta(days=10))
    _reminder(client, invoice_id=inv, sent_by="auto-dunning", notes="[delivered]",
              stage="first_reminder", at=NOW - timedelta(days=3))
    _reminder(client, invoice_id=inv, sent_by="auto-dunning",
              notes="[skipped: no customer email] (t=14)", stage="second_reminder",
              at=NOW - timedelta(days=1))
    _reminder(client, invoice_id=inv, sent_by=staff, notes="left voicemail",
              channel="phone", at=NOW - timedelta(hours=3))
    # Pre-2026-07-07 shape: channel email, no marker, nothing actually sent.
    _reminder(client, invoice_id=inv, sent_by=staff, notes=None,
              at=NOW - timedelta(days=20))
    other = _mk_invoice(client)
    _reminder(client, invoice_id=other, sent_by="auto-dunning", notes="[delivered]")

    body = client.get(f"/api/invoices/{inv}/activity").json()
    items = body["items"]
    assert body["total"] == 5
    assert [i["label"] for i in items] == [
        "Payment reminder logged (phone)",
        "Payment reminder not sent",
        "Payment reminder emailed",
        "Sent",
        "Payment reminder logged (email)",
    ]
    assert items[0]["user_name"] == "Robin Desk"
    assert items[1]["user_name"] == "System — automatic payment reminders"
    assert items[1]["details"]["skip_reason"] == "no customer email"
    assert items[2]["details"]["stage"] == "first_reminder"
    assert items[2]["action"] == "payment_reminder"


def test_customer_view_summary_counts_past_the_page(client):
    """The header line must not depend on how many rows fit on the page."""
    inv = _mk_invoice(client)
    for h in range(5):
        _audit(client, entity_id=inv, action="invoice_viewed_by_customer",
               user_id="customer", at=NOW - timedelta(days=5, hours=h))
    for m in range(3):
        _audit(client, entity_id=inv, action="invoice_sent", at=NOW - timedelta(minutes=m))

    body = client.get(f"/api/invoices/{inv}/activity", params={"limit": 2}).json()
    assert len(body["items"]) == 2
    assert all(i["action"] == "invoice_sent" for i in body["items"])
    views = body["context"]["customer_views"]
    assert views["count"] == 5
    assert datetime.fromisoformat(views["last_at"]) == NOW - timedelta(days=5)


def test_no_views_reads_zero(client):
    inv = _mk_invoice(client)
    _audit(client, entity_id=inv, action="invoice_sent")
    views = client.get(f"/api/invoices/{inv}/activity").json()["context"]["customer_views"]
    assert views["count"] == 0 and views["last_at"] is None


def _token(client, inv) -> str:
    db = _db(client)
    try:
        from uuid import UUID
        return db.get(Invoice, UUID(inv)).public_token
    finally:
        db.close()


def _email(client, inv, *, status="sent", body=None, kind="document", bounced=False,
           at=NOW - timedelta(days=1), to="pat@example.com", skip_reason=None,
           initiator_kind="user", initiator_ref="user-1"):
    from gdx_dispatch.models.tenant_models import OutboundEmail

    db = _db(client)
    try:
        db.add(OutboundEmail(
            company_id=TENANT, initiator_kind=initiator_kind, initiator_ref=initiator_ref, kind=kind,
            entity_type="invoice", entity_id=inv, to_email=to, subject="Invoice",
            body_html=body if body is not None else f'<a href="https://x.test/pay/{_token(client, inv)}">Pay</a>',
            status=status, skip_reason=skip_reason, created_at=at,
            bounced_at=at if bounced else None,
        ))
        db.commit()
    finally:
        db.close()


def _link_sent_at(client, inv):
    return client.get(f"/api/invoices/{inv}/activity").json()["context"]["customer_views"]["link_sent_at"]


def test_link_sent_only_when_a_delivered_email_carried_the_link(client):
    inv = _mk_invoice(client)
    _email(client, inv)
    assert datetime.fromisoformat(_link_sent_at(client, inv)) == NOW - timedelta(days=1)
    # A reminder email carrying the link counts as well.
    rem = _mk_invoice(client)
    _email(client, rem, kind="reminder")
    assert _link_sent_at(client, rem) is not None


def test_no_link_sent_for_failed_bounced_linkless_or_old_emails(client):
    """Every way an email can leave the customer without a working link."""
    cases = (
        {"status": "failed", "skip_reason": "graph_error"},
        {"bounced": True},
        {"body": "<p>Balance $0.00 — thank you</p>"},  # zero balance / no Stripe: no link
        {"at": datetime(2026, 7, 1, tzinfo=UTC)},  # before views were recorded
    )
    for case in cases:
        inv = _mk_invoice(client)
        _email(client, inv, **case)
        assert _link_sent_at(client, inv) is None, case


def test_texts_count_only_when_confirmed(client):
    for action, expect in (("mobile_invoice_sent_sms", True), ("invoice_sent_sms_scheduled", True),
                           ("invoice_sms_unconfirmed", False), ("invoice_marked_sent", False)):
        inv = _mk_invoice(client)
        _audit(client, entity_id=inv, action=action, at=NOW - timedelta(hours=1))
        assert (_link_sent_at(client, inv) is not None) is expect, action


def test_real_send_to_a_customer_without_email_is_not_a_link_send(client, monkeypatch):
    """Through the real POST /send: the invoice_sent row is written before
    the email is attempted, so it must not stand in for delivery. The trail
    says "Sent", not "Emailed", and no link is claimed."""
    monkeypatch.setenv("GDX_PUBLIC_BASE_URL", "https://pay.example.test")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    db = _db(client)
    try:
        from uuid import UUID
        c = Customer(id=uuid4(), name="No Mail", email=None, company_id=TENANT)
        db.add(c)
        db.commit()
        inv = _mk_invoice(client)
        row = db.get(Invoice, UUID(inv))
        row.customer_id = c.id
        row.verified_at = NOW
        db.commit()
    finally:
        db.close()
    r = client.post(f"/api/invoices/{inv}/send", json={})
    assert r.status_code == 200, r.text
    assert r.json().get("email_sent") is False
    body = client.get(f"/api/invoices/{inv}/activity").json()
    assert [i["label"] for i in body["items"]] == ["Sent"]
    assert body["context"]["customer_views"]["link_sent_at"] is None


def test_email_rows_show_what_the_email_did(client):
    inv = _mk_invoice(client)
    _email(client, inv, at=NOW - timedelta(hours=3))
    _email(client, inv, status="failed", skip_reason="graph_error", at=NOW - timedelta(hours=2))
    _email(client, inv, bounced=True, at=NOW - timedelta(hours=1))
    _email(client, inv, kind="reminder", at=NOW)  # delivered: shown via payment_reminders instead
    body = client.get(f"/api/invoices/{inv}/activity").json()
    # An invoice email's bounce is the detector's row to tell (see the real
    # detector tests below); the email row itself records the send.
    assert [i["label"] for i in body["items"]] == [
        "Invoice email sent to pat@example.com", "Invoice email not sent",
        "Invoice email sent to pat@example.com",
    ]
    assert body["items"][1]["details"]["skip_reason"] == "graph_error"
    assert body["total"] == 3


def test_each_reminder_outcome_shows_exactly_once(client):
    """One owner per outcome, seeded the way both reminder writers really
    record: payment_reminders says emailed / not sent; outbound_emails adds
    only a bounce, which nothing else records."""
    inv = _mk_invoice(client)
    # Delivered, then bounced: the reminder row says emailed; the email row adds the bounce.
    _reminder(client, invoice_id=inv, sent_by="auto-dunning", notes="[delivered]",
              at=NOW - timedelta(hours=3))
    _email(client, inv, kind="reminder", bounced=True, at=NOW - timedelta(hours=2))
    # Failed at the mail sender: both writers record the skip AND the sender logs
    # a failed email row — shown once, via the reminder row.
    _reminder(client, invoice_id=inv, sent_by="auto-dunning",
              notes="[skipped: no_email_provider_connected] (t=14)", at=NOW - timedelta(hours=1))
    _email(client, inv, kind="reminder", status="failed",
           skip_reason="no_email_provider_connected", at=NOW - timedelta(hours=1))
    body = client.get(f"/api/invoices/{inv}/activity").json()
    assert [i["label"] for i in body["items"]] == [
        "Payment reminder not sent", "Reminder email bounced", "Payment reminder emailed",
    ]
    assert body["total"] == 3



def test_reminder_verdict_is_the_last_marker(client):
    """Manual sends store the staff note, then the system's marker. A note
    that quotes a marker must not overturn the system's verdict."""
    inv = _mk_invoice(client)
    _reminder(client, invoice_id=inv, sent_by="user-1",
              notes="customer said [skipped: out of town] last time [delivered]")
    item = client.get(f"/api/invoices/{inv}/activity").json()["items"][0]
    assert item["label"] == "Payment reminder emailed"


def _bounce_through_the_real_detector(client, *, subject_matches: bool):
    """Send a linked invoice email, then run the real bounce matcher on an
    NDR — by subject (the detector's main path, which never stamps
    bounced_at) or by recipient address (which stamps it and also writes the
    rejected row)."""
    from types import SimpleNamespace
    from uuid import UUID

    from gdx_dispatch.modules.outlook import bounce_detect

    inv = _mk_invoice(client)
    db = _db(client)
    try:
        row = db.get(Invoice, UUID(inv))
        row.sent_at = NOW - timedelta(hours=1)
        number = row.invoice_number
        db.commit()
    finally:
        db.close()
    _email(client, inv, at=NOW - timedelta(hours=1))
    subject = f"Undeliverable: Invoice {number} from Acme" if subject_matches else "Undeliverable: something custom"
    ndr = SimpleNamespace(subject=subject, to_addresses=["pat@example.com"], body_preview="",
                          graph_message_id="m1", conversation_id=None, account_id=None)
    db = _db(client)
    try:
        assert bounce_detect._match_invoices(db, ndr, NOW) >= 1
        db.commit()
    finally:
        db.close()
    return client.get(f"/api/invoices/{inv}/activity").json()


@pytest.mark.parametrize("subject_matches", [True, False])
def test_a_real_bounce_shows_once_and_voids_the_link(client, subject_matches):
    body = _bounce_through_the_real_detector(client, subject_matches=subject_matches)
    assert [i["label"] for i in body["items"]] == [
        "Email bounced — the customer did not receive it",
        "Invoice email sent to pat@example.com",
    ]
    assert body["context"]["customer_views"]["link_sent_at"] is None


def test_an_email_sent_after_a_bounce_counts_again(client):
    """The office fixed the address and re-sent: that later delivered email
    is proof the link arrived, even though an earlier one bounced."""
    body = _bounce_through_the_real_detector(client, subject_matches=True)
    inv = body["items"][0]["entity_id"]
    # The detector stamps its row with the wall clock, so the re-send must
    # come after real now, not after the fixture's NOW.
    resent = datetime.now(UTC) + timedelta(minutes=5)
    _email(client, inv, at=resent, to="pat.fixed@example.com")
    views = client.get(f"/api/invoices/{inv}/activity").json()["context"]["customer_views"]
    assert views["link_sent_at"] is not None
    assert datetime.fromisoformat(views["link_sent_at"]) == resent


def test_machine_sent_emails_name_the_system_and_read_cleanly(client):
    """A bounced automatic reminder has no user behind it; an automation
    email has a kind with no noun. Neither may surface a raw slug or
    "Email email"."""
    inv = _mk_invoice(client)
    _email(client, inv, kind="reminder", bounced=True, initiator_kind="reminder_task",
           initiator_ref=None, at=NOW - timedelta(hours=2))
    _email(client, inv, kind="automation", initiator_kind="workflow_rule",
           initiator_ref="rule-7", at=NOW - timedelta(hours=1))
    items = client.get(f"/api/invoices/{inv}/activity").json()["items"]
    assert [(i["label"], i["user_name"]) for i in items] == [
        ("Email sent to pat@example.com", "System — automation rule"),
        ("Reminder email bounced", "System — automatic payment reminders"),
    ]

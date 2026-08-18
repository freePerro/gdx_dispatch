"""outbound_emails audit trail + person-aware recipient resolution.

Locked requirement (2026-08-18): every send ATTEMPT — success, failure, or
missing recipient — writes one outbound_emails row with initiator, exact
rendered body, resolved recipient, and outcome. And business accounts greet
the person (CustomerContact), never the company name, once a contact exists.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select

from gdx_dispatch.core import transactional_email as te
from gdx_dispatch.core.email_recipients import resolve_recipient
from gdx_dispatch.models.tenant_models import Customer, CustomerContact, OutboundEmail

TENANT = "11111111-1111-1111-1111-111111111111"


@pytest.fixture()
def db(tenant_db):
    return tenant_db


def _customer(db, *, name="Acme Lumber Yard", email="front@acme.example"):
    cust = Customer(id=uuid4(), name=name, email=email, company_id=TENANT)
    db.add(cust)
    db.flush()
    return cust


def _contact(db, cust, *, name="Bob Jones", email="bob@acme.example",
             is_primary=False, deleted_at=None):
    contact = CustomerContact(
        company_id=TENANT, customer_id=cust.id, name=name, email=email,
        is_primary=is_primary, deleted_at=deleted_at,
    )
    db.add(contact)
    db.flush()
    return contact


# --------------------------------------------------------------------------- #
# resolve_recipient
# --------------------------------------------------------------------------- #

def test_residential_account_unchanged(db):
    cust = _customer(db, name="Jane Smith", email="jane@example.com")
    r = resolve_recipient(db, cust)
    assert r.email == "jane@example.com"
    assert r.greeting_name == "Jane Smith"
    assert r.source == "account_email"
    assert r.ok


def test_primary_contact_wins_for_automated_paths(db):
    cust = _customer(db)
    _contact(db, cust, name="Sue Ops", email="sue@acme.example", is_primary=True)
    r = resolve_recipient(db, cust)
    assert r.email == "sue@acme.example"
    assert r.greeting_name == "Sue"  # first name, not "Sue Ops"
    assert r.to_name == "Sue Ops"
    assert r.source == "primary_contact"


def test_explicit_contact_beats_primary(db):
    cust = _customer(db)
    _contact(db, cust, name="Sue Ops", email="sue@acme.example", is_primary=True)
    bob = _contact(db, cust, name="Bob Jones", email="bob@acme.example")
    r = resolve_recipient(db, cust, contact_id=str(bob.id))
    assert r.email == "bob@acme.example"
    assert r.greeting_name == "Bob"
    assert r.source == "contact"
    assert r.contact_id == str(bob.id)


def test_stale_or_foreign_contact_falls_back_to_account(db):
    cust = _customer(db)
    other = _customer(db, name="Other Co", email="o@other.example")
    foreign = _contact(db, other, name="Eve", email="eve@other.example")
    r = resolve_recipient(db, cust, contact_id=str(foreign.id))
    # A contact belonging to another customer must never be used.
    assert r.email == "front@acme.example"
    assert r.source == "account_email"


def test_contact_without_email_is_skipped(db):
    cust = _customer(db)
    _contact(db, cust, name="No Mail", email="", is_primary=True)
    r = resolve_recipient(db, cust)
    assert r.email == "front@acme.example"
    assert r.source == "account_email"


def test_customer_with_no_email_reports_not_ok(db):
    cust = _customer(db, email="")
    r = resolve_recipient(db, cust)
    assert not r.ok
    assert r.source == "none"


# --------------------------------------------------------------------------- #
# outbound_emails audit trail
# --------------------------------------------------------------------------- #

def _rows(db):
    return db.execute(select(OutboundEmail).order_by(OutboundEmail.created_at)).scalars().all()


def test_missing_recipient_still_writes_an_attempt_row(db):
    sent, provider, skip = te.send_transactional_email(
        tenant_db=db, tenant_id=TENANT, user_id=None,
        to_email="", to_name="", subject="s", html_body="<p>b</p>",
        entity_type="invoice", entity_id="inv-1",
    )
    assert sent is False and skip == "no_recipient_email"
    (row,) = _rows(db)
    assert row.status == "failed"
    assert row.skip_reason == "no_recipient_email"
    assert row.entity_type == "invoice" and row.entity_id == "inv-1"


def test_smtp_success_records_provider_body_and_initiator(db, monkeypatch):
    monkeypatch.setattr(
        te, "_try_smtp",
        lambda **kw: (True, None),
    )
    sent, provider, skip = te.send_transactional_email(
        tenant_db=db, tenant_id=TENANT, user_id="u-1",
        to_email="bob@acme.example", to_name="Bob Jones",
        subject="Invoice #9", html_body="<p>exact bytes</p>",
        attachments=[{"name": "invoice-9.pdf", "content_type": "application/pdf",
                      "content_base64": "QUJD"}],
        entity_type="invoice", entity_id="inv-9",
        recipient_source="contact", recipient_contact_id="c-1",
    )
    assert sent is True and provider == "smtp"
    (row,) = _rows(db)
    assert row.status == "sent"
    assert row.provider == "smtp"
    assert row.body_html == "<p>exact bytes</p>"
    assert row.subject == "Invoice #9"
    assert row.initiator_kind == "user" and row.initiator_ref == "u-1"
    assert row.recipient_source == "contact"
    assert row.recipient_contact_id == "c-1"
    (att,) = row.attachments_meta
    assert att["name"] == "invoice-9.pdf" and att["size_bytes"] == 3


def test_all_provider_failure_records_skip_reason(db, monkeypatch):
    monkeypatch.setattr(te, "_try_smtp", lambda **kw: (False, "smtp_not_configured"))
    sent, provider, skip = te.send_transactional_email(
        tenant_db=db, tenant_id=TENANT, user_id=None,
        to_email="x@example.com", to_name="", subject="s", html_body="<p>b</p>",
        initiator_kind="reminder_task", initiator_ref="beat",
    )
    assert sent is False and skip == "no_email_provider_connected"
    (row,) = _rows(db)
    assert row.status == "failed"
    assert row.skip_reason == "no_email_provider_connected"
    assert row.initiator_kind == "reminder_task"
    assert row.initiator_ref == "beat"


def test_audit_write_failure_never_blocks_the_send(monkeypatch):
    # The audit layer's own failures are swallowed inside _record_outbound —
    # a down audit path must never turn a delivered email into an error.
    monkeypatch.setattr(te, "_try_smtp", lambda **kw: (True, None))

    class ExplodingSession:
        def add(self, *_a, **_k):
            raise RuntimeError("audit db down")

        def flush(self):  # pragma: no cover
            raise RuntimeError("audit db down")

        def get_bind(self):
            raise RuntimeError("audit db down")

    sent, provider, skip = te.send_transactional_email(
        tenant_db=ExplodingSession(), tenant_id=TENANT, user_id=None,
        to_email="x@example.com", to_name="", subject="s", html_body="<p>b</p>",
    )
    assert sent is True and provider == "smtp"

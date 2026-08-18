"""Outbound email log endpoints + office contact default (email overhaul).

The log is the locked auditability requirement's UI face: filterable list,
detail with the exact rendered body. make-primary is how the office sets the
person automated sends greet on a business account.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gdx_dispatch.core.auth import get_current_user
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import Customer, CustomerContact, OutboundEmail

TENANT = "44444444-4444-4444-4444-444444444444"


@pytest.fixture()
def client(tenant_db):
    from gdx_dispatch.routers import customers as customers_router
    from gdx_dispatch.routers import outbound_emails as oe_router

    app = FastAPI()

    @app.middleware("http")
    async def inject_state(request, call_next):
        request.state.tenant = {"id": TENANT}
        request.state.current_user = {"user_id": "u1", "role": "admin"}
        return await call_next(request)

    app.include_router(oe_router.router)
    app.include_router(customers_router.router)
    def _db():
        yield tenant_db

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = lambda: {"user_id": "u1", "role": "admin"}
    tc = TestClient(app)
    tc._db = tenant_db
    yield tc
    app.dependency_overrides.clear()


def _row(db, **kw):
    defaults = dict(
        company_id=TENANT, initiator_kind="user", kind="document",
        entity_type="invoice", entity_id="inv-1",
        to_email="bob@acme.example", subject="Invoice #9",
        body_html="<p>exact bytes</p>", status="sent", provider="outlook_graph",
    )
    defaults.update(kw)
    row = OutboundEmail(**defaults)
    db.add(row)
    db.commit()
    return row


def test_list_filters_and_detail_body(client):
    db = client._db
    _row(db)
    _row(db, status="failed", skip_reason="no_email_provider_connected",
         provider=None, kind="reminder", initiator_kind="reminder_task",
         to_email="sue@acme.example", subject="Payment reminder")

    r = client.get("/api/outbound-emails")
    assert r.status_code == 200, r.text
    assert len(r.json()["items"]) == 2
    # Bodies stay out of the list payload.
    assert "body_html" not in r.json()["items"][0]

    r = client.get("/api/outbound-emails", params={"status": "failed"})
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["skip_reason"] == "no_email_provider_connected"
    assert items[0]["initiator_kind"] == "reminder_task"

    r = client.get("/api/outbound-emails", params={"kind": "document"})
    assert len(r.json()["items"]) == 1

    r = client.get("/api/outbound-emails", params={"entity_type": "invoice", "entity_id": "inv-1"})
    assert len(r.json()["items"]) == 2

    detail_id = r.json()["items"][0]["id"]
    d = client.get(f"/api/outbound-emails/{detail_id}")
    assert d.status_code == 200
    assert d.json()["body_html"]  # the dispute answer


def test_make_primary_is_single_writer(client):
    db = client._db
    cust = Customer(id=uuid4(), name="Acme Lumber Yard",
                    email="front@acme.example", company_id=TENANT)
    db.add(cust)
    db.flush()
    a = CustomerContact(company_id=TENANT, customer_id=cust.id,
                        name="Sue", email="sue@acme.example", is_primary=True)
    b = CustomerContact(company_id=TENANT, customer_id=cust.id,
                        name="Bob", email="bob@acme.example")
    no_mail = CustomerContact(company_id=TENANT, customer_id=cust.id,
                              name="Gate Guy", email="")
    db.add_all([a, b, no_mail])
    db.commit()

    r = client.post(f"/api/customers/{cust.id}/contacts/{b.id}/make-primary")
    assert r.status_code == 200, r.text
    db.refresh(a)
    db.refresh(b)
    assert b.is_primary is True
    assert a.is_primary is False  # at most one live primary

    # A contact without an email can't be the default recipient.
    r = client.post(f"/api/customers/{cust.id}/contacts/{no_mail.id}/make-primary")
    assert r.status_code == 422

    listed = client.get(f"/api/customers/{cust.id}/contacts").json()
    primaries = [c for c in listed if c["is_primary"]]
    assert len(primaries) == 1 and primaries[0]["name"] == "Bob"

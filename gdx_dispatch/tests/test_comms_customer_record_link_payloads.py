"""The server half of "a call, an email or an SMS thread names its customer,
and the name is the route to the record".

Sibling of test_customer_record_link_payloads.py, which pins the same contract
on the estimate and invoice detail payloads (#777). Three payloads live here:

* ``GET /api/phone-com/calls/{id}``   (get_call_detail)      — the call detail
  sheet on mobile and the call dialog on the desktop calls view.
* ``GET /api/phone-com/messages/threads`` (list_message_threads) — the row that
  becomes ``selectedThread``, which is what BOTH conversation headers render.
* the Outlook message serializer (``_link_labels`` / ``_to_out``) behind
  ``GET /api/outlook/messages`` and ``/messages/{id}`` — the link chip in the
  mobile and desktop inbox detail panes.

All three already emitted a customer id and a customer name. What none of them
emitted was whether that customer still exists, and **that** is the half the
link guard needs:

``GET /api/customers/{id}`` filters ``deleted_at`` and 404s on a soft-deleted
record, so a link there is a dead end. The lever is a FLAG, never a blanked
name — this is the inverted case from the estimate payload, and deliberately
so. These payloads are read by people triaging messages, where "who was this
from" matters most precisely when the customer record has since been deleted.
Blanking the name would lose that AND leave any consumer that guards on
``customer_id`` alone still rendering the dead link.

So the property pinned per payload is two-sided:
    a soft-deleted customer is STILL NAMED, and is flagged.

The client half is pinned in
frontend/src/views/__tests__/CommsCustomerRecordLinks.spec.js.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.auth import get_current_user
from gdx_dispatch.core.database import get_db, get_tenant_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.core.tenant_settings import Base as ControlBase
from gdx_dispatch.core.tenant_settings import Tenant
from gdx_dispatch.models.tenant_models import Customer
from gdx_dispatch.modules.outlook.models import OutlookAccount, OutlookMessage
from gdx_dispatch.modules.outlook.views_router import _to_detail, _to_out_all
from gdx_dispatch.modules.phone_com.models import PhoneComCall, PhoneComMessage
from gdx_dispatch.modules.phone_com.router import router as phone_com_router


@pytest.fixture(autouse=True)
def fernet_env(monkeypatch):
    monkeypatch.setenv("GDX_FERNET_KEY", Fernet.generate_key().decode())


@pytest.fixture(autouse=True)
def _no_audit(monkeypatch):
    monkeypatch.setattr(
        "gdx_dispatch.modules.phone_com.key_storage.log_audit_event_sync",
        lambda *a, **kw: None, raising=False,
    )


@pytest.fixture
def control_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    for name in ("tenants", "tenant_settings"):
        if name in ControlBase.metadata.tables:
            ControlBase.metadata.tables[name].create(engine, checkfirst=True)
    return engine


@pytest.fixture
def tenant_engine():
    # From the ORM, never hand-written DDL: create_all tables diverge from the
    # models, and a hand-built schema once hid a column the feature could not
    # be inserted without at all.
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine)
    return engine


@pytest.fixture
def tenant_id(control_engine):
    sm = sessionmaker(bind=control_engine, expire_on_commit=False)
    s = sm()
    tid = uuid4()
    s.add(Tenant(id=tid, slug="t1", name="T"))
    s.commit()
    s.close()
    return tid


@pytest.fixture
def tsm(tenant_engine):
    return sessionmaker(bind=tenant_engine, expire_on_commit=False)


@pytest.fixture
def client(control_engine, tenant_engine, tenant_id):
    app = FastAPI()
    app.include_router(phone_com_router)
    csm = sessionmaker(bind=control_engine, expire_on_commit=False)
    tsm_ = sessionmaker(bind=tenant_engine, expire_on_commit=False)

    def fake_control_db():
        s = csm()
        try:
            yield s
        finally:
            s.close()

    def fake_tenant_db():
        s = tsm_()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": str(uuid4()), "role": "admin", "tenant_id": str(tenant_id),
    }
    app.dependency_overrides[get_db] = fake_control_db
    app.dependency_overrides[get_tenant_db] = fake_tenant_db
    app.dependency_overrides[require_module("phone_com")] = lambda: None
    return TestClient(app)


def _customer(tsm, name: str = "Acme Doors", *, deleted: bool = False) -> UUID:
    s = tsm()
    c = Customer(name=name, company_id="t1")
    if deleted:
        c.deleted_at = datetime.now(UTC)
    s.add(c)
    s.commit()
    s.refresh(c)
    cid = c.id
    s.close()
    return cid


def _call(tsm, *, customer_id=None) -> UUID:
    s = tsm()
    call = PhoneComCall(
        phone_com_call_id=str(uuid4()),
        direction="in",
        from_number="+13205550101",
        to_number="+18005550199",
        started_at=datetime.now(UTC),
        duration_s=42,
        status="completed",
        customer_id=customer_id,
        raw_payload={},
    )
    s.add(call)
    s.commit()
    s.refresh(call)
    cid = call.id
    s.close()
    return cid


def _sms(tsm, *, customer_id=None, thread_key="t-1") -> None:
    s = tsm()
    s.add(PhoneComMessage(
        phone_com_message_id=str(uuid4()),
        thread_key=thread_key,
        direction="in",
        from_number="+13205550101",
        to_number="+18005550199",
        body="the door won't close",
        sent_at=datetime.now(UTC) - timedelta(minutes=5),
        customer_id=customer_id,
        raw_payload={},
    ))
    s.commit()
    s.close()


# ── GET /api/phone-com/calls/{id} ─────────────────────────────────────────


def test_call_detail_names_the_customer_and_reports_it_live(client, tsm):
    cust = _customer(tsm)
    call = _call(tsm, customer_id=cust)

    body = client.get(f"/api/phone-com/calls/{call}").json()

    assert body["customer_id"] == str(cust)
    assert body["customer_name"] == "Acme Doors"
    # The whole point: present AND false, not merely absent. A consumer that
    # reads a missing key as falsey would pass either way.
    assert body["customer_deleted"] is False


def test_call_detail_keeps_naming_a_soft_deleted_customer_and_flags_it(client, tsm):
    cust = _customer(tsm, "Gone Doors", deleted=True)
    call = _call(tsm, customer_id=cust)

    body = client.get(f"/api/phone-com/calls/{call}").json()

    # Named — this is the inverse of the estimate payload, on purpose. Whoever
    # is looking at this call still needs to know who rang.
    assert body["customer_name"] == "Gone Doors"
    assert body["customer_id"] == str(cust)
    # ...and flagged, which is what withholds the link.
    assert body["customer_deleted"] is True


def test_call_detail_with_no_customer_is_unnamed_and_unflagged(client, tsm):
    call = _call(tsm, customer_id=None)

    body = client.get(f"/api/phone-com/calls/{call}").json()

    assert body["customer_id"] is None
    assert body["customer_name"] is None
    assert body["customer_deleted"] is False


# ── GET /api/phone-com/messages/threads ───────────────────────────────────
# The thread ROW is the only place either conversation header can learn the
# customer from — neither view re-fetches a per-thread detail — so the flag has
# to travel on this payload, not on a message.


def test_thread_row_names_the_customer_and_reports_it_live(client, tsm):
    cust = _customer(tsm)
    _sms(tsm, customer_id=cust)

    items = client.get("/api/phone-com/messages/threads").json()["items"]

    assert len(items) == 1
    assert items[0]["customer_id"] == str(cust)
    assert items[0]["customer_name"] == "Acme Doors"
    assert items[0]["customer_deleted"] is False


def test_thread_row_keeps_naming_a_soft_deleted_customer_and_flags_it(client, tsm):
    cust = _customer(tsm, "Gone Doors", deleted=True)
    _sms(tsm, customer_id=cust)

    items = client.get("/api/phone-com/messages/threads").json()["items"]

    assert items[0]["customer_name"] == "Gone Doors"
    assert items[0]["customer_deleted"] is True


def test_thread_row_with_no_customer_falls_back_to_the_number(client, tsm):
    _sms(tsm, customer_id=None)

    items = client.get("/api/phone-com/messages/threads").json()["items"]

    assert items[0]["customer_id"] is None
    assert items[0]["customer_name"] is None
    assert items[0]["customer_deleted"] is False
    # The header renders this instead, unlinked.
    assert items[0]["other_party_number"] == "+13205550101"


# ── the Outlook message serializer ────────────────────────────────────────


def _message(tsm, *, customer_id=None) -> UUID:
    s = tsm()
    acct = OutlookAccount(user_id=str(uuid4()), upn="office@example.com")
    s.add(acct)
    s.commit()
    msg = OutlookMessage(
        account_id=acct.id,
        graph_message_id=str(uuid4()),
        subject="Broken spring",
        from_address="alice@example.com",
        to_addresses=["office@example.com"],
        direction="inbound",
        received_at=datetime.now(UTC),
        linked_customer_id=customer_id,
    )
    s.add(msg)
    s.commit()
    s.refresh(msg)
    mid = msg.id
    s.close()
    return mid


def test_outlook_list_names_the_linked_customer_and_reports_it_live(tsm):
    cust = _customer(tsm)
    _message(tsm, customer_id=cust)
    s = tsm()
    try:
        rows = s.query(OutlookMessage).all()
        out = _to_out_all(s, rows)
    finally:
        s.close()

    assert out[0].linked_customer_id == cust
    assert out[0].linked_customer_name == "Acme Doors"
    assert out[0].linked_customer_deleted is False


def test_outlook_list_keeps_naming_a_soft_deleted_customer_and_flags_it(tsm):
    cust = _customer(tsm, "Gone Doors", deleted=True)
    _message(tsm, customer_id=cust)
    s = tsm()
    try:
        rows = s.query(OutlookMessage).all()
        out = _to_out_all(s, rows)
    finally:
        s.close()

    # _link_labels does not filter deleted_at and must not start: the chip has
    # to keep saying who the mail is attributed to.
    assert out[0].linked_customer_name == "Gone Doors"
    assert out[0].linked_customer_deleted is True


def test_outlook_detail_carries_the_flag_too(tsm):
    """The detail pane is the surface that renders the LINK, so the flag has to
    survive _to_detail's **base spread, not only the list serializer."""
    cust = _customer(tsm, "Gone Doors", deleted=True)
    _message(tsm, customer_id=cust)
    s = tsm()
    try:
        msg = s.query(OutlookMessage).one()
        detail = _to_detail(msg, tenant_db=s)
    finally:
        s.close()

    assert detail.linked_customer_name == "Gone Doors"
    assert detail.linked_customer_deleted is True


def test_outlook_unlinked_message_is_unnamed_and_unflagged(tsm):
    _message(tsm, customer_id=None)
    s = tsm()
    try:
        rows = s.query(OutlookMessage).all()
        out = _to_out_all(s, rows)
    finally:
        s.close()

    assert out[0].linked_customer_id is None
    assert out[0].linked_customer_name is None
    assert out[0].linked_customer_deleted is False


def test_outlook_flag_is_false_when_the_label_lookup_degrades(tsm, monkeypatch):
    """_link_labels is best-effort: a lookup failure costs the chip its label
    rather than 500ing the inbox. When that happens the message must come back
    unnamed and UNFLAGGED — never flagged-and-unnamed, which would tell a
    consumer the customer is deleted on no evidence at all. The UI guards on
    the name, so this degrades to an inert chip.
    """
    cust = _customer(tsm)
    _message(tsm, customer_id=cust)

    import gdx_dispatch.models.tenant_models as tm

    class Boom:
        def __getattr__(self, name):
            raise RuntimeError("customer lookup exploded")

    monkeypatch.setattr(tm, "Customer", Boom())
    s = tsm()
    try:
        rows = s.query(OutlookMessage).all()
        out = _to_out_all(s, rows)
    finally:
        s.close()

    assert out[0].linked_customer_id == cust
    assert out[0].linked_customer_name is None
    assert out[0].linked_customer_deleted is False

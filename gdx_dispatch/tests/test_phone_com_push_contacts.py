"""P2.8 — customer→Phone.com contact push."""
from __future__ import annotations

from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.modules.phone_com.models import PhoneComContactPush
from gdx_dispatch.modules.phone_com.push_contacts import push_contacts_for_tenant


@pytest.fixture(autouse=True)
def fernet_env(monkeypatch):
    monkeypatch.setenv("GDX_FERNET_KEY", Fernet.generate_key().decode())


@pytest.fixture
def tenant_session():
    e = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(e)
    sm = sessionmaker(bind=e, expire_on_commit=False)
    return sm()


def _make_customer(sess, *, name: str, phone: str, company: str | None = None):
    from gdx_dispatch.models.tenant_models import Customer
    c = Customer(name=name, phone=phone, company_id=str(uuid4()))
    if company is not None:
        c.company = company
    sess.add(c)
    sess.commit()
    sess.refresh(c)
    return c


def test_push_creates_contact_for_new_customer(tenant_session):
    sess = tenant_session
    c = _make_customer(sess, name="Jane Doe", phone="+15555550001")
    fake_client = MagicMock()
    fake_client.create_contact.return_value = {"id": "pcc-001"}

    res = push_contacts_for_tenant(sess, fake_client, cap=10)
    assert res["pushed"] == 1
    assert res["failed"] == 0
    fake_client.create_contact.assert_called_once()
    kwargs = fake_client.create_contact.call_args.kwargs
    assert kwargs["first_name"] == "Jane"
    assert kwargs["last_name"] == "Doe"
    assert kwargs["external_id"] == str(c.id)
    assert kwargs["phone_numbers"] == [{"number": "+15555550001", "type": "mobile"}]

    row = sess.query(PhoneComContactPush).first()
    assert row is not None
    assert row.phone_com_contact_id == "pcc-001"
    assert row.customer_id == c.id


def test_push_skips_already_pushed_customer_with_same_name(tenant_session):
    sess = tenant_session
    c = _make_customer(sess, name="Jane Doe", phone="+15555550001")
    sess.add(PhoneComContactPush(
        customer_id=c.id, phone_e164="+15555550001",
        phone_com_contact_id="pcc-001", name_pushed="Jane Doe",
    ))
    sess.commit()
    fake_client = MagicMock()

    res = push_contacts_for_tenant(sess, fake_client, cap=10)
    assert res["pushed"] == 0
    assert res["skipped"] == 1
    fake_client.create_contact.assert_not_called()


def test_push_records_failure_without_raising(tenant_session):
    from gdx_dispatch.modules.phone_com.client import PhoneComAPIError
    sess = tenant_session
    c = _make_customer(sess, name="Bob", phone="+15555550002")
    fake_client = MagicMock()
    fake_client.create_contact.side_effect = PhoneComAPIError(
        "rate limited", status_code=429,
    )

    res = push_contacts_for_tenant(sess, fake_client, cap=10)
    assert res["failed"] == 1
    assert res["pushed"] == 0
    row = sess.query(PhoneComContactPush).filter_by(customer_id=c.id).first()
    assert row is not None
    assert "rate limited" in (row.last_error or "")
    assert row.phone_com_contact_id is None


def test_push_skips_customer_without_e164_phone(tenant_session):
    sess = tenant_session
    _make_customer(sess, name="Garbage", phone="not-a-number")
    fake_client = MagicMock()
    res = push_contacts_for_tenant(sess, fake_client, cap=10)
    assert res["skipped"] == 1
    fake_client.create_contact.assert_not_called()


def test_push_caps_at_n_per_run(tenant_session):
    sess = tenant_session
    for i in range(5):
        _make_customer(sess, name=f"Cust{i}", phone=f"+155555500{i:02d}")
    fake_client = MagicMock()
    fake_client.create_contact.side_effect = lambda **kw: {"id": f"pcc-{kw['external_id']}"}

    res = push_contacts_for_tenant(sess, fake_client, cap=2)
    assert res["pushed"] == 2
    assert sess.query(PhoneComContactPush).count() == 2


def test_create_contact_client_payload_shape():
    """Sanity-check the v4 contact body shape we send."""
    import httpx
    import respx
    from gdx_dispatch.modules.phone_com.client import BASE_URL, PhoneComClient

    with respx.mock:
        route = respx.post(f"{BASE_URL}/accounts/1000000/contacts").mock(
            return_value=httpx.Response(200, json={"id": "pcc-1"})
        )
        c = PhoneComClient(token="t", voip_id=1000000)
        out = c.create_contact(
            first_name="Jane", last_name="Doe",
            phone_numbers=[{"number": "+1", "type": "mobile"}],
            external_id="abc",
        )
        import json
        body = json.loads(route.calls.last.request.read())
        assert body == {
            "first_name": "Jane",
            "last_name": "Doe",
            "phone_numbers": [{"number": "+1", "type": "mobile"}],
            "external_id": "abc",
        }
        assert out["id"] == "pcc-1"

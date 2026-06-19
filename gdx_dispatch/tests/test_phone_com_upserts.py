"""Wave F / S11 — normalize_status + upsert_call PII split tests."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.modules.phone_com.models import PhoneComCall
from gdx_dispatch.modules.phone_com.upserts import normalize_status, upsert_call


@pytest.fixture
def tenant_db():
    e = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(e)
    sm = sessionmaker(bind=e, expire_on_commit=False)
    return sm()


@pytest.mark.parametrize("raw,expected_status,expected_target", [
    # Voicemail family
    ("type voicemail_received", "voicemail", None),
    ("voicemail_received", "voicemail", None),
    ("voicemail received", "voicemail", None),
    ("voicemail", "voicemail", None),
    # Forwarded with dialed number — the PII case
    ("dial_out +13202325143", "forwarded", "+13202325143"),
    ("dial_out 13202325143", "forwarded", "13202325143"),
    ("DIAL_OUT +18005550199", "forwarded", "+18005550199"),
    ("forwarded to extension", "forwarded", None),
    ("forwarded", "forwarded", None),
    # Answered / completed
    ("answered", "answered", None),
    ("completed", "answered", None),
    # Missed / busy / no_answer
    ("missed", "missed", None),
    ("busy", "missed", None),
    ("no_answer", "missed", None),
    # Canceled / hung
    ("canceled", "canceled", None),
    ("cancelled", "canceled", None),
    ("hung_up", "canceled", None),
    # Empty / None
    ("", (None,), (None,)),
    (None, (None,), (None,)),
    ("   ", (None,), (None,)),
])
def test_normalize_status(raw, expected_status, expected_target):
    s, t = normalize_status(raw)
    if isinstance(expected_status, tuple):
        assert s is None
        assert t is None
    else:
        assert s == expected_status
        assert t == expected_target


def test_unknown_status_preserved_no_target():
    """Unknown shape stays in status (cleaned), target left None — we don't
    guess at PII splits we can't confidently parse."""
    s, t = normalize_status("some_weird_carrier_code")
    assert s == "some weird carrier code"
    assert t is None


def test_upsert_call_writes_split_fields(tenant_db):
    upsert_call(tenant_db, {
        "id": "phc-call-1",
        "direction": "in",
        "caller_id": "+15551234567",
        "called_number": "+18005550199",
        "status": "dial_out +13202325143",
    })
    row = tenant_db.query(PhoneComCall).first()
    assert row.status == "forwarded"
    assert row.final_action_target == "+13202325143"


def test_upsert_call_voicemail_no_target(tenant_db):
    upsert_call(tenant_db, {
        "id": "phc-call-2",
        "direction": "in",
        "caller_id": "+15551234567",
        "called_number": "+18005550199",
        "status": "type voicemail_received",
    })
    row = tenant_db.query(PhoneComCall).first()
    assert row.status == "voicemail"
    assert row.final_action_target is None


def test_upsert_call_falls_back_to_final_action(tenant_db):
    """When `status` not in payload but `final_action` is, normalize that."""
    upsert_call(tenant_db, {
        "id": "phc-call-3",
        "direction": "in",
        "caller_id": "+15551234567",
        "called_number": "+18005550199",
        "final_action": "dial_out +12182525555",
    })
    row = tenant_db.query(PhoneComCall).first()
    assert row.status == "forwarded"
    assert row.final_action_target == "+12182525555"


def test_upsert_call_idempotent_status_does_not_drift(tenant_db):
    """Re-running upsert with same payload must not flip a clean status to
    something else."""
    payload = {
        "id": "phc-call-4",
        "direction": "in",
        "caller_id": "+15551234567",
        "called_number": "+18005550199",
        "status": "dial_out +13202325143",
    }
    upsert_call(tenant_db, payload)
    upsert_call(tenant_db, payload)
    row = tenant_db.query(PhoneComCall).filter(
        PhoneComCall.phone_com_call_id == "phc-call-4",
    ).one()
    assert row.status == "forwarded"
    assert row.final_action_target == "+13202325143"

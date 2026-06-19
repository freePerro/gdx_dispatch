"""pc-s15 (early) — Phone.com full-resync backfill tests.

Covers run_full_resync: walks Phone.com pagination, calls upserts.upsert_*,
returns count summary. Idempotent on re-run.
"""
from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
import respx
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.control.models import Base as ControlBase
from gdx_dispatch.control.models import Tenant
from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models.tenant_models import AppSettings
from gdx_dispatch.modules.phone_com import key_storage
from gdx_dispatch.modules.phone_com.client import BASE_URL
from gdx_dispatch.modules.phone_com.models import (
    PhoneComCall,
    PhoneComExtension,
    PhoneComMessage,
    PhoneComNumber,
    PhoneComVoicemail,
)
from gdx_dispatch.modules.phone_com.sync import run_full_resync


VID = 1000000


def _envelope(items, total=None):
    return {
        "filters": {}, "sort": {"id": "desc"},
        "total": total if total is not None else len(items),
        "limit": 50, "offset": None, "items": items,
    }


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
    e = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    for n in ("tenants", "tenant_settings"):
        if n in ControlBase.metadata.tables:
            ControlBase.metadata.tables[n].create(e, checkfirst=True)
    return e


@pytest.fixture
def tenant_engine():
    e = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(e)
    return e


@pytest.fixture
def setup(control_engine, tenant_engine, monkeypatch):
    csm = sessionmaker(bind=control_engine, expire_on_commit=False)
    cs = csm()
    tid = uuid4()
    cs.add(Tenant(id=tid, slug="t1", name="T"))
    cs.commit()
    key_storage.set_token(cs, tid, "phc-good-token-12345")
    cs.close()

    tsm = sessionmaker(bind=tenant_engine, expire_on_commit=False)
    ts = tsm()
    ts.add(AppSettings(phone_com_voip_id=str(VID)))
    ts.commit()
    ts.close()

    # Patch SessionLocal so _open_tenant_session returns a session on the
    # tenant_engine (single-tenant: both "control" and "tenant" are the same DB).
    monkeypatch.setattr("gdx_dispatch.modules.phone_com.sync.SessionLocal", tsm)

    return csm, tsm, tid


@respx.mock
def test_resync_pulls_calls_and_messages(setup):
    csm, tsm, tid = setup
    # Calls page (single page, total=2)
    call_items = [
        {
            "id": "phc-call-A", "direction": "in",
            "caller_id": "+13202959628", "called_number": "+18005550199",
            "duration": 30, "status": "completed",
        },
        {
            "id": "phc-call-B", "direction": "out",
            "caller_id": "+18005550199", "called_number": "+15551112222",
            "duration": 12, "status": "completed",
        },
    ]
    msg_items = [
        {
            "id": "phc-msg-1", "direction": "in",
            "from": "+13202959628", "to": "+18005550199", "text": "hi",
        },
        {
            "id": "phc-msg-2", "direction": "out",
            "from": "+18005550199", "to": "+13202959628", "text": "thanks",
        },
        {
            "id": "phc-msg-3", "direction": "in",
            "from": "+15551112222", "to": "+18005550199", "text": "yo",
        },
    ]
    respx.get(f"{BASE_URL}/accounts/{VID}/call-logs").mock(
        return_value=httpx.Response(200, json=_envelope(call_items)),
    )
    respx.get(f"{BASE_URL}/accounts/{VID}/messages").mock(
        return_value=httpx.Response(200, json=_envelope(msg_items)),
    )
    respx.get(f"{BASE_URL}/accounts/{VID}/extensions").mock(
        return_value=httpx.Response(200, json=_envelope([])),
    )
    respx.get(f"{BASE_URL}/accounts/{VID}/phone-numbers").mock(
        return_value=httpx.Response(200, json=_envelope([])),
    )
    respx.get(f"{BASE_URL}/accounts/{VID}/fax").mock(
        return_value=httpx.Response(200, json=_envelope([])),
    )

    cs = csm()
    try:
        result = run_full_resync(cs, tid)
    finally:
        cs.close()

    assert result["ok"] is True
    assert result["calls_synced"] == 2
    assert result["messages_synced"] == 3

    ts = tsm()
    try:
        assert ts.query(PhoneComCall).count() == 2
        assert ts.query(PhoneComMessage).count() == 3
    finally:
        ts.close()


@respx.mock
def test_resync_idempotent_on_rerun(setup):
    csm, tsm, tid = setup
    call_items = [{"id": "phc-call-X", "direction": "in", "caller_id": "+1"}]
    respx.get(f"{BASE_URL}/accounts/{VID}/call-logs").mock(
        return_value=httpx.Response(200, json=_envelope(call_items)),
    )
    respx.get(f"{BASE_URL}/accounts/{VID}/messages").mock(
        return_value=httpx.Response(200, json=_envelope([])),
    )
    respx.get(f"{BASE_URL}/accounts/{VID}/extensions").mock(
        return_value=httpx.Response(200, json=_envelope([])),
    )
    respx.get(f"{BASE_URL}/accounts/{VID}/phone-numbers").mock(
        return_value=httpx.Response(200, json=_envelope([])),
    )
    respx.get(f"{BASE_URL}/accounts/{VID}/fax").mock(
        return_value=httpx.Response(200, json=_envelope([])),
    )

    cs = csm()
    try:
        first = run_full_resync(cs, tid)
        second = run_full_resync(cs, tid)
    finally:
        cs.close()

    assert first["calls_synced"] == 1
    assert second["calls_synced"] == 1  # walked again, but upsert is idempotent
    ts = tsm()
    try:
        assert ts.query(PhoneComCall).count() == 1  # no duplicates
    finally:
        ts.close()


@respx.mock
def test_resync_synthesizes_voicemail_from_inline_call_payload(setup):
    csm, tsm, tid = setup
    call_items = [{
        "id": "phc-call-VM", "direction": "in",
        "caller_id": "+13202959628",
        "voicemail_url": "https://api.phone.com/.../vm.wav",
        "voicemail_transcript": "leave a message after the beep",
        "voicemail_duration": 18,
    }]
    respx.get(f"{BASE_URL}/accounts/{VID}/call-logs").mock(
        return_value=httpx.Response(200, json=_envelope(call_items)),
    )
    respx.get(f"{BASE_URL}/accounts/{VID}/messages").mock(
        return_value=httpx.Response(200, json=_envelope([])),
    )
    respx.get(f"{BASE_URL}/accounts/{VID}/extensions").mock(
        return_value=httpx.Response(200, json=_envelope([])),
    )
    respx.get(f"{BASE_URL}/accounts/{VID}/phone-numbers").mock(
        return_value=httpx.Response(200, json=_envelope([])),
    )
    respx.get(f"{BASE_URL}/accounts/{VID}/fax").mock(
        return_value=httpx.Response(200, json=_envelope([])),
    )

    cs = csm()
    try:
        result = run_full_resync(cs, tid)
    finally:
        cs.close()

    assert result["voicemails_synced"] == 1
    ts = tsm()
    try:
        vm = ts.query(PhoneComVoicemail).first()
        assert vm is not None
        assert vm.transcript == "leave a message after the beep"
        # voicemail row links to the call
        assert vm.call_id is not None
    finally:
        ts.close()


@respx.mock
def test_resync_no_token_returns_error(control_engine, tenant_engine, monkeypatch):
    csm = sessionmaker(bind=control_engine, expire_on_commit=False)
    cs = csm()
    tid = uuid4()
    cs.add(Tenant(id=tid, slug="t1", name="T"))
    cs.commit()
    cs.close()
    # No key_storage.set_token call

    tsm = sessionmaker(bind=tenant_engine, expire_on_commit=False)
    monkeypatch.setattr("gdx_dispatch.modules.phone_com.sync.SessionLocal", tsm)

    cs = csm()
    try:
        result = run_full_resync(cs, tid)
    finally:
        cs.close()

    assert result["ok"] is False
    assert "not configured" in result["error"]


@respx.mock
def test_resync_stamps_last_synced_at(setup):
    """Wave B / S17: successful resync writes app_settings.phone_com_last_synced_at."""
    csm, tsm, tid = setup
    respx.get(f"{BASE_URL}/accounts/{VID}/call-logs").mock(
        return_value=httpx.Response(200, json=_envelope([])),
    )
    respx.get(f"{BASE_URL}/accounts/{VID}/messages").mock(
        return_value=httpx.Response(200, json=_envelope([])),
    )
    respx.get(f"{BASE_URL}/accounts/{VID}/extensions").mock(
        return_value=httpx.Response(200, json=_envelope([])),
    )
    respx.get(f"{BASE_URL}/accounts/{VID}/phone-numbers").mock(
        return_value=httpx.Response(200, json=_envelope([])),
    )
    respx.get(f"{BASE_URL}/accounts/{VID}/fax").mock(
        return_value=httpx.Response(200, json=_envelope([])),
    )

    ts = tsm()
    try:
        before = ts.query(AppSettings).first().phone_com_last_synced_at
    finally:
        ts.close()
    assert before is None

    cs = csm()
    try:
        result = run_full_resync(cs, tid)
    finally:
        cs.close()

    assert result["ok"] is True

    ts = tsm()
    try:
        after = ts.query(AppSettings).first().phone_com_last_synced_at
    finally:
        ts.close()
    assert after is not None


@respx.mock
def test_resync_pulls_extensions_and_numbers(setup):
    """Wave C / S3 + S4: resync upserts phone_com_extensions and
    phone_com_numbers from the catalog endpoints."""
    csm, tsm, tid = setup
    respx.get(f"{BASE_URL}/accounts/{VID}/call-logs").mock(
        return_value=httpx.Response(200, json=_envelope([])),
    )
    respx.get(f"{BASE_URL}/accounts/{VID}/messages").mock(
        return_value=httpx.Response(200, json=_envelope([])),
    )
    respx.get(f"{BASE_URL}/accounts/{VID}/extensions").mock(
        return_value=httpx.Response(200, json=_envelope([
            {"id": "100", "name": "Main", "extension": "100", "is_active": True},
            {"id": "101", "name": "Tech", "extension": "101", "is_active": True},
        ])),
    )
    respx.get(f"{BASE_URL}/accounts/{VID}/phone-numbers").mock(
        return_value=httpx.Response(200, json=_envelope([
            {"phone_number": "+18005550199", "name": "Main", "is_default_outbound": True},
            {"phone_number": "+13201234567", "name": None, "is_default_outbound": False},
        ])),
    )
    respx.get(f"{BASE_URL}/accounts/{VID}/fax").mock(
        return_value=httpx.Response(200, json=_envelope([])),
    )

    cs = csm()
    try:
        result = run_full_resync(cs, tid)
    finally:
        cs.close()

    assert result["ok"] is True

    ts = tsm()
    try:
        assert ts.query(PhoneComExtension).count() == 2
        assert ts.query(PhoneComNumber).count() == 2
        n = ts.query(PhoneComNumber).filter(
            PhoneComNumber.phone_com_number == "+18005550199",
        ).first()
        assert n is not None
        assert n.label == "Main"
        assert n.is_default_outbound is True
    finally:
        ts.close()


@respx.mock
def test_resync_marks_token_validated(setup):
    """Wave E / S10: a successful sync stamps phone_com_token_last_validated_at
    on tenant_settings — token validity is implied by sync success."""
    csm, tsm, tid = setup
    respx.get(f"{BASE_URL}/accounts/{VID}/call-logs").mock(
        return_value=httpx.Response(200, json=_envelope([])),
    )
    respx.get(f"{BASE_URL}/accounts/{VID}/messages").mock(
        return_value=httpx.Response(200, json=_envelope([])),
    )
    respx.get(f"{BASE_URL}/accounts/{VID}/extensions").mock(
        return_value=httpx.Response(200, json=_envelope([])),
    )
    respx.get(f"{BASE_URL}/accounts/{VID}/phone-numbers").mock(
        return_value=httpx.Response(200, json=_envelope([])),
    )
    respx.get(f"{BASE_URL}/accounts/{VID}/fax").mock(
        return_value=httpx.Response(200, json=_envelope([])),
    )

    cs = csm()
    try:
        # Sanity: pre-state is null
        from gdx_dispatch.control.models import TenantSettings as _TS
        ts_pre = cs.get(_TS, tid)
        assert ts_pre.phone_com_token_last_validated_at is None
        result = run_full_resync(cs, tid)
        assert result["ok"] is True
        cs.refresh(ts_pre)
        assert ts_pre.phone_com_token_last_validated_at is not None
    finally:
        cs.close()


@respx.mock
def test_resync_skips_when_catalog_endpoints_500(setup):
    """Wave C: extension/number endpoints failing must NOT poison the call+msg
    upsert. The whole resync still reports ok=True."""
    csm, tsm, tid = setup
    respx.get(f"{BASE_URL}/accounts/{VID}/call-logs").mock(
        return_value=httpx.Response(200, json=_envelope([])),
    )
    respx.get(f"{BASE_URL}/accounts/{VID}/messages").mock(
        return_value=httpx.Response(200, json=_envelope([])),
    )
    respx.get(f"{BASE_URL}/accounts/{VID}/extensions").mock(
        return_value=httpx.Response(500, json={"error": "boom"}),
    )
    respx.get(f"{BASE_URL}/accounts/{VID}/phone-numbers").mock(
        return_value=httpx.Response(500, json={"error": "boom"}),
    )
    respx.get(f"{BASE_URL}/accounts/{VID}/fax").mock(
        return_value=httpx.Response(500, json={"error": "boom"}),
    )

    cs = csm()
    try:
        result = run_full_resync(cs, tid)
    finally:
        cs.close()

    assert result["ok"] is True  # call/msg success is the load-bearing path

    ts = tsm()
    try:
        assert ts.query(PhoneComExtension).count() == 0
        assert ts.query(PhoneComNumber).count() == 0
    finally:
        ts.close()

"""pc-s11 — phone_com stats + catalog router tests."""
from __future__ import annotations

from datetime import date, timedelta
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.control.models import Base as ControlBase
from gdx_dispatch.control.models import Tenant
from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.auth import get_current_user
from gdx_dispatch.core.database import get_db, get_tenant_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.modules.phone_com.models import (
    PhoneComExtension,
    PhoneComNumber,
    PhoneComStatsDaily,
)
from gdx_dispatch.modules.phone_com.router import router as ops_router


@pytest.fixture(autouse=True)
def fernet_env(monkeypatch):
    monkeypatch.setenv("GDX_FERNET_KEY", Fernet.generate_key().decode())


def _engines():
    ce = create_engine("sqlite:///:memory:",
                       connect_args={"check_same_thread": False},
                       poolclass=StaticPool)
    for n in ("tenants", "tenant_settings"):
        if n in ControlBase.metadata.tables:
            ControlBase.metadata.tables[n].create(ce, checkfirst=True)
    te = create_engine("sqlite:///:memory:",
                       connect_args={"check_same_thread": False},
                       poolclass=StaticPool)
    TenantBase.metadata.create_all(te)
    return ce, te


def _app(ce, te, tid):
    app = FastAPI()
    app.include_router(ops_router)
    csm = sessionmaker(bind=ce, expire_on_commit=False)
    tsm = sessionmaker(bind=te, expire_on_commit=False)

    def fake_user():
        return {"user_id": str(uuid4()), "role": "admin", "tenant_id": str(tid)}

    def fake_control_db():
        s = csm()
        try:
            yield s
        finally:
            s.close()

    def fake_tenant_db():
        s = tsm()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_current_user] = fake_user
    app.dependency_overrides[get_db] = fake_control_db
    app.dependency_overrides[get_tenant_db] = fake_tenant_db
    app.dependency_overrides[require_module("phone_com")] = lambda: None
    return app, csm, tsm


def _seed_tenant(csm):
    s = csm()
    tid = uuid4()
    s.add(Tenant(id=tid, slug="t1", name="T"))
    s.commit()
    s.close()
    return tid


def test_stats_summary_empty():
    ce, te = _engines()
    csm = sessionmaker(bind=ce, expire_on_commit=False)
    tid = _seed_tenant(csm)
    app, _, _ = _app(ce, te, tid)
    r = TestClient(app).get("/api/phone-com/stats/summary?days=7")
    assert r.status_code == 200
    body = r.json()
    assert body["calls_in"] == 0
    assert body["by_day"] == []


def test_stats_summary_aggregates():
    ce, te = _engines()
    csm = sessionmaker(bind=ce, expire_on_commit=False)
    tid = _seed_tenant(csm)
    tsm = sessionmaker(bind=te, expire_on_commit=False)
    s = tsm()
    today = date.today()
    for i in range(3):
        s.add(PhoneComStatsDaily(
            stat_date=today - timedelta(days=i),
            calls_in=10, calls_out=5, calls_missed=2,
            sms_in=8, sms_out=4, voicemails_new=1,
            total_call_minutes=30, raw_payload={},
        ))
    s.commit()
    s.close()
    app, _, _ = _app(ce, te, tid)
    r = TestClient(app).get("/api/phone-com/stats/summary?days=7")
    body = r.json()
    assert body["calls_in"] == 30
    assert body["calls_out"] == 15
    assert body["sms_in"] == 24
    assert len(body["by_day"]) == 3
    # Newest first
    assert body["by_day"][0]["date"] == today.isoformat()


def test_list_extensions():
    ce, te = _engines()
    csm = sessionmaker(bind=ce, expire_on_commit=False)
    tid = _seed_tenant(csm)
    tsm = sessionmaker(bind=te, expire_on_commit=False)
    s = tsm()
    s.add(PhoneComExtension(
        phone_com_extension_id="ext-100", name="Example Owner", number="100", is_active=True,
    ))
    s.add(PhoneComExtension(
        phone_com_extension_id="ext-500", name="Example Garage Doors", number="500", is_active=True,
    ))
    s.commit()
    s.close()
    app, _, _ = _app(ce, te, tid)
    r = TestClient(app).get("/api/phone-com/extensions")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    nums = [e["number"] for e in body["items"]]
    assert nums == ["100", "500"]


def test_list_numbers_default_outbound_first():
    ce, te = _engines()
    csm = sessionmaker(bind=ce, expire_on_commit=False)
    tid = _seed_tenant(csm)
    tsm = sessionmaker(bind=te, expire_on_commit=False)
    s = tsm()
    s.add(PhoneComNumber(phone_com_number="+13205550100", label="local",
                          is_default_outbound=False))
    s.add(PhoneComNumber(phone_com_number="+18005550199", label="GDX",
                          is_default_outbound=True))
    s.commit()
    s.close()
    app, _, _ = _app(ce, te, tid)
    r = TestClient(app).get("/api/phone-com/numbers")
    body = r.json()
    assert body["total"] == 2
    assert body["items"][0]["phone_com_number"] == "+18005550199"
    assert body["items"][0]["is_default_outbound"] is True

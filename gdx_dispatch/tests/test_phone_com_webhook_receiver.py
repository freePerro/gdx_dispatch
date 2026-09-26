"""pc-s12 — phone_com webhook receiver tests."""
from __future__ import annotations

import ast
import logging
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.tenant_settings import Base as ControlBase
from gdx_dispatch.core.tenant_settings import Tenant
from gdx_dispatch.models.tenant_models import AppSettings, Customer
from gdx_dispatch.modules.phone_com import key_storage
from gdx_dispatch.modules.phone_com import webhook_router as wr
from gdx_dispatch.modules.phone_com.models import (
    PhoneComCall,
    PhoneComMessage,
    PhoneComVoicemail,
)


@pytest.fixture(autouse=True)
def fernet_env(monkeypatch):
    monkeypatch.setenv("GDX_FERNET_KEY", Fernet.generate_key().decode())


@pytest.fixture(autouse=True)
def _no_audit(monkeypatch):
    monkeypatch.setattr(
        "gdx_dispatch.modules.phone_com.key_storage.log_audit_event_sync",
        lambda *a, **kw: None, raising=False,
    )
    monkeypatch.setattr(
        "gdx_dispatch.modules.phone_com.webhook_router.log_audit_event_sync",
        lambda *a, **kw: None, raising=False,
    )


@pytest.fixture
def unified_engine():
    # Phase C: single DB for control + tenant tables.
    e = create_engine("sqlite:///:memory:",
                      connect_args={"check_same_thread": False},
                      poolclass=StaticPool)
    ControlBase.metadata.create_all(e, checkfirst=True)
    TenantBase.metadata.create_all(e, checkfirst=True)
    from gdx_dispatch.models.tenant_models import Base as TenantModelsBase
    TenantModelsBase.metadata.create_all(e, checkfirst=True)
    return e


@pytest.fixture
def setup(unified_engine, monkeypatch):
    """Seed a tenant + voip_id + webhook secret into the unified DB."""
    sm = sessionmaker(bind=unified_engine, expire_on_commit=False)
    s = sm()
    tid = uuid4()
    s.add(Tenant(id=tid, slug="t1", name="T"))
    s.commit()
    secret = key_storage.get_or_create_webhook_secret(s, tid)
    s.add(AppSettings(phone_com_voip_id="1000000"))
    s.commit()
    s.close()

    monkeypatch.setattr(wr, "SessionLocal", sm)
    monkeypatch.setattr(wr, "_open_tenant_session", lambda *_: sm())
    # Phase C: webhook_router calls single_tenant() for tenant resolution.
    # Pin it to the test tenant so TenantSettings lookups find the right row.
    monkeypatch.setattr("gdx_dispatch.core.tenant.single_tenant",
                        lambda: {"id": str(tid), "slug": "t1", "db_url": ""})

    app = FastAPI()
    app.include_router(wr.router)
    return app, sm, sm, tid, secret


# ── short-circuit + auth ──────────────────────────────────────────────


def test_test_ping_short_circuits_204(setup):
    app, _, _, _, _ = setup
    # Bad slug + bad secret — must still 204 because body is the test ping.
    r = TestClient(app).post(
        "/api/webhooks/phone-com/no-such-tenant/garbage",
        json={"test": 1},
    )
    assert r.status_code == 204


def test_unknown_tenant_slug_returns_404(setup):
    app, _, _, _, _ = setup
    r = TestClient(app).post(
        "/api/webhooks/phone-com/no-such-tenant/anything",
        json={"voip_id": 1},
    )
    assert r.status_code == 404


def test_bad_path_secret_returns_404(setup):
    app, _, _, tid, _ = setup
    r = TestClient(app).post(
        "/api/webhooks/phone-com/t1/wrong-secret",
        json={"voip_id": 1000000, "type": "call.completed", "id": "c1"},
    )
    assert r.status_code == 404


def test_voip_id_mismatch_returns_400(setup):
    app, _, _, _, secret = setup
    r = TestClient(app).post(
        f"/api/webhooks/phone-com/t1/{secret}",
        json={"voip_id": 99999, "type": "call.completed", "id": "c1"},
    )
    assert r.status_code == 400


# ── call upsert ────────────────────────────────────────────────────────


def test_call_completed_upserts_row(setup):
    app, _, tsm, _, secret = setup
    payload = {
        "voip_id": 1000000, "type": "call.completed",
        "id": "phc-call-001", "direction": "in",
        "caller_id": "+13202959628", "called_number": "+18005550199",
        "duration": 30, "status": "completed",
    }
    r = TestClient(app).post(
        f"/api/webhooks/phone-com/t1/{secret}", json=payload,
    )
    assert r.status_code == 204
    s = tsm()
    rows = s.query(PhoneComCall).all()
    assert len(rows) == 1
    assert rows[0].phone_com_call_id == "phc-call-001"
    assert rows[0].direction == "in"
    assert rows[0].duration_s == 30
    s.close()


def test_call_completed_resolves_customer(setup):
    """Inbound call from a known phone hashes to a Customer row → customer_id populated."""
    app, _, tsm, _, secret = setup
    s = tsm()
    cust = Customer(name="Becky", phone="+13202959628", company_id="t1")
    s.add(cust)
    s.commit()
    cust_id = cust.id
    s.close()

    payload = {
        "voip_id": 1000000, "type": "call.completed",
        "id": "phc-call-002", "direction": "in",
        "caller_id": "+13202959628", "called_number": "+18005550199",
    }
    r = TestClient(app).post(
        f"/api/webhooks/phone-com/t1/{secret}", json=payload,
    )
    assert r.status_code == 204
    s = tsm()
    row = s.query(PhoneComCall).filter_by(phone_com_call_id="phc-call-002").first()
    assert row.customer_id == cust_id
    s.close()


def test_call_upsert_idempotent(setup):
    app, _, tsm, _, secret = setup
    payload = {
        "voip_id": 1000000, "type": "call.completed",
        "id": "phc-dup", "direction": "in",
        "caller_id": "+1", "duration": 10,
    }
    for _ in range(3):
        r = TestClient(app).post(
            f"/api/webhooks/phone-com/t1/{secret}", json=payload,
        )
        assert r.status_code == 204
    s = tsm()
    assert s.query(PhoneComCall).count() == 1
    s.close()


# ── message upsert ─────────────────────────────────────────────────────


def test_sms_received_upserts_message(setup):
    app, _, tsm, _, secret = setup
    payload = {
        "voip_id": 1000000, "type": "sms.received",
        "id": "phc-msg-001", "direction": "in",
        "from": "+13202959628", "to": "+18005550199", "text": "hi",
    }
    r = TestClient(app).post(
        f"/api/webhooks/phone-com/t1/{secret}", json=payload,
    )
    assert r.status_code == 204
    s = tsm()
    row = s.query(PhoneComMessage).filter_by(phone_com_message_id="phc-msg-001").first()
    assert row is not None
    assert row.body == "hi"
    assert row.direction == "in"
    assert row.thread_key == "+13202959628|+18005550199"
    s.close()


# ── voicemail upsert ───────────────────────────────────────────────────


def test_voicemail_created_upserts_and_links_to_call(setup):
    app, _, tsm, _, secret = setup
    s = tsm()
    call = PhoneComCall(
        phone_com_call_id="phc-call-vm", direction="in",
        from_number="+1", raw_payload={},
    )
    s.add(call)
    s.commit()
    call_id = call.id
    s.close()

    payload = {
        "voip_id": 1000000, "type": "voicemail.created",
        "id": "phc-vm-001", "call_id": "phc-call-vm",
        "audio_url": "https://api.phone.com/.../vm.wav",
        "transcript": "hello", "duration": 12,
    }
    r = TestClient(app).post(
        f"/api/webhooks/phone-com/t1/{secret}", json=payload,
    )
    assert r.status_code == 204
    s = tsm()
    vm = s.query(PhoneComVoicemail).filter_by(phone_com_voicemail_id="phc-vm-001").first()
    assert vm is not None
    assert vm.call_id == call_id
    assert vm.transcript == "hello"
    s.close()


# ── unknown event ──────────────────────────────────────────────────────


def test_unknown_event_returns_204_no_row(setup):
    app, _, tsm, _, secret = setup
    r = TestClient(app).post(
        f"/api/webhooks/phone-com/t1/{secret}",
        json={"voip_id": 1000000, "type": "weird.unknown.event", "id": "x"},
    )
    # 204 even on unknown event types — Phone.com retries on 5xx.
    assert r.status_code == 204
    s = tsm()
    assert s.query(PhoneComCall).count() == 0
    assert s.query(PhoneComMessage).count() == 0
    s.close()


def test_empty_body_returns_204(setup):
    app, _, _, _, secret = setup
    r = TestClient(app).post(
        f"/api/webhooks/phone-com/t1/{secret}",
        content="",
    )
    assert r.status_code == 204


# ── no log noise on a good delivery (GDXA-70) ─────────────────────────


def test_successful_delivery_logs_no_warning(setup, caplog):
    """A delivery that upserts cleanly must be silent at WARNING.

    Guards GDXA-70: Step 6 used to ``from gdx_dispatch.events import emit``,
    a module that has never existed, so every accepted delivery logged
    ``phone_com_webhook event emit skipped`` with a traceback. Constant
    WARNING noise on a busy voice line masks the real warnings this router
    does emit (api-error, unknown_event, upsert failure).

    Scope: the autouse ``_no_audit`` fixture stubs ``log_audit_event_sync``,
    so this does NOT exercise Step 5's ``phone_com_webhook audit failed``
    branch — it covers the upsert-and-return path only.
    """
    app, _, _, _, secret = setup
    caplog.set_level(logging.WARNING, logger=wr.log.name)
    r = TestClient(app).post(
        f"/api/webhooks/phone-com/t1/{secret}",
        json={
            "voip_id": 1000000, "type": "call.completed",
            "id": "phc-quiet-001", "direction": "in", "caller_id": "+1",
        },
    )
    assert r.status_code == 204
    noisy = [rec.getMessage() for rec in caplog.records
             if rec.name == wr.log.name and rec.levelno >= logging.WARNING]
    assert noisy == []


def test_every_first_party_import_in_router_resolves():
    """No import in this router names a gdx_dispatch module that isn't there.

    Guards the GDXA-70 class, not just its one instance: ``gdx_dispatch.events``
    has never existed in this repo's history, so Step 6's import could only ever
    raise into its own ``except``. Resolution is filesystem-only, so it executes
    no package ``__init__`` and cannot be fooled by import-time side effects,
    and it walks the AST so a module named in prose does not count as an import.
    """
    import gdx_dispatch

    repo = Path(gdx_dispatch.__file__).resolve().parent.parent

    def resolves(mod: str) -> bool:
        rel = repo.joinpath(*mod.split("."))
        return rel.with_suffix(".py").is_file() or rel.is_dir()

    # `wr` lives at gdx_dispatch.modules.phone_com.webhook_router, so its
    # package is everything but the last segment.
    pkg = wr.__name__.rsplit(".", 1)[0].split(".")

    tree = ast.parse(Path(wr.__file__).read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                # Relative form: `from .events import emit` is the same defect
                # wearing a different hat, so resolve it against the package
                # rather than skipping it.
                base = pkg[: len(pkg) - node.level + 1]
                if base:
                    imported.append(".".join(base + ([node.module] if node.module else [])))
            elif node.module and node.module.split(".")[0] == "gdx_dispatch":
                imported.append(node.module)
        elif isinstance(node, ast.Import):
            imported += [a.name for a in node.names
                         if a.name.split(".")[0] == "gdx_dispatch"]

    # The scan must be able to fail: if it found nothing to check, it proves nothing.
    assert imported, "no first-party imports found — the AST walk is broken"
    assert [m for m in imported if not resolves(m)] == []

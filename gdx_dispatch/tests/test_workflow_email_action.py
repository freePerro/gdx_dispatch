"""Workflow send_email action — real, optional, auditable (Phase 4a).

Locked decision 2026-08-18: automation emails are an on/off OPTION, default
OFF (the actions were no-ops forever; pre-existing active rules must not
surprise-send on deploy). And the engine is finally wired to the real domain-
event stream — before this, fire_trigger had zero callers.
"""
from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import select

from gdx_dispatch.models.tenant_models import AppSettings, Customer
from gdx_dispatch.modules.workflows import engine
from gdx_dispatch.modules.workflows.models import WorkflowRule, WorkflowRun

TENANT = "22222222-2222-2222-2222-222222222222"


@pytest.fixture()
def db(tenant_db):
    return tenant_db


def _rule(db, *, actions, trigger="invoice.paid", conditions=None):
    rule = WorkflowRule(
        id=uuid4(), name="r1", trigger_event=trigger,
        conditions=conditions or [], actions=actions, is_active=True,
    )
    db.add(rule)
    db.commit()
    return rule


def _settings(db, *, enabled: bool, sender: str | None = None):
    row = db.query(AppSettings).first()
    if row is None:
        row = AppSettings(company_name="Acme Door Co")
        db.add(row)
    row.automation_emails_enabled = enabled
    row.automation_sender_user_id = sender
    db.commit()


def _customer(db):
    cust = Customer(id=uuid4(), name="Acme Lumber Yard",
                    email="front@acme.example", company_id=TENANT)
    db.add(cust)
    db.commit()
    return cust


def _last_run(db) -> WorkflowRun:
    return db.execute(
        select(WorkflowRun).order_by(WorkflowRun.triggered_at.desc())
    ).scalars().first()


def test_send_email_disabled_by_default_records_skip(db):
    _settings(db, enabled=False)
    cust = _customer(db)
    rule = _rule(db, actions=[{"action_type": "send_email",
                               "params": {"subject": "s", "body": "b"}}])
    asyncio.run(engine.execute_rule(
        str(rule.id),
        {"entity_type": "invoice", "entity_id": "x", "customer_id": str(cust.id)},
        db,
    ))
    run = _last_run(db)
    assert run.status == "success"
    assert run.actions_run[0]["result"] == "skipped_disabled"


def test_send_email_enabled_sends_through_pipeline(db, monkeypatch):
    import gdx_dispatch.core.transactional_email as te
    captured = {}

    def _fake(**kw):
        captured.update(kw)
        return (True, "outlook_graph", None)

    monkeypatch.setattr(te, "send_transactional_email", _fake)
    _settings(db, enabled=True, sender="sender-user-1")
    cust = _customer(db)
    rule = _rule(db, actions=[{
        "action_type": "send_email",
        "params": {"subject": "Thanks {{customer_name}}",
                   "body": "Hi {{customer_name}},\nInvoice {{invoice_number}} is paid."},
    }])
    asyncio.run(engine.execute_rule(
        str(rule.id),
        {"entity_type": "invoice", "entity_id": "inv-1",
         "customer_id": str(cust.id), "invoice_number": "INV-7"},
        db,
    ))
    run = _last_run(db)
    assert run.actions_run[0]["result"] == "sent"
    # The greeting resolves through the recipient resolver, not raw context.
    assert captured["subject"] == "Thanks Acme Lumber Yard"
    assert "INV-7" in captured["html_body"]
    assert captured["initiator_kind"] == "workflow_rule"
    assert captured["initiator_ref"] == str(rule.id)
    assert captured["user_id"] == "sender-user-1"
    # Branded shell, not a bare paragraph.
    assert '<table role="presentation"' in captured["html_body"]


def test_send_email_enabled_but_no_customer_is_honest(db):
    _settings(db, enabled=True)
    rule = _rule(db, actions=[{"action_type": "send_email",
                               "params": {"subject": "s", "body": "b"}}])
    asyncio.run(engine.execute_rule(
        str(rule.id), {"entity_type": "invoice", "entity_id": str(uuid4())}, db,
    ))
    assert _last_run(db).actions_run[0]["result"] == "no_customer_for_entity"


def test_other_actions_report_not_implemented(db):
    _settings(db, enabled=True)
    rule = _rule(db, actions=[{"action_type": "send_sms", "params": {}}])
    asyncio.run(engine.execute_rule(
        str(rule.id), {"entity_type": "invoice", "entity_id": "x"}, db,
    ))
    # 'logged' used to read like success; honesty cleanup names the truth.
    assert _last_run(db).actions_run[0]["result"] == "not_implemented"


def test_emit_stages_workflow_dispatch_for_active_rule(db):
    from gdx_dispatch.core.webhooks import emit as emit_mod

    _rule(db, actions=[{"action_type": "send_email", "params": {}}],
          trigger="invoice.paid")
    staged = emit_mod._emit(
        db, TENANT, "invoice.paid", "inv-9",
        {"invoice_id": "inv-9", "customer_id": "c-1"},
    )
    _ = staged
    jobs = db.info.get(emit_mod._WORKFLOW_PENDING_KEY)
    assert jobs and jobs[0]["event_type"] == "invoice.paid"
    assert jobs[0]["context"]["entity_id"] == "inv-9"
    assert jobs[0]["context"]["entity_type"] == "invoice"


def test_emit_stages_nothing_without_rules(db):
    from gdx_dispatch.core.webhooks import emit as emit_mod

    emit_mod._emit(db, TENANT, "invoice.paid", "inv-9", {})
    assert not db.info.get(emit_mod._WORKFLOW_PENDING_KEY)


def test_router_rejects_dead_rules(db):
    """CLAUDE.md 'can someone actually use it': a rule on a trigger nothing
    emits, or with an unknown action, sits active with run_count 0 forever.
    The router now 422s at create/update instead."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from gdx_dispatch.core.database import get_db
    from gdx_dispatch.core.modules import require_module
    from gdx_dispatch.modules.workflows import router as wf
    from gdx_dispatch.routers.auth import get_current_user

    app = FastAPI()
    app.include_router(wf.router)

    def _db():
        yield db

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = lambda: {"user_id": "u"}
    app.dependency_overrides[require_module("workflows")] = lambda: None
    c = TestClient(app)

    ok = c.post("/api/workflows", json={
        "name": "r1", "trigger_event": "invoice.paid",
        "actions": [{"action_type": "send_email", "params": {"subject": "s", "body": "b"}}],
    })
    assert ok.status_code == 200, ok.text
    rid = ok.json()["id"]

    bad_trigger = c.post("/api/workflows", json={"name": "x", "trigger_event": "not.an.event"})
    assert bad_trigger.status_code == 422
    assert "not.an.event" in bad_trigger.json()["detail"]

    bad_action = c.post("/api/workflows", json={
        "name": "x", "trigger_event": "invoice.paid",
        "actions": [{"action_type": "launch_rockets", "params": {}}],
    })
    assert bad_action.status_code == 422

    bad_update = c.put(f"/api/workflows/{rid}", json={"trigger_event": "nope.nope"})
    assert bad_update.status_code == 422

    # The list round-trips (serialization proof — the UI reads this).
    listed = c.get("/api/workflows")
    assert listed.status_code == 200
    assert listed.json()[0]["name"] == "r1"


def test_router_rejects_actions_without_an_executor(db):
    """2026-08-31: SUPPORTED_ACTIONS names five actions; only send_email
    executes. A rule created with any of the other four would sit active and
    report "not_implemented" on every fire — the retired automation-sequences
    class. The router refuses them at create/update with a reason."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from gdx_dispatch.core.database import get_db
    from gdx_dispatch.core.modules import require_module
    from gdx_dispatch.modules.workflows import router as wf
    from gdx_dispatch.modules.workflows.engine import IMPLEMENTED_ACTIONS, SUPPORTED_ACTIONS
    from gdx_dispatch.routers.auth import get_current_user

    assert set(IMPLEMENTED_ACTIONS) < set(SUPPORTED_ACTIONS)

    app = FastAPI()
    app.include_router(wf.router)

    def _db():
        yield db

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = lambda: {"user_id": "u"}
    app.dependency_overrides[require_module("workflows")] = lambda: None
    c = TestClient(app)

    for dead in sorted(set(SUPPORTED_ACTIONS) - set(IMPLEMENTED_ACTIONS)):
        r = c.post("/api/workflows", json={
            "name": f"r-{dead}", "trigger_event": "invoice.paid",
            "actions": [{"action_type": dead, "params": {}}],
        })
        assert r.status_code == 422, (dead, r.text)
        assert "no executor" in r.json()["detail"], dead

    ok = c.post("/api/workflows", json={
        "name": "r-email", "trigger_event": "invoice.paid",
        "actions": [{"action_type": "send_email", "params": {"subject": "s", "body": "b"}}],
    })
    assert ok.status_code == 200, ok.text
    # Update path is guarded the same way.
    rid = ok.json()["id"]
    bad = c.put(f"/api/workflows/{rid}", json={"actions": [{"action_type": "send_sms", "params": {}}]})
    assert bad.status_code == 422 and "no executor" in bad.json()["detail"]

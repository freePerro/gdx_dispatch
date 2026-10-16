"""GDXA-1314: the estimate routes that change an estimate are permission-gated.

Until this issue POST /api/estimates/{id}/send took get_current_user alone, so
any signed-in technician could email a customer, while its SMS twin already
required estimates.send. The same shape covered nearly every mutating route in
routers/estimates.py and the tier routes in modules/proposals/router.py.

The shared fixtures elsewhere authenticate as `admin`, and require_permission's
admin escape hatch passes everything, so they would stay green with every gate
deleted. These tests use the builtin roles that do NOT hold the key.
"""
from __future__ import annotations

from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import Customer
from gdx_dispatch.modules.proposals.models import Estimate
from gdx_dispatch.modules.proposals.router import router as proposals_router
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.estimates import router as estimates_router

TENANT = "tenant-test"
ZERO = "00000000-0000-0000-0000-000000000000"

SEND = "estimates.send"
WRITE = "estimates.write"

# (method, path, key). {e} is a real estimate; the other ids need not exist,
# because the gate refuses before the handler looks anything up.
GATED = [
    ("POST", "/api/estimates", WRITE),
    ("PATCH", "/api/estimates/{e}", WRITE),
    ("DELETE", "/api/estimates/{e}", WRITE),
    ("POST", "/api/estimates/{e}/lines", WRITE),
    ("PATCH", "/api/estimates/{e}/lines/" + ZERO, WRITE),
    ("DELETE", "/api/estimates/{e}/lines/" + ZERO, WRITE),
    ("POST", "/api/estimates/{e}/mark-sent", SEND),
    ("POST", "/api/estimates/{e}/email-preview", SEND),
    ("POST", "/api/estimates/{e}/send", SEND),
    ("POST", "/api/estimates/{e}/reopen", WRITE),
    ("POST", "/api/estimates/{e}/convert-to-job", WRITE),
    ("POST", "/api/estimates/{e}/duplicate", WRITE),
    ("POST", "/api/estimates/expire-stale", WRITE),
    ("POST", "/api/estimates/{e}/attachments", WRITE),
    ("PATCH", "/api/estimates/{e}/attachments/" + ZERO, WRITE),
    ("DELETE", "/api/estimates/{e}/attachments/" + ZERO, WRITE),
    ("POST", "/api/estimates/{e}/reassign-customer", WRITE),
    ("POST", "/api/estimates/{e}/proposal-tiers", WRITE),
    ("PATCH", "/api/estimates/{e}/proposal-tiers/" + ZERO, WRITE),
    ("POST", "/api/estimates/{e}/proposal-tiers/" + ZERO + "/lines", WRITE),
    ("PATCH", "/api/estimates/{e}/proposal-tiers/" + ZERO + "/lines/" + ZERO, WRITE),
    ("DELETE", "/api/estimates/{e}/proposal-tiers/" + ZERO + "/lines/" + ZERO, WRITE),
    ("DELETE", "/api/estimates/{e}/proposal-tiers/" + ZERO, WRITE),
]

# Mutating routes left without require_permission, each with its reason. A new
# mutating route must land in GATED or here, never neither. Being here is not a
# clean bill: the first three are open to EVERY signed-in role, viewer
# included, until a role decision narrows them.
UNGATED_BY_DESIGN = {
    # The technician's phone (MobileEstimatesView) records the customer's
    # yes/no in the driveway; techs hold no estimates.* write key.
    ("POST", "/api/estimates/{estimate_id}/accept"),
    ("POST", "/api/estimates/{estimate_id}/decline"),
    # Dispatchers hold estimates.write but not invoices.write, accounting the
    # reverse; which of them requests a deposit is a role decision (GDXA-1314
    # report), not something to settle inside an auth fix.
    ("POST", "/api/estimates/{estimate_id}/deposit-invoice"),
    # Gated by role (_require_dispatch), not by a permission key.
    ("POST", "/api/estimates/{estimate_id}/proposal/accept"),
    # Customer-facing, scoped by the unguessable proposal token.
    ("POST", "/api/proposals/{token}/accept"),
    ("POST", "/api/proposals/{token}/deposit/pay"),
    ("POST", "/api/proposals/{token}/decline"),
}


@pytest.fixture()
def client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    TenantBase.metadata.create_all(engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    setup = Session()
    setup.execute(text("""
        CREATE TABLE IF NOT EXISTS company_module_grants (
            id TEXT PRIMARY KEY, company_id TEXT, module_key TEXT,
            granted_at TEXT, created_at TEXT, expires_at TEXT,
            UNIQUE(company_id, module_key)
        )
    """))
    setup.execute(text("""
        INSERT OR IGNORE INTO company_module_grants (id, company_id, module_key, granted_at, created_at)
        VALUES ('g2', 'tenant-test', 'estimates', datetime('now'), datetime('now'))
    """))
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

    app.include_router(estimates_router)
    app.include_router(proposals_router)
    app.dependency_overrides[get_db] = _override_db
    _as(app, "admin")
    tc = TestClient(app, raise_server_exceptions=True)
    yield tc
    app.dependency_overrides.clear()
    engine.dispose()


def _as(app: FastAPI, role: str) -> None:
    # A non-UUID subject has no users row, so the resolver falls back to
    # BUILTIN_ROLES[role] — the shipped default for that role.
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": f"{role}-1", "sub": f"{role}-1", "role": role, "tenant_id": TENANT,
    }


def _estimate(client: TestClient) -> str:
    db = next(client.app.dependency_overrides[get_db]())
    try:
        row = Customer(name="Acme", email="customer@example.com", company_id=TENANT)
        db.add(row)
        db.commit()
        cid = str(row.id)
    finally:
        db.close()
    r = client.post("/api/estimates", json={"customer_id": cid, "label": "Spring swap"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _status(client: TestClient, estimate_id: str) -> tuple[str, object]:
    db = next(client.app.dependency_overrides[get_db]())
    try:
        row = db.get(Estimate, UUID(estimate_id))
        return row.status, row.sent_at
    finally:
        db.close()


def _mock_email_success(monkeypatch):
    import gdx_dispatch.core.transactional_email as te
    monkeypatch.setattr(te, "send_transactional_email", lambda **kw: (True, "outlook_graph", None))


@pytest.mark.parametrize(("method", "path", "key"), GATED, ids=[f"{m} {p}" for m, p, _ in GATED])
def test_technician_is_refused(client: TestClient, method: str, path: str, key: str):
    est = _estimate(client)
    _as(client.app, "technician")
    r = client.request(method, path.format(e=est), json={})
    assert r.status_code == 403, f"{method} {path}: expected 403, got {r.status_code} {r.text}"
    assert key in r.json()["detail"]


def test_technician_send_leaves_the_estimate_unsent(client: TestClient, monkeypatch):
    """The refusal happens before the handler: nothing is emailed or stamped."""
    sent: list[dict] = []
    import gdx_dispatch.core.transactional_email as te
    monkeypatch.setattr(te, "send_transactional_email", lambda **kw: sent.append(kw) or (True, "x", None))
    est = _estimate(client)
    _as(client.app, "technician")
    assert client.post(f"/api/estimates/{est}/send").status_code == 403
    assert client.post(f"/api/estimates/{est}/mark-sent", json={"channel": "email"}).status_code == 403
    assert sent == []
    assert _status(client, est) == ("draft", None)


def test_sales_can_send_and_mark_sent(client: TestClient, monkeypatch):
    _mock_email_success(monkeypatch)
    est = _estimate(client)
    _as(client.app, "sales")
    r = client.post(f"/api/estimates/{est}/send")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "sent"
    _as(client.app, "admin")
    other = _estimate(client)
    _as(client.app, "sales")
    r = client.post(f"/api/estimates/{other}/mark-sent", json={"channel": "email"})
    assert r.status_code == 200, r.text
    assert _status(client, other)[0] == "sent"


def test_sales_and_dispatcher_can_create_and_edit(client: TestClient):
    for role in ("sales", "dispatcher"):
        _as(client.app, "admin")
        est = _estimate(client)
        _as(client.app, role)
        r = client.patch(f"/api/estimates/{est}", json={"label": f"edited by {role}"})
        assert r.status_code == 200, f"{role}: {r.text}"
        r = client.post(f"/api/estimates/{est}/lines", json={"description": "Spring", "quantity": 1, "unit_price": 100})
        assert r.status_code == 201, f"{role}: {r.text}"


def test_dispatcher_does_not_hold_estimates_send(client: TestClient, monkeypatch):
    """Pins the shipped role default: dispatcher carries estimates.write but
    not estimates.send, so /send now refuses it exactly as /send-sms already
    did. A tenant grants it in Roles & Permissions."""
    _mock_email_success(monkeypatch)
    est = _estimate(client)
    _as(client.app, "dispatcher")
    assert client.post(f"/api/estimates/{est}/send").status_code == 403


def test_technician_can_still_accept_and_decline_from_the_truck(client: TestClient):
    """MobileEstimatesView calls these as the technician; gating them would
    break the in-driveway yes/no."""
    a = _estimate(client)
    d = _estimate(client)
    _as(client.app, "technician")
    assert client.post(f"/api/estimates/{a}/accept", json={}).status_code == 200
    r = client.post(f"/api/estimates/{d}/decline", json={"reason": "price"})
    assert r.status_code == 200, r.text


def _has_permission_gate(route: APIRoute) -> bool:
    stack = list(route.dependant.dependencies)
    while stack:
        dep = stack.pop()
        if "require_permission" in getattr(dep.call, "__qualname__", ""):
            return True
        stack.extend(dep.dependencies)
    return False


def test_every_mutating_route_is_gated_or_listed():
    """The class, not the instance: a new mutating route on either router
    that has no require_permission and no reason in UNGATED_BY_DESIGN is red."""
    ungated = set()
    for router in (estimates_router, proposals_router):
        for route in router.routes:
            if not isinstance(route, APIRoute):
                continue
            for method in route.methods - {"GET", "HEAD", "OPTIONS"}:
                if not _has_permission_gate(route):
                    ungated.add((method, route.path))
    assert ungated == UNGATED_BY_DESIGN

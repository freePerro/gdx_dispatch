"""GDXA-450: a stale estimate PATCH is refused with 409, not silently applied.

The estimate editor autosaves the whole header (label, jobsite, notes, tax,
discount, ...) on every flush. Without a concurrency token, an editor left
open overwrote a colleague's later save with what it loaded. The PATCH now
takes the `version` the editor loaded (body `expected_version` or If-Match);
a mismatch is a 409 that writes nothing, and an omitted token is accepted as
before. Every write reports the version it started from and the one it left,
so the editor can follow its OWN line edits without 409-ing itself.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import Customer
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.estimates import router


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
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
    setup.execute(text(
        "INSERT OR IGNORE INTO company_module_grants (id, company_id, module_key, granted_at, created_at) "
        "VALUES ('g2', 'tenant-test', 'estimates', datetime('now'), datetime('now'))"
    ))
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
        request.state.tenant = {"id": "tenant-test"}
        return await call_next(request)

    app.include_router(router)
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "user-1", "sub": "user-1", "name": "Tester",
        "role": "admin", "tenant_id": "tenant-test",
    }
    tc = TestClient(app, raise_server_exceptions=True)
    yield tc
    app.dependency_overrides.clear()


def _create_estimate(client: TestClient) -> str:
    with next(client.app.dependency_overrides[get_db]()) as db:
        c = Customer(name="Acme", email="x@y.com", company_id="tenant-test")
        db.add(c)
        db.commit()
        db.refresh(c)
        cid = str(c.id)
    r = client.post("/api/estimates", json={"customer_id": cid, "label": "Loaded"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _get(client: TestClient, eid: str) -> dict:
    r = client.get(f"/api/estimates/{eid}")
    assert r.status_code == 200, r.text
    return r.json()


def test_second_of_two_stale_patches_is_refused_and_writes_nothing(client):
    eid = _create_estimate(client)
    loaded = _get(client, eid)["version"]
    assert isinstance(loaded, int)

    # Two editors opened the estimate at the same version.
    first = client.patch(f"/api/estimates/{eid}", json={"label": "Alice", "expected_version": loaded})
    assert first.status_code == 200, first.text
    assert first.json()["version"] > loaded

    second = client.patch(
        f"/api/estimates/{eid}",
        json={"label": "Bob", "notes": "stale", "expected_version": loaded},
    )
    assert second.status_code == 409, second.text
    detail = second.json()["detail"]
    assert detail["code"] == "estimate_version_conflict"
    assert detail["expected_version"] == loaded
    assert detail["current_version"] == first.json()["version"]

    after = _get(client, eid)
    assert after["label"] == "Alice"
    assert after["notes"] is None
    assert after["version"] == first.json()["version"]


def test_an_omitted_token_is_still_accepted(client):
    eid = _create_estimate(client)
    loaded = _get(client, eid)["version"]
    client.patch(f"/api/estimates/{eid}", json={"label": "Alice"})
    r = client.patch(f"/api/estimates/{eid}", json={"label": "Bob"})
    assert r.status_code == 200, r.text
    assert r.json()["label"] == "Bob"
    assert r.json()["version"] > loaded


def test_expected_version_is_checked_never_written(client):
    eid = _create_estimate(client)
    loaded = _get(client, eid)["version"]
    r = client.patch(f"/api/estimates/{eid}", json={"notes": "x", "expected_version": loaded})
    assert r.status_code == 200, r.text
    # The token moved forward from what was sent; it was not assigned.
    assert r.json()["version"] > loaded
    assert r.json()["version_before"] == loaded


def test_if_match_header_carries_the_same_check(client):
    eid = _create_estimate(client)
    loaded = _get(client, eid)["version"]
    ok = client.patch(f"/api/estimates/{eid}", json={"label": "A"}, headers={"If-Match": f'"{loaded}"'})
    assert ok.status_code == 200, ok.text
    stale = client.patch(f"/api/estimates/{eid}", json={"label": "B"}, headers={"If-Match": f'W/"{loaded}"'})
    assert stale.status_code == 409, stale.text
    assert _get(client, eid)["label"] == "A"

    any_version = client.patch(f"/api/estimates/{eid}", json={"label": "C"}, headers={"If-Match": "*"})
    assert any_version.status_code == 200, any_version.text

    bad = client.patch(f"/api/estimates/{eid}", json={"label": "D"}, headers={"If-Match": '"abc"'})
    assert bad.status_code == 400, bad.text
    current = _get(client, eid)["version"]
    disagree = client.patch(
        f"/api/estimates/{eid}",
        json={"label": "E", "expected_version": current},
        headers={"If-Match": f'"{current + 5}"'},
    )
    assert disagree.status_code == 400, disagree.text
    assert _get(client, eid)["label"] == "C"


def test_own_line_edits_chain_the_token_so_the_editor_does_not_409_itself(client):
    eid = _create_estimate(client)
    token = _get(client, eid)["version"]

    # A line write recalculates the total, which bumps the estimate's version.
    added = client.post(f"/api/estimates/{eid}/lines", json={"description": "Spring", "quantity": 1, "unit_price": 50})
    assert added.status_code == 201, added.text
    body = added.json()
    assert body["estimate_version_before"] == token
    assert body["estimate_version"] > token
    token = body["estimate_version"]

    patched = client.patch(f"/api/estimates/{eid}/lines/{body['id']}", json={"unit_price": 60})
    assert patched.status_code == 200, patched.text
    assert patched.json()["estimate_version_before"] == token
    token = patched.json()["estimate_version"]

    deleted = client.delete(f"/api/estimates/{eid}/lines/{body['id']}")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["deleted"] is True
    assert deleted.json()["estimate_version_before"] == token
    token = deleted.json()["estimate_version"]

    header = client.patch(f"/api/estimates/{eid}", json={"label": "Mine", "expected_version": token})
    assert header.status_code == 200, header.text


def test_a_colleagues_write_breaks_the_chain(client):
    eid = _create_estimate(client)
    token = _get(client, eid)["version"]

    # A colleague saves the header; then this editor adds a line.
    client.patch(f"/api/estimates/{eid}", json={"label": "Colleague"})
    added = client.post(f"/api/estimates/{eid}/lines", json={"description": "Spring", "quantity": 1, "unit_price": 50})
    assert added.status_code == 201, added.text
    # The line response starts from the colleague's version, not the editor's
    # token, so the editor must not adopt it; its next header save is refused.
    assert added.json()["estimate_version_before"] != token
    stale = client.patch(f"/api/estimates/{eid}", json={"label": "Mine", "expected_version": token})
    assert stale.status_code == 409, stale.text
    assert _get(client, eid)["label"] == "Colleague"


def test_decline_then_reopen_chain_the_token(client):
    # The editor declines, changes its mind, reopens and edits on. Both status
    # writes bump the version, so both must report it or the edit 409s itself.
    eid = _create_estimate(client)
    token = _get(client, eid)["version"]

    declined = client.post(f"/api/estimates/{eid}/decline", json={"reason": "Price"})
    assert declined.status_code == 200, declined.text
    assert declined.json()["version_before"] == token
    token = declined.json()["version"]

    reopened = client.post(f"/api/estimates/{eid}/reopen", json={})
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["version_before"] == token
    token = reopened.json()["version"]

    header = client.patch(f"/api/estimates/{eid}", json={"label": "Mine", "expected_version": token})
    assert header.status_code == 200, header.text


def test_a_read_reports_no_transition(client):
    eid = _create_estimate(client)
    body = _get(client, eid)
    assert "version_before" not in body

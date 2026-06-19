"""Tests for gdx_dispatch.routers.auth.pats (SS-14 slice D)."""
from __future__ import annotations

from uuid import UUID, uuid4

import bcrypt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from types import SimpleNamespace

from gdx_dispatch.core.auth_dispatcher import get_current_principal
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.platform_extensions import AccessToken
from gdx_dispatch.routers.auth.pats import router


def RouterPrincipal(*, identity_id, tenant_id, capabilities=None):
    return SimpleNamespace(
        identity_id=identity_id,
        tenant_id=tenant_id,
        principal_role="tech",
        capabilities=tuple(capabilities or ()),
        is_super_admin=False,
    )
from gdx_dispatch.tests.factories.platform import (
    make_capability,
    make_capability_set,
    make_identity,
    make_tenant,
)


@pytest.fixture
def client_and_db(control_db):
    app = FastAPI()
    app.include_router(router)

    def _override_db():
        try:
            yield control_db
        finally:
            pass  # fixture owns teardown

    app.dependency_overrides[get_db] = _override_db

    def _set_principal(principal) -> None:
        app.dependency_overrides[get_current_principal] = lambda: principal

    client = TestClient(app)
    return client, control_db, _set_principal


def _make_principal(db, *, tenant_slug=None, capabilities=None):
    identity = make_identity(db)
    tenant = make_tenant(db, slug=tenant_slug) if tenant_slug else make_tenant(db)
    if capabilities is None:
        capabilities = [{"action": "read", "resource_type": "job"}]
    db.commit()
    return RouterPrincipal(
        identity_id=identity.id,
        tenant_id=tenant.slug,
        capabilities=capabilities,
    )


def test_mint_pat_returns_secret_once(client_and_db):
    client, db, set_principal = client_and_db
    principal = _make_principal(db)
    # Seed a capability in a caller-held capset we authorize on
    donor_capset = make_capability_set(db)
    donor_cap = make_capability(
        db, capability_set=donor_capset, action="read", resource_type="job"
    )
    db.commit()
    set_principal(principal)

    resp = client.post(
        "/api/pats",
        json={"name": "ci-token", "capability_ids": [str(donor_cap.id)], "expires_in_days": 30},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "ci-token"
    assert body["secret"].startswith("gdx_pat_live_")
    assert body["prefix"] == "gdx_pat_live_"
    assert body["expires_at"] is not None

    # Stored row has a bcrypt hash, NOT the plaintext, and matches the secret.
    pat = db.get(AccessToken, UUID(body["id"]))
    assert pat is not None
    assert pat.secret_hash != body["secret"]
    assert bcrypt.checkpw(body["secret"].encode(), pat.secret_hash.encode())


# test_mint_pat_sandbox_tenant_uses_test_prefix removed (D97 031): the
# ``-sandbox`` slug-suffix branch was retired in pats.py + admin_pats.py
# (zero prod tenants matched). All PATs mint with gdx_pat_live_ until a
# Tenant.is_sandbox flag replaces the slug-shape sniff.


def test_mint_pat_rejects_capability_superset(client_and_db):
    client, db, set_principal = client_and_db
    # Principal holds ("read","job") but requests ("write","customer")
    principal = _make_principal(db, capabilities=[{"action": "read", "resource_type": "job"}])
    donor_capset = make_capability_set(db)
    donor_cap = make_capability(db, capability_set=donor_capset, action="write", resource_type="customer")
    db.commit()
    set_principal(principal)

    resp = client.post(
        "/api/pats",
        json={"name": "bad", "capability_ids": [str(donor_cap.id)]},
    )
    assert resp.status_code == 403
    assert "write:customer" in resp.json()["detail"]


def test_mint_pat_wildcard_principal_may_grant_any(client_and_db):
    client, db, set_principal = client_and_db
    principal = _make_principal(db, capabilities=[{"action": "*", "resource_type": "*"}])
    donor_capset = make_capability_set(db)
    donor_cap = make_capability(db, capability_set=donor_capset, action="delete", resource_type="invoice")
    db.commit()
    set_principal(principal)

    resp = client.post(
        "/api/pats",
        json={"name": "super", "capability_ids": [str(donor_cap.id)]},
    )
    assert resp.status_code == 201


def test_mint_pat_rejects_empty_name(client_and_db):
    client, db, set_principal = client_and_db
    set_principal(_make_principal(db))
    resp = client.post("/api/pats", json={"name": "   "})
    assert resp.status_code == 400


def test_mint_pat_rejects_unknown_capability_id(client_and_db):
    client, db, set_principal = client_and_db
    set_principal(_make_principal(db))
    resp = client.post(
        "/api/pats",
        json={"name": "orphan", "capability_ids": [str(uuid4())]},
    )
    assert resp.status_code == 400
    assert "unknown capability_ids" in resp.json()["detail"]


def test_mint_pat_expiry_capped_at_366_days(client_and_db):
    client, db, set_principal = client_and_db
    set_principal(_make_principal(db))
    resp = client.post("/api/pats", json={"name": "long", "expires_in_days": 10_000})
    assert resp.status_code == 201
    # expires_at roughly now + 366d; since we capped, it's not 10k days out.
    from datetime import datetime
    exp = datetime.fromisoformat(resp.json()["expires_at"])
    delta_days = (exp - datetime.now(exp.tzinfo)).days
    assert delta_days <= 366


def test_list_pats_excludes_secrets_and_revoked(client_and_db):
    client, db, set_principal = client_and_db
    principal = _make_principal(db)
    set_principal(principal)

    a = client.post("/api/pats", json={"name": "a"}).json()
    b = client.post("/api/pats", json={"name": "b"}).json()
    # Revoke b.
    assert client.delete(f"/api/pats/{b['id']}").status_code == 200

    resp = client.get("/api/pats")
    assert resp.status_code == 200
    rows = resp.json()
    ids = {r["id"] for r in rows}
    assert a["id"] in ids
    assert b["id"] not in ids
    for row in rows:
        assert "secret" not in row
        assert "secret_hash" not in row


def test_revoke_pat_idempotent(client_and_db):
    client, db, set_principal = client_and_db
    set_principal(_make_principal(db))

    created = client.post("/api/pats", json={"name": "r"}).json()
    r1 = client.delete(f"/api/pats/{created['id']}")
    r2 = client.delete(f"/api/pats/{created['id']}")
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json() == {"revoked": True, "id": created["id"]}


def test_revoke_pat_other_owner_returns_404(client_and_db):
    client, db, set_principal = client_and_db
    # Owner A creates a PAT.
    principal_a = _make_principal(db)
    set_principal(principal_a)
    created = client.post("/api/pats", json={"name": "a-owned"}).json()

    # Owner B tries to revoke it.
    principal_b = _make_principal(db)
    set_principal(principal_b)
    resp = client.delete(f"/api/pats/{created['id']}")
    assert resp.status_code == 404


def test_revoke_unknown_pat_returns_404(client_and_db):
    client, db, set_principal = client_and_db
    set_principal(_make_principal(db))
    resp = client.delete(f"/api/pats/{uuid4()}")
    assert resp.status_code == 404

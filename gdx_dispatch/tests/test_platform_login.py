"""Tests for /auth/platform-login — email-first cross-tenant login.

The platform host (app.example.com) has no tenant context. This
endpoint resolves the user via control-plane Identity → Membership(s),
then verifies password against the per-tenant `users` row.

Three cases covered: 0 memberships → 401; 1 membership → access token +
redirect_url; N memberships → select_tenant payload (no token until the
SPA POSTs a tenant choice).
"""
from __future__ import annotations

from uuid import uuid4

import bcrypt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text as sa_text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.control.models import Base as ControlBase, Tenant
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.platform import (  # noqa: F401 — register mappers
    CapabilitySet,
    Identity,
    Membership,
)
from gdx_dispatch.models.tenant_models import Base as TenantBase, User
from gdx_dispatch.routers import auth as auth_module
from gdx_dispatch.routers.auth import core as auth_core  # patch target — see _patch_targets note below

# _patch_targets note: monkeypatching ``auth_module.X`` after the
# Slice 8 Phase A package shim only sets the *package* attribute; the
# functions in ``gdx_dispatch/routers/auth/core.py`` resolve names via that
# module's own globals, so the patch must target ``auth_core`` (the
# implementation module) for the substitution to reach the call site.
# This is the standard "patch where it's used" rule from unittest.mock.


# ---------------------------------------------------------------------------
# Fixtures — minimal control DB + a stand-in tenant DB the seam returns.
# ---------------------------------------------------------------------------


def _make_control_session():
    import gdx_dispatch.models.platform  # noqa: F401
    import gdx_dispatch.models.platform_extensions  # noqa: F401
    # StaticPool keeps a single in-memory SQLite DB shared across the
    # threads TestClient may use, otherwise each connection gets its own
    # empty DB and "no such table" surfaces from the worker thread.
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    ControlBase.metadata.create_all(eng)
    Sess = sessionmaker(bind=eng, autoflush=False, autocommit=False)
    return Sess(), eng


def _make_tenant_session_with_user(email: str, password: str, *, role: str = "admin"):
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(eng)
    Sess = sessionmaker(bind=eng, autoflush=False, autocommit=False)
    s = Sess()
    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    s.add(User(
        id=uuid4(), username=email, email=email, name=email,
        full_name=email, password_hash=pw_hash, role=role, active=True,
        company_id=str(uuid4()),
    ))
    s.commit()
    return s, eng


def _seed_capset(cdb) -> CapabilitySet:
    cs = CapabilitySet(name="role:admin", scope_type="tenant", description="Admin")
    cdb.add(cs)
    cdb.commit()
    return cs


def _seed_tenant_with_membership(cdb, slug: str, email: str, capset_id) -> tuple[Tenant, Identity]:
    t = Tenant(
        id=uuid4(), slug=slug, name=slug.title(),  # value irrelevant — seam is patched
    )
    cdb.add(t)
    cdb.flush()
    ident = cdb.query(Identity).filter(Identity.email == email).one_or_none()
    if ident is None:
        ident = Identity(id=uuid4(), email=email, display_name=email, status="active")
        cdb.add(ident)
        cdb.flush()
    cdb.add(Membership(
        id=uuid4(), identity_id=ident.id, tenant_id=t.id,
        role="admin", capability_set_id=capset_id,
    ))
    cdb.commit()
    return t, ident


@pytest.fixture
def app_client(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "x" * 64)
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/15")  # never connected

    # Stub redis used by _issue/refresh sets — auth module imports redis at top.
    class _FakeRedis:
        def sadd(self, *a, **k): return 1
        def expire(self, *a, **k): return True
        def sismember(self, *a, **k): return False
        def hgetall(self, *a, **k): return {}
        def hset(self, *a, **k): return 1
        def srem(self, *a, **k): return 1
    monkeypatch.setattr(auth_core, "redis", _FakeRedis())

    cdb, _ceng = _make_control_session()
    tenant_sessions: dict[str, tuple] = {}

    # Stand in for the SECURITY DEFINER fn from migration 072 — SQLite has
    # no equivalent, so the test monkeypatches the helper to read from the
    # ORM tables the test fixtures populate.
    def _stub_lookup(_db, email_norm: str):
        from sqlalchemy import select as _sel, func as _func
        from types import SimpleNamespace as _SN
        ident = cdb.execute(
            _sel(Identity).where(_func.lower(Identity.email) == email_norm)
        ).scalar_one_or_none()
        if ident is None:
            return []
        pairs = cdb.execute(
            _sel(Membership, Tenant)
            .join(Tenant, Tenant.id == Membership.tenant_id)
            .where(
                Membership.identity_id == ident.id,
                Membership.revoked_at.is_(None),
            )
        ).all()
        return [
            _SN(identity_id=ident.id, tenant_id=t.id, slug=t.slug,
                name=t.name, role=m.role)
            for (m, t) in pairs
        ]
    monkeypatch.setattr(auth_core, "_lookup_memberships_by_email", _stub_lookup)

    def _seam(tenant):
        # Look up by tenant.slug because tests stash tenant DBs under slug.
        pair = tenant_sessions.get(str(tenant.slug))
        if pair is None:
            raise RuntimeError(f"no fake tenant DB registered for {tenant.slug}")
        s, e = pair
        # Return a fresh session bound to the same engine so the handler
        # can close it without affecting the test's own session view.
        Sess = sessionmaker(bind=e, autoflush=False, autocommit=False)
        return e, Sess()
    monkeypatch.setattr(auth_core, "_open_tenant_session_for_login", _seam)

    app = FastAPI()
    app.include_router(auth_module.router)
    app.dependency_overrides[get_db] = lambda: cdb

    client = TestClient(app)
    return client, cdb, tenant_sessions


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


def test_unknown_email_returns_401(app_client):
    client, _cdb, _ = app_client
    r = client.post("/auth/platform-login", json={
        "email": "nobody@example.invalid", "password": "whatever",
    })
    assert r.status_code == 401
    assert r.json()["detail"] == "Invalid credentials"


def test_email_with_no_membership_returns_401(app_client):
    client, cdb, _ = app_client
    cdb.add(Identity(id=uuid4(), email="orphan@x.com", display_name="orphan", status="active"))
    cdb.commit()
    r = client.post("/auth/platform-login", json={
        "email": "orphan@x.com", "password": "whatever",
    })
    assert r.status_code == 401


def test_single_membership_happy_path(app_client):
    client, cdb, tenant_sessions = app_client
    cs = _seed_capset(cdb)
    email, pw = "doug@gdx.test", "correct-horse-battery-staple"
    tenant, _ident = _seed_tenant_with_membership(cdb, "gdx", email, cs.id)
    tdb_session, tdb_engine = _make_tenant_session_with_user(email, pw)
    tenant_sessions[tenant.slug] = (tdb_session, tdb_engine)

    r = client.post("/auth/platform-login", json={"email": email, "password": pw})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["access_token"]
    assert body["redirect_url"] == "https://gdx.example.com/dashboard"
    assert body["tenant"]["slug"] == "gdx"
    assert body["user"]["email"] == email


def test_single_membership_wrong_password_returns_401(app_client):
    client, cdb, tenant_sessions = app_client
    cs = _seed_capset(cdb)
    email = "doug@gdx.test"
    tenant, _ident = _seed_tenant_with_membership(cdb, "gdx", email, cs.id)
    tdb_session, tdb_engine = _make_tenant_session_with_user(email, "actual-password")
    tenant_sessions[tenant.slug] = (tdb_session, tdb_engine)

    r = client.post("/auth/platform-login", json={"email": email, "password": "wrong"})
    assert r.status_code == 401


def test_multiple_memberships_returns_picker(app_client):
    client, cdb, tenant_sessions = app_client
    cs = _seed_capset(cdb)
    email, pw = "owner@multi.test", "pw"
    t1, _ = _seed_tenant_with_membership(cdb, "alpha", email, cs.id)
    t2, _ = _seed_tenant_with_membership(cdb, "bravo", email, cs.id)
    tenant_sessions[t1.slug] = _make_tenant_session_with_user(email, pw)
    tenant_sessions[t2.slug] = _make_tenant_session_with_user(email, pw)

    r = client.post("/auth/platform-login", json={"email": email, "password": pw})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "select_tenant"
    slugs = {x["slug"] for x in body["tenants"]}
    assert slugs == {"alpha", "bravo"}
    # No token issued at the picker stage.
    assert "access_token" not in body


def test_multi_membership_with_explicit_choice_issues_token(app_client):
    client, cdb, tenant_sessions = app_client
    cs = _seed_capset(cdb)
    email, pw = "owner@multi.test", "pw"
    t1, _ = _seed_tenant_with_membership(cdb, "alpha", email, cs.id)
    t2, _ = _seed_tenant_with_membership(cdb, "bravo", email, cs.id)
    tenant_sessions[t1.slug] = _make_tenant_session_with_user(email, pw)
    tenant_sessions[t2.slug] = _make_tenant_session_with_user(email, pw)

    r = client.post("/auth/platform-login", json={
        "email": email, "password": pw, "tenant_id": str(t2.id),
    })
    assert r.status_code == 200
    body = r.json()
    assert body["access_token"]
    assert body["tenant"]["slug"] == "bravo"


def test_email_lookup_is_case_insensitive(app_client):
    client, cdb, tenant_sessions = app_client
    cs = _seed_capset(cdb)
    stored, pw = "Doug@GDX.Test", "pw"
    tenant, _ident = _seed_tenant_with_membership(cdb, "gdx", stored, cs.id)
    tdb_session, tdb_engine = _make_tenant_session_with_user(stored, pw)
    tenant_sessions[tenant.slug] = (tdb_session, tdb_engine)

    r = client.post("/auth/platform-login", json={
        "email": "doug@gdx.test", "password": pw,
    })
    assert r.status_code == 200, r.text

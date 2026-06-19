"""Single-tenant collapse (genesis Phase A) — the data-plane pin.

Locks in the two load-bearing changes that make GDXDispatch single-tenant:

1. ``get_db()`` yields a session on the one application engine
   (``SessionLocal``) regardless of request — no per-tenant ``engine_registry``
   lookup off ``request.state.tenant``.
2. ``TenantMiddleware`` pins the one tenant on ``request.state`` for every
   request, without querying a control plane.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

from gdx_dispatch.core.database import SessionLocal, app_engine, get_db
from gdx_dispatch.core.tenant import TenantMiddleware, single_tenant


def test_get_db_yields_the_single_engine_session():
    gen = get_db()
    db = next(gen)
    try:
        assert db.get_bind() is app_engine
        assert next(get_db()).get_bind() is SessionLocal().get_bind()
    finally:
        gen.close()


def test_get_db_ignores_request_state_tenant():
    """A request carrying a different tenant db_url must NOT redirect the
    session — single-tenant always uses the one engine."""
    req = MagicMock()
    req.state = SimpleNamespace(tenant={"id": "other", "db_url": "sqlite:///./other.db"})
    gen = get_db(req)
    db = next(gen)
    try:
        assert db.get_bind() is app_engine
    finally:
        gen.close()


def test_middleware_pins_single_tenant_without_lookup(monkeypatch):
    monkeypatch.setattr(
        "gdx_dispatch.core.tenant._lookup_tenant",
        lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("single-tenant middleware must not query tenants")
        ),
    )
    mw = TenantMiddleware(app=MagicMock())
    req = MagicMock()
    req.url.path = "/api/jobs"
    req.method = "GET"
    req.headers = {"host": "whatever.example.com"}
    req.state = SimpleNamespace()

    seen = {}

    async def fake_next(request):
        seen["tenant"] = request.state.tenant
        seen["tenant_id"] = request.state.tenant_id
        return MagicMock(spec=[], status_code=200)

    asyncio.run(mw.dispatch(req, fake_next))
    assert seen["tenant"] == single_tenant()
    assert seen["tenant_id"] == single_tenant()["id"]


def test_single_tenant_id_is_env_driven(monkeypatch):
    monkeypatch.setenv("GDX_TENANT_ID", "company-uuid-123")
    assert single_tenant()["id"] == "company-uuid-123"
    monkeypatch.delenv("GDX_TENANT_ID", raising=False)
    monkeypatch.delenv("GDX_DEFAULT_TENANT_ID", raising=False)
    assert single_tenant()["id"] == "gdx"

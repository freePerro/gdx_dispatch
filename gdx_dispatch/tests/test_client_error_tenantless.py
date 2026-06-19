"""MH-0 (mobile hardening sprint, 2026-05-19) — regression test:
``POST /api/feedback/client-error`` must succeed when the host doesn't
resolve to a tenant.

Background (from `ai-queue/brainstorm/mobile_ux_audit_2026-05-19.md` P0
#2): on `gdx.example.com` the SPA error-capture plugin posts to
``/api/feedback/client-error`` and the request 404'd because the tenant
middleware short-circuited before the route. Result: every client-side
JS error from the platform host was silently lost. Two parts of the fix:

1. Add the path to ``TenantMiddleware._BYPASS_PATHS``.
2. Make the handler tolerate ``request.state.tenant`` being absent.

This test asserts only #2 at the unit level — calling the handler with a
request that has no tenant returns a 200-shape (``{"status": "logged_tenantless"}``)
and does NOT raise. The bypass itself (#1) is exercised by the
middleware tests + the post-deploy browser walk.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi import Request

from gdx_dispatch.routers.bug_reports import ClientErrorIn, report_client_error


def _mk_request_no_tenant() -> Request:
    request = MagicMock(spec=Request)
    request.state = SimpleNamespace()  # explicitly no `tenant`
    return request


def _mk_request_tenant_attr_none() -> Request:
    request = MagicMock(spec=Request)
    request.state = SimpleNamespace(tenant=None)
    return request


def _mk_request_tenant_empty_dict() -> Request:
    request = MagicMock(spec=Request)
    request.state = SimpleNamespace(tenant={})
    return request


def _payload() -> ClientErrorIn:
    return ClientErrorIn(
        kind="window_error",
        detail="ReferenceError: foo is not defined",
        page="/login",
        source="https://gdx.example.com/assets/index-PCNP98J3.js",
        lineno=62,
        colno=10,
        stack="at Foo (index.js:62:10)\nat Bar (index.js:14:3)",
        user_agent="Mozilla/5.0",
        url="",
        method="GET",
        status=0,
    )


def test_client_error_no_tenant_state_returns_logged_tenantless():
    """When ``request.state`` has no ``tenant`` attribute at all (the case
    on platform/unresolved hosts after the middleware bypass), the handler
    must NOT raise and must return the tenantless marker."""
    result = report_client_error(_mk_request_no_tenant(), _payload())
    assert result == {"status": "logged_tenantless"}


def test_client_error_tenant_none_returns_logged_tenantless():
    """A request where ``request.state.tenant is None`` is the equivalent
    of the no-attribute case; same contract."""
    result = report_client_error(_mk_request_tenant_attr_none(), _payload())
    assert result == {"status": "logged_tenantless"}


def test_client_error_tenant_empty_dict_returns_logged_tenantless():
    """An empty dict tenant (truthy-falsy boundary) must also degrade to
    the tenantless branch — guards against the audit's exact prior
    behavior where ``.get("id", "")`` returned ``""`` and the handler
    still tried to write to the DB with an empty company_id."""
    result = report_client_error(_mk_request_tenant_empty_dict(), _payload())
    assert result == {"status": "logged_tenantless"}


def test_client_error_handler_never_raises_on_bad_payload_shape():
    """Defensive: even if a future schema drift sends fields the handler
    doesn't know about, the tenantless branch must still return cleanly.
    Pydantic enforces the schema at the route layer; this directly
    exercises the function with a minimal payload."""
    minimal = ClientErrorIn(kind="vue_error", detail="x")
    result = report_client_error(_mk_request_no_tenant(), minimal)
    assert result == {"status": "logged_tenantless"}


def test_path_is_tenantless_allowed_not_bypassed():
    """The route MUST be in `_TENANTLESS_ALLOWED_PATHS` (lookup-still-runs,
    proceed-if-unresolved) and MUST NOT be in `_BYPASS_PATHS`
    (short-circuit-before-lookup). The second case would kill the
    ClientError sink on real tenants too — caught in the MH-0 audit
    before ship. This assertion is the load-bearing regression guard."""
    from gdx_dispatch.core.tenant import TenantMiddleware

    assert "/api/feedback/client-error" in TenantMiddleware._TENANTLESS_ALLOWED_PATHS, (
        "tenantless-allowed entry missing"
    )
    assert "/api/feedback/client-error" not in TenantMiddleware._BYPASS_PATHS, (
        "DO NOT add to _BYPASS_PATHS — bypass runs before _lookup_tenant, "
        "so real tenant hosts would also lose their tenant context and "
        "client errors would stop reaching the ClientError table. "
        "Use _TENANTLESS_ALLOWED_PATHS instead."
    )


def test_client_error_writes_to_tenant_db_when_tenant_resolved(monkeypatch):
    """Auditor's gap: zero coverage of the tenant-resolved branch. This
    test mocks the engine + sessionmaker boundary and asserts:
      (a) a ClientError row is created with company_id = tenant id
      (b) db.add + db.commit are called
      (c) the return value is {"status": "logged"} — not the tenantless
          marker.
    """
    from unittest.mock import MagicMock

    captured = {"added": None, "committed": False, "closed": False, "rolled_back": False}

    fake_engine = MagicMock()
    fake_session = MagicMock()
    fake_session.add.side_effect = lambda row: captured.__setitem__("added", row)
    fake_session.commit.side_effect = lambda: captured.__setitem__("committed", True)
    fake_session.close.side_effect = lambda: captured.__setitem__("closed", True)
    fake_session.rollback.side_effect = lambda: captured.__setitem__("rolled_back", True)

    # engine_registry.get_engine returns our fake engine
    from gdx_dispatch.core import tenant as tenant_mod
    monkeypatch.setattr(tenant_mod.engine_registry, "get_engine", lambda *a, **kw: fake_engine)

    # The handler builds a sessionmaker and calls it — patch sessionmaker
    # to return our fake_session factory.
    import gdx_dispatch.routers.bug_reports as bug_reports_mod
    # The route imports sessionmaker locally inside the function, so we
    # patch sqlalchemy.orm directly.
    import sqlalchemy.orm
    monkeypatch.setattr(
        sqlalchemy.orm,
        "sessionmaker",
        lambda **kw: (lambda: fake_session),
    )

    # _decrypt_db_url is a plaintext passthrough — but patch it to skip
    # InvalidToken handling on a stub value.
    from gdx_dispatch.core import database as db_mod
    monkeypatch.setattr(db_mod, "_decrypt_db_url", lambda s: s)

    request = MagicMock(spec=Request)
    request.state = SimpleNamespace(tenant={
        "id": "11111111-1111-1111-1111-111111111111",
        "db_url": "postgresql://x",
        "slug": "gdx",
    })

    result = bug_reports_mod.report_client_error(request, _payload())

    assert result == {"status": "logged"}, "tenant-resolved branch must NOT log_tenantless"
    assert captured["committed"] is True, "must db.commit() the row"
    assert captured["closed"] is True, "must close the session"
    assert captured["rolled_back"] is False, "no rollback on the happy path"
    row = captured["added"]
    assert row is not None, "must db.add() a ClientError row"
    assert row.company_id == "11111111-1111-1111-1111-111111111111"
    assert row.page_url == "/login"
    assert "[window_error]" in row.detail
    assert "ReferenceError" in row.detail


def test_client_error_tenant_resolved_path_rolls_back_on_db_error(monkeypatch):
    """The handler must roll back AND close the session if the commit
    fails, and STILL return 200-shape (we never let a telemetry write
    failure bubble up to the SPA's keepalive fetch and trigger a second
    capture)."""
    from unittest.mock import MagicMock

    state = {"rolled_back": False, "closed": False}

    fake_session = MagicMock()
    fake_session.add.return_value = None
    fake_session.commit.side_effect = RuntimeError("boom")
    fake_session.rollback.side_effect = lambda: state.__setitem__("rolled_back", True)
    fake_session.close.side_effect = lambda: state.__setitem__("closed", True)

    from gdx_dispatch.core import tenant as tenant_mod
    monkeypatch.setattr(tenant_mod.engine_registry, "get_engine", lambda *a, **kw: MagicMock())
    import sqlalchemy.orm
    monkeypatch.setattr(
        sqlalchemy.orm,
        "sessionmaker",
        lambda **kw: (lambda: fake_session),
    )
    from gdx_dispatch.core import database as db_mod
    monkeypatch.setattr(db_mod, "_decrypt_db_url", lambda s: s)

    request = MagicMock(spec=Request)
    request.state = SimpleNamespace(tenant={
        "id": "2222",
        "db_url": "postgresql://x",
        "slug": "gdx",
    })

    import gdx_dispatch.routers.bug_reports as bug_reports_mod
    result = bug_reports_mod.report_client_error(request, _payload())

    # Returns 200-shape, doesn't raise — protects the SPA keepalive contract.
    # Audit round 2: distinct status so a failed tenant write doesn't look
    # identical to a successful one.
    assert result == {"status": "logged_tenant_db_failed"}
    assert state["rolled_back"] is True
    assert state["closed"] is True


def test_middleware_pins_single_tenant_on_state(monkeypatch):
    """Single-tenant collapse (Phase A): the middleware no longer resolves
    a tenant from the host or short-circuits tenantless paths — it pins the
    one GDX tenant on ``request.state.tenant`` for every request. The dict
    is always non-empty (and never ``None``), which keeps downstream
    middlewares that do ``getattr(request.state, "tenant", {}).get(...)``
    working. ``_lookup_tenant`` must never run."""
    import asyncio
    from unittest.mock import MagicMock

    from gdx_dispatch.core.tenant import TenantMiddleware, single_tenant

    monkeypatch.setattr(
        "gdx_dispatch.core.tenant._lookup_tenant",
        lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("single-tenant middleware must not query tenants")
        ),
    )

    mw = TenantMiddleware(app=MagicMock())
    req = MagicMock()
    req.url.path = "/api/feedback/client-error"
    req.method = "POST"
    req.headers = {"host": "gdx_dispatch.example.com"}
    req.state = SimpleNamespace()

    captured = {"state_tenant": "UNSET"}

    async def fake_next(request):
        captured["state_tenant"] = getattr(request.state, "tenant", "MISSING")
        return MagicMock(spec=[], status_code=200)

    asyncio.run(mw.dispatch(req, fake_next))
    assert captured["state_tenant"] == single_tenant()
    assert captured["state_tenant"], "pinned tenant must be a non-empty dict, never None/{}"


def test_middleware_never_402s_trial_in_single_tenant(monkeypatch):
    """The multi-tenant trial-expiry 402 has been removed. Every request —
    on any host — reaches its handler; there is no subscription gate."""
    import asyncio
    from unittest.mock import MagicMock

    from gdx_dispatch.core.tenant import TenantMiddleware

    mw = TenantMiddleware(app=MagicMock())
    req = MagicMock()
    req.url.path = "/api/jobs"
    req.method = "POST"
    req.headers = {"host": "anything.example.com"}
    req.state = SimpleNamespace()

    next_called = {"called": False}

    async def fake_next(_request):
        next_called["called"] = True
        return MagicMock(spec=[], status_code=200)

    asyncio.run(mw.dispatch(req, fake_next))
    assert next_called["called"] is True

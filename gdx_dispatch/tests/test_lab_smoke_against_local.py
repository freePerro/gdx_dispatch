"""Sprint MCP-Streamable-HTTP S6 — self-test the lab smoke script.

Stands up an in-process app that mirrors the lab deploy as closely as
the test harness allows (TenantMiddleware + well_known + oauth2 +
mounted MCP transport with bearer auth) and points the smoke script's
Smoke class at it via httpx.MockTransport. Confirms that every step
in the runbook passes against a known-good deployment.

Catches: routing breakage between the smoke script and the real
endpoints (e.g. a renamed param, a status-code drift) before the
operator runs it against the lab. Doesn't substitute for an actual
lab smoke; it ensures the smoke client itself is correct.
"""
from __future__ import annotations

import contextlib
import uuid
from unittest.mock import patch

import fakeredis
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import gdx_dispatch.core.mcp_tools  # noqa: F401  — side-effect: registers tools
from gdx_dispatch.core import oauth2_grants
from gdx_dispatch.core.mcp_mount import mcp_subapp_lifespan, mount_mcp
from gdx_dispatch.core.tenant import TenantMiddleware
from gdx_dispatch.models.platform_ss20_additions import DevPortalBase
from gdx_dispatch.routers.auth import oauth2 as oauth2_router_mod
from gdx_dispatch.routers.well_known import router as well_known_router
from gdx_dispatch.tools.mcp_lab_smoke import Smoke, run


GDX_HOST = "gdx.lab.example.com"
ACME_HOST = "acme.lab.example.com"
GDX_UUID = uuid.UUID("11111111-1111-1111-1111-111111111111")
ACME_UUID = uuid.UUID("22222222-2222-2222-2222-222222222222")


def _fake_tenant(slug: str | None) -> dict | None:
    if slug == "gdx":
        return {"id": GDX_UUID, "slug": "gdx", "db_url": "sqlite:///:memory:",
                "subscription_status": "active", "db_provisioned": True,
                "trial_ends_at": None}
    if slug == "acme":
        return {"id": ACME_UUID, "slug": "acme", "db_url": "sqlite:///:memory:",
                "subscription_status": "active", "db_provisioned": True,
                "trial_ends_at": None}
    return None


class _Sess:
    def close(self):
        pass


@pytest.fixture(scope="module")
def lab_app():
    """In-process replica of the lab deploy."""
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    DevPortalBase.metadata.create_all(eng)
    SessionLocal = sessionmaker(bind=eng, autoflush=False, autocommit=False)

    @contextlib.asynccontextmanager
    async def lifespan(a: FastAPI):
        async with mcp_subapp_lifespan(a):
            yield

    app = FastAPI(lifespan=lifespan)
    app.add_middleware(TenantMiddleware, control_session_factory=_Sess)
    app.include_router(well_known_router)
    app.include_router(oauth2_router_mod.router)
    mount_mcp(app)

    def _get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[oauth2_router_mod.get_db] = _get_db
    code_redis = fakeredis.FakeRedis(decode_responses=True)
    token_redis = fakeredis.FakeRedis(decode_responses=True)
    oauth2_grants.set_code_store_for_tests(oauth2_grants._RedisCodeStore(code_redis))
    oauth2_router_mod.set_token_store_for_tests(oauth2_router_mod._RedisTokenStore(token_redis))

    def _resolver(db, *, slug=None, tenant_id=None):
        return _fake_tenant(slug)

    with patch("gdx_dispatch.core.tenant._lookup_tenant", side_effect=_resolver):
        yield app

    oauth2_grants.set_code_store_for_tests(None)
    oauth2_router_mod.set_token_store_for_tests(None)
    eng.dispose()


@pytest.fixture
def smoke_against(lab_app, monkeypatch):
    """Patch httpx.Client to route through the lab_app via ASGITransport."""
    from fastapi.testclient import TestClient

    # `with TestClient(...)` triggers the parent app's lifespan, which
    # in turn enters FastMCP's session-manager task group. Skip the
    # context manager and /mcp 500s with "Task group is not initialized".
    tc_cm = TestClient(lab_app)
    tc = tc_cm.__enter__()

    class _ProxyClient:
        """Forward httpx.Client API to the underlying TestClient."""
        def __init__(self, *args, headers=None, follow_redirects=False, timeout=None):
            self._tc = tc
            self._default_headers = dict(headers or {})
            self._follow = follow_redirects

        def get(self, url, **kw):
            h = {**self._default_headers, **(kw.pop("headers", None) or {})}
            return self._tc.get(_strip(url), headers=h,
                                follow_redirects=self._follow, **kw)

        def post(self, url, **kw):
            h = {**self._default_headers, **(kw.pop("headers", None) or {})}
            return self._tc.post(_strip(url), headers=h,
                                 follow_redirects=self._follow, **kw)

        def close(self):
            pass  # TestClient lives for the fixture's scope

    def _strip(url: str) -> str:
        # Smoke calls `f"{base}/path"`; TestClient just needs the path.
        if "://" in url:
            return "/" + url.split("/", 3)[3] if url.count("/") >= 3 else "/"
        return url

    monkeypatch.setattr("gdx_dispatch.tools.mcp_lab_smoke.httpx.Client", _ProxyClient)
    yield tc
    tc_cm.__exit__(None, None, None)


# ── tests ───────────────────────────────────────────────────────────────────


def test_smoke_full_chain_passes_against_in_process_lab(smoke_against):
    """Every required step (D1..M3) should pass against a known-good
    deployment. Optional X1 cross-tenant denial also verified."""
    import argparse
    args = argparse.Namespace(
        host=GDX_HOST,
        scheme="http",  # TestClient is plain HTTP
        redirect_uri="https://example.invalid/cb",
        call_tool=None,
        call_args=None,
        cross_host=ACME_HOST,
    )
    rc = run(args)
    # When all required + X1 pass, exit code is 0.
    assert rc == 0, "smoke script reported failure against known-good lab"


def test_smoke_fails_loud_when_well_known_404(smoke_against, lab_app):
    """If the AS metadata is missing, D2 must report it and the script
    must exit non-zero — never fall through silently."""
    # Override the well_known endpoint to 404 to simulate a misdeploy.
    import argparse
    from fastapi import APIRouter
    bad = APIRouter()
    @bad.get("/.well-known/oauth-authorization-server")
    def _fail():
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="not deployed")

    # Surgically replace the existing route — fastest way is to
    # prepend, since FastAPI matches in registration order.
    lab_app.router.routes.insert(0, bad.routes[0])
    try:
        args = argparse.Namespace(
            host=GDX_HOST, scheme="http",
            redirect_uri="https://example.invalid/cb",
            call_tool=None, call_args=None, cross_host=None,
        )
        rc = run(args)
        assert rc == 1, "smoke script must exit non-zero on missing AS metadata"
    finally:
        lab_app.router.routes.pop(0)

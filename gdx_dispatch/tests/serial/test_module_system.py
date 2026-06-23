from __future__ import annotations

from collections.abc import Generator

import pytest
from conftest import make_fresh_db
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.tenant import engine_registry
from gdx_dispatch.modules.inventory.router import router as inventory_router
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.settings import router as settings_router


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    engine = make_fresh_db()
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def _override_db() -> Generator[Session, None, None]:
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    async def _tenant_context(request: Request, call_next):
        request.state.tenant = {
            "id": "tenant-module-system",
            "slug": "tenant-module-system",
            "subscription_status": "trialing",
        }
        # Populate request.state.user so require_role() role gates pass in
        # tests. The dependency_overrides below substitute a value for the
        # get_current_user Depends, but require_role reads directly from
        # request.state and doesn't see those overrides.
        request.state.user = {"id": "admin-1", "role": "admin"}
        return await call_next(request)

    app = FastAPI()
    app.middleware("http")(_tenant_context)
    app.include_router(inventory_router)
    app.include_router(settings_router)
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = lambda: {"id": "admin-1", "role": "admin"}

    tc = TestClient(app, raise_server_exceptions=True)
    try:
        yield tc
    finally:
        app.dependency_overrides.clear()
        engine.dispose()
        engine_registry.dispose_all()


def test_module_gating(client: TestClient):
    # Single-tenant: the owner owns the whole install, so every module is
    # seeded enabled on first access (see core.modules._seed_default_modules).
    # The first request seeds + passes the gate.
    assert client.get("/api/inventory/parts").status_code == 200

    # The require_module gate 403s only once an admin explicitly disables the
    # module — that's the gating behavior this test pins.
    disable = client.post("/api/settings/modules/inventory/disable")
    assert disable.status_code == 200, disable.text

    response = client.get("/api/inventory/parts")
    assert response.status_code == 403
    assert "not enabled" in response.text


def test_module_enable(client: TestClient):
    enable_response = client.post(
        "/api/settings/modules/inventory/enable",
        headers={"x-tenant-tier": "professional"},
    )
    assert enable_response.status_code == 200, enable_response.text

    response = client.get("/api/inventory/parts")
    assert response.status_code == 200, response.text
    assert isinstance(response.json(), list)



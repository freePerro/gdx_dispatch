"""Tier 7 contract-gap fixes — gating alignment (backend half).

The frontend half is nav alignment in constants/modules.js; the backend
half tested here is the seeder unification: the /api/settings/modules
first-GET bootstrap used to seed only `default: True` modules while
core/modules seeds EVERY module (the single-tenant decision). Whichever
seeder a fresh tenant hit first decided which modules existed — an empty
grants table + this GET first meant google_maps/reports_advanced/
equipment_tracking landed explicitly disabled and the nav hid them.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.modules import MODULES
from gdx_dispatch.models import tenant_models  # noqa: F401

TENANT = "tenant-test"


def test_modules_get_seeds_every_module_on_fresh_tenant():
    from gdx_dispatch.core.auth import get_current_user as core_gcu
    from gdx_dispatch.core.database import get_db as core_get_db
    from gdx_dispatch.routers import branding_public
    from gdx_dispatch.routers.auth import get_current_user as routers_gcu

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    app = FastAPI()

    @app.middleware("http")
    async def _tenant_shim(request, call_next):
        request.state.tenant = {"id": TENANT, "slug": TENANT}
        return await call_next(request)

    app.include_router(branding_public.router)

    def _override_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    user = {"sub": "u-1", "role": "admin", "tenant_id": TENANT}
    app.dependency_overrides[core_get_db] = _override_db
    app.dependency_overrides[routers_gcu] = lambda: user
    app.dependency_overrides[core_gcu] = lambda: user

    try:
        client = TestClient(app)
        body = client.get("/api/settings/modules").json()
        by_key = {m["key"]: m for m in body["modules"]}
        # EVERY module gets a grant row — including the professional-tier
        # non-defaults the old bootstrap skipped. A skipped module reads as
        # an EXPLICIT enabled:false, which beats the frontend's
        # undefined→enabled fallback and hides its nav.
        for key in ("google_maps", "reports_advanced", "equipment_tracking", "jobs"):
            assert key in by_key, key
            assert by_key[key]["enabled"] is True, f"{key} not seeded enabled"
        assert len(by_key) == len(MODULES)
    finally:
        engine.dispose()

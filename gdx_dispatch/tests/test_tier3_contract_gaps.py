"""Tier 3 contract-gap fixes — data captured but never shown
(docs/design/backend-vue-contract-gaps-2026-07-24.md).

The frontend halves are badges/columns/toasts; the backend half tested here
is the one wire change: /api/jobs list rows now carry is_return_visit, so
the dispatch board and jobs list can finally tell a warranty return trip
from fresh work.
"""
from __future__ import annotations

from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models import tenant_models  # noqa: F401 — registers tables
from gdx_dispatch.models.tenant_models import Job

TENANT = "tenant-test"


def test_jobs_list_carries_is_return_visit():
    from gdx_dispatch.core.auth import get_current_user as core_gcu
    from gdx_dispatch.core.database import get_db as core_get_db
    from gdx_dispatch.routers import jobs as jobs_router
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

    app.include_router(jobs_router.router)

    def _override_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    user = {"sub": "u-1", "user_id": "u-1", "role": "admin", "tenant_id": TENANT}
    app.dependency_overrides[core_get_db] = _override_db
    app.dependency_overrides[routers_gcu] = lambda: user
    app.dependency_overrides[core_gcu] = lambda: user

    try:
        db = SessionLocal()
        db.add(Job(id=uuid4(), company_id=TENANT, title="fresh work", status="Scheduled"))
        db.add(
            Job(
                id=uuid4(),
                company_id=TENANT,
                title="warranty return trip",
                status="Scheduled",
                is_return_visit=True,
            )
        )
        db.commit()
        db.close()

        client = TestClient(app)
        items = client.get("/api/jobs").json()["items"]
        by_title = {j["title"]: j for j in items}
        assert bool(by_title["warranty return trip"]["is_return_visit"]) is True
        assert bool(by_title["fresh work"]["is_return_visit"]) is False
    finally:
        engine.dispose()

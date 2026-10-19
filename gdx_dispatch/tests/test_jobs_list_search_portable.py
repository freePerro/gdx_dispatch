"""GET /api/jobs?search= matches title and job number case-insensitively on
SQLite as well as Postgres (GDXA-1647: the raw-SQL filter used ILIKE, which
SQLite does not have)."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from conftest import make_fresh_db
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import Job
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.jobs import router as jobs_router

TENANT_ID = "00000000-0000-4000-8000-0000000000ad"


@pytest.fixture
def harness() -> tuple[TestClient, sessionmaker]:
    engine = make_fresh_db()
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    setup = Session()
    setup.execute(
        text(
            "INSERT OR IGNORE INTO company_module_grants "
            "(id, company_id, module_key, granted_at, created_at) "
            "VALUES (:id, :tid, 'jobs', datetime('now'), datetime('now'))"
        ),
        {"id": f"grant-{TENANT_ID}", "tid": TENANT_ID},
    )
    now = datetime.now(UTC)
    for title, number in (("Spring Replacement", "JOB-2026-014"), ("Opener install", "JOB-2026-015")):
        setup.add(Job(
            id=uuid.uuid4(), title=title, job_number=number, company_id=TENANT_ID,
            lifecycle_stage="scheduled", is_return_visit=False, created_at=now, updated_at=now,
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
    async def _inject_tenant(request, call_next):
        request.state.tenant = {"id": TENANT_ID}
        request.state.request_id = "jobs-search-test"
        return await call_next(request)

    app.include_router(jobs_router)
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "user-search", "sub": "user-search", "role": "admin", "tenant_id": TENANT_ID,
    }
    return TestClient(app, raise_server_exceptions=True), Session


def _titles(client: TestClient, search: str) -> list[str]:
    r = client.get("/api/jobs", params={"search": search, "per_page": 50})
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    rows = body if isinstance(body, list) else (body.get("items") or body.get("jobs") or body.get("data") or [])
    return sorted(j["title"] for j in rows)


@pytest.mark.parametrize("search", ["spring", "SPRING", "rEpLaCe"])
def test_search_matches_title_in_any_case(harness, search):
    client, _ = harness
    assert _titles(client, search) == ["Spring Replacement"]


def test_search_matches_job_number_in_any_case(harness):
    client, _ = harness
    assert _titles(client, "job-2026-015") == ["Opener install"]


def test_search_with_no_match_is_empty(harness):
    client, _ = harness
    assert _titles(client, "garage") == []

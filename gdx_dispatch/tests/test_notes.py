"""Tests for the notes router (job notes + sticky notes)."""
from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models import tenant_models  # noqa: F401  (register models on TenantBase.metadata)
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.notes import router


def _make_client(
    tenant_id: str = "tenant-test",
    user_sub: str = "user-1",
    user_role: str = "dispatcher",
    engine=None,
) -> TestClient:
    if engine is None:
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    TenantBase.metadata.create_all(engine, checkfirst=True)

    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    setup = Session()
    setup.execute(
        text(
            """
            INSERT OR IGNORE INTO company_module_grants (id, company_id, module_key, granted_at, created_at)
            VALUES (:id, :tid, 'jobs', datetime('now'), datetime('now'))
            """
        ),
        {"id": f"g2-{tenant_id}", "tid": tenant_id},
    )
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
        request.state.tenant = {"id": tenant_id}
        return await call_next(request)

    app.include_router(router)
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": user_sub,
        "sub": user_sub,
        "role": user_role,
        "tenant_id": tenant_id,
        "email": f"{user_sub}@example.com",
    }

    tc = TestClient(app, raise_server_exceptions=True)
    tc._engine = engine  # type: ignore[attr-defined]
    return tc


@pytest.fixture()
def client():
    tc = _make_client()
    yield tc
    tc.app.dependency_overrides.clear()
    tc._engine.dispose()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Job note tests
# ---------------------------------------------------------------------------


def test_create_job_note(client: TestClient):
    job_id = str(uuid4())
    r = client.post(
        f"/api/jobs/{job_id}/notes",
        json={"body": "Door spring replaced on arrival", "visibility": "internal"},
    )
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["id"]
    assert data["job_id"] == job_id
    assert data["body"] == "Door spring replaced on arrival"
    assert data["visibility"] == "internal"
    assert data["author_id"] == "user-1"
    assert data["company_id"] == "tenant-test"


def test_list_job_notes_tenant_scoped():
    c1 = _make_client(tenant_id="tenant-a", user_sub="ua")
    c2 = _make_client(tenant_id="tenant-b", user_sub="ub")
    try:
        job_id = str(uuid4())
        r1 = c1.post(
            f"/api/jobs/{job_id}/notes", json={"body": "A note", "visibility": "internal"}
        )
        assert r1.status_code == 201
        r2 = c2.post(
            f"/api/jobs/{job_id}/notes", json={"body": "B note", "visibility": "internal"}
        )
        assert r2.status_code == 201

        list1 = c1.get(f"/api/jobs/{job_id}/notes").json()
        list2 = c2.get(f"/api/jobs/{job_id}/notes").json()
        assert len(list1) == 1 and list1[0]["body"] == "A note"
        assert len(list2) == 1 and list2[0]["body"] == "B note"
    finally:
        c1.app.dependency_overrides.clear()
        c2.app.dependency_overrides.clear()
        c1._engine.dispose()  # type: ignore[attr-defined]
        c2._engine.dispose()  # type: ignore[attr-defined]


def test_author_can_edit_own_note(client: TestClient):
    job_id = str(uuid4())
    created = client.post(
        f"/api/jobs/{job_id}/notes",
        json={"body": "first draft", "visibility": "internal"},
    ).json()
    r = client.patch(
        f"/api/jobs/{job_id}/notes/{created['id']}",
        json={"body": "edited by author", "visibility": "external"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["body"] == "edited by author"
    assert data["visibility"] == "external"


def test_non_author_cannot_edit_others_note():
    shared = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # Author creates a note
    author = _make_client(
        tenant_id="tenant-x", user_sub="author-1", user_role="dispatcher", engine=shared
    )
    # Different non-admin tries to edit (same DB)
    other = _make_client(
        tenant_id="tenant-x", user_sub="other-1", user_role="dispatcher", engine=shared
    )
    try:
        job_id = str(uuid4())
        created = author.post(
            f"/api/jobs/{job_id}/notes",
            json={"body": "private note", "visibility": "internal"},
        ).json()
        r = other.patch(
            f"/api/jobs/{job_id}/notes/{created['id']}",
            json={"body": "hijack"},
        )
        assert r.status_code == 403, r.text

        d = other.delete(f"/api/jobs/{job_id}/notes/{created['id']}")
        assert d.status_code == 403
    finally:
        author.app.dependency_overrides.clear()
        other.app.dependency_overrides.clear()
        author._engine.dispose()  # type: ignore[attr-defined]
        other._engine.dispose()  # type: ignore[attr-defined]


def test_admin_can_edit_any_note():
    shared = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    author = _make_client(
        tenant_id="tenant-y", user_sub="author-2", user_role="dispatcher", engine=shared
    )
    admin = _make_client(
        tenant_id="tenant-y", user_sub="admin-1", user_role="admin", engine=shared
    )
    try:
        job_id = str(uuid4())
        created = author.post(
            f"/api/jobs/{job_id}/notes",
            json={"body": "tech note", "visibility": "internal"},
        ).json()
        r = admin.patch(
            f"/api/jobs/{job_id}/notes/{created['id']}",
            json={"body": "admin override"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["body"] == "admin override"
    finally:
        author.app.dependency_overrides.clear()
        admin.app.dependency_overrides.clear()
        author._engine.dispose()  # type: ignore[attr-defined]
        admin._engine.dispose()  # type: ignore[attr-defined]


def test_job_note_id_columns_are_string_not_uuid():
    """Guard the Flask-era schema match.

    Live prod `job_notes` has `id` and `job_id` as TEXT (legacy schema). The ORM
    must match — declaring them as Uuid re-introduces the
    `operator does not exist: text = uuid` 500 on GET that B2 audit found.
    SQLite-backed tests don't catch this (no real type system), so assert the
    declared column types directly.
    """
    from sqlalchemy import String

    from gdx_dispatch.models.tenant_models import JobNote

    for col_name in ("id", "job_id"):
        col = JobNote.__table__.c[col_name]
        assert isinstance(col.type, String), (
            f"JobNote.{col_name} must be String to match live text schema, "
            f"got {type(col.type).__name__}"
        )

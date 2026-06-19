from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock
from uuid import uuid4

import pytest


def _mock_request(tenant_id="test-tenant"):
    r = MagicMock()
    r.state.tenant = {"id": tenant_id}
    r.client.host = "127.0.0.1"
    return r

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.warranties import (
    create_warranty,
    delete_warranty,
    expiring_warranties,
    file_warranty_claim,
    get_warranty,
    list_warranties,
    router,
    update_warranty,
)


@pytest.fixture
def tenant_db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()

    db.execute(
        text(
            """
            CREATE TABLE warranties (
                id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                customer_id TEXT NOT NULL,
                description TEXT NOT NULL,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                terms TEXT,
                status TEXT NOT NULL,
                claim_count INTEGER NOT NULL DEFAULT 0,
                last_claim_at TEXT,
                last_claim_notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT,
                deleted_at TEXT
            )
            """
        )
    )
    db.commit()

    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def _seed_warranty(
    db,
    *,
    job_id: str | None = None,
    customer_id: str | None = None,
    description: str = "Standard labor warranty",
    start_date: date | None = None,
    end_date: date | None = None,
    status: str = "active",
    deleted: bool = False,
    claim_count: int = 0,
) -> str:
    warranty_id = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        text(
            """
            INSERT INTO warranties (
                id, job_id, customer_id, description, start_date, end_date,
                terms, status, claim_count, created_at, updated_at, deleted_at
            ) VALUES (
                :id, :job_id, :customer_id, :description, :start_date, :end_date,
                :terms, :status, :claim_count, :created_at, NULL, :deleted_at
            )
            """
        ),
        {
            "id": warranty_id,
            "job_id": job_id or str(uuid4()),
            "customer_id": customer_id or str(uuid4()),
            "description": description,
            "start_date": (start_date or date.today()).isoformat(),
            "end_date": (end_date or (date.today() + timedelta(days=365))).isoformat(),
            "terms": "Standard terms",
            "status": status,
            "claim_count": claim_count,
            "created_at": now,
            "deleted_at": now if deleted else None,
        },
    )
    db.commit()
    return warranty_id


def test_all_warranty_routes_require_auth_dependency():
    guarded_paths = set()
    for route in router.routes:
        if not hasattr(route, "dependant"):
            continue
        for dep in route.dependant.dependencies:
            if dep.call is get_current_user:
                guarded_paths.add(route.path)
                break

    assert "/api/warranties" in guarded_paths
    assert "/api/warranties/expiring" in guarded_paths
    assert "/api/warranties/{warranty_id}" in guarded_paths
    assert "/api/warranties/{warranty_id}/claim" in guarded_paths


def test_create_and_get_warranty(tenant_db_session):
    payload = {
        "job_id": str(uuid4()),
        "customer_id": str(uuid4()),
        "description": "Opener repair warranty",
        "start_date": "2026-04-01",
        "end_date": "2027-04-01",
        "terms": "Parts and labor",
    }
    created = create_warranty(request=_mock_request(), payload=payload, user={}, db=tenant_db_session)

    assert created["id"]
    assert created["status"] == "active"
    assert created["claim_count"] == 0
    assert created["description"] == payload["description"]

    detail = get_warranty(warranty_id=created["id"], _={}, db=tenant_db_session)
    assert detail["id"] == created["id"]
    assert detail["job_id"] == payload["job_id"]


def test_create_warranty_rejects_missing_required_fields(tenant_db_session):
    with pytest.raises(Exception) as exc:
        create_warranty(request=_mock_request(), payload={"description": "Incomplete"}, user={}, db=tenant_db_session)
    assert getattr(exc.value, "status_code", None) == 422


def test_list_warranties_excludes_soft_deleted(tenant_db_session):
    active_id = _seed_warranty(tenant_db_session, description="Active")
    _seed_warranty(tenant_db_session, description="Deleted", deleted=True)

    rows = list_warranties(_={}, db=tenant_db_session)
    ids = {row["id"] for row in rows}

    assert active_id in ids
    assert len(ids) == 1


def test_patch_warranty_updates_fields(tenant_db_session):
    warranty_id = _seed_warranty(tenant_db_session, description="Before patch")

    body = update_warranty(
        warranty_id=warranty_id,
        request=_mock_request(),
        payload={
            "description": "After patch",
            "terms": "Updated terms",
            "end_date": (date.today() + timedelta(days=200)).isoformat(),
            "status": "voided",
        },
        user={},
        db=tenant_db_session,
    )

    assert body["description"] == "After patch"
    assert body["terms"] == "Updated terms"
    assert body["status"] == "voided"


def test_delete_warranty_soft_deletes(tenant_db_session):
    warranty_id = _seed_warranty(tenant_db_session)

    response = delete_warranty(warranty_id=warranty_id, request=_mock_request(), user={}, db=tenant_db_session)
    assert response.status_code == 204

    rows = list_warranties(_={}, db=tenant_db_session)
    assert all(item["id"] != warranty_id for item in rows)

    with pytest.raises(Exception) as exc:
        get_warranty(warranty_id=warranty_id, _={}, db=tenant_db_session)
    assert getattr(exc.value, "status_code", None) == 404


def test_get_warranty_not_found(tenant_db_session):
    with pytest.raises(Exception) as exc:
        get_warranty(warranty_id=str(uuid4()), _={}, db=tenant_db_session)
    assert getattr(exc.value, "status_code", None) == 404


def test_expiring_warranties_returns_only_next_30_days(tenant_db_session):
    soon_id = _seed_warranty(
        tenant_db_session,
        end_date=date.today() + timedelta(days=10),
        status="active",
    )
    _seed_warranty(
        tenant_db_session,
        end_date=date.today() + timedelta(days=45),
        status="active",
    )
    _seed_warranty(
        tenant_db_session,
        end_date=date.today() - timedelta(days=1),
        status="active",
    )
    _seed_warranty(
        tenant_db_session,
        end_date=date.today() + timedelta(days=15),
        status="voided",
    )

    body = expiring_warranties(_={}, db=tenant_db_session)

    assert body["count"] == 1
    assert len(body["data"]) == 1
    assert body["data"][0]["id"] == soon_id


def test_claim_warranty_increments_count_and_sets_claimed(tenant_db_session):
    warranty_id = _seed_warranty(tenant_db_session, claim_count=0, status="active")

    body = file_warranty_claim(
        warranty_id=warranty_id,
        request=_mock_request(),
        payload={"notes": "Spring failed under normal use"},
        user={},
        db=tenant_db_session,
    )

    assert body["id"] == warranty_id
    assert body["claim_count"] == 1
    assert body["status"] == "claimed"
    assert body["last_claim_notes"] == "Spring failed under normal use"
    assert body["last_claim_at"] is not None


def test_claim_warranty_not_found(tenant_db_session):
    with pytest.raises(Exception) as exc:
        file_warranty_claim(warranty_id=str(uuid4()), request=_mock_request(), payload={"notes": "Any"}, user={}, db=tenant_db_session)
    assert getattr(exc.value, "status_code", None) == 404

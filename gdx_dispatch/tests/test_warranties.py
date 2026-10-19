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

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import AuditLog
from gdx_dispatch.core.pay_periods import shop_today_from_settings
from gdx_dispatch.models.tenant_models import AppSettings, Warranty
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

    # The schema comes from the ORM, not hand-written DDL: the old DDL let
    # updated_at be NULL where the model says NOT NULL (GDXA-1629).
    Warranty.__table__.create(bind=engine)
    # The expiring read takes "today" from AppSettings.timezone (GDXA-509);
    # with no row it falls back to the default zone.
    AppSettings.__table__.create(bind=engine)
    AuditLog.__table__.create(bind=engine)
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
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    db.add(Warranty(
        id=warranty_id,
        job_id=job_id or str(uuid4()),
        customer_id=customer_id or str(uuid4()),
        description=description,
        start_date=start_date or date.today(),
        end_date=end_date or (date.today() + timedelta(days=365)),
        terms="Standard terms",
        status=status,
        claim_count=claim_count,
        created_at=now,
        updated_at=now,
        deleted_at=now if deleted else None,
    ))
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
    created = create_warranty(request=_mock_request(), payload=payload, user={"sub": "office-1"},
                              db=tenant_db_session)

    assert created["id"]
    assert created["status"] == "active"
    assert created["claim_count"] == 0
    assert created["description"] == payload["description"]

    detail = get_warranty(warranty_id=created["id"], _={}, db=tenant_db_session)
    assert detail["id"] == created["id"]
    assert detail["job_id"] == payload["job_id"]

    (row,) = _audit_rows(tenant_db_session, "warranty_created", created["id"])
    assert row.user_id == "office-1"
    assert row.entity_type == "warranty"
    assert row.details["job_id"] == payload["job_id"]
    assert row.details["customer_id"] == payload["customer_id"]


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

    response = delete_warranty(warranty_id=warranty_id, request=_mock_request(), user={"sub": "office-2"},
                               db=tenant_db_session)
    assert response.status_code == 204

    (row,) = _audit_rows(tenant_db_session, "warranty_deleted", warranty_id)
    assert row.user_id == "office-2"
    assert row.entity_type == "warranty"
    stored = tenant_db_session.get(Warranty, warranty_id)
    assert stored is not None and stored.deleted_at is not None  # soft, not hard

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
    today = shop_today_from_settings(tenant_db_session)
    soon_id = _seed_warranty(
        tenant_db_session,
        end_date=today + timedelta(days=10),
        status="active",
    )
    _seed_warranty(
        tenant_db_session,
        end_date=today + timedelta(days=45),
        status="active",
    )
    _seed_warranty(
        tenant_db_session,
        end_date=today - timedelta(days=1),
        status="active",
    )
    _seed_warranty(
        tenant_db_session,
        end_date=today + timedelta(days=15),
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
        user={"sub": "office-3"},
        db=tenant_db_session,
    )

    assert body["id"] == warranty_id
    assert body["claim_count"] == 1
    assert body["status"] == "claimed"
    assert body["last_claim_notes"] == "Spring failed under normal use"
    assert body["last_claim_at"] is not None

    (row,) = _audit_rows(tenant_db_session, "warranty_claim_filed", warranty_id)
    assert row.user_id == "office-3"
    assert row.entity_type == "warranty"
    assert row.details["notes"] == "Spring failed under normal use"


def test_claim_warranty_not_found(tenant_db_session):
    with pytest.raises(Exception) as exc:
        file_warranty_claim(warranty_id=str(uuid4()), request=_mock_request(), payload={"notes": "Any"}, user={}, db=tenant_db_session)
    assert getattr(exc.value, "status_code", None) == 404


# GDXA-997: the update audit row records what CHANGED, not what was sent.

def _audit_rows(db, action: str, warranty_id: str) -> list[AuditLog]:
    return db.execute(
        select(AuditLog)
        .where(AuditLog.action == action, AuditLog.entity_id == warranty_id)
        .order_by(AuditLog.created_at, AuditLog.id)
    ).scalars().all()


def _warranty_audit_rows(db, warranty_id: str) -> list[dict]:
    return [r.details for r in _audit_rows(db, "warranty_updated", warranty_id)]


def _stored_updated_at(db, warranty_id: str):
    return db.execute(text("SELECT updated_at FROM warranties WHERE id = :id"), {"id": warranty_id}).scalar_one()


def test_patch_audit_row_records_old_new_of_changed_fields_only(tenant_db_session):
    warranty_id = _seed_warranty(tenant_db_session, description="Spring warranty", status="active")

    update_warranty(
        warranty_id=warranty_id,
        request=_mock_request(),
        # description resent unchanged, status changed, two keys the handler ignores
        payload={"description": "Spring warranty", "status": "voided", "claim_count": 9, "bogus": 1},
        user={"sub": "office-1"},
        db=tenant_db_session,
    )

    rows = _warranty_audit_rows(tenant_db_session, warranty_id)
    assert len(rows) == 1
    assert rows[0]["fields_updated"] == ["status"]
    assert rows[0]["changes"] == {"status": {"old": "active", "new": "voided"}}


def test_patch_audit_row_carries_date_and_terms_old_new(tenant_db_session):
    start = date(2026, 1, 1)
    warranty_id = _seed_warranty(tenant_db_session, start_date=start, end_date=date(2027, 1, 1))

    update_warranty(
        warranty_id=warranty_id,
        request=_mock_request(),
        payload={"start_date": "2026-01-01", "end_date": "2027-06-30", "terms": "Parts only"},
        user={},
        db=tenant_db_session,
    )

    (row,) = _warranty_audit_rows(tenant_db_session, warranty_id)
    assert row["changes"] == {
        "end_date": {"old": "2027-01-01", "new": "2027-06-30"},
        "terms": {"text_changed": True},  # free text is named, never copied into the feed
    }


def test_same_value_patch_writes_no_audit_row_and_keeps_updated_at(tenant_db_session):
    warranty_id = _seed_warranty(tenant_db_session, description="Opener warranty", status="active")
    before = get_warranty(warranty_id=warranty_id, _={}, db=tenant_db_session)
    stored_before = _stored_updated_at(tenant_db_session, warranty_id)

    body = update_warranty(
        warranty_id=warranty_id,
        request=_mock_request(),
        payload={
            "description": "  Opener warranty  ",
            "status": "active",
            "terms": before["terms"],
            "start_date": before["start_date"],
            "end_date": before["end_date"],
            "claim_count": 4,
        },
        user={},
        db=tenant_db_session,
    )

    assert body == before
    assert _warranty_audit_rows(tenant_db_session, warranty_id) == []
    assert stored_before is not None
    assert _stored_updated_at(tenant_db_session, warranty_id) == stored_before  # never bumped


def test_patch_with_only_unknown_keys_is_still_400(tenant_db_session):
    warranty_id = _seed_warranty(tenant_db_session)
    with pytest.raises(Exception) as exc:
        update_warranty(warranty_id=warranty_id, request=_mock_request(), payload={"claim_count": 3},
                        user={}, db=tenant_db_session)
    assert getattr(exc.value, "status_code", None) == 400


def test_patch_non_string_status_is_422_over_http_not_500(tenant_db_session):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from gdx_dispatch.core.database import get_db

    app = FastAPI()
    app.include_router(router)
    for dep in router.dependencies:  # require_module("warranties")
        app.dependency_overrides[dep.dependency] = lambda: None
    app.dependency_overrides[get_db] = lambda: tenant_db_session
    app.dependency_overrides[get_current_user] = lambda: {"sub": "office-1"}
    warranty_id = _seed_warranty(tenant_db_session, status="active")

    client = TestClient(app, raise_server_exceptions=False)
    for bad in ([], ["active"], {"s": 1}, 1, None):
        r = client.patch(f"/api/warranties/{warranty_id}", json={"status": bad})
        assert r.status_code == 422, (bad, r.status_code, r.text)

    ok = client.patch(f"/api/warranties/{warranty_id}", json={"status": "claimed"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "claimed"

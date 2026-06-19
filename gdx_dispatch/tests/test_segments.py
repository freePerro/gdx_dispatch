from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest


def _mock_request(tenant_id="test-tenant"):
    r = MagicMock()
    r.state.tenant = {"id": tenant_id}
    r.client.host = "127.0.0.1"
    return r

from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.auth import get_current_user
from gdx_dispatch.routers import segments as segments_router
from gdx_dispatch.routers.segments import (
    SegmentCreateIn,
    create_segment,
    delete_segment,
    get_segment,
    list_segment_customers,
    list_segments,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def tenant_db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    setup_db = Session()

    setup_db.execute(
        text(
            """
            CREATE TABLE customers (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT,
                phone TEXT,
                address TEXT,
                company_id TEXT,
                created_at TEXT NOT NULL,
                deleted_at TEXT
            )
            """
        )
    )
    setup_db.execute(
        text(
            """
            CREATE TABLE jobs (
                id TEXT PRIMARY KEY,
                customer_id TEXT NOT NULL,
                company_id TEXT,
                created_at TEXT NOT NULL,
                deleted_at TEXT
            )
            """
        )
    )
    setup_db.execute(
        text(
            """
            CREATE TABLE invoices (
                id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                total NUMERIC NOT NULL,
                company_id TEXT,
                deleted_at TEXT
            )
            """
        )
    )
    setup_db.execute(
        text(
            """
            CREATE TABLE segments (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                rules JSON NOT NULL,
                created_at TEXT NOT NULL,
                deleted_at TEXT
            )
            """
        )
    )
    setup_db.commit()
    setup_db.close()

    try:
        yield Session
    finally:
        engine.dispose()


def _iso_days_ago(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


def _seed_customer(Session, *, name: str, created_days_ago: int) -> str:
    cid = str(uuid.uuid4())
    db = Session()
    db.execute(
        text(
            """
            INSERT INTO customers (id, name, email, phone, address, company_id, created_at, deleted_at)
            VALUES (:id, :name, :email, :phone, :address, 'tenant-test', :created_at, NULL)
            """
        ),
        {
            "id": cid,
            "name": name,
            "email": f"{name.lower().replace(' ', '.')}@example.com",
            "phone": "555-0000",
            "address": "100 Main",
            "created_at": _iso_days_ago(created_days_ago),
        },
    )
    db.commit()
    db.close()
    return cid


def _seed_job(Session, *, customer_id: str, days_ago: int) -> str:
    jid = str(uuid.uuid4())
    db = Session()
    db.execute(
        text(
            """
            INSERT INTO jobs (id, customer_id, company_id, created_at, deleted_at)
            VALUES (:id, :customer_id, 'tenant-test', :created_at, NULL)
            """
        ),
        {"id": jid, "customer_id": customer_id, "created_at": _iso_days_ago(days_ago)},
    )
    db.commit()
    db.close()
    return jid


def _seed_invoice(Session, *, job_id: str, total: float) -> str:
    iid = str(uuid.uuid4())
    db = Session()
    db.execute(
        text(
            """
            INSERT INTO invoices (id, job_id, total, company_id, deleted_at)
            VALUES (:id, :job_id, :total, 'tenant-test', NULL)
            """
        ),
        {"id": iid, "job_id": job_id, "total": total},
    )
    db.commit()
    db.close()
    return iid


def test_all_segment_routes_require_auth_dependency():
    guarded_paths = set()
    for route in segments_router.router.routes:
        if not hasattr(route, "dependant"):
            continue
        for dep in route.dependant.dependencies:
            if dep.call is get_current_user:
                guarded_paths.add(route.path)
                break

    assert "/api/segments" in guarded_paths
    assert "/api/segments/{segment_id}" in guarded_paths
    assert "/api/segments/{segment_id}/customers" in guarded_paths


async def test_list_segments_includes_builtins(tenant_db_session):
    db = tenant_db_session()
    out = await list_segments(_={}, db=db)
    db.close()
    ids = {seg.id for seg in out.items}
    assert {"at-risk", "high-value", "new", "inactive"} <= ids


async def test_create_custom_segment_and_list(tenant_db_session):
    db = tenant_db_session()
    payload = SegmentCreateIn(
        name="Dormant 90d",
        rules={"field": "last_job_date", "operator": "older_than", "value": "90 days"},
    )
    created = await create_segment(payload=payload, request=_mock_request(), user={}, db=db)
    assert created.name == "Dormant 90d"
    assert created.rules["field"] == "last_job_date"
    assert created.is_builtin is False

    listed = await list_segments(_={}, db=db)
    custom = [x for x in listed.items if x.name == "Dormant 90d"]
    assert len(custom) == 1
    db.close()


def test_create_segment_requires_name_and_rules():
    with pytest.raises(Exception):
        SegmentCreateIn(name="", rules=None)


async def test_get_builtin_segment_returns_count(tenant_db_session):
    Session = tenant_db_session
    high_value = _seed_customer(Session, name="High Value Co", created_days_ago=120)
    normal = _seed_customer(Session, name="Normal Co", created_days_ago=120)

    hv_job = _seed_job(Session, customer_id=high_value, days_ago=20)
    normal_job = _seed_job(Session, customer_id=normal, days_ago=20)
    _seed_invoice(Session, job_id=hv_job, total=6001.00)
    _seed_invoice(Session, job_id=normal_job, total=4999.99)

    db = Session()
    out = await get_segment(segment_id="high-value", _={}, db=db)
    db.close()
    assert out.id == "high-value"
    assert out.matching_customer_count == 1


async def test_get_segment_customers_for_at_risk(tenant_db_session):
    Session = tenant_db_session
    stale = _seed_customer(Session, name="Stale Co", created_days_ago=300)
    fresh = _seed_customer(Session, name="Fresh Co", created_days_ago=300)
    _seed_customer(Session, name="Never Job Co", created_days_ago=300)

    _seed_job(Session, customer_id=stale, days_ago=210)
    _seed_job(Session, customer_id=fresh, days_ago=15)

    db = Session()
    out = await list_segment_customers(segment_id="at-risk", _={}, db=db)
    db.close()
    names = {item["name"] for item in out.items}
    assert "Stale Co" in names
    assert "Never Job Co" in names
    assert "Fresh Co" not in names


async def test_get_custom_segment_details_with_count(tenant_db_session):
    Session = tenant_db_session
    target = _seed_customer(Session, name="Target Co", created_days_ago=200)
    _seed_customer(Session, name="Recent Co", created_days_ago=200)
    _seed_job(Session, customer_id=target, days_ago=120)

    db = Session()
    created = await create_segment(
        payload=SegmentCreateIn(
            name="No recent jobs",
            rules={"field": "last_job_date", "operator": "older_than", "value": "90 days"},
        ),
        request=_mock_request(),
        user={},
        db=db,
    )
    out = await get_segment(segment_id=created.id, _={}, db=db)
    assert out.id == created.id
    assert out.matching_customer_count >= 1
    db.close()


async def test_unknown_segment_returns_404(tenant_db_session):
    db = tenant_db_session()
    with pytest.raises(HTTPException) as exc:
        await get_segment(segment_id=str(uuid.uuid4()), _={}, db=db)
    db.close()
    assert exc.value.status_code == 404


async def test_delete_custom_segment(tenant_db_session):
    db = tenant_db_session()
    created = await create_segment(
        payload=SegmentCreateIn(
            name="Temporary Segment",
            rules={"field": "created_at", "operator": "older_than", "value": "1 days"},
        ),
        request=_mock_request(),
        user={},
        db=db,
    )
    resp = await delete_segment(segment_id=created.id, request=_mock_request(), user={}, db=db)
    assert resp.status_code == 204
    with pytest.raises(HTTPException) as exc:
        await get_segment(segment_id=created.id, _={}, db=db)
    db.close()
    assert exc.value.status_code == 404


async def test_delete_builtin_segment_rejected(tenant_db_session):
    db = tenant_db_session()
    with pytest.raises(HTTPException) as exc:
        await delete_segment(segment_id="at-risk", request=_mock_request(), user={}, db=db)
    db.close()
    assert exc.value.status_code == 400

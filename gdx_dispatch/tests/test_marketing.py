from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest


def _mock_request(tenant_id="test-tenant"):
    r = MagicMock()
    r.state.tenant = {"id": tenant_id}
    r.client.host = "127.0.0.1"
    return r

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.modules import require_module
from gdx_dispatch.routers import segments as segments_router
from gdx_dispatch.routers.marketing import ReferralCreateIn
from gdx_dispatch.routers.segments import (
    SegmentCreateIn,
    create_segment,
    get_segment_count,
    list_segment_customers,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def db_sessionmaker():
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
            CREATE TABLE customers (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT,
                phone TEXT,
                address TEXT,
                customer_type TEXT,
                metadata JSON,
                company_id TEXT,
                created_at TEXT NOT NULL,
                deleted_at TEXT
            )
            """
        )
    )
    db.execute(
        text(
            """
            CREATE TABLE jobs (
                id TEXT PRIMARY KEY,
                customer_id TEXT,
                title TEXT,
                status TEXT,
                lifecycle_stage TEXT,
                company_id TEXT,
                created_at TEXT,
                completed_at TEXT,
                deleted_at TEXT
            )
            """
        )
    )
    db.execute(
        text(
            """
            CREATE TABLE invoices (
                id TEXT PRIMARY KEY,
                job_id TEXT,
                total NUMERIC,
                company_id TEXT,
                deleted_at TEXT
            )
            """
        )
    )
    db.execute(
        text(
            """
            CREATE TABLE segments (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                rules JSON NOT NULL,
                created_at TEXT,
                deleted_at TEXT
            )
            """
        )
    )
    db.execute(
        text(
            """
            CREATE TABLE loyalty_referrals (
                id TEXT PRIMARY KEY,
                referrer_id TEXT NOT NULL,
                referee_name TEXT NOT NULL,
                referee_phone TEXT NOT NULL,
                status TEXT NOT NULL,
                converted_customer_id TEXT,
                converted_at TEXT,
                rewarded_at TEXT,
                created_at TEXT
            )
            """
        )
    )
    db.execute(
        text(
            """
            CREATE TABLE loyalty_points (
                id TEXT PRIMARY KEY,
                customer_id TEXT NOT NULL,
                amount INTEGER NOT NULL,
                reason TEXT NOT NULL,
                created_by TEXT,
                created_at TEXT
            )
            """
        )
    )
    db.execute(
        text(
            """
            CREATE TABLE audit_log (
                id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                actor_id TEXT,
                actor_role TEXT,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                payload JSON NOT NULL,
                ip_address TEXT,
                request_id TEXT,
                created_at TEXT,
                hash TEXT NOT NULL,
                prev_hash TEXT
            )
            """
        )
    )
    db.commit()
    db.close()

    try:
        yield Session
    finally:
        engine.dispose()


def _iso_days_ago(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


def _seed_customer(Session, *, name: str, created_days_ago: int, customer_type: str = "Retail", phone: str | None = None) -> str:
    cid = str(uuid.uuid4())
    db = Session()
    db.execute(
        text(
            """
            INSERT INTO customers (id, name, email, phone, address, customer_type, metadata, company_id, created_at, deleted_at)
            VALUES (:id, :name, :email, :phone, :address, :customer_type, :metadata, 'tenant-test', :created_at, NULL)
            """
        ),
        {
            "id": cid,
            "name": name,
            "email": f"{name.lower().replace(' ', '.')}@example.com",
            "phone": phone,
            "address": "123 Main",
            "customer_type": customer_type,
            "metadata": json.dumps({"customer_type": customer_type}),
            "created_at": _iso_days_ago(created_days_ago),
        },
    )
    db.commit()
    db.close()
    return cid


def _seed_job(Session, *, customer_id: str, created_days_ago: int, completed: bool = False) -> str:
    jid = str(uuid.uuid4())
    db = Session()
    status = "completed" if completed else "in_progress"
    completed_at = _iso_days_ago(created_days_ago) if completed else None
    db.execute(
        text(
            """
            INSERT INTO jobs (id, customer_id, title, status, lifecycle_stage, company_id, created_at, completed_at, deleted_at)
            VALUES (:id, :customer_id, :title, :status, :lifecycle_stage, 'tenant-test', :created_at, :completed_at, NULL)
            """
        ),
        {
            "id": jid,
            "customer_id": customer_id,
            "title": "Service Call",
            "status": status,
            "lifecycle_stage": status,
            "created_at": _iso_days_ago(created_days_ago),
            "completed_at": completed_at,
        },
    )
    db.commit()
    db.close()
    return jid


def _seed_invoice(Session, *, job_id: str, total: float) -> None:
    db = Session()
    db.execute(
        text(
            """
            INSERT INTO invoices (id, job_id, total, company_id, deleted_at)
            VALUES (:id, :job_id, :total, 'tenant-test', NULL)
            """
        ),
        {"id": str(uuid.uuid4()), "job_id": job_id, "total": total},
    )
    db.commit()
    db.close()


async def test_at_risk_segment_calculation(db_sessionmaker):
    Session = db_sessionmaker
    stale = _seed_customer(Session, name="Stale One", created_days_ago=300)
    fresh = _seed_customer(Session, name="Fresh One", created_days_ago=300)
    no_jobs = _seed_customer(Session, name="Never Job", created_days_ago=300)

    _seed_job(Session, customer_id=stale, created_days_ago=210, completed=True)
    _seed_job(Session, customer_id=fresh, created_days_ago=15, completed=True)

    db = Session()
    out = await list_segment_customers(segment_id="at-risk", _={}, db=db)
    db.close()

    ids = {row["id"] for row in out.items}
    assert stale in ids
    assert no_jobs in ids
    assert fresh not in ids


async def test_custom_segment_json_rules(db_sessionmaker):
    Session = db_sessionmaker
    commercial = _seed_customer(Session, name="Com Co", created_days_ago=90, customer_type="Commercial")
    _seed_customer(Session, name="Res Co", created_days_ago=90, customer_type="Residential")

    db = Session()
    mock_request = _mock_request()
    seg = await create_segment(
        payload=SegmentCreateIn(
            name="Commercial Only",
            rules={"field": "customer_type", "operator": "equals", "value": "Commercial"},
        ),
        request=mock_request,
        user={"sub": "test-user"},
        db=db,
    )
    matches = await list_segment_customers(segment_id=seg.id, _={}, db=db)
    db.close()

    assert {row["id"] for row in matches.items} == {commercial}


async def test_segment_count_endpoint(db_sessionmaker):
    Session = db_sessionmaker
    old = _seed_customer(Session, name="Old", created_days_ago=200)
    _seed_customer(Session, name="New", created_days_ago=2)
    _seed_job(Session, customer_id=old, created_days_ago=190, completed=True)

    db = Session()
    count = await get_segment_count(segment_id="at-risk", _={}, db=db)
    db.close()

    assert count["segment_id"] == "at-risk"
    assert count["count"] >= 1


def test_module_requirements_wired_for_segments_campaigns_loyalty():
    from gdx_dispatch.routers import campaigns as live_campaigns_router
    from gdx_dispatch.routers import referrals as referrals_router

    seg_dep = require_module("segments")

    segment_route = next(r for r in segments_router.router.routes if getattr(r, "path", "") == "/api/segments/{segment_id}/count")

    assert any(dep.call is seg_dep for dep in segment_route.dependant.dependencies)

    # /api/referrals moved from marketing_router to dedicated referrals_router
    # The loyalty module gate is set at the router level via dependencies=[Depends(require_module("loyalty"))]
    # Verify at least one route exists on the referrals router
    referral_routes = [r for r in referrals_router.router.routes if hasattr(r, "endpoint")]
    assert len(referral_routes) >= 1, "referrals router should have at least one route"
    # Router-level dependencies propagate to each route — the _dependency closure
    # is created by require_module() and its __closure__ captures the canonical_key
    referral_route = referral_routes[0]
    module_deps = [
        d for d in referral_route.dependant.dependencies
        if getattr(d.call, "__name__", "") == "_dependency"
        and hasattr(d.call, "__closure__")
        and d.call.__closure__
    ]
    assert module_deps, "referrals router should have a require_module dependency"
    # Verify the captured module key is 'loyalty'
    captured_keys = [
        cell.cell_contents for dep in module_deps
        for cell in dep.call.__closure__
        if isinstance(cell.cell_contents, str)
    ]
    assert "loyalty" in captured_keys, f"Expected 'loyalty' module gate, got: {captured_keys}"

    # The campaigns gate moved with the feature: modules/campaigns/router.py is gone
    # (2026-09-07), so the surviving routers/campaigns.py must carry it at router level.
    campaign_routes = [r for r in live_campaigns_router.router.routes if hasattr(r, "endpoint")]
    assert campaign_routes, "campaigns router should have at least one route"
    campaign_keys = [
        cell.cell_contents
        for d in campaign_routes[0].dependant.dependencies
        if getattr(d.call, "__name__", "") == "_dependency" and getattr(d.call, "__closure__", None)
        for cell in d.call.__closure__
        if isinstance(cell.cell_contents, str)
    ]
    assert "campaigns" in campaign_keys, f"Expected 'campaigns' module gate, got: {campaign_keys}"


async def test_referral_create_requires_required_fields():
    with pytest.raises(Exception):
        ReferralCreateIn(referrer_customer_id="", referee_name="", referee_phone="")

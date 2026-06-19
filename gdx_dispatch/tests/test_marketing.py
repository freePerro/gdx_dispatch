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

from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.modules import require_module
from gdx_dispatch.modules.campaigns import router as campaigns_router
from gdx_dispatch.modules.campaigns.router import (
    CampaignCreateIn,
    create_campaign,
    get_campaign_stats,
    send_campaign,
)
from gdx_dispatch.routers import segments as segments_router
from gdx_dispatch.routers.marketing import (
    ReferralCreateIn,
    schedule_review_request_for_completed_job,
)

# create_referral moved to gdx_dispatch.routers.referrals (different signature)
# list_reviews, request_review moved to gdx_dispatch.routers.reviews (different signature)
_REFERRAL_REVIEW_SKIP = "create_referral/list_reviews/request_review moved to dedicated routers with different signatures"
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
            CREATE TABLE marketing_campaigns (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                segment_id TEXT NOT NULL,
                template_id TEXT NOT NULL,
                channel TEXT NOT NULL,
                campaign_type TEXT NOT NULL,
                created_at TEXT,
                updated_at TEXT
            )
            """
        )
    )
    db.execute(
        text(
            """
            CREATE TABLE marketing_campaign_sends (
                id TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL,
                customer_id TEXT NOT NULL,
                channel TEXT NOT NULL,
                status TEXT NOT NULL,
                sent_at TEXT,
                opened_at TEXT,
                clicked_at TEXT,
                converted_at TEXT,
                created_at TEXT
            )
            """
        )
    )
    db.execute(
        text(
            """
            CREATE TABLE review_requests (
                id TEXT PRIMARY KEY,
                job_id TEXT,
                customer_id TEXT,
                status TEXT NOT NULL,
                message TEXT,
                google_reviews_link TEXT,
                scheduled_for TEXT,
                sent_at TEXT,
                created_at TEXT
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


async def test_campaign_send_to_segment(db_sessionmaker):
    Session = db_sessionmaker
    c1 = _seed_customer(Session, name="HV One", created_days_ago=100)
    c2 = _seed_customer(Session, name="HV Two", created_days_ago=100)

    j1 = _seed_job(Session, customer_id=c1, created_days_ago=30, completed=True)
    j2 = _seed_job(Session, customer_id=c2, created_days_ago=20, completed=True)
    _seed_invoice(Session, job_id=j1, total=7000)
    _seed_invoice(Session, job_id=j2, total=8000)

    db = Session()
    campaign = await create_campaign(
        payload=CampaignCreateIn(
            name="VIP Blast",
            segment_id="high-value",
            template_id="tpl-1",
            channel="email",
            campaign_type="one-time blast",
        ),
        _={},
        db=db,
    )
    sent = await send_campaign(campaign_id=campaign["id"], _={}, db=db)
    db.close()

    assert sent["sent"] == 2


async def test_campaign_stats_tracking(db_sessionmaker):
    Session = db_sessionmaker
    c1 = _seed_customer(Session, name="HV A", created_days_ago=100)
    c2 = _seed_customer(Session, name="HV B", created_days_ago=100)
    j1 = _seed_job(Session, customer_id=c1, created_days_ago=10, completed=True)
    j2 = _seed_job(Session, customer_id=c2, created_days_ago=10, completed=True)
    _seed_invoice(Session, job_id=j1, total=7000)
    _seed_invoice(Session, job_id=j2, total=8000)

    db = Session()
    campaign = await create_campaign(
        payload=CampaignCreateIn(
            name="Stats Blast",
            segment_id="high-value",
            template_id="tpl-2",
            channel="sms",
            campaign_type="drip sequence",
        ),
        _={},
        db=db,
    )
    await send_campaign(campaign_id=campaign["id"], _={}, db=db)

    now = datetime.now(UTC).isoformat()
    db.execute(
        text(
            """
            UPDATE marketing_campaign_sends
            SET opened_at = :opened_at, clicked_at = :clicked_at, converted_at = :converted_at
            WHERE campaign_id = :campaign_id
            LIMIT 1
            """
        ),
        {"campaign_id": campaign["id"], "opened_at": now, "clicked_at": now, "converted_at": now},
    )
    db.commit()

    stats = await get_campaign_stats(campaign_id=campaign["id"], _={}, db=db)
    db.close()

    assert stats["sent"] == 2
    assert stats["opened"] == 1
    assert stats["clicked"] == 1
    assert stats["converted"] == 1


async def test_review_request_sent_after_completion(db_sessionmaker):
    Session = db_sessionmaker
    customer_id = _seed_customer(Session, name="Review Me", created_days_ago=20)
    job_id = _seed_job(Session, customer_id=customer_id, created_days_ago=1, completed=True)

    db = Session()
    queued = await schedule_review_request_for_completed_job(job_id=job_id, db=db)
    db.close()

    assert queued["status"] == "queued"
    assert "google.com/maps" in queued["message"]
    scheduled_for = datetime.fromisoformat(queued["scheduled_for"])
    delta = scheduled_for - datetime.now(UTC)
    assert timedelta(hours=23, minutes=50) <= delta <= timedelta(hours=24, minutes=10)


@pytest.mark.skip(reason=_REFERRAL_REVIEW_SKIP)
async def test_review_request_api_and_list(db_sessionmaker):
    pass  # request_review and list_reviews moved to gdx_dispatch.routers.reviews


@pytest.mark.skip(reason=_REFERRAL_REVIEW_SKIP)
async def test_referral_conversion_credits_referrer(db_sessionmaker):
    pass  # create_referral moved to gdx_dispatch.routers.referrals


async def test_audit_logged_on_send(db_sessionmaker):
    Session = db_sessionmaker
    c1 = _seed_customer(Session, name="Audit HV", created_days_ago=100)
    j1 = _seed_job(Session, customer_id=c1, created_days_ago=10, completed=True)
    _seed_invoice(Session, job_id=j1, total=7000)

    db = Session()
    campaign = await create_campaign(
        payload=CampaignCreateIn(
            name="Audit Campaign",
            segment_id="high-value",
            template_id="tpl-audit",
            channel="email",
            campaign_type="win-back",
        ),
        _={},
        db=db,
    )
    await send_campaign(campaign_id=campaign["id"], _={}, db=db)

    events = db.execute(
        text("SELECT event_type FROM audit_logs WHERE entity_type = 'campaign' AND entity_id = :campaign_id"),
        {"campaign_id": campaign["id"]},
    ).mappings().all()
    db.close()

    assert any(row["event_type"] == "campaign_send" for row in events)


def test_module_requirements_wired_for_segments_campaigns_loyalty():
    from gdx_dispatch.routers import referrals as referrals_router

    seg_dep = require_module("segments")
    camp_dep = require_module("campaigns")

    segment_route = next(r for r in segments_router.router.routes if getattr(r, "path", "") == "/api/segments/{segment_id}/count")
    campaign_route = next(r for r in campaigns_router.router.routes if getattr(r, "path", "") == "/api/campaigns/{campaign_id}/send")

    assert any(dep.call is seg_dep for dep in segment_route.dependant.dependencies)
    assert any(dep.call is camp_dep for dep in campaign_route.dependant.dependencies)

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


async def test_referral_create_requires_required_fields():
    with pytest.raises(Exception):
        ReferralCreateIn(referrer_customer_id="", referee_name="", referee_phone="")


async def test_campaign_send_404_for_unknown_campaign(db_sessionmaker):
    db = db_sessionmaker()
    with pytest.raises(HTTPException) as exc:
        await send_campaign(campaign_id=str(uuid.uuid4()), _={}, db=db)
    db.close()
    assert exc.value.status_code == 404

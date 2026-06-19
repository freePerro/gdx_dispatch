from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.models.tenant_models import LoyaltyPoints, LoyaltyReferral, LoyaltyTier
from gdx_dispatch.routers.loyalty import (
    PointsAward,
    ReferralCreate,
    TierCreate,
    TierPatch,
    award_points,
    create_referral,
    create_tier,
    get_customer_points,
    get_customer_tier,
    get_tier,
    list_referrals,
    list_tiers,
    update_tier,
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

    LoyaltyTier.__table__.create(bind=engine, checkfirst=True)
    LoyaltyPoints.__table__.create(bind=engine, checkfirst=True)
    LoyaltyReferral.__table__.create(bind=engine, checkfirst=True)

    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def test_list_default_tiers(tenant_db_session):
    rows = list_tiers(_={}, db=tenant_db_session)
    assert [x["name"] for x in rows] == ["bronze", "silver", "gold", "platinum"]


def test_create_tier(tenant_db_session):
    created = create_tier(
        payload=TierCreate(name="diamond", min_spend=5000, discount_pct=20),
        _={},
        db=tenant_db_session,
    )

    assert UUID(created["id"])
    assert created["name"] == "diamond"
    assert created["min_spend"] == 5000.0
    assert created["discount_pct"] == 20.0


def test_create_tier_validates_blank_name():
    with pytest.raises(ValidationError):
        TierCreate(name="", min_spend=100, discount_pct=5)


def test_get_tier_by_id(tenant_db_session):
    created = create_tier(
        payload=TierCreate(name="vip", min_spend=9000, discount_pct=25),
        _={},
        db=tenant_db_session,
    )

    body = get_tier(tier_id=UUID(created["id"]), _={}, db=tenant_db_session)
    assert body["id"] == created["id"]
    assert body["name"] == "vip"


def test_get_tier_not_found(tenant_db_session):
    with pytest.raises(Exception) as exc:
        get_tier(tier_id=uuid4(), _={}, db=tenant_db_session)
    assert getattr(exc.value, "status_code", None) == 404


def test_patch_tier_updates_values(tenant_db_session):
    created = create_tier(
        payload=TierCreate(name="vip", min_spend=7000, discount_pct=15),
        _={},
        db=tenant_db_session,
    )

    updated = update_tier(
        tier_id=UUID(created["id"]),
        payload=TierPatch(name="vip-plus", min_spend=9000, discount_pct=25),
        _={},
        db=tenant_db_session,
    )

    assert updated["name"] == "vip-plus"
    assert updated["min_spend"] == 9000.0
    assert updated["discount_pct"] == 25.0


def test_get_customer_points_defaults_zero(tenant_db_session):
    body = get_customer_points(customer_id="cust-1", _={}, db=tenant_db_session)
    assert body == {"customer_id": "cust-1", "points": 0}


def test_award_points_and_get_balance(tenant_db_session):
    awarded = award_points(
        customer_id="cust-1",
        payload=PointsAward(amount=120, reason="Invoice paid"),
        user={"user_id": "user-test"},
        db=tenant_db_session,
    )
    assert UUID(awarded["id"])
    assert awarded["customer_id"] == "cust-1"
    assert awarded["amount"] == 120

    balance = get_customer_points(customer_id="cust-1", _={}, db=tenant_db_session)
    assert balance["points"] == 120

    row = tenant_db_session.execute(
        select(LoyaltyPoints).where(LoyaltyPoints.customer_id == "cust-1")
    ).scalar_one()
    assert row.reason == "Invoice paid"


def test_award_points_rejects_non_positive():
    with pytest.raises(ValidationError):
        PointsAward(amount=0, reason="no-op")


def test_get_customer_tier_uses_highest_matching_tier(tenant_db_session):
    award_points(
        customer_id="cust-9",
        payload=PointsAward(amount=6000, reason="Many invoices"),
        user={"user_id": "user-test"},
        db=tenant_db_session,
    )

    body = get_customer_tier(customer_id="cust-9", _={}, db=tenant_db_session)
    assert body["customer_id"] == "cust-9"
    assert body["points"] == 6000
    assert body["tier"]["name"] == "gold"


def test_create_and_list_referrals(tenant_db_session):
    from types import SimpleNamespace
    mock_req = SimpleNamespace(state=SimpleNamespace(tenant={"id": "tenant-test"}))
    created = create_referral(
        payload=ReferralCreate(
            referrer_id="cust-1",
            referee_name="Pat Doe",
            referee_phone="555-1212",
        ),
        request=mock_req,
        _={},
        db=tenant_db_session,
    )

    assert UUID(created["id"])
    assert created["referrer_id"] == "cust-1"
    assert created["referee_name"] == "Pat Doe"

    listed = list_referrals(_={}, db=tenant_db_session)
    assert len(listed) == 1
    assert listed[0]["id"] == created["id"]


def test_create_referral_requires_fields():
    with pytest.raises(ValidationError):
        ReferralCreate(referrer_id="cust-1", referee_name="Pat", referee_phone="")


def test_loyalty_routes_registered_in_main_app():
    app_source = (Path(__file__).resolve().parents[1] / "app.py").read_text()
    assert "from gdx_dispatch.routers import loyalty as loyalty_router" in app_source
    assert "app.include_router(loyalty_router.router if hasattr(loyalty_router, \"router\") else loyalty_router)" in app_source

"""The Loyalty page lists the real points ledger (GDXA-316).

LoyaltyView read ``GET /api/loyalty``, a ui_compat stub that answered
``{"members": [], ...}`` whatever ``loyalty_points`` held, so the page could
never list anyone; its buttons posted to routes that never existed, so the
ledger stayed empty too. It now reads ``GET /api/loyalty/members``. These tests
run the real router against a fresh SQLite schema built from the ORM, where a
``Uuid`` is stored as 32 dashless hex: a name lookup by raw dashed-string SQL
would return nothing here and pass on Postgres.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import Customer, LoyaltyTier
from gdx_dispatch.routers import loyalty as loyalty_router
from gdx_dispatch.routers import ui_compat
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.tests.conftest import make_fresh_db

TENANT = "tenant-a"


@pytest.fixture
def client_and_db(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "x" * 64)
    engine = make_fresh_db()
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()

    app = FastAPI()
    app.include_router(loyalty_router.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "user-1", "tenant_id": TENANT, "role": "admin",
    }
    app.dependency_overrides[require_module("loyalty")] = lambda: True

    yield TestClient(app), db
    db.close()
    engine.dispose()


def _customer(db, name: str) -> Customer:
    c = Customer(id=uuid4(), name=name, phone="555-1111", company_id=TENANT)
    db.add(c)
    db.commit()
    return c


def _award(client, customer_id: str, amount: int) -> None:
    r = client.post(f"/api/loyalty/customers/{customer_id}/points", json={"amount": amount, "reason": "Visit"})
    assert r.status_code == 201, r.text


def test_members_lists_each_customer_with_balance_name_and_tier(client_and_db):
    client, db = client_and_db
    big = _customer(db, "Big Spender")
    small = _customer(db, "Small Fry")
    _award(client, str(big.id), 4000)
    _award(client, str(big.id), 2000)
    _award(client, str(small.id), 10)

    r = client.get("/api/loyalty/members")
    assert r.status_code == 200, r.text
    rows = r.json()
    assert [m["customer_id"] for m in rows] == [str(big.id), str(small.id)]
    assert rows[0]["customer_name"] == "Big Spender"
    assert rows[0]["points"] == 6000
    # Default tiers: gold starts at 5000.
    assert rows[0]["tier"] == "gold"
    assert rows[0]["tier_discount_pct"] == 10.0
    assert rows[0]["customer_deleted"] is False
    assert rows[0]["joined_at"]
    assert rows[1]["customer_name"] == "Small Fry"
    assert rows[1]["points"] == 10
    assert rows[1]["tier"] == "bronze"
    assert rows[1]["tier_discount_pct"] == 0.0


def test_members_merges_one_customer_stored_under_two_id_spellings(client_and_db):
    client, db = client_and_db
    c = _customer(db, "Split")
    _award(client, str(c.id), 3000)
    _award(client, c.id.hex, 3000)

    rows = client.get("/api/loyalty/members").json()
    assert len(rows) == 1
    assert rows[0]["customer_id"] == str(c.id)
    assert rows[0]["customer_name"] == "Split"
    assert rows[0]["points"] == 6000
    assert rows[0]["tier"] == "gold"


def test_members_flags_a_soft_deleted_customer(client_and_db):
    from datetime import UTC, datetime

    client, db = client_and_db
    c = _customer(db, "Gone Away")
    _award(client, str(c.id), 5)
    c.deleted_at = datetime.now(UTC)
    db.commit()

    (row,) = client.get("/api/loyalty/members").json()
    assert row["customer_name"] == "Gone Away"
    assert row["customer_deleted"] is True


def test_members_uses_configured_tiers_over_defaults(client_and_db):
    client, db = client_and_db
    c = _customer(db, "Tiered")
    db.add(LoyaltyTier(name="vip", min_spend=100, discount_pct=7))
    db.commit()
    _award(client, str(c.id), 150)

    (row,) = client.get("/api/loyalty/members").json()
    assert row["tier"] == "vip"


def test_members_keeps_a_ledger_row_whose_customer_id_is_not_a_uuid(client_and_db):
    client, _db = client_and_db
    _award(client, "legacy-7", 25)

    (row,) = client.get("/api/loyalty/members").json()
    assert row == {
        "customer_id": "legacy-7",
        "customer_name": None,
        "customer_deleted": False,
        "points": 25,
        "tier": "bronze",
        "tier_discount_pct": 0.0,
        "joined_at": row["joined_at"],
    }


def test_members_is_empty_when_no_points_were_ever_awarded(client_and_db):
    client, _db = client_and_db
    assert client.get("/api/loyalty/members").json() == []


def test_ui_compat_no_longer_serves_the_empty_loyalty_index():
    paths = {(m, r.path) for r in ui_compat.router.routes for m in getattr(r, "methods", ())}
    assert ("GET", "/api/loyalty") not in paths

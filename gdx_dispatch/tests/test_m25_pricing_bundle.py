"""M25 pricing-bundle guards (money audit 2026-08-04).

Four of the seven M25 items are code-fixed here; each test pins one:
markup-as-margin in /api/pricing/*, archived matrix rows still pricing,
the double-accept race's stale-identity half, and the CO bare-amount PATCH
diverging from its lines (billing follows the lines).
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase, ensure_audit_table

TENANT = "tenant-m25"
OFFICE = {"user_id": "office-user", "email": "office@example.com", "role": "admin"}


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    ensure_audit_table(session)
    yield session
    session.close()
    engine.dispose()


# ── 1. /api/pricing/* uses real margin math ────────────────────────────────

def test_calculate_sell_price_uses_margin_not_markup(monkeypatch):
    """30% margin on $100 cost is $142.86 (cost/(1−m)) — the engine's one
    convention. The old markup math gave $130, a 9% revenue gap."""
    from gdx_dispatch.routers import pricing

    monkeypatch.setattr(pricing, "_get_margin", lambda *_: 0.30)
    monkeypatch.setattr(pricing, "_get_volume_discount", lambda *_: 0.0)
    out = pricing.calculate_sell_price(
        cost=100.0, customer_type="retail", annual_spend=0, labor_hours=0,
        tech_id=None, high_lift=False, low_headroom=False, insulation=False, _={},
    )
    assert float(out["sell_price"]) == pytest.approx(142.86)


def test_calculate_refuses_a_margin_of_one_or_more(monkeypatch):
    from gdx_dispatch.routers import pricing

    monkeypatch.setattr(pricing, "_get_margin", lambda *_: 1.0)
    with pytest.raises(HTTPException) as exc:
        pricing.calculate_sell_price(
            cost=100.0, customer_type="retail", annual_spend=0, labor_hours=0,
            tech_id=None, high_lift=False, low_headroom=False, insulation=False, _={},
        )
    assert exc.value.status_code == 422


def test_price_comparison_uses_margin_not_markup(monkeypatch):
    from gdx_dispatch.routers import pricing

    monkeypatch.setattr(pricing, "_get_margin", lambda *_: 0.30)
    monkeypatch.setattr(
        pricing, "_tenant_vendor_lists",
        lambda: {"k": {"vendor_name": "V", "sku": "S", "cost": 100.0, "description": "d"}},
    )
    rows = pricing.price_comparison(customer_type="retail", _={})
    assert float(rows[0]["sell_price" if "sell_price" in rows[0] else "sell"]) == pytest.approx(142.86)


# ── 2. archived matrix rows stop pricing ───────────────────────────────────

def _matrix_db():
    from gdx_dispatch.models.labor_pricing import LaborPriceItem

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    LaborPriceItem.__table__.create(engine)
    return sessionmaker(bind=engine)(), LaborPriceItem


def test_archived_matrix_row_prices_nothing():
    from gdx_dispatch.routers.estimates import _resolve_labor_matrix_row

    db, LPI = _matrix_db()
    row = LPI(id=uuid.uuid4(), description="Retired", service_type="install",
              flat_price=150, assumed_man_hours=2, active=False)
    db.add(row)
    db.commit()
    with pytest.raises(HTTPException) as exc:
        _resolve_labor_matrix_row(db, row.id)
    assert exc.value.status_code == 404


def test_effective_to_past_prices_nothing():
    from gdx_dispatch.routers.estimates import _resolve_labor_matrix_row

    db, LPI = _matrix_db()
    row = LPI(id=uuid.uuid4(), description="Superseded", service_type="install",
              flat_price=150, assumed_man_hours=2, active=True,
              effective_to=date.today() - timedelta(days=1))
    db.add(row)
    db.commit()
    with pytest.raises(HTTPException):
        _resolve_labor_matrix_row(db, row.id)


def test_live_matrix_row_still_resolves():
    from gdx_dispatch.routers.estimates import _resolve_labor_matrix_row

    db, LPI = _matrix_db()
    row = LPI(id=uuid.uuid4(), description="Live", service_type="install",
              flat_price=150, assumed_man_hours=2, active=True)
    db.add(row)
    db.commit()
    assert _resolve_labor_matrix_row(db, row.id).id == row.id


# ── 3. double-accept: the stale-identity half ──────────────────────────────

def test_accept_refuses_past_a_stale_identity_map(db):
    """Two concurrent accepts both read status='sent'. The locked
    populate_existing re-read makes the loser SEE the winner's write —
    poison the map, flip the row behind the ORM's back, and accept must
    409 instead of double-writing (and double-minting deposits)."""
    from gdx_dispatch.modules.proposals.models import Estimate
    from gdx_dispatch.routers.estimates import accept_estimate

    est = Estimate(id=uuid.uuid4(), estimate_number=f"EST-{uuid.uuid4().hex[:6]}",
                   status="sent", company_id=TENANT, public_token=uuid.uuid4().hex)
    db.add(est)
    db.commit()
    stale = db.get(Estimate, est.id)   # poison the identity map
    assert stale.status == "sent"
    db.execute(text("UPDATE estimates SET status = 'accepted' WHERE id = :i"),
               {"i": est.id.hex})
    with pytest.raises(HTTPException) as exc:
        accept_estimate(est.id, payload=None, _=OFFICE, db=db)
    assert exc.value.status_code == 409


# ── 4. CO bare-amount PATCH cannot diverge from its lines ──────────────────

def _co_with_lines(db):
    from gdx_dispatch.routers.change_orders import (
        ChangeOrderIn,
        ChangeOrderLineIn,
        create_change_order,
    )

    out = create_change_order(
        ChangeOrderIn(
            title="Extra spring",
            line_items=[
                ChangeOrderLineIn(description="Spring", quantity=2, unit_price=250.0),
                ChangeOrderLineIn(description="Labor", quantity=1, unit_price=200.0),
            ],
        ),
        user=OFFICE, db=db,
    )
    return out


def test_bare_amount_patch_conflicting_with_lines_refuses(db):
    from gdx_dispatch.routers.change_orders import ChangeOrderIn, update_change_order

    co = _co_with_lines(db)  # lines sum 700
    with pytest.raises(HTTPException) as exc:
        update_change_order(uuid.UUID(co["id"]), ChangeOrderIn(title="Extra spring", amount=500.0),
                            current_user=OFFICE, db=db)
    assert exc.value.status_code == 409
    assert "follows the lines" in str(exc.value.detail)


def test_bare_amount_patch_matching_the_lines_is_fine(db):
    from gdx_dispatch.routers.change_orders import ChangeOrderIn, update_change_order

    co = _co_with_lines(db)
    out = update_change_order(uuid.UUID(co["id"]), ChangeOrderIn(title="Extra spring", amount=700.0),
                              current_user=OFFICE, db=db)
    assert float(out["amount"]) == 700.0


def test_flatwins_shape_bare_amount_patch_is_fine(db):
    """Audit round 2 (replacement): the M31/M33-mandated flow — all-$0
    descriptive lines, money on the flat amount — sends line_items: [] and
    must NOT 409. Zero-sum lines carry no money to diverge from."""
    from gdx_dispatch.routers.change_orders import (
        ChangeOrderIn,
        ChangeOrderLineIn,
        create_change_order,
        update_change_order,
    )

    co = create_change_order(
        ChangeOrderIn(
            title="Descriptive-only",
            line_items=[
                ChangeOrderLineIn(description="Scope note A", quantity=1, unit_price=0.0),
                ChangeOrderLineIn(description="Scope note B", quantity=1, unit_price=0.0),
            ],
        ),
        user=OFFICE, db=db,
    )
    out = update_change_order(uuid.UUID(co["id"]),
                              ChangeOrderIn(title="Descriptive-only", amount=700.0),
                              current_user=OFFICE, db=db)
    assert float(out["amount"]) == 700.0


def test_decline_refuses_past_a_stale_identity_map(db):
    """Same finalization-race shape as accept — an accept that landed behind
    the ORM's back must make the decline 409, not get overwritten."""
    from gdx_dispatch.modules.proposals.models import Estimate
    from gdx_dispatch.routers.estimates import DeclineIn, decline_estimate

    est = Estimate(id=uuid.uuid4(), estimate_number=f"EST-{uuid.uuid4().hex[:6]}",
                   status="sent", company_id=TENANT, public_token=uuid.uuid4().hex)
    db.add(est)
    db.commit()
    stale = db.get(Estimate, est.id)
    assert stale.status == "sent"
    db.execute(text("UPDATE estimates SET status = 'accepted' WHERE id = :i"),
               {"i": est.id.hex})
    with pytest.raises(HTTPException) as exc:
        decline_estimate(est.id, DeclineIn(reason="changed mind"), _=OFFICE, db=db)
    assert exc.value.status_code == 409


def test_matrix_row_expiring_today_still_prices():
    """Boundary matches billing_lanes/_is_retired: retired is STRICTLY past —
    a row whose effective_to is today prices today."""
    from gdx_dispatch.routers.estimates import _resolve_labor_matrix_row

    db, LPI = _matrix_db()
    row = LPI(id=uuid.uuid4(), description="Last day", service_type="install",
              flat_price=150, assumed_man_hours=2, active=True,
              effective_to=date.today())
    db.add(row)
    db.commit()
    assert _resolve_labor_matrix_row(db, row.id).id == row.id

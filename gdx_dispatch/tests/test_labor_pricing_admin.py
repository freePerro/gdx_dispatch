"""Sprint S97 slice 3 — labor pricing matrix CRUD tests.

Direct handler tests bypass FastAPI dependencies (auth + tenant binding) so
they isolate the labor-pricing business logic.
"""
from __future__ import annotations

from unittest.mock import MagicMock
from uuid import UUID

import pytest
from fastapi import HTTPException

from gdx_dispatch.models.labor_pricing import LaborPriceItem
from gdx_dispatch.routers.labor_pricing_admin import (
    LaborPriceItemIn,
    archive_item,
    create_item,
    get_item,
    list_items,
    update_item,
)


def _stub_user():
    return {"sub": "u-1", "user_id": "u-1"}


def _stub_request():
    req = MagicMock()
    req.state.tenant = {"id": "t-test"}
    req.headers = {}
    req.client = MagicMock(host="127.0.0.1")
    return req


def _payload(**overrides):
    base = dict(
        sku=None,
        description="10x8 Sectional Install",
        service_type="install",
        width_ft=10,
        height_ft=8,
        flat_price=500.0,
        assumed_man_hours=5.0,
        notes=None,
        active=True,
        sort_order=0,
    )
    base.update(overrides)
    return LaborPriceItemIn(**base)


def test_list_items_empty_initially(tenant_db):
    out = list_items(db=tenant_db)
    assert out == []


def test_create_item_happy_path(tenant_db):
    out = create_item(
        payload=_payload(),
        request=_stub_request(),
        user=_stub_user(),
        db=tenant_db,
    )
    assert out["description"] == "10x8 Sectional Install"
    assert out["service_type"] == "install"
    assert out["width_ft"] == 10
    assert out["height_ft"] == 8
    assert out["flat_price"] == 500.0
    assert out["assumed_man_hours"] == 5.0
    assert out["implied_hourly_rate"] == 100.0  # 500/5
    assert out["active"] is True
    assert "id" in out

    # Persisted
    rows = tenant_db.query(LaborPriceItem).all()
    assert len(rows) == 1


def test_create_item_size_pair_must_be_both_or_neither(tenant_db):
    """Either both width+height set, or neither — half-set is unfindable."""
    with pytest.raises(HTTPException) as exc:
        create_item(
            payload=_payload(width_ft=10, height_ft=None),
            request=_stub_request(),
            user=_stub_user(),
            db=tenant_db,
        )
    assert exc.value.status_code == 422


def test_create_sku_keyed_row_no_size(tenant_db):
    """SKU-keyed rows (no size) are valid for tenants who price by description."""
    out = create_item(
        payload=_payload(
            description="9 ft Torsion Spring R&R",
            service_type="spring",
            sku="SPR-9FT",
            width_ft=None,
            height_ft=None,
            flat_price=180.0,
            assumed_man_hours=1.5,
        ),
        request=_stub_request(),
        user=_stub_user(),
        db=tenant_db,
    )
    assert out["sku"] == "SPR-9FT"
    assert out["width_ft"] is None
    assert out["implied_hourly_rate"] == 120.0


def test_zero_hours_rejected(tenant_db):
    """Doug 2026-05-07 / EST-000030: assumed_man_hours=0 is invalid input.
    The 8x7 install row had hours=0 in prod and silently zeroed scheduling
    + cost downstream. Field validator now requires gt=0."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _payload(flat_price=0, assumed_man_hours=0)


def test_excessive_hours_rejected(tenant_db):
    """Doug 2026-05-07 / EST-000030 line 7: 700 was typed where 7 was meant.
    No real garage-door scope exceeds 48 man-hours; cap rejects the typo."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _payload(assumed_man_hours=700)


def test_update_item_replaces_fields(tenant_db):
    created = create_item(
        payload=_payload(),
        request=_stub_request(),
        user=_stub_user(),
        db=tenant_db,
    )
    item_id = UUID(created["id"])
    updated = update_item(
        item_id=item_id,
        payload=_payload(
            description="10x8 Insulated Install",
            flat_price=575.0,
            assumed_man_hours=5.5,
        ),
        request=_stub_request(),
        user=_stub_user(),
        db=tenant_db,
    )
    assert updated["description"] == "10x8 Insulated Install"
    assert updated["flat_price"] == 575.0
    assert updated["assumed_man_hours"] == 5.5


def test_archive_item_soft_deletes(tenant_db):
    created = create_item(
        payload=_payload(),
        request=_stub_request(),
        user=_stub_user(),
        db=tenant_db,
    )
    item_id = UUID(created["id"])
    out = archive_item(
        item_id=item_id,
        request=_stub_request(),
        user=_stub_user(),
        db=tenant_db,
    )
    assert out["ok"] is True

    # Row still exists, but inactive
    after = get_item(item_id=item_id, db=tenant_db)
    assert after["active"] is False
    assert after["effective_to"] is not None  # stamped


def test_list_items_filters_by_service_type(tenant_db):
    create_item(
        payload=_payload(description="Install A", service_type="install"),
        request=_stub_request(), user=_stub_user(), db=tenant_db,
    )
    create_item(
        payload=_payload(description="Removal A", service_type="removal", flat_price=125, assumed_man_hours=1),
        request=_stub_request(), user=_stub_user(), db=tenant_db,
    )
    installs = list_items(service_type="install", db=tenant_db)
    removals = list_items(service_type="removal", db=tenant_db)
    assert len(installs) == 1
    assert len(removals) == 1
    assert installs[0]["service_type"] == "install"


def test_list_items_filters_by_active(tenant_db):
    a = create_item(
        payload=_payload(),
        request=_stub_request(), user=_stub_user(), db=tenant_db,
    )
    archive_item(item_id=UUID(a["id"]), request=_stub_request(), user=_stub_user(), db=tenant_db)
    create_item(
        payload=_payload(description="still active"),
        request=_stub_request(), user=_stub_user(), db=tenant_db,
    )
    active = list_items(active=True, db=tenant_db)
    archived = list_items(active=False, db=tenant_db)
    assert len(active) == 1
    assert len(archived) == 1
    assert active[0]["description"] == "still active"

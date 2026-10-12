"""GDXA-508 — the labor matrix's dates are the shop's calendar day.

Each test freezes the clock at 21:00 Central on Jan 15, which is 03:00 UTC on
Jan 16: after UTC midnight, before shop midnight. On the old code every site
read ``date.today()`` (the container's UTC day, Jan 16) and each assertion
below fails; on the shop's day (Jan 15) each passes.
"""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import freezegun
import pytest
from fastapi import HTTPException

from gdx_dispatch.models.labor_pricing import LaborPriceItem
from gdx_dispatch.models.tenant_models import AppSettings

# Imported before any clock is frozen: pydantic cannot build a schema for
# freezegun's FakeDate, so a router imported under freeze_time fails to load.
from gdx_dispatch.routers.estimates import _compute_price_drift, _resolve_labor_matrix_row
from gdx_dispatch.routers.labor_pricing_admin import LaborPriceItemIn, archive_item, create_item

EVENING_UTC = datetime(2026, 1, 16, 3, 0, tzinfo=UTC)  # 21:00 Jan 15 Central
SHOP_DAY = date(2026, 1, 15)  # the old code's date.today() was Jan 16


@pytest.fixture
def db(tenant_db):
    tenant_db.add(AppSettings(timezone="America/Chicago"))
    tenant_db.commit()
    freezegun.configure(extend_ignore_list=["_pytest"])
    with freezegun.freeze_time(EVENING_UTC):
        yield tenant_db


def _request():
    req = MagicMock()
    req.state.tenant = {"id": "t-test"}
    req.headers = {}
    req.client = MagicMock(host="127.0.0.1")
    return req


def _row(db, **kw):
    row = LaborPriceItem(id=uuid.uuid4(), description="16x7 install",
                         service_type="install", flat_price=400,
                         assumed_man_hours=4, active=True, **kw)
    db.add(row)
    db.commit()
    return row


def test_create_item_defaults_effective_from_to_the_shop_day(db):
    payload = LaborPriceItemIn(description="10x8 install", service_type="install",
                               width_ft=10, height_ft=8, flat_price=500.0,
                               assumed_man_hours=5.0)
    out = create_item(payload=payload, request=_request(),
                      user={"sub": "u-1", "user_id": "u-1"}, db=db)
    row = db.get(LaborPriceItem, uuid.UUID(out["id"]))
    assert row.effective_from == SHOP_DAY


def test_archive_item_stamps_effective_to_with_the_shop_day(db):
    row = _row(db)
    archive_item(item_id=row.id, request=_request(),
                 user={"sub": "u-1", "user_id": "u-1"}, db=db)
    db.refresh(row)
    assert row.effective_to == SHOP_DAY


def test_row_expiring_on_the_shop_day_still_prices_that_evening(db):
    row = _row(db, effective_to=SHOP_DAY)
    assert _resolve_labor_matrix_row(db, row.id).id == row.id


def test_row_retired_before_the_shop_day_prices_nothing(db):
    row = _row(db, effective_to=SHOP_DAY - timedelta(days=1))
    with pytest.raises(HTTPException) as exc:
        _resolve_labor_matrix_row(db, row.id)
    assert exc.value.status_code == 404


def test_price_drift_does_not_retire_a_row_expiring_on_the_shop_day(db):
    row = _row(db, effective_to=SHOP_DAY)
    line = SimpleNamespace(id=uuid.uuid4(), description="16x7 install",
                           quantity=1, unit_price=400, labor_price_item_id=row.id)
    assert _compute_price_drift(SimpleNamespace(lines=[line]), db) == []

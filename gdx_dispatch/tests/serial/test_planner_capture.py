"""Tests for the call-capture additions to the planner router (2026-07-07).

Covers the quick-capture fields on task create, the needs_action sort, and the
match-phone / recent-calls / link-customer endpoints. Mirrors test_planner.py's
direct-call style with a fresh ORM DB per test.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from conftest import make_fresh_db
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.models.tenant_models import Customer, PlannerTask
from gdx_dispatch.routers.planner import (
    LinkCustomerIn,
    TaskIn,
    create_task,
    link_customer,
    list_tasks,
    match_phone,
    recent_calls,
)


class DummyRequest:
    def __init__(self, tenant_id: str = "tenant-capture-test") -> None:
        self.state = SimpleNamespace(tenant={"id": tenant_id}, request_id="req-cap")
        self.client = SimpleNamespace(host="127.0.0.1")
        self.headers: dict[str, str] = {}


@pytest.fixture()
def ctx():
    engine = make_fresh_db()
    SL = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = SL()
    req = DummyRequest()
    user = {"user_id": "admin-1", "sub": "admin-1", "role": "admin"}
    try:
        yield db, req, user
    finally:
        db.close()
        engine.dispose()


def _one(db, task_id):
    return db.get(PlannerTask, task_id)


def test_capture_stores_phone_and_source(ctx):
    db, req, user = ctx
    body = TaskIn(
        title="wants 2 openers quoted, call back Thu",
        contact_phone="(320) 555-0142",
        source="quick_capture",
    )
    res = create_task(body=body, request=req, user=user, db=db)
    row = _one(db, res["id"])
    assert row.source == "quick_capture"
    # Normalized to E.164 when phonenumbers is available; otherwise stored raw.
    assert row.contact_phone in ("+13205550142", "(320) 555-0142")


def test_capture_autolinks_customer_by_phone(ctx):
    db, req, user = ctx
    cust = Customer(company_id="tenant-capture-test", name="Sarah Miller",
                    phone="+13205550142")
    db.add(cust)
    db.commit()
    # A capture typing that same number should resolve to the customer.
    body = TaskIn(title="call back", contact_phone="320-555-0142")
    res = create_task(body=body, request=req, user=user, db=db)
    row = _one(db, res["id"])
    assert row.customer_id == str(cust.id)


def test_capture_keeps_number_when_no_match(ctx):
    db, req, user = ctx
    body = TaskIn(title="unknown caller", contact_phone="+13205559999")
    res = create_task(body=body, request=req, user=user, db=db)
    row = _one(db, res["id"])
    assert row.customer_id is None
    assert row.contact_phone == "+13205559999"


def test_needs_action_sort_puts_overdue_first(ctx):
    db, req, user = ctx
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    # Newest-created but far-future due
    create_task(body=TaskIn(title="future", due_date=(now + timedelta(days=30)).isoformat()),
                request=req, user=user, db=db)
    # Older-created but overdue → must surface first under needs_action
    create_task(body=TaskIn(title="overdue", due_date=(now - timedelta(days=2)).isoformat()),
                request=req, user=user, db=db)
    items = list_tasks(request=req, user=user, db=db, sort="needs_action")["items"]
    assert items[0]["title"] == "overdue"


def test_quick_capture_defaults_due_date_to_today(ctx):
    # Server guarantees the "never scroll away" invariant even if the client
    # omits a due date: a quick_capture with no due_date gets one.
    db, req, user = ctx
    res = create_task(body=TaskIn(title="no due", source="quick_capture"),
                      request=req, user=user, db=db)
    row = _one(db, res["id"])
    assert row.due_date is not None
    # A plain task (no source) keeps a null due date — default is capture-only.
    res2 = create_task(body=TaskIn(title="plain"), request=req, user=user, db=db)
    assert _one(db, res2["id"]).due_date is None


def test_match_phone_returns_null_for_unknown(ctx):
    db, req, user = ctx
    out = match_phone(request=req, user=user, db=db, phone="+13205550000")
    assert out["customer_id"] is None
    assert out["name"] is None


def test_match_phone_finds_customer(ctx):
    db, req, user = ctx
    cust = Customer(company_id="tenant-capture-test", name="Bob Vance",
                    phone="+13205551212")
    db.add(cust)
    db.commit()
    out = match_phone(request=req, user=user, db=db, phone="(320) 555-1212")
    assert out["customer_id"] == str(cust.id)
    assert out["name"] == "Bob Vance"


def test_recent_calls_empty_when_no_phone_data(ctx):
    db, req, user = ctx
    # No phone_com_calls rows → empty list, never raises.
    out = recent_calls(request=req, user=user, db=db)
    assert out == {"items": []}


def test_link_customer_sets_customer_on_task(ctx):
    db, req, user = ctx
    res = create_task(body=TaskIn(title="captured", contact_phone="+13205558888"),
                      request=req, user=user, db=db)
    cust = Customer(company_id="tenant-capture-test", name="New Cust")
    db.add(cust)
    db.commit()
    cid = str(cust.id)
    out = link_customer(res["id"], LinkCustomerIn(customer_id=cid),
                        request=req, user=user, db=db)
    assert out["customer_id"] == cid
    assert _one(db, res["id"]).customer_id == cid


def test_link_customer_backfills_unmatched_call(ctx):
    db, req, user = ctx
    from gdx_dispatch.modules.phone_com.models import PhoneComCall

    # An unmatched inbound call whose from_number is stored RAW, the way
    # Phone.com actually ingests it (upserts.py does no normalization). The
    # backfill must still match after normalizing both sides.
    db.add(PhoneComCall(
        phone_com_call_id="call-abc", direction="in",
        from_number="(320) 555-7777", customer_id=None,
    ))
    db.commit()
    res = create_task(body=TaskIn(title="captured", contact_phone="320-555-7777"),
                      request=req, user=user, db=db)
    cust = Customer(company_id="tenant-capture-test", name="Backfill Cust")
    db.add(cust)
    db.commit()
    out = link_customer(res["id"], LinkCustomerIn(customer_id=str(cust.id)),
                        request=req, user=user, db=db)
    # The raw-format call row (UUID customer_id column) got the coerced id.
    assert out["calls_backfilled"] == 1
    call = db.query(PhoneComCall).filter(PhoneComCall.phone_com_call_id == "call-abc").first()
    assert str(call.customer_id) == str(cust.id)


def test_link_customer_no_double_count_same_call(ctx):
    # A capture from the recent-calls strip carries BOTH the call id and the
    # phone. Both backfill paths target the same call row; it must count once.
    db, req, user = ctx
    from gdx_dispatch.modules.phone_com.models import PhoneComCall

    db.add(PhoneComCall(
        phone_com_call_id="call-xyz", direction="in",
        from_number="(320) 555-6666", customer_id=None,
    ))
    db.commit()
    res = create_task(
        body=TaskIn(title="captured", contact_phone="+13205556666",
                    phone_com_call_id="call-xyz"),
        request=req, user=user, db=db,
    )
    cust = Customer(company_id="tenant-capture-test", name="Once Cust")
    db.add(cust)
    db.commit()
    out = link_customer(res["id"], LinkCustomerIn(customer_id=str(cust.id)),
                        request=req, user=user, db=db)
    assert out["calls_backfilled"] == 1  # not 2

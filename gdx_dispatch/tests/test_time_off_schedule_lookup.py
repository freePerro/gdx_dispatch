"""core/time_off.approved_time_off_for_range — the schedule's one view of time off.

GDXA-1775/1828: time off is one record (`TimeOffRequest`), and the schedule
board reads it through this lookup. Every request here goes through the real
routes (request, approve, deny, revoke, cancel), so the status the lookup
filters on is the status the workflow actually writes.

Pinned, named for the failure it prevents:

* only an approved request blocks; pending, denied and cancelled never do;
* a revoke stops blocking the moment it lands;
* the request's USER id reaches the board's TECHNICIAN id, whichever way the
  link is spelled, and a soft-deleted technician or request blocks nobody;
* the range is an inclusive overlap.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from gdx_dispatch.core.time_off import approved_time_off_for_range
from gdx_dispatch.models.tenant_models import Technician, TimeOffRequest
from gdx_dispatch.tests.test_silent_success_sweep import TENANT, _teardown
from gdx_dispatch.tests.test_time_off_router import (
    OFFICE,
    OTHER_TECH,
    TECH,
    _as,
    _client,
    _next,
    _seed,
)

TECH_ROW = str(uuid4())
OTHER_ROW = str(uuid4())
GONE_ROW = str(uuid4())


@pytest.fixture()
def board():
    c = _client()
    _seed(c)
    now = datetime.now(UTC)
    with c.SessionLocal() as db:
        db.add(Technician(id=TECH_ROW, company_id=TENANT, user_id=TECH, name="Amber", active=True))
        # The link spelled dashless: still the same person.
        db.add(Technician(id=OTHER_ROW, company_id=TENANT, user_id=UUID(OTHER_TECH).hex,
                          name="Michael", active=True))
        db.add(Technician(id=GONE_ROW, company_id=TENANT, user_id=TECH, name="Old row",
                          active=True, deleted_at=now))
        db.commit()
    yield c
    _teardown(c)


def _request(client, user_id: str, start, end=None) -> str:
    _as(client, user_id, "technician")
    r = client.post("/api/timeclock/time-off/requests", json={
        "start_date": start.isoformat(), "end_date": (end or start).isoformat(), "notes": "off",
    })
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _rule(client, request_id: str, action: str) -> None:
    _as(client, OFFICE, "dispatcher")
    r = client.post(f"/api/timeclock/time-off/requests/{request_id}/{action}", json={"note": "ok"})
    assert r.status_code == 200, r.text


def _lookup(client, start, end, **kw):
    with client.SessionLocal() as db:
        return approved_time_off_for_range(db, start, end, **kw)


def test_only_approved_blocks_and_revoke_stops_blocking(board):
    mon = _next(0)
    tue, wed, thu = (mon + timedelta(days=n) for n in (1, 2, 3))
    approved = _request(board, TECH, mon, tue)
    _rule(board, approved, "approve")
    denied = _request(board, TECH, wed)
    _rule(board, denied, "deny")
    _request(board, TECH, thu)  # left pending
    cancelled = _request(board, OTHER_TECH, mon)
    _as(board, OTHER_TECH, "technician")
    assert board.post(f"/api/timeclock/time-off/requests/{cancelled}/cancel", json={}).status_code == 200

    got = _lookup(board, mon, thu)
    assert list(got) == [TECH_ROW], "the deleted technician row and the cancelled request block nobody"
    [row] = got[TECH_ROW]
    assert (row.request_id, row.user_id, row.entry_type) == (approved, TECH, "vacation")
    assert (row.start_date, row.end_date) == (mon, tue)
    assert row.covers(tue) and not row.covers(wed)

    _rule(board, approved, "revoke")
    assert _lookup(board, mon, thu) == {}


def test_the_dashless_link_maps_and_the_range_is_an_inclusive_overlap(board):
    mon = _next(0)
    fri = mon + timedelta(days=4)
    rid = _request(board, OTHER_TECH, mon, fri)
    _rule(board, rid, "approve")

    assert [r.request_id for r in _lookup(board, fri, fri + timedelta(days=7))[OTHER_ROW]] == [rid]
    assert [r.request_id for r in _lookup(board, mon - timedelta(days=7), mon)[OTHER_ROW]] == [rid]
    assert _lookup(board, fri + timedelta(days=1), fri + timedelta(days=9)) == {}
    assert _lookup(board, mon - timedelta(days=9), mon - timedelta(days=1)) == {}
    assert _lookup(board, fri, mon) == {}, "an inverted range is empty, not an error"

    assert list(_lookup(board, mon, fri, technician_ids={OTHER_ROW})) == [OTHER_ROW]
    assert _lookup(board, mon, fri, technician_ids={TECH_ROW}) == {}
    assert _lookup(board, mon, fri, technician_ids=set()) == {}


def test_a_soft_deleted_approved_request_blocks_nobody(board):
    mon = _next(0)
    with board.SessionLocal() as db:
        db.add(TimeOffRequest(
            id="gone", company_id=TENANT, technician_id=TECH, entry_type="vacation",
            start_date=mon, end_date=mon, minutes_per_day=480, status="approved",
            requested_by=TECH, deleted_at=datetime.now(UTC),
        ))
        db.commit()
    assert _lookup(board, mon, mon) == {}

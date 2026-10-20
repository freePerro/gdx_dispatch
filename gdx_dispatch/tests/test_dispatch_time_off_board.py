"""The schedule reads approved time off — GDXA-1829 (parent GDXA-1775).

Time off is one record, the `TimeOffRequest`, and the schedule reads it: the
available-slots route offers no slot on an approved day off, and the board's
GET /api/dispatch/time-off lists the approved days per technician so the day
and week views can block them. One app serves the time-off, scheduling and
board routers, and every request moves through the real routes (request,
approve, deny, revoke), so the status the schedule filters on is the status the
workflow writes.

Pinned:
* approved blocks the slot and shows on the board, on every day it covers;
* pending and denied never block; a revoke stops blocking the moment it lands;
* the request's USER id reaches the board's TECHNICIAN id;
* the board read is gated on jobs.read_all, like the board's other reads.
"""
from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest

from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import Technician
from gdx_dispatch.routers import dispatch_scheduling, scheduling
from gdx_dispatch.tests.test_silent_success_sweep import TENANT, _teardown
from gdx_dispatch.tests.test_time_off_router import OFFICE, OTHER_TECH, TECH, _as, _client, _next, _seed

TECH_ROW = str(uuid4())
OTHER_ROW = str(uuid4())


@pytest.fixture()
def board():
    c = _client()
    c.app.include_router(scheduling.router)
    c.app.include_router(dispatch_scheduling.router)
    for key in ("jobs", "dispatch"):
        c.app.dependency_overrides[require_module(key)] = lambda: None
    _seed(c)
    with c.SessionLocal() as db:
        db.add(Technician(id=TECH_ROW, company_id=TENANT, user_id=TECH, name="Amber", active=True))
        db.add(Technician(id=OTHER_ROW, company_id=TENANT, user_id=OTHER_TECH, name="Michael", active=True))
        db.commit()
    yield c
    _teardown(c)


def _request(client, start, end=None) -> str:
    _as(client, TECH, "technician")
    r = client.post("/api/timeclock/time-off/requests", json={
        "start_date": start.isoformat(), "end_date": (end or start).isoformat(), "notes": "off",
    })
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _rule(client, request_id: str, action: str) -> None:
    _as(client, OFFICE, "dispatcher")
    r = client.post(f"/api/timeclock/time-off/requests/{request_id}/{action}", json={"note": "ok"})
    assert r.status_code == 200, r.text


def _slots(client, tech_row: str, day) -> list[dict]:
    _as(client, OFFICE, "dispatcher")
    r = client.get("/api/appointments/available-slots", params={"tech_id": tech_row, "date": day.isoformat()})
    assert r.status_code == 200, r.text
    return r.json()


def _board(client, first, last) -> list[dict]:
    _as(client, OFFICE, "dispatcher")
    r = client.get("/api/dispatch/time-off", params={"date_from": first.isoformat(), "date_to": last.isoformat()})
    assert r.status_code == 200, r.text
    return r.json()["items"]


def test_approved_time_off_blocks_the_slot_and_shows_on_the_board(board):
    tue = _next(1)
    wed, thu = tue + timedelta(days=1), tue + timedelta(days=2)
    rid = _request(board, tue, wed)
    _rule(board, rid, "approve")

    for day in (tue, wed):
        assert _slots(board, TECH_ROW, day) == [], day
    # The day after, and another tech on the same days, keep their slots.
    assert _slots(board, TECH_ROW, thu)
    assert _slots(board, OTHER_ROW, tue)

    items = _board(board, tue - timedelta(days=2), thu)
    assert [(i["technician_id"], i["start_date"], i["end_date"], i["request_id"]) for i in items] == [
        (TECH_ROW, tue.isoformat(), wed.isoformat(), rid),
    ]
    assert items[0]["entry_type"] == "vacation"
    assert items[0]["label"] == "Vacation"
    # A window that starts inside the request still sees it; one after it does not.
    assert len(_board(board, wed, wed)) == 1
    assert _board(board, thu, thu) == []


@pytest.mark.parametrize("outcome", ["pending", "deny"])
def test_pending_and_denied_time_off_block_nothing(board, outcome):
    tue = _next(1)
    rid = _request(board, tue)
    if outcome != "pending":
        _rule(board, rid, outcome)

    assert _slots(board, TECH_ROW, tue)
    assert _board(board, tue, tue) == []


def test_a_revoke_stops_blocking(board):
    tue = _next(1)
    rid = _request(board, tue)
    _rule(board, rid, "approve")
    assert _slots(board, TECH_ROW, tue) == []
    assert len(_board(board, tue, tue)) == 1

    _rule(board, rid, "revoke")
    assert _slots(board, TECH_ROW, tue)
    assert _board(board, tue, tue) == []


def test_board_time_off_needs_jobs_read_all(board):
    tue = _next(1)
    _as(board, TECH, "technician")
    r = board.get("/api/dispatch/time-off", params={"date": tue.isoformat()})
    assert r.status_code == 403, r.text


@pytest.mark.parametrize("params", [
    {},
    {"date": "not-a-date"},
    {"date_from": "2026-11-10", "date_to": "2026-11-09"},
    {"date_from": "2026-01-01", "date_to": "2027-01-02"},
])
def test_board_time_off_rejects_a_bad_window(board, params):
    _as(board, OFFICE, "dispatcher")
    assert board.get("/api/dispatch/time-off", params=params).status_code == 422

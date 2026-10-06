"""The office's own visit planners (multi-day jobs plan §5.3a): Add day(s),
Move, Remove. Pure, so every refusal is pinned without a database; the
routes are driven end to end in ``test_job_visits_routes.py``."""
from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pytest

from gdx_dispatch.services.visit_sync import (
    MAX_DAYS,
    UNSET,
    Insert,
    Move,
    Reassign,
    Retire,
    VisitRow,
    expand_range,
    plan_add_visits,
    plan_move_visit,
    plan_remove_visit,
)

TZ = "America/Chicago"
TODAY = date(2026, 11, 2)  # a Monday
A, B = "tech-a", "tech-b"


def _at(day: date, hour: int = 8) -> datetime:
    # Chicago is UTC-6 in November.
    return datetime(day.year, day.month, day.day, hour + 6, tzinfo=UTC)


def _row(vid, day, tech=A, status="scheduled", arrived=None, hour=8) -> VisitRow:
    start = _at(day, hour)
    return VisitRow(id=vid, tech_id=tech, start_at=start, end_at=start + timedelta(hours=8),
                    status=status, arrived_at=arrived)


def _add(visits, days, techs=(A,), state="live", today=TODAY):
    return plan_add_visits(
        visits, tz_name=TZ, today=today, job_state=state, days=list(days),
        start_time=time(8, 0), duration_minutes=480, tech_ids=list(techs), fields={"title": "Install"},
    )


D1, D2, D3 = TODAY, TODAY + timedelta(days=1), TODAY + timedelta(days=2)


# ── Add day(s) ───────────────────────────────────────────────────────


def test_add_books_each_tech_on_each_day_at_the_shop_time():
    plan = _add([_row(1, D1)], [D2, D3], techs=(A, B))
    assert plan.refusal is None
    inserts = [(a.tech_id, a.start_at, a.reason) for a in plan.actions]
    assert inserts == [
        (A, _at(D2), "visits_booked"), (B, _at(D2), "visits_booked"),
        (A, _at(D3), "visits_booked"), (B, _at(D3), "visits_booked"),
    ]
    assert all(isinstance(a, Insert) and a.end_at - a.start_at == timedelta(hours=8) for a in plan.actions)


def test_add_with_no_tech_books_one_unassigned_slot_per_day():
    plan = _add([], [D2], techs=())
    assert [(a.tech_id, a.start_at) for a in plan.actions] == [(None, _at(D2))]


def test_add_today_is_allowed_and_yesterday_is_refused():
    assert _add([], [D1]).refusal is None
    plan = _add([], [D1 - timedelta(days=1), D2])
    assert plan.refusal.code == "past_day"
    assert plan.refusal.detail["day"] == (D1 - timedelta(days=1)).isoformat()
    assert plan.actions == []


@pytest.mark.parametrize("status,arrived", [
    ("scheduled", None),              # OPEN
    ("arrived", _at(D2)),             # ON SITE
    ("completed", None),              # CLOSED
    ("cancelled", _at(D2)),           # CLOSED (cancelled after arriving)
])
def test_add_onto_a_day_the_tech_already_holds_is_refused(status, arrived):
    plan = _add([_row(1, D2, status=status, arrived=arrived)], [D2, D3])
    assert plan.refusal.code == "double_booked"
    assert plan.refusal.detail == {"tech_ids": [A], "day": D2.isoformat()}
    assert plan.actions == []


def test_add_onto_a_day_whose_visit_was_cancelled_unworked_books_it():
    plan = _add([_row(1, D2, status="cancelled")], [D2])
    assert plan.refusal is None
    assert [(a.tech_id, a.start_at) for a in plan.actions] == [(A, _at(D2))]


def test_add_a_second_tech_onto_a_day_another_tech_holds_books_only_them():
    plan = _add([_row(1, D2, tech=A)], [D2], techs=(B,))
    assert [(a.tech_id, a.start_at) for a in plan.actions] == [(B, _at(D2))]


def test_add_an_unassigned_slot_onto_a_day_holding_one_is_refused():
    plan = _add([_row(1, D2, tech=None)], [D2], techs=())
    assert plan.refusal.code == "double_booked"


def test_add_the_same_day_twice_is_refused():
    assert _add([], [D2, D2]).refusal.code == "double_booked"


@pytest.mark.parametrize("state", ["completed", "cancelled"])
def test_add_on_a_finished_job_is_refused(state):
    assert _add([], [D2], state=state).refusal.code == "job_finished"


def test_add_caps_at_twenty_days_and_needs_one():
    days = [TODAY + timedelta(days=i) for i in range(MAX_DAYS + 1)]
    assert _add([], days).refusal.code == "too_many_days"
    assert _add([], days[:MAX_DAYS]).refusal is None
    assert _add([], []).refusal.code == "no_days"


def test_add_reads_the_shop_clock_across_daylight_saving():
    # 2026-11-01 is the fall-back Sunday: 08:00 Chicago is 13:00 UTC the
    # day before, 14:00 UTC after.
    plan = _add([], [date(2026, 10, 30), date(2026, 11, 3)], today=date(2026, 10, 30))
    assert [a.start_at for a in plan.actions] == [
        datetime(2026, 10, 30, 13, tzinfo=UTC), datetime(2026, 11, 3, 14, tzinfo=UTC),
    ]


# ── expand_range ─────────────────────────────────────────────────────


def test_range_includes_both_ends_and_skips_weekends_when_asked():
    fri, tue = date(2026, 11, 6), date(2026, 11, 10)
    assert expand_range(fri, tue, skip_weekends=False) == [fri + timedelta(days=i) for i in range(5)]
    assert expand_range(fri, tue, skip_weekends=True) == [fri, date(2026, 11, 9), tue]


def test_range_refuses_reversed_and_over_the_cap():
    with pytest.raises(ValueError):
        expand_range(D2, D1, skip_weekends=False)
    with pytest.raises(ValueError):
        expand_range(D1, D1 + timedelta(days=MAX_DAYS), skip_weekends=False)
    # Weekends skipped do not count toward the cap.
    assert len(expand_range(D1, D1 + timedelta(days=27), skip_weekends=True)) == MAX_DAYS


# ── Move ─────────────────────────────────────────────────────────────


def _move(visits, vid, start, *, tech=UNSET, hours=8, state="live"):
    return plan_move_visit(
        visits, tz_name=TZ, today=TODAY, job_state=state, visit_id=vid,
        start_at=start, end_at=start + timedelta(hours=hours), tech_id=tech,
    )


def test_move_changes_time_and_tech():
    plan = _move([_row(1, D2)], 1, _at(D3, 9), tech=B)
    assert plan.refusal is None
    assert [type(a) for a in plan.actions] == [Reassign, Move]
    assert plan.actions[0].tech_id == B
    assert plan.actions[1].start_at == _at(D3, 9)


def test_move_to_the_same_slot_writes_nothing():
    assert _move([_row(1, D2)], 1, _at(D2)).actions == []


@pytest.mark.parametrize("status,arrived", [("arrived", _at(D2)), ("completed", None), ("cancelled", None)])
def test_move_refuses_a_visit_that_is_not_open(status, arrived):
    plan = _move([_row(1, D2, status=status, arrived=arrived)], 1, _at(D3))
    assert plan.refusal.code == "visit_not_open"


def test_move_onto_a_day_the_tech_already_holds_is_refused():
    plan = _move([_row(1, D2), _row(2, D3, status="completed")], 1, _at(D3))
    assert plan.refusal.code == "double_booked"
    assert plan.refusal.detail["day"] == D3.isoformat()


def test_move_to_another_tech_who_holds_that_day_is_refused():
    plan = _move([_row(1, D2, tech=A), _row(2, D2, tech=B)], 1, _at(D2), tech=B)
    assert plan.refusal.code == "double_booked"


def test_move_into_the_past_is_refused_but_a_late_visit_may_change_tech():
    late = _row(1, D1 - timedelta(days=2))
    assert _move([late], 1, _at(D1 - timedelta(days=1))).refusal.code == "past_day"
    plan = _move([late], 1, late.start_at, tech=B)
    assert plan.refusal is None and [type(a) for a in plan.actions] == [Reassign]


def test_move_refuses_an_unknown_visit_and_a_finished_job():
    assert _move([_row(1, D2)], 99, _at(D3)).refusal.code == "visit_not_found"
    assert _move([_row(1, D2)], 1, _at(D3), state="completed").refusal.code == "job_finished"
    assert _move([_row(1, D2)], 1, _at(D3), hours=0).refusal.code == "bad_length"


# ── Remove ───────────────────────────────────────────────────────────


def _remove(visits, vid, state="live"):
    return plan_remove_visit(visits, tz_name=TZ, job_state=state, visit_id=vid)


def test_remove_retires_one_open_day():
    plan = _remove([_row(1, D2), _row(2, D3)], 2)
    assert plan.refusal is None
    assert plan.actions == [Retire(2, "visit_removed")]


def test_remove_refuses_the_last_booked_day():
    # Day 1 worked, day 2 the only one booked: removing it would leave the
    # job dated on a day nobody is coming.
    plan = _remove([_row(1, D1, status="completed"), _row(2, D2)], 2)
    assert plan.refusal.code == "last_open_visit"
    assert plan.actions == []


def test_remove_is_allowed_while_someone_is_on_site_another_day():
    plan = _remove([_row(1, D1, status="arrived", arrived=_at(D1)), _row(2, D2)], 2)
    assert plan.refusal is None


def test_remove_refuses_a_visit_that_is_not_open():
    plan = _remove([_row(1, D1, status="arrived", arrived=_at(D1)), _row(2, D2)], 1)
    assert plan.refusal.code == "visit_not_open"

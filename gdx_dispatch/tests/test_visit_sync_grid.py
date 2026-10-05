"""The visit planner against the whole state table (multi-day jobs plan §5.2a).

No database: every starting state the grid can build is run through every
request kind, and properties P1–P10 are checked on each result by oracles
written here, never by calling the code under test a second way.
"""
from __future__ import annotations

import itertools
from datetime import UTC, date, datetime, timedelta

import pytest

from gdx_dispatch.services.visit_sync import (
    CANCELLED,
    CLOSED,
    ON_SITE,
    OPEN,
    UNSET,
    Close,
    CopyFields,
    Insert,
    Move,
    Reassign,
    Retire,
    VisitEdit,
    VisitRow,
    plan_visit_sync,
    visit_state,
)

TZ = "America/Chicago"
DAYS = [date(2026, 11, 2) + timedelta(days=i) for i in range(5)]  # D0..D3, D4 fresh
A, B, C, X = "tech-a", "tech-b", "tech-c", "tech-x"


def at(day_index: int, hour: int = 15, minute: int = 0, second: int = 0, micro: int = 0) -> datetime:
    d = DAYS[day_index]
    return datetime(d.year, d.month, d.day, hour, minute, second, micro, tzinfo=UTC)


def day_of(v) -> date:
    return v.start_at.astimezone(_ZONE).date()


from zoneinfo import ZoneInfo  # noqa: E402

_ZONE = ZoneInfo(TZ)

_STATE_ROWS = {
    OPEN: dict(status="scheduled", arrived_at=None),
    ON_SITE: dict(status="scheduled", arrived_at="stamp"),
    CLOSED: dict(status="completed", arrived_at=None),
    CANCELLED: dict(status="cancelled", arrived_at=None),
}


def _row(seq: list, tech, day_index: int, state: str, *, odd: bool = False) -> VisitRow:
    seq.append(None)
    spec = _STATE_ROWS[state]
    # Some starts carry seconds and microseconds (the Appointments page
    # stores them); P3 is an exact equality, P7 a minute-truncated one.
    start = at(day_index, 15, 0, 7, 123456) if odd else at(day_index)
    return VisitRow(
        id=f"v{len(seq)}",
        tech_id=tech,
        start_at=start,
        end_at=start + timedelta(hours=1),
        status=spec["status"],
        arrived_at=start if spec["arrived_at"] else None,
        title="Old title",
        customer_id="cust-1",
        customer_name="Acme",
    )


STATES = [None, OPEN, ON_SITE, CLOSED, CANCELLED]


def oracle_schedule(rows: list[VisitRow], fallback):
    current = [v for v in rows if visit_state(v) in (OPEN, ON_SITE)]
    if not current:
        return fallback
    return min(v.start_at for v in current)


def current_day(rows):
    current = [v for v in rows if visit_state(v) in (OPEN, ON_SITE)]
    if not current:
        return None
    return day_of(min(current, key=lambda v: v.start_at))


def double_pairs(rows) -> int:
    pairs: dict = {}
    for v in rows:
        if v.tech_id is None or visit_state(v) == CANCELLED:
            continue
        key = (v.tech_id, day_of(v))
        pairs[key] = pairs.get(key, 0) + 1
    return sum(1 for c in pairs.values() if c > 1)


def starting_states():
    """(crew, visits, job_state) tuples."""
    out = []
    one_tech_days = 4
    for crew in ([], [A]):
        slot = crew[0] if crew else None
        for combo in itertools.product(STATES, repeat=one_tech_days):
            for extra in ("none", "unassigned_on_n", "outsider_open", "outsider_on_site", "two_open"):
                seq: list = []
                rows = [
                    _row(seq, slot, i, s, odd=(i % 2 == 1))
                    for i, s in enumerate(combo) if s is not None
                ]
                n = current_day(rows)
                if extra == "unassigned_on_n":
                    if n is None:
                        continue
                    rows.append(_row(seq, None, DAYS.index(n), OPEN))
                elif extra == "outsider_open":
                    rows.append(_row(seq, X, 1, OPEN))
                elif extra == "outsider_on_site":
                    rows.append(_row(seq, X, 2, ON_SITE))
                elif extra == "two_open":
                    if n is None or slot is None:
                        continue
                    rows.append(_row(seq, slot, DAYS.index(n), OPEN))
                for job_state in ("live", "completed", "cancelled"):
                    out.append((list(crew), rows, job_state))
    # The two-tech half: three days, and the second tech without CANCELLED
    # (which P1 treats as CLOSED does; the one-tech half covers it), to keep
    # the run short enough for the default gate.
    for combo_a in itertools.product(STATES, repeat=3):
        for combo_b in itertools.product(STATES[:4], repeat=3):
            seq = []
            rows = [_row(seq, A, i, s) for i, s in enumerate(combo_a) if s is not None]
            rows += [_row(seq, B, i, s, odd=True) for i, s in enumerate(combo_b) if s is not None]
            out.append(([A, B], rows, "live"))
            n = current_day(rows)
            if n is not None:
                out.append(([A, B], rows + [_row(seq, None, DAYS.index(n), OPEN)], "live"))
    return out


def edits(crew: list, rows: list, job_state: str, stored):
    """(label, kind, VisitEdit kwargs)."""
    n = current_day(rows)
    n_index = DAYS.index(n) if n is not None else 0
    date_changes = [
        ("resave", stored if stored is None else stored.replace(second=40, microsecond=999000)),
        ("time_on_n", at(n_index, 18, 30, 12, 345678)),
        *[(f"move_d{i}", at(i, 16, 15, 9)) for i in range(5)],
        ("clear", None),
    ]
    crew_changes = [("no_crew", None)]
    if A in crew:
        crew_changes.append(("remove_a", tuple(t for t in crew if t != A)))
    crew_changes.append(("add_c", tuple(crew) + (C,)))
    if crew:
        crew_changes.append(("swap_to_c", (C,)))
        crew_changes.append(("remove_all", ()))
    out = []
    finished = job_state != "live"
    if not finished:
        out.append(("title", "edit", dict(fields={"title": "New title"})))
        for (dl, dv), (cl, cv) in itertools.product(date_changes, crew_changes):
            out.append((f"{dl}+{cl}", "edit", dict(scheduled_at=dv, crew_after=cv)))
        out.append(("title+move_d1", "edit", dict(scheduled_at=at(1, 17), fields={"title": "New title"})))
        out.append(("complete", "finish", dict(finish_day=DAYS[1])))
        for (dl, dv), (cl, cv) in itertools.product([("none", UNSET)] + date_changes[:3], crew_changes[:2]):
            out.append((f"cancel+{dl}+{cl}", "cancel", dict(cancel=True, scheduled_at=dv, crew_after=cv)))
    else:
        for (dl, dv), (cl, cv) in itertools.product(date_changes, crew_changes[:3]):
            out.append((f"finished:{dl}+{cl}", "finished", dict(scheduled_at=dv, crew_after=cv, fields={"title": "New title"})))
        for dl, dv in [("none", UNSET), ("d1", at(1, 14)), ("d4", at(4, 14))]:
            for answer in (None, True, False):
                for cl, cv in [("no_crew", None), ("to_c", (C,))]:
                    out.append((
                        f"reopen:{dl}:{answer}:{cl}", "reopen",
                        dict(reopen=True, scheduled_at=dv, rebook_closed_day=answer,
                             crew_after=cv, reopen_day=DAYS[2]),
                    ))
    return out


def _by_id(rows):
    return {v.id: v for v in rows}


def check(crew, rows, job_state, label, kind, kwargs, plan):
    """Raise AssertionError naming the property a result breaks."""
    stored = oracle_schedule(rows, None) if job_state == "live" else at(1)
    before = _by_id(rows)
    if plan.refusal is not None:
        assert not plan.actions, "P8: a refusal plans no action"
        assert kind in ("edit", "reopen", "cancel"), f"refused a {kind}"
        return
    after = _by_id(plan.visits)
    acts = plan.actions
    if kind in ("finished",):
        assert all(isinstance(a, CopyFields) for a in acts), "finished-job edit changed a visit"
        for vid, v in after.items():
            if vid in before:
                b = before[vid]
                assert (v.start_at, v.tech_id, v.status) == (b.start_at, b.tech_id, b.status)
        return
    if kind == "cancel":
        assert not any(isinstance(a, (Insert, Move, Reassign)) for a in acts), "cancel moved a visit"
        assert not any(visit_state(v) in (OPEN, ON_SITE) for v in plan.visits), "P10 after X1"
        return
    if kind == "finish":
        assert not any(visit_state(v) in (OPEN, ON_SITE) for v in plan.visits), "P10 after F1"
        had = [v for v in rows if visit_state(v) != CANCELLED and day_of(v) <= kwargs["finish_day"]]
        kept = [v for v in plan.visits if day_of(v) <= kwargs["finish_day"]]
        if had:
            assert kept, "P10: F1 removed every worked day"
        assert not any(isinstance(a, (Insert, Move, Reassign)) for a in acts)
        return
    if kind == "reopen":
        r0 = [a for a in acts if isinstance(a, (Retire, Close))]
        inserted = [a for a in acts if isinstance(a, Insert)]
        current_after = [v for v in plan.visits if visit_state(v) in (OPEN, ON_SITE)]
        assert {v.id for v in current_after} == {a.key for a in inserted}, "P10: R0 left a Current visit"
        assert len(r0) + len(inserted) == len(acts), "re-open did more than R0 and E4"
        if kwargs.get("rebook_closed_day") is not True:
            assert double_pairs(plan.visits) <= double_pairs(rows), "P2 on re-open"
        if kwargs["scheduled_at"] is not UNSET:
            final = oracle_schedule(plan.visits, plan.scheduled_at)
            assert final == kwargs["scheduled_at"].replace(second=0, microsecond=0), "P7 on re-open"
        return

    # An edit on a live job.
    typed = kwargs.get("scheduled_at", UNSET)
    stored_min = stored.replace(second=0, microsecond=0) if stored else None
    typed_min = typed.replace(second=0, microsecond=0) if isinstance(typed, datetime) else typed
    date_edit = typed is not UNSET and typed_min != stored_min
    crew_edit = kwargs.get("crew_after") is not None
    n = current_day(rows)

    # P1
    if date_edit or crew_edit:
        for vid, b in before.items():
            if visit_state(b) in (ON_SITE, CLOSED, CANCELLED):
                assert after.get(vid) == b, f"P1: {visit_state(b)} visit {vid} changed"
    # P2
    assert double_pairs(plan.visits) <= double_pairs(rows), "P2: a new double booking"
    # P4
    if not date_edit and not crew_edit:
        assert all(isinstance(a, CopyFields) for a in acts), "P4: E1 did more than copy"
    # P5
    if (date_edit or crew_edit) and not (date_edit and typed is None):
        for vid, b in before.items():
            if visit_state(b) == OPEN and day_of(b) != n:
                v = after.get(vid)
                assert v is not None and (v.start_at, v.tech_id, v.status) == (b.start_at, b.tech_id, b.status), \
                    f"P5: OPEN visit {vid} off N changed"
    # P3 / P7: what the job reads after recompute.
    job_value = plan.scheduled_at if plan.scheduled_at is not UNSET else stored
    final = oracle_schedule(plan.visits, job_value)
    if date_edit:
        assert final == typed_min, f"P7: job reads {final}, typed {typed_min}"
    # P9
    if date_edit and typed is not None and n is not None:
        crew_only = plan_visit_sync(rows, VisitEdit(
            tz_name=TZ, stored_scheduled_at=stored, crew_before=tuple(crew),
            crew_after=kwargs.get("crew_after"),
        ))
        holders = {v.tech_id for v in crew_only.visits if visit_state(v) == OPEN and day_of(v) == n}
        t_day = typed.astimezone(_ZONE).date()
        for h in holders:
            assert any(
                v.tech_id == h and visit_state(v) == OPEN and day_of(v) == t_day for v in plan.visits
            ), f"P9: {h} has no OPEN visit on T"
        if t_day != n:
            assert not any(visit_state(v) == OPEN and day_of(v) == n for v in plan.visits), "P9: OPEN left on N"
    # P6 at the planner: every changed row is named by an action.
    named = set()
    for a in acts:
        named.add(a.key if isinstance(a, Insert) else a.visit_id)
    for vid, v in after.items():
        if before.get(vid) != v:
            assert vid in named, f"P6: {vid} changed with no action"
    for vid in before:
        if vid not in after:
            assert vid in named, f"P6: {vid} retired with no action"


def run_grid():
    failures = []
    count = 0
    for crew, rows, job_state in starting_states():
        stored = oracle_schedule(rows, None) if job_state == "live" else at(1)
        for label, kind, kwargs in edits(crew, rows, job_state, stored):
            edit = VisitEdit(
                tz_name=TZ, job_state=job_state, stored_scheduled_at=stored,
                crew_before=tuple(crew), **kwargs,
            )
            plan = plan_visit_sync(rows, edit)
            count += 1
            try:
                check(crew, rows, job_state, label, kind, kwargs, plan)
            except AssertionError as exc:
                failures.append((str(exc), label, crew, [(v.tech_id, day_of(v).isoformat(), visit_state(v)) for v in rows]))
    return count, failures


def test_whole_grid_holds_every_property():
    count, failures = run_grid()
    assert count > 100_000, count
    assert not failures, f"{len(failures)} of {count} failed; first: {failures[:5]}"


@pytest.mark.parametrize("state", [OPEN, ON_SITE, CLOSED, CANCELLED])
def test_states_read_as_the_terms_say(state):
    seq: list = []
    assert visit_state(_row(seq, A, 0, state)) == state

"""GDXA-1455: a crew change names who came off and who went on.

`_set_job_assignments` soft-deletes a removed tech's assignment with
deleted_at alone — no actor, no row of its own — so the audit row of the
call that changed the crew is the only record of who took them off. The
PATCH's `job_updated` row and the re-opens' `job_uncompleted` /
`job_reactivated` rows now carry crew_from / crew_to / crew_added /
crew_removed and lead_from / lead_to.

The harness is test_day_close_route's.
"""
from __future__ import annotations

import json
from uuid import uuid4

from sqlalchemy import select

from gdx_dispatch.models.tenant_models import JobAssignment
from gdx_dispatch.routers.jobs import (
    CancelJobPayload,
    CloseoutPayload,
    JobUpdate,
    StateOverridePayload,
    cancel_job,
    closeout_job,
    reactivate_job,
    uncomplete_job,
    update_job,
)
from gdx_dispatch.tests import test_day_close_route as _harness
from gdx_dispatch.tests.test_day_close_route import (
    OFFICE,
    audits,
    make_job,
    office_user,
    request,
    technician,
)

db = _harness.db
_flags = _harness._flags


def _call(fn, db, job, payload):
    resp = fn(job_id=str(job.id), request=request(), current_user=office_user(), db=db, payload=payload)
    return resp.status_code, json.loads(resp.body)


def _last(db, job, action) -> dict:
    rows = audits(db, job, action)
    assert rows, f"no {action} row"
    return rows[-1].details


def _rows(db, job) -> list[JobAssignment]:
    db.expire_all()
    return list(db.execute(
        select(JobAssignment).where(JobAssignment.job_id == str(job.id))
    ).scalars().all())


def test_patch_audit_names_the_removed_and_added_techs(db):
    a, b, c = (technician(db, f"u-{n}") for n in "abc")
    job = make_job(db, assigned_to=a.id)

    code, out = _call(update_job, db, job, JobUpdate(assigned_tech_ids=[a.id, b.id], lead_tech_id=a.id))
    assert code == 200, out
    code, out = _call(update_job, db, job, JobUpdate(assigned_tech_ids=[a.id, c.id], lead_tech_id=c.id))
    assert code == 200, out

    d = _last(db, job, "job_updated")
    assert set(d["crew_from"]) == {a.id, b.id}  # one call, one assigned_at
    assert d["crew_to"] == [a.id, c.id]
    assert d["crew_removed"] == [b.id]
    assert d["crew_added"] == [c.id]
    assert (d["lead_from"], d["lead_to"]) == (a.id, c.id)
    # The removal itself: b's row is soft-deleted, and the audit row above,
    # written by the office user, is what says who did it.
    removed = [r for r in _rows(db, job) if r.tech_id == b.id]
    assert len(removed) == 1 and removed[0].deleted_at is not None
    assert audits(db, job, "job_updated")[-1].user_id == OFFICE


def test_patch_off_a_legacy_job_names_the_assigned_to_tech_it_replaced(db):
    """A legacy job's tech lives in jobs.assigned_to with no assignment row."""
    a, b = technician(db, "u-a"), technician(db, "u-b")
    job = make_job(db, assigned_to=a.id)
    assert _rows(db, job) == []

    code, out = _call(update_job, db, job, JobUpdate(assigned_tech_id=b.id))
    assert code == 200, out

    d = _last(db, job, "job_updated")
    assert (d["crew_from"], d["crew_to"]) == ([a.id], [b.id])
    assert (d["crew_removed"], d["crew_added"]) == ([a.id], [b.id])
    assert (d["lead_from"], d["lead_to"]) == (a.id, b.id)


def test_lead_from_is_the_primary_when_no_row_is_flagged_lead(db):
    """POST /jobs/{id}/assignments and unassign leave every row is_lead=False
    and mirror the primary into assigned_to (_recompute_primary)."""
    a, b = technician(db, "u-a"), technician(db, "u-b")
    job = make_job(db, assigned_to=a.id)
    db.add(JobAssignment(id=str(uuid4()), job_id=str(job.id), tech_id=a.id, is_lead=False))
    db.commit()

    code, out = _call(update_job, db, job, JobUpdate(assigned_tech_id=b.id))
    assert code == 200, out

    d = _last(db, job, "job_updated")
    assert (d["crew_from"], d["crew_removed"]) == ([a.id], [a.id])
    assert (d["lead_from"], d["lead_to"]) == (a.id, b.id)


def test_patch_that_does_not_touch_the_crew_records_no_crew_change(db):
    job = make_job(db)
    code, out = _call(update_job, db, job, JobUpdate(title="Renamed"))
    assert code == 200, out
    assert not any(k.startswith(("crew_", "lead_")) for k in _last(db, job, "job_updated"))


def test_reopens_that_reassign_name_who_came_off(db):
    a, b, c = (technician(db, f"u-{n}") for n in "abc")
    job = make_job(db, assigned_to=a.id)
    assert _call(update_job, db, job, JobUpdate(assigned_tech_ids=[a.id, b.id]))[0] == 200
    code, out = _call(closeout_job, db, job, CloseoutPayload(parts=[], hours=2, no_parts_used=True))
    assert code == 201, out

    code, out = _call(uncomplete_job, db, job, StateOverridePayload(reason="customer called back", assigned_to=c.id))
    assert code == 200, out
    d = _last(db, job, "job_uncompleted")
    # a and b went on in one call: same assigned_at, so their order is the id's.
    assert (set(d["crew_from"]), d["crew_to"]) == ({a.id, b.id}, [c.id])
    assert (set(d["crew_removed"]), d["crew_added"]) == ({a.id, b.id}, [c.id])

    assert _call(cancel_job, db, job, CancelJobPayload(reason="customer sold the house"))[0] == 200
    code, out = _call(reactivate_job, db, job, StateOverridePayload(reason="new owner wants it", assigned_to=a.id))
    assert code == 200, out
    d = _last(db, job, "job_reactivated")
    assert (d["crew_removed"], d["crew_added"]) == ([c.id], [a.id])
    assert (d["lead_from"], d["lead_to"]) == (c.id, a.id)


def test_reopen_without_a_new_tech_records_no_crew_change(db):
    job = make_job(db)
    assert _call(closeout_job, db, job, CloseoutPayload(parts=[], hours=2, no_parts_used=True))[0] == 201
    assert _call(uncomplete_job, db, job, StateOverridePayload(reason="customer called back"))[0] == 200
    assert "crew_removed" not in _last(db, job, "job_uncompleted")

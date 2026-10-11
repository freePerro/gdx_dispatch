"""GDXA-599: the starting and finishing doors refuse a job already past that
stage, /complete needs a closeout, and every stage-changing audit row says
which stage the job moved from and to, inside the change's own transaction.

The harness is test_day_close_route's.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import HTTPException

from gdx_dispatch.models.tenant_models import Job, JobCloseout
from gdx_dispatch.routers.jobs import (
    CancelJobPayload,
    CloseoutPayload,
    CloseWithoutWorkPayload,
    JobCompletePayload,
    JobUpdate,
    StateOverridePayload,
    cancel_job,
    close_job_without_work,
    closeout_job,
    complete_job,
    reactivate_job,
    start_job,
    uncomplete_job,
    update_job,
)
from gdx_dispatch.tests import test_day_close_route as _harness
from gdx_dispatch.tests.test_day_close_route import audits, make_job, office_user, request

db = _harness.db
_flags = _harness._flags


def _call(fn, db, job, payload=None):
    kw = {"job_id": str(job.id), "request": request(), "current_user": office_user(), "db": db}
    if payload is not None:
        kw["payload"] = payload
    resp = fn(**kw)
    return resp.status_code, json.loads(resp.body)


def start(db, job):
    return _call(start_job, db, job)


def complete(db, job):
    return _call(complete_job, db, job, JobCompletePayload())


def closeout(db, job):
    return _call(closeout_job, db, job, CloseoutPayload(parts=[], hours=2, no_parts_used=True))


def uncomplete(db, job):
    return _call(uncomplete_job, db, job, StateOverridePayload(reason="customer called back"))


def fresh(db, job) -> Job:
    db.expire_all()
    return db.get(Job, job.id)


def only(db, job, action) -> dict:
    rows = audits(db, job, action)
    assert len(rows) == 1, [r.details for r in rows]
    return rows[0].details


def _finished(db) -> Job:
    job = make_job(db)
    code, out = closeout(db, job)
    assert code == 201, out
    assert fresh(db, job).lifecycle_stage == "completed"
    return job


# ------------------------------------------------------------ [2] refusals


def test_start_refuses_a_completed_job_and_leaves_it_finished(db):
    job = _finished(db)
    completed_at = fresh(db, job).completed_at

    code, out = start(db, job)

    assert code == 409 and out["code"] == "job_completed" and out["use"] == "reopen", out
    row = fresh(db, job)
    assert row.lifecycle_stage == "completed" and row.status == "Completed"
    assert row.completed_at == completed_at
    assert audits(db, job, "job_started") == []


def test_complete_refuses_a_completed_job_without_restamping_it(db):
    job = _finished(db)
    completed_at = fresh(db, job).completed_at

    code, out = complete(db, job)

    assert code == 409 and out["code"] == "job_completed", out
    assert out["detail"] == "This job is already completed."  # close-without-work's wording
    assert fresh(db, job).completed_at == completed_at
    assert audits(db, job, "job_completed") == []


def test_close_without_work_still_refuses_a_completed_job(db):
    job = _finished(db)
    code, _ = _call(close_job_without_work, db, job, CloseWithoutWorkPayload(reason="duplicate job"))
    assert code == 409


def test_closeout_on_a_completed_job_is_still_a_restatement(db):
    """The supersede path stays open: the triage ruled it deliberate."""
    job = _finished(db)
    code, out = closeout(db, job)
    assert code == 201, out
    rows = audits(db, job, "job_closeout")
    assert len(rows) == 2
    assert rows[1].details["from_stage"] == rows[1].details["to_stage"] == "completed"


# ---------------------------------------------------------- [4] closeout


def test_complete_refuses_a_job_with_no_closeout(db):
    job = make_job(db)
    code, out = complete(db, job)
    assert code == 409 and out["code"] == "closeout_required" and out["use"] == "closeout", out
    assert fresh(db, job).lifecycle_stage == "in_progress"
    assert audits(db, job, "job_completed") == []


def test_complete_refinishes_a_reopened_job_on_its_closeout(db):
    job = _finished(db)
    assert uncomplete(db, job)[0] == 200

    code, out = complete(db, job)

    assert code == 200, out
    assert fresh(db, job).lifecycle_stage == "completed"
    d = only(db, job, "job_completed")
    assert (d["from_stage"], d["to_stage"]) == ("in_progress", "completed")
    assert (d["from_status"], d["to_status"]) == ("In Progress", "Completed")


def test_a_superseded_closeout_does_not_count(db):
    """Only the CURRENT closeout attests; a superseded row is history."""
    job = make_job(db)
    db.add(JobCloseout(
        id=uuid4(), job_id=job.id, hours_worked=1, techs_on_site=1,
        closed_by_user_id=str(uuid4()), closed_at=datetime.now(UTC),
        superseded_at=datetime.now(UTC),
    ))
    db.commit()
    code, out = complete(db, job)
    assert code == 409 and out["code"] == "closeout_required", out


# ------------------------------------------------------ [3] from/to + txn


def test_start_audits_from_and_to_and_rolls_back_with_its_audit(db, monkeypatch):
    import gdx_dispatch.core.audit as audit_mod

    job = make_job(db, stage="scheduled")
    real = audit_mod.log_audit_event_sync

    def boom(*a, **k):
        if k.get("action") == "job_started":
            raise RuntimeError("audit store down")
        return real(*a, **k)

    monkeypatch.setattr(audit_mod, "log_audit_event_sync", boom)
    with pytest.raises(HTTPException) as exc:
        start(db, job)
    assert exc.value.status_code == 500
    assert fresh(db, job).lifecycle_stage == "scheduled"
    assert fresh(db, job).started_at is None

    monkeypatch.setattr(audit_mod, "log_audit_event_sync", real)
    code, out = start(db, job)
    assert code == 200, out
    d = only(db, job, "job_started")
    assert (d["from_stage"], d["to_stage"]) == ("scheduled", "in_progress")
    assert d["to_status"] == "In Progress"


def test_update_job_audits_from_and_to_and_rolls_back_with_its_audit(db, monkeypatch):
    import gdx_dispatch.core.audit as audit_mod

    job = make_job(db)
    real = audit_mod.log_audit_event_sync

    def boom(*a, **k):
        if k.get("action") == "job_updated":
            raise RuntimeError("audit store down")
        return real(*a, **k)

    monkeypatch.setattr(audit_mod, "log_audit_event_sync", boom)
    with pytest.raises(HTTPException) as exc:
        _call(update_job, db, job, JobUpdate(title="Renamed"))
    assert exc.value.status_code == 500
    assert fresh(db, job).title == "Door repair"

    monkeypatch.setattr(audit_mod, "log_audit_event_sync", real)
    code, out = _call(update_job, db, job, JobUpdate(status="Scheduled"))
    assert code == 200, out
    d = only(db, job, "job_updated")
    assert "lifecycle_stage" in d["fields"]
    assert (d["from_stage"], d["to_stage"]) == ("in_progress", "scheduled")
    assert (d["from_status"], d["to_status"]) == ("In Progress", "Scheduled")


def test_reopen_cancel_and_reactivate_audit_from_and_to(db):
    job = _finished(db)
    d = only(db, job, "job_closeout")
    assert (d["from_stage"], d["to_stage"]) == ("in_progress", "completed")

    assert uncomplete(db, job)[0] == 200
    d = only(db, job, "job_uncompleted")
    assert (d["from_stage"], d["to_stage"]) == ("completed", "in_progress")

    assert _call(cancel_job, db, job, CancelJobPayload(reason="customer sold the house"))[0] == 200
    d = only(db, job, "job_cancelled")
    assert (d["from_stage"], d["to_stage"]) == ("in_progress", "cancelled")

    assert _call(reactivate_job, db, job, StateOverridePayload(reason="new owner wants it"))[0] == 200
    d = only(db, job, "job_reactivated")
    assert d["from_stage"] == "cancelled"
    assert d["to_stage"] == fresh(db, job).lifecycle_stage


def test_close_without_work_audits_from_and_to(db):
    job = make_job(db)
    code, _ = _call(close_job_without_work, db, job, CloseWithoutWorkPayload(reason="duplicate job"))
    assert code == 200
    d = only(db, job, "job_closed_without_work")
    assert (d["prior_stage"], d["from_stage"], d["to_stage"]) == ("in_progress", "in_progress", "completed")

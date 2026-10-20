"""A re-opened job's dispatch starts over (GDXA-1929).

/uncomplete flipped the stage back to in_progress and left dispatch_status
at "done", the terminal value the completion wrote. The phone gates every
tap on it (mobile._transition_gate), so the re-opened job could never go en
route again and dropped off the tech's list. /reactivate had the same shape
for a cancelled job carrying "done" when the office named no tech. Both
re-opens now set dispatch_status from the crew the job has after the
re-open, and record the from/to pair in their audit row.

Driven through the real routes (jobs and mobile) with the fixture and
seeders of test_arrival_undo.
"""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from gdx_dispatch.core.audit import AuditLog
from gdx_dispatch.models.tenant_models import Job
from gdx_dispatch.tests.test_arrival_undo import (  # noqa: F401  (ctx is a fixture)
    TECH,
    TECH_USER,
    _fresh,
    _job,
    ctx,
)


def _audit(db, job, action) -> dict:
    db.expire_all()
    rows = db.execute(
        select(AuditLog).where(AuditLog.entity_id == str(job.id), AuditLog.action == action)
    ).scalars().all()
    assert len(rows) == 1, [r.details for r in rows]
    return rows[0].details


def test_closed_then_uncompleted_job_can_go_en_route_again(ctx):  # noqa: F811
    client, db, be = ctx
    job = _job(db, crew=(TECH,))
    # The shared completion write (_mark_job_completed) that /complete uses.
    r = client.post(f"/api/jobs/{job.id}/close-without-work", json={"reason": "duplicate visit"})
    assert r.status_code == 200, r.text
    assert _fresh(db, Job, job.id).dispatch_status == "done"

    r = client.post(f"/api/jobs/{job.id}/uncomplete", json={"reason": "customer called back"})
    assert r.status_code == 200, r.text
    j = _fresh(db, Job, job.id)
    assert (j.lifecycle_stage, j.dispatch_status) == ("in_progress", "assigned")
    assert _audit(db, job, "job_uncompleted")["dispatch_status"] == {"from": "done", "to": "assigned"}

    be(TECH_USER, "technician")
    r = client.post(f"/api/mobile/jobs/{job.id}/en-route", json={})
    assert r.status_code == 200, r.text
    assert _fresh(db, Job, job.id).dispatch_status == "en_route"


def test_uncompleted_job_with_no_crew_reads_unassigned(ctx):  # noqa: F811
    client, db, _ = ctx
    job = _job(db, crew=(), stage="completed", dispatch_status="done")
    j = _fresh(db, Job, job.id)
    j.status, j.completed_at = "Completed", datetime.now(UTC)
    db.commit()

    r = client.post(f"/api/jobs/{job.id}/uncomplete", json={"reason": "customer called back"})
    assert r.status_code == 200, r.text
    assert _fresh(db, Job, job.id).dispatch_status == "unassigned"
    assert _audit(db, job, "job_uncompleted")["dispatch_status"] == {"from": "done", "to": "unassigned"}


def test_uncomplete_naming_a_tech_reads_assigned(ctx):  # noqa: F811
    client, db, _ = ctx
    job = _job(db, crew=(), stage="completed", dispatch_status="done")
    j = _fresh(db, Job, job.id)
    j.status, j.completed_at = "Completed", datetime.now(UTC)
    db.commit()

    r = client.post(f"/api/jobs/{job.id}/uncomplete",
                    json={"reason": "customer called back", "assigned_to": TECH})
    assert r.status_code == 200, r.text
    d = _audit(db, job, "job_uncompleted")
    assert d["dispatch_status"] == {"from": "done", "to": "assigned"}
    assert "crew_to" in d  # the crew change still rides the same row


def test_reactivated_job_that_carried_done_reads_assigned_without_naming_a_tech(ctx):  # noqa: F811
    client, db, _ = ctx
    # "done" on a job that never completed: the phone's old complete tap
    # (mobile_job_complete, deleted 2026-10-19, GDXA-1937) wrote it without
    # moving the stage, and rows it wrote still carry it.
    job = _job(db, crew=(TECH,), stage="in_progress", dispatch_status="done")
    r = client.post(f"/api/jobs/{job.id}/cancel", json={"reason": "customer went elsewhere"})
    assert r.status_code == 200, r.text

    r = client.post(f"/api/jobs/{job.id}/reactivate", json={"reason": "customer called back"})
    assert r.status_code == 200, r.text
    assert _fresh(db, Job, job.id).dispatch_status == "assigned"
    assert _audit(db, job, "job_reactivated")["dispatch_status"] == {"from": "done", "to": "assigned"}


def test_reactivate_naming_a_tech_reads_assigned_and_keeps_the_crew_change(ctx):  # noqa: F811
    client, db, _ = ctx
    job = _job(db, crew=(), stage="in_progress", dispatch_status="en_route")
    r = client.post(f"/api/jobs/{job.id}/cancel", json={"reason": "customer went elsewhere"})
    assert r.status_code == 200, r.text

    r = client.post(f"/api/jobs/{job.id}/reactivate",
                    json={"reason": "customer called back", "assigned_to": TECH})
    assert r.status_code == 200, r.text
    assert _fresh(db, Job, job.id).dispatch_status == "assigned"
    d = _audit(db, job, "job_reactivated")
    assert d["dispatch_status"] == {"from": "en_route", "to": "assigned"}
    assert "crew_to" in d

"""PATCH /api/jobs/{id} keeps a finished job's crew and lane, and an under-way
job's crew (GDXA-1499).

The stage guard in update_job runs only when a stage is requested, so until
this change `assigned_to`, `assigned_tech_ids` and `holding_area_id` were
written on any stage. A client holding a stale or unread crew (the Jobs list
edit dialog, before its read landed) could empty a crew or cut it to one. A
resend of the stored crew stays accepted: that dialog resends it every save.

Every refusal below has a control request beside it that the same fixture
accepts, so a 409 cannot be the route refusing everything.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text

from gdx_dispatch.models.tenant_models import HoldingArea, JobAssignment
from gdx_dispatch.tests.test_jobs_write_authz import ASSIGNED_TECH, STRANGER_TECH, _call
from gdx_dispatch.tests.test_jobs_write_authz import ctx as _authz_ctx  # noqa: F401


@pytest.fixture
def ctx(_authz_ctx):  # noqa: F811 — the fixture param IS the import
    return _authz_ctx


def _job(db, job_id):
    raw = str(job_id)
    return db.execute(
        text(
            "SELECT assigned_to, holding_area_id, title FROM jobs "
            "WHERE CAST(id AS TEXT) IN (:j, :jh)"
        ),
        {"j": raw, "jh": raw.replace("-", "").lower()},
    ).first()


def _live_crew(db, job_id):
    return sorted(
        r[0] for r in db.execute(
            text("SELECT tech_id FROM job_assignments WHERE job_id = :j AND deleted_at IS NULL"),
            {"j": str(job_id)},
        ).all()
    )


def _set(db, job, **fields):
    for k, v in fields.items():
        setattr(job, k, v)
    db.commit()


def _crew(db, job, techs, lead):
    for t in techs:
        db.add(JobAssignment(id=str(uuid4()), job_id=str(job.id), tech_id=t,
                             is_lead=(t == lead)))
    db.commit()


def _office(be):
    be(str(uuid4()), "dispatcher")


class TestAFinishedJobKeepsItsCrewAndLane:
    """The fixture job is completed, assigned to ASSIGNED_TECH."""

    @pytest.mark.parametrize("body", [
        {"assigned_tech_ids": []},
        {"assigned_tech_ids": [STRANGER_TECH]},
        {"assigned_tech_ids": [ASSIGNED_TECH, STRANGER_TECH]},
        {"assigned_to": None},
        {"assigned_tech_id": STRANGER_TECH},
    ])
    def test_a_crew_change_is_refused(self, ctx, body):
        client, db, job, _dep, be, _ = ctx
        _office(be)
        r = _call(client, "PATCH", f"/api/jobs/{job.id}", body)
        assert r.status_code == 409, r.text[:300]
        assert r.json()["use"] == "reopen"
        db.expire_all()
        assert _job(db, job.id)[0] == ASSIGNED_TECH
        assert _live_crew(db, job.id) == []

    def test_a_cancelled_job_is_refused_too(self, ctx):
        client, db, job, _dep, be, _ = ctx
        _set(db, job, lifecycle_stage="cancelled", status="Cancelled", completed_at=None)
        _office(be)
        r = _call(client, "PATCH", f"/api/jobs/{job.id}", {"assigned_tech_ids": []})
        assert r.status_code == 409, r.text[:300]
        db.expire_all()
        assert _job(db, job.id)[0] == ASSIGNED_TECH

    def test_a_lane_change_is_refused(self, ctx):
        client, db, job, _dep, be, _ = ctx
        lane = str(uuid4())
        _set(db, job, holding_area_id=lane)
        _office(be)
        r = _call(client, "PATCH", f"/api/jobs/{job.id}", {"holding_area_id": None})
        assert r.status_code == 409, r.text[:300]
        db.expire_all()
        assert _job(db, job.id)[1] == lane

    def test_the_jobs_dialog_resend_is_accepted_and_writes_no_crew(self, ctx):
        """Control: the Jobs list edit dialog's whole payload on a finished
        job, crew unchanged. The title edit lands; the crew is not rewritten
        (no assignment rows minted for the legacy assigned_to)."""
        client, db, job, _dep, be, _ = ctx
        lane = str(uuid4())
        db.add(HoldingArea(id=lane, company_id=job.company_id, name="Waiting on Parts"))
        _set(db, job, holding_area_id=lane)
        _office(be)
        r = _call(client, "PATCH", f"/api/jobs/{job.id}", {
            "title": "Renamed",
            "assigned_tech_ids": [ASSIGNED_TECH],
            "lead_tech_id": None,
            "assigned_tech_id": ASSIGNED_TECH,
            "holding_area_id": lane,
            "status": "Complete",
            "lifecycle_stage": "Complete",
        })
        assert r.status_code == 200, r.text[:300]
        db.expire_all()
        assigned_to, holding, title = _job(db, job.id)
        assert (assigned_to, holding, title) == (ASSIGNED_TECH, lane, "Renamed")
        assert _live_crew(db, job.id) == []

    def test_a_crew_resend_alone_writes_nothing(self, ctx):
        client, db, job, _dep, be, _ = ctx
        _office(be)
        r = _call(client, "PATCH", f"/api/jobs/{job.id}", {"assigned_tech_ids": [ASSIGNED_TECH]})
        assert r.status_code == 200, r.text[:300]
        n = db.execute(text(
            "SELECT COUNT(*) FROM audit_logs WHERE action='job_updated' AND entity_id=:e"
        ), {"e": str(job.id)}).scalar()
        assert n == 0

    def test_a_crew_job_resend_with_its_lead_is_accepted(self, ctx):
        client, db, job, _dep, be, _ = ctx
        _crew(db, job, [ASSIGNED_TECH, STRANGER_TECH], lead=STRANGER_TECH)
        _office(be)
        r = _call(client, "PATCH", f"/api/jobs/{job.id}", {
            "title": "Renamed",
            "assigned_tech_ids": [ASSIGNED_TECH, STRANGER_TECH],
            "lead_tech_id": STRANGER_TECH,
        })
        assert r.status_code == 200, r.text[:300]
        db.expire_all()
        assert _live_crew(db, job.id) == sorted([ASSIGNED_TECH, STRANGER_TECH])

    def test_a_lead_change_is_refused(self, ctx):
        client, db, job, _dep, be, _ = ctx
        _crew(db, job, [ASSIGNED_TECH, STRANGER_TECH], lead=STRANGER_TECH)
        _office(be)
        r = _call(client, "PATCH", f"/api/jobs/{job.id}", {
            "assigned_tech_ids": [ASSIGNED_TECH, STRANGER_TECH],
            "lead_tech_id": ASSIGNED_TECH,
        })
        assert r.status_code == 409, r.text[:300]


class TestAnUnderWayJobKeepsACrew:
    @pytest.fixture
    def started(self, ctx):
        client, db, job, _dep, be, _ = ctx
        _set(db, job, lifecycle_stage="in_progress", status="In Progress", completed_at=None)
        _office(be)
        return client, db, job

    @pytest.mark.parametrize("body", [
        {"assigned_tech_ids": []},
        {"assigned_to": None},
        {"assigned_tech_id": None, "assigned_tech_ids": [], "lead_tech_id": None},
    ])
    def test_emptying_the_crew_is_refused(self, started, body):
        client, db, job = started
        r = _call(client, "PATCH", f"/api/jobs/{job.id}", body)
        assert r.status_code == 409, r.text[:300]
        assert r.json()["use"] == "crew"
        db.expire_all()
        assert _job(db, job.id)[0] == ASSIGNED_TECH

    def test_a_swap_is_accepted(self, started):
        """Control: an under-way job may change who is on it, just not be
        left with nobody."""
        client, db, job = started
        r = _call(client, "PATCH", f"/api/jobs/{job.id}", {"assigned_tech_ids": [STRANGER_TECH]})
        assert r.status_code == 200, r.text[:300]
        db.expire_all()
        assert _job(db, job.id)[0] == STRANGER_TECH
        assert _live_crew(db, job.id) == [STRANGER_TECH]

    def test_its_lane_can_change(self, started):
        """Dispatch parks an under-way job with its crew on (GDXA-1370)."""
        client, db, job = started
        _set(db, job, holding_area_id=str(uuid4()))
        r = _call(client, "PATCH", f"/api/jobs/{job.id}", {"holding_area_id": None})
        assert r.status_code == 200, r.text[:300]
        db.expire_all()
        assert _job(db, job.id)[1] is None


def test_a_scheduled_job_can_still_be_unassigned(ctx):
    """Control: the guard is stage-specific; a job not yet started may lose
    its crew (the dispatch board's drop into the queue does exactly that)."""
    client, db, job, _dep, be, _ = ctx
    _set(db, job, lifecycle_stage="scheduled", status="Scheduled", completed_at=None)
    _office(be)
    r = _call(client, "PATCH", f"/api/jobs/{job.id}", {"assigned_tech_ids": []})
    assert r.status_code == 200, r.text[:300]
    db.expire_all()
    assert _job(db, job.id)[0] is None

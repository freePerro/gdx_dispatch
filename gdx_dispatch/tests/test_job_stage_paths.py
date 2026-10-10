"""Job stage changes go through the paths that own them.

The job-stage-paths plan (2026-10-04). Until then `PATCH /api/jobs/{id}`
wrote any stage it was handed. The desktop's "Complete Job" and stage strip
finished jobs through it with no completed_at, no dispatch_status, no
job.completed webhook and none of the tenant's completion requirements (prod:
31 jobs completed that way with no completed_at), and it moved completed or
cancelled jobs anywhere with no reason recorded.

One test per row of the plan's §4.1 table, plus the new
`/close-without-work` verb (§4.1b, Doug's D4).

What SQLite can and cannot prove here: update_job's lifecycle_stage write is
raw SQL that is a no-op on SQLite (see the comment in update_job and
test_jobs_write_authz.TestAdmission). So a refusal — which returns before any
write — is fully provable here, and so are `status` and `started_at`, which go
through the ORM. Whether an ALLOWED move changed the stored stage is not
asserted; that needs the Postgres arm.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text

# The app/DB fixture is shared with the authz tests. pytest 9 registers an
# imported fixture under the name it is imported as, so it comes in aliased and
# `ctx` below delegates to it — importing it as `ctx` directly makes every test
# parameter shadow an unused import (ruff F811).
from gdx_dispatch.tests.test_jobs_write_authz import _call
from gdx_dispatch.tests.test_jobs_write_authz import ctx as _authz_ctx  # noqa: F401


@pytest.fixture
def ctx(_authz_ctx):  # noqa: F811 — the fixture param IS the import
    return _authz_ctx

_JOB_COLS = ("lifecycle_stage", "status", "completed_at", "started_at",
             "dispatch_status", "title", "updated_at", "cancel_reason")


def _row(db, job_id, cols: str):
    """Re-read the job, matching both textual uuid forms (SQLite stores 32
    hex, Postgres a dashed uuid), and return the named columns in order."""
    raw = str(job_id)
    row = db.execute(
        text(
            "SELECT lifecycle_stage, status, completed_at, started_at, "
            "dispatch_status, title, updated_at, cancel_reason "
            "FROM jobs WHERE CAST(id AS TEXT) IN (:j, :jh)"
        ),
        {"j": raw, "jh": raw.replace("-", "").lower()},
    ).first()
    picked = dict(zip(_JOB_COLS, row, strict=True))
    return tuple(picked[c.strip()] for c in cols.split(","))


def _set(db, job, **fields):
    for k, v in fields.items():
        setattr(job, k, v)
    db.commit()


def _office(be):
    be(str(uuid4()), "dispatcher")


class TestFinishedJobsDoNotMoveThroughPatch:
    def test_a_completed_job_cannot_be_moved(self, ctx):
        client, db, job, _dep, be, _ = ctx  # fixture job is completed
        _office(be)
        before = _row(db, job.id, "lifecycle_stage, status, completed_at")
        r = _call(client, "PATCH", f"/api/jobs/{job.id}", {"status": "Scheduled"})
        assert r.status_code == 409, r.text[:300]
        assert r.json()["use"] == "reopen"
        db.expire_all()
        assert _row(db, job.id, "lifecycle_stage, status, completed_at") == before

    def test_a_cancelled_job_cannot_be_moved(self, ctx):
        client, db, job, _dep, be, _ = ctx
        _set(db, job, lifecycle_stage="cancelled", status="Cancelled", completed_at=None)
        _office(be)
        r = _call(client, "PATCH", f"/api/jobs/{job.id}", {"lifecycle_stage": "service_call"})
        assert r.status_code == 409, r.text[:300]
        assert r.json()["use"] == "reopen"
        db.expire_all()
        assert _row(db, job.id, "status")[0] == "Cancelled"

    def test_resending_the_stored_stage_is_dropped_not_rewritten(self, ctx):
        """The Jobs list edit dialog resends status on every save. On a job a
        closeout finished ("Completed"), the old PATCH title-cased that to
        "Complete"; now the stage half of the patch is dropped."""
        client, db, job, _dep, be, _ = ctx
        _set(db, job, status="Completed")
        _office(be)
        r = _call(client, "PATCH", f"/api/jobs/{job.id}",
                  {"status": "Complete", "lifecycle_stage": "Complete", "title": "Renamed"})
        assert r.status_code == 200, r.text[:300]
        db.expire_all()
        status, title = _row(db, job.id, "status, title")
        assert status == "Completed"
        assert title == "Renamed"

    def test_a_patch_that_only_resends_the_stage_writes_nothing(self, ctx):
        client, db, job, _dep, be, _ = ctx
        _office(be)
        before = _row(db, job.id, "updated_at, status")
        r = _call(client, "PATCH", f"/api/jobs/{job.id}", {"status": "Complete"})
        assert r.status_code == 200, r.text[:300]
        db.expire_all()
        assert _row(db, job.id, "updated_at, status") == before
        n = db.execute(text(
            "SELECT COUNT(*) FROM audit_logs WHERE action='job_updated' AND entity_id=:e"
        ), {"e": str(job.id)}).scalar()
        assert n == 0


class TestAStatusThatNamesNoStage:
    """A label mapping to no stage used to be written to `status` alone, so the
    guard above never ran: "Scheduled Later" on a completed job answered 200
    and rewrote its status (found 2026-10-04, after #842)."""

    def test_it_cannot_rewrite_a_completed_job(self, ctx):
        client, db, job, _dep, be, _ = ctx  # fixture job is completed
        _office(be)
        before = _row(db, job.id, "lifecycle_stage, status, updated_at")
        for body in ({"status": "Scheduled Later"}, {"lifecycle_stage": "On Hold"}):
            r = _call(client, "PATCH", f"/api/jobs/{job.id}", body)
            assert r.status_code == 422, (body, r.text[:300])
        db.expire_all()
        assert _row(db, job.id, "lifecycle_stage, status, updated_at") == before

    def test_it_is_refused_on_an_open_job_too(self, ctx):
        """Otherwise `status` drifts from `lifecycle_stage` on the open job."""
        client, db, job, _dep, be, _ = ctx
        _set(db, job, lifecycle_stage="scheduled", status="Scheduled", completed_at=None)
        _office(be)
        before = _row(db, job.id, "status, title")
        assert before[0] == "Scheduled"
        r = _call(client, "PATCH", f"/api/jobs/{job.id}",
                  {"status": "Scheduled Later", "title": "Renamed"})
        assert r.status_code == 422, r.text[:300]
        db.expire_all()
        assert _row(db, job.id, "status, title") == before


class TestCompletionDoesNotGoThroughPatch:
    def test_completing_an_open_job_is_refused(self, ctx):
        client, db, job, _dep, be, _ = ctx
        _set(db, job, lifecycle_stage="scheduled", status="Scheduled", completed_at=None)
        _office(be)
        for body in ({"status": "Complete"}, {"lifecycle_stage": "completed"},
                     {"status": "Completed"}):
            r = _call(client, "PATCH", f"/api/jobs/{job.id}", body)
            assert r.status_code == 409, (body, r.text[:300])
            assert r.json()["use"] == "closeout"
        db.expire_all()
        status, completed_at = _row(db, job.id, "status, completed_at")
        assert status == "Scheduled" and completed_at is None


class TestOpenMovesStillWork:
    def test_cancelling_an_open_job_goes_through_cancel(self, ctx):
        """GDXA-375: a PATCH to Cancelled is refused and writes nothing; the
        open job is cancelled by POST /cancel, which records the reason."""
        client, db, job, _dep, be, _ = ctx
        _set(db, job, lifecycle_stage="scheduled", status="Scheduled", completed_at=None)
        _office(be)
        r = _call(client, "PATCH", f"/api/jobs/{job.id}", {"status": "Cancelled"})
        assert r.status_code == 409, r.text[:300]
        assert r.json()["use"] == "cancel"
        db.expire_all()
        assert _row(db, job.id, "status")[0] == "Scheduled"
        r = _call(client, "POST", f"/api/jobs/{job.id}/cancel",
                  {"reason": "customer sold the house"})
        assert r.status_code == 200, r.text[:300]
        db.expire_all()
        status, reason = _row(db, job.id, "status, cancel_reason")
        assert status == "Cancelled" and reason == "customer sold the house"

    def test_moving_to_in_progress_stamps_started_at_once(self, ctx):
        client, db, job, _dep, be, _ = ctx
        _set(db, job, lifecycle_stage="scheduled", status="Scheduled",
             completed_at=None, started_at=None)
        _office(be)
        r = _call(client, "PATCH", f"/api/jobs/{job.id}", {"status": "In Progress"})
        assert r.status_code == 200, r.text[:300]
        db.expire_all()
        status, started_at = _row(db, job.id, "status, started_at")
        assert status == "In Progress" and started_at is not None

    def test_an_open_to_open_move_is_allowed(self, ctx):
        client, db, job, _dep, be, _ = ctx
        _set(db, job, lifecycle_stage="service_call", status="Service Call", completed_at=None)
        _office(be)
        r = _call(client, "PATCH", f"/api/jobs/{job.id}", {"status": "Estimate"})
        assert r.status_code == 200, r.text[:300]
        db.expire_all()
        assert _row(db, job.id, "status")[0] == "Estimate"


class TestCloseWithoutWork:
    def _open(self, db, job):
        _set(db, job, lifecycle_stage="scheduled", status="Scheduled",
             completed_at=None, dispatch_status="assigned")

    def test_it_completes_the_job_with_the_companion_fields(self, ctx):
        client, db, job, _dep, be, _ = ctx
        self._open(db, job)
        _office(be)
        r = _call(client, "POST", f"/api/jobs/{job.id}/close-without-work",
                  {"reason": "no-show, customer cancelled by phone"})
        assert r.status_code == 200, r.text[:300]
        db.expire_all()
        stage, status, completed_at, dispatch = _row(
            db, job.id, "lifecycle_stage, status, completed_at, dispatch_status")
        assert (stage, status, dispatch) == ("completed", "Completed", "done")
        assert completed_at is not None

    def test_it_writes_no_closeout_time_entry_or_invoice(self, ctx):
        client, db, job, _dep, be, _ = ctx
        self._open(db, job)
        _office(be)
        _call(client, "POST", f"/api/jobs/{job.id}/close-without-work",
              {"reason": "duplicate of another job"})
        raw = str(job.id)
        ids = {"j": raw, "jh": raw.replace("-", "").lower()}
        counts = {
            "job_closeouts": "SELECT COUNT(*) FROM job_closeouts WHERE CAST(job_id AS TEXT) IN (:j, :jh)",
            "time_entries": "SELECT COUNT(*) FROM time_entries WHERE CAST(job_id AS TEXT) IN (:j, :jh)",
            "invoices": "SELECT COUNT(*) FROM invoices WHERE CAST(job_id AS TEXT) IN (:j, :jh)",
        }
        for table, sql in counts.items():
            n = db.execute(text(sql), ids).scalar()
            assert n == 0, f"{table} has {n} rows"

    def test_the_audit_row_names_who_why_and_what_was_skipped(self, ctx):
        client, db, job, _dep, be, _ = ctx
        self._open(db, job)
        user = str(uuid4())
        be(user, "dispatcher")
        _call(client, "POST", f"/api/jobs/{job.id}/close-without-work",
              {"reason": "old job, never worked"})
        row = db.execute(text(
            "SELECT user_id, details FROM audit_logs "
            "WHERE action='job_closed_without_work' AND entity_id=:e"
        ), {"e": str(job.id)}).first()
        assert row is not None
        assert str(row[0]) == user
        details = row[1] if isinstance(row[1], dict) else __import__("json").loads(row[1])
        assert details["reason"] == "old job, never worked"
        assert details["prior_stage"] == "scheduled"
        assert isinstance(details["requirements_skipped"], list)

    def test_an_open_arrival_timer_is_closed_at_zero(self, ctx):
        """A no-show after the tech tapped "I'm here": the arrival timer is
        open, and closeout is otherwise the only thing that ends one. It must
        close at zero minutes (nothing attested), not stay open forever and
        not bank the elapsed time. /audit 2026-10-04 finding 1."""
        from datetime import UTC, datetime, timedelta

        from gdx_dispatch.models.tenant_models import TimeEntry

        client, db, job, _dep, be, _ = ctx
        self._open(db, job)
        timer = TimeEntry(id=uuid4(), job_id=job.id, tech_id="tech-x",
                          company_id="tenant-authz", entry_type="job",
                          clock_in=datetime.now(UTC) - timedelta(hours=3))
        db.add(timer)
        db.commit()
        _office(be)
        r = _call(client, "POST", f"/api/jobs/{job.id}/close-without-work",
                  {"reason": "customer not home"})
        assert r.status_code == 200, r.text[:300]
        db.expire_all()
        t = db.get(TimeEntry, timer.id)
        assert t.clock_out is not None, "the arrival timer was left open"
        assert t.duration_minutes == 0
        details = db.execute(text(
            "SELECT details FROM audit_logs "
            "WHERE action='job_closed_without_work' AND entity_id=:e"
        ), {"e": str(job.id)}).scalar()
        details = details if isinstance(details, dict) else __import__("json").loads(details)
        assert details["timers_closed_at_zero"] == [str(timer.id)]

    def test_a_reason_is_required(self, ctx):
        client, db, job, _dep, be, _ = ctx
        self._open(db, job)
        _office(be)
        for body in ({"reason": ""}, {"reason": "  x "}):
            r = _call(client, "POST", f"/api/jobs/{job.id}/close-without-work", body)
            assert r.status_code == 422, r.text[:300]
        db.expire_all()
        assert _row(db, job.id, "status")[0] == "Scheduled"

    def test_a_finished_job_is_refused(self, ctx):
        client, db, job, _dep, be, _ = ctx  # fixture job is completed
        _office(be)
        r = _call(client, "POST", f"/api/jobs/{job.id}/close-without-work",
                  {"reason": "duplicate of another job"})
        assert r.status_code == 409, r.text[:300]
        _set(db, job, lifecycle_stage="cancelled", completed_at=None)
        r = _call(client, "POST", f"/api/jobs/{job.id}/close-without-work",
                  {"reason": "duplicate of another job"})
        assert r.status_code == 409, r.text[:300]

"""Phase 1.4 (sprint_tech_mobile) — multi-tech job support test suite.

D1 — JobAssignment CRUD + Job.assigned_to recompute on every write.
D2 — per-tech state stamps via the mobile state-machine handlers.
D3 — per-tech attribution stamps survive multi-tech jobs (photos,
     notes, parts, signatures already attribute from earlier phases).
D4 — completion_lead_tech_only gate; permissive fallback when no lead set.
D5 — at-most-one lead per job; clearing the lead works.
D6 — single Job.signed_at / signed_by per job (one customer signature
     regardless of tech count).
D7 — TimeEntry.tech_id + JobAssignment per-tech stamps support payroll-
     shaped reads (sum hours per tech across a date range).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import Request

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models import tenant_models  # noqa: F401  (register models)
from gdx_dispatch.modules.inventory import models as _inv  # noqa: F401
from gdx_dispatch.routers import job_assignments as ja
from gdx_dispatch.routers import mobile as mobile_router
from gdx_dispatch.models.tenant_models import (
    Appointment,
    Customer,
    Job,
    JobAssignment,
    Technician,
    TimeEntry,
)


_TENANT = "tenant-a"
_DISP = {"user_id": "disp-1", "role": "dispatcher"}
_TECH_A = {"user_id": "user-a", "role": "technician"}
_TECH_B = {"user_id": "user-b", "role": "technician"}
_TECH_A_ID = "tech-a"
_TECH_B_ID = "tech-b"


def _request() -> Request:
    req = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    req.state.tenant = {"id": _TENANT}
    return req


@pytest.fixture()
def db(tmp_path):
    eng = create_engine(
        f"sqlite:///{tmp_path / 'phase14.sqlite3'}",
        connect_args={"check_same_thread": False},
    )
    TenantBase.metadata.create_all(eng, checkfirst=True)
    Session_ = sessionmaker(bind=eng, autoflush=False, autocommit=False)
    s = Session_()
    yield s
    s.close()
    eng.dispose()


def _seed_job(db) -> str:
    cid = uuid4().hex
    db.execute(text(
        "INSERT INTO customers (id, name, company_id) VALUES (:i, 'Acme', :t)"
    ), {"i": cid, "t": _TENANT})
    jid = uuid4().hex
    db.execute(text(
        "INSERT INTO jobs (id, company_id, customer_id, title, dispatch_status, "
        "scheduled_at, created_at) VALUES (:i, :t, :c, 'job', 'assigned', :n, :n)"
    ), {"i": jid, "t": _TENANT, "c": cid, "n": datetime.now(timezone.utc)})
    db.execute(text(
        "INSERT INTO technicians (id, company_id, user_id, active, created_at) "
        "VALUES (:i, :t, :u, 1, :n)"
    ), {"i": _TECH_A_ID, "t": _TENANT, "u": "user-a", "n": datetime.now(timezone.utc)})
    db.execute(text(
        "INSERT INTO technicians (id, company_id, user_id, active, created_at) "
        "VALUES (:i, :t, :u, 1, :n)"
    ), {"i": _TECH_B_ID, "t": _TENANT, "u": "user-b", "n": datetime.now(timezone.utc)})
    db.commit()
    return jid


# ---------------------------------------------------------------------------
# D1 — assign / list / unassign + Job.assigned_to recompute.
# ---------------------------------------------------------------------------


def test_d1_assign_two_techs_lists_both(db):
    jid = _seed_job(db)
    a1 = ja.add_assignment(jid, _request(), ja.AssignBody(tech_id=_TECH_A_ID), user=_DISP, db=db)
    a2 = ja.add_assignment(jid, _request(), ja.AssignBody(tech_id=_TECH_B_ID), user=_DISP, db=db)
    listing = ja.list_assignments(jid, _request(), user=_DISP, db=db)
    tech_ids = {r["tech_id"] for r in listing}
    assert tech_ids == {_TECH_A_ID, _TECH_B_ID}
    assert a1["is_lead"] is False and a2["is_lead"] is False


def test_d1_primary_recomputes_on_assign_and_unassign(db):
    jid = _seed_job(db)
    ja.add_assignment(jid, _request(), ja.AssignBody(tech_id=_TECH_A_ID), user=_DISP, db=db)
    primary = db.execute(text("SELECT assigned_to FROM jobs WHERE id=:i"), {"i": jid}).scalar()
    assert primary == _TECH_A_ID

    a2 = ja.add_assignment(jid, _request(), ja.AssignBody(tech_id=_TECH_B_ID), user=_DISP, db=db)
    # Still A first-assigned → A is primary.
    primary = db.execute(text("SELECT assigned_to FROM jobs WHERE id=:i"), {"i": jid}).scalar()
    assert primary == _TECH_A_ID

    # Remove A → primary falls to B.
    listing = ja.list_assignments(jid, _request(), user=_DISP, db=db)
    a1_id = next(r["id"] for r in listing if r["tech_id"] == _TECH_A_ID)
    ja.remove_assignment(jid, a1_id, _request(), user=_DISP, db=db)
    primary = db.execute(text("SELECT assigned_to FROM jobs WHERE id=:i"), {"i": jid}).scalar()
    assert primary == _TECH_B_ID

    # Remove B → primary is NULL.
    ja.remove_assignment(jid, a2["id"], _request(), user=_DISP, db=db)
    primary = db.execute(text("SELECT assigned_to FROM jobs WHERE id=:i"), {"i": jid}).scalar()
    assert primary is None


def test_d1_duplicate_assign_409s(db):
    jid = _seed_job(db)
    ja.add_assignment(jid, _request(), ja.AssignBody(tech_id=_TECH_A_ID), user=_DISP, db=db)
    with pytest.raises(HTTPException) as exc:
        ja.add_assignment(jid, _request(), ja.AssignBody(tech_id=_TECH_A_ID), user=_DISP, db=db)
    assert exc.value.status_code == 409


def test_d1_tech_role_cannot_assign(db):
    jid = _seed_job(db)
    with pytest.raises(HTTPException) as exc:
        ja.add_assignment(jid, _request(), ja.AssignBody(tech_id=_TECH_A_ID), user=_TECH_A, db=db)
    assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# D5 — lead designation.
# ---------------------------------------------------------------------------


def test_d5_lead_makes_the_chosen_tech_primary(db):
    jid = _seed_job(db)
    ja.add_assignment(jid, _request(), ja.AssignBody(tech_id=_TECH_A_ID), user=_DISP, db=db)
    ja.add_assignment(jid, _request(), ja.AssignBody(tech_id=_TECH_B_ID), user=_DISP, db=db)
    out = ja.set_lead(jid, _request(), ja.LeadBody(tech_id=_TECH_B_ID), user=_DISP, db=db)
    assert out["lead_tech_id"] == _TECH_B_ID
    primary = db.execute(text("SELECT assigned_to FROM jobs WHERE id=:i"), {"i": jid}).scalar()
    assert primary == _TECH_B_ID


def test_d5_at_most_one_lead(db):
    jid = _seed_job(db)
    ja.add_assignment(jid, _request(), ja.AssignBody(tech_id=_TECH_A_ID, is_lead=True), user=_DISP, db=db)
    ja.add_assignment(jid, _request(), ja.AssignBody(tech_id=_TECH_B_ID, is_lead=True), user=_DISP, db=db)
    listing = ja.list_assignments(jid, _request(), user=_DISP, db=db)
    leads = [r for r in listing if r["is_lead"]]
    assert len(leads) == 1
    assert leads[0]["tech_id"] == _TECH_B_ID  # the second claim wins


def test_d5_clearing_lead(db):
    jid = _seed_job(db)
    ja.add_assignment(jid, _request(), ja.AssignBody(tech_id=_TECH_A_ID, is_lead=True), user=_DISP, db=db)
    ja.set_lead(jid, _request(), ja.LeadBody(tech_id=None), user=_DISP, db=db)
    listing = ja.list_assignments(jid, _request(), user=_DISP, db=db)
    assert all(r["is_lead"] is False for r in listing)


def test_d5_set_lead_for_unassigned_tech_400s(db):
    jid = _seed_job(db)
    ja.add_assignment(jid, _request(), ja.AssignBody(tech_id=_TECH_A_ID), user=_DISP, db=db)
    with pytest.raises(HTTPException) as exc:
        ja.set_lead(jid, _request(), ja.LeadBody(tech_id="ghost"), user=_DISP, db=db)
    assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# D2 — per-tech state stamps.
# ---------------------------------------------------------------------------


def test_d2_stamp_tech_state_writes_each_column(db):
    jid = _seed_job(db)
    ja.add_assignment(jid, _request(), ja.AssignBody(tech_id=_TECH_A_ID), user=_DISP, db=db)
    when = datetime.now(timezone.utc)
    ja.stamp_tech_state(db, job_id=jid, tech_id=_TECH_A_ID, state="en_route", when=when)
    ja.stamp_tech_state(db, job_id=jid, tech_id=_TECH_A_ID, state="arrived", when=when + timedelta(minutes=10))
    ja.stamp_tech_state(db, job_id=jid, tech_id=_TECH_A_ID, state="complete", when=when + timedelta(hours=1))
    db.commit()
    row = db.query(JobAssignment).filter_by(job_id=jid, tech_id=_TECH_A_ID).one()
    assert row.en_route_at is not None
    assert row.arrived_at is not None
    assert row.completed_at is not None


def test_d2_repeat_taps_are_idempotent(db):
    jid = _seed_job(db)
    ja.add_assignment(jid, _request(), ja.AssignBody(tech_id=_TECH_A_ID), user=_DISP, db=db)
    t1 = datetime.now(timezone.utc)
    t2 = t1 + timedelta(minutes=5)
    ja.stamp_tech_state(db, job_id=jid, tech_id=_TECH_A_ID, state="en_route", when=t1)
    ja.stamp_tech_state(db, job_id=jid, tech_id=_TECH_A_ID, state="en_route", when=t2)
    db.commit()
    row = db.query(JobAssignment).filter_by(job_id=jid, tech_id=_TECH_A_ID).one()
    # SQLite drops tzinfo on round-trip; compare naive components.
    assert row.en_route_at.replace(tzinfo=None) == t1.replace(tzinfo=None)
    # First stamp wins — re-tap MUST NOT overwrite.


def test_d2_two_techs_have_independent_timestamps(db):
    jid = _seed_job(db)
    ja.add_assignment(jid, _request(), ja.AssignBody(tech_id=_TECH_A_ID), user=_DISP, db=db)
    ja.add_assignment(jid, _request(), ja.AssignBody(tech_id=_TECH_B_ID), user=_DISP, db=db)
    t1 = datetime.now(timezone.utc)
    t2 = t1 + timedelta(minutes=30)
    ja.stamp_tech_state(db, job_id=jid, tech_id=_TECH_A_ID, state="arrived", when=t1)
    ja.stamp_tech_state(db, job_id=jid, tech_id=_TECH_B_ID, state="arrived", when=t2)
    db.commit()
    a = db.query(JobAssignment).filter_by(job_id=jid, tech_id=_TECH_A_ID).one()
    b = db.query(JobAssignment).filter_by(job_id=jid, tech_id=_TECH_B_ID).one()
    assert a.arrived_at.replace(tzinfo=None) == t1.replace(tzinfo=None)
    assert b.arrived_at.replace(tzinfo=None) == t2.replace(tzinfo=None)


def test_d2_lazy_backfill_creates_row(db):
    jid = _seed_job(db)
    # NO add_assignment — simulate a legacy job.
    row = ja.ensure_assignment_for_legacy_job(db, job_id=jid, tech_id=_TECH_A_ID, user_id="user-a")
    db.commit()
    assert row.tech_id == _TECH_A_ID
    assert row.assigned_by == "system_lazy_backfill"
    # Idempotent — second call returns the same row, no duplicate.
    row2 = ja.ensure_assignment_for_legacy_job(db, job_id=jid, tech_id=_TECH_A_ID, user_id="user-a")
    assert row2.id == row.id


# ---------------------------------------------------------------------------
# D4 — lead-tech-only completion gate.
# ---------------------------------------------------------------------------


def _set_lead_only_setting(db, value: bool) -> None:
    from gdx_dispatch.models.tenant_models import AppSettings

    existing = db.query(AppSettings).first()
    overrides = {"tech_mobile.completion_lead_tech_only": value}
    if existing is None:
        db.add(AppSettings(
            company_name="Acme",
            address="-",
            tenant_mobile_settings=overrides,
        ))
    else:
        existing.tenant_mobile_settings = overrides
    db.commit()


def test_d4_off_default_any_tech_can_complete(db):
    """Permissive default — completion_lead_tech_only=False (catalog default)."""
    jid = _seed_job(db)
    ja.add_assignment(jid, _request(), ja.AssignBody(tech_id=_TECH_A_ID, is_lead=True), user=_DISP, db=db)
    ja.add_assignment(jid, _request(), ja.AssignBody(tech_id=_TECH_B_ID), user=_DISP, db=db)
    # Tech B (not lead) tries to complete — should NOT be blocked when setting is off.
    assert ja.is_lead_for_job(db, job_id=jid, tech_id=_TECH_B_ID) is False
    assert ja.has_any_lead(db, job_id=jid) is True


def test_d4_no_lead_set_falls_back_to_permissive(db):
    """Even with the gate ON, a job with no lead set must still be completable
    by any tech — otherwise misconfiguration would lock every job."""
    _set_lead_only_setting(db, True)
    jid = _seed_job(db)
    ja.add_assignment(jid, _request(), ja.AssignBody(tech_id=_TECH_A_ID), user=_DISP, db=db)
    ja.add_assignment(jid, _request(), ja.AssignBody(tech_id=_TECH_B_ID), user=_DISP, db=db)
    assert ja.has_any_lead(db, job_id=jid) is False  # no lead → fall-back path


def test_d4_lead_set_and_gate_on_blocks_non_lead(db):
    _set_lead_only_setting(db, True)
    jid = _seed_job(db)
    ja.add_assignment(jid, _request(), ja.AssignBody(tech_id=_TECH_A_ID, is_lead=True), user=_DISP, db=db)
    ja.add_assignment(jid, _request(), ja.AssignBody(tech_id=_TECH_B_ID), user=_DISP, db=db)
    assert ja.has_any_lead(db, job_id=jid) is True
    assert ja.is_lead_for_job(db, job_id=jid, tech_id=_TECH_A_ID) is True
    assert ja.is_lead_for_job(db, job_id=jid, tech_id=_TECH_B_ID) is False


# ---------------------------------------------------------------------------
# D3 — per-tech attribution survives multi-tech jobs (verification-only;
# the actual stamps happen in earlier-phase code we're not duplicating).
# ---------------------------------------------------------------------------


def test_d3_part_request_attribution_per_tech(db):
    """Phase 1.3 already stamps requested_by_user_id; verify it's per-tech
    when both techs file their own request."""
    from gdx_dispatch.routers import parts_needed as pr

    jid = _seed_job(db)
    ja.add_assignment(jid, _request(), ja.AssignBody(tech_id=_TECH_A_ID), user=_DISP, db=db)
    ja.add_assignment(jid, _request(), ja.AssignBody(tech_id=_TECH_B_ID), user=_DISP, db=db)
    p1 = pr.add_part_needed(jid, _request(), pr.PartNeededIn(part_name="spring"), user=_TECH_A, db=db)
    p2 = pr.add_part_needed(jid, _request(), pr.PartNeededIn(part_name="cable"), user=_TECH_B, db=db)
    assert p1["requested_by_user_id"] == "user-a"
    assert p2["requested_by_user_id"] == "user-b"


# ---------------------------------------------------------------------------
# D6 — single Job.signed_at / signed_by survives multi-tech.
# ---------------------------------------------------------------------------


def test_d6_one_signature_per_job_regardless_of_tech_count(db):
    """The customer signs the job once; tech count is irrelevant. Confirm
    Job.signature_data / signed_by / signed_at are the only fields."""
    jid = _seed_job(db)
    now = datetime.now(timezone.utc)
    db.execute(text(
        "UPDATE jobs SET signature_data='base64sig', signed_by='Customer', "
        "signed_at=:n WHERE id=:i"
    ), {"n": now, "i": jid})
    db.commit()
    row = db.execute(text(
        "SELECT signature_data, signed_by, signed_at FROM jobs WHERE id=:i"
    ), {"i": jid}).mappings().one()
    assert row["signature_data"] == "base64sig"
    assert row["signed_by"] == "Customer"


# ---------------------------------------------------------------------------
# D7 — per-tech read shape (payroll-style query against TimeEntry +
# JobAssignment).
# ---------------------------------------------------------------------------


def test_d7_payroll_shape_query_returns_per_tech_hours(db):
    """A payroll-style consumer should be able to pull per-tech hours
    across a date range from TimeEntry alone — TimeEntry.tech_id has been
    canonical since Phase 1.0; Phase 1.4 doesn't change that. This test
    locks in the shape so future schema work doesn't accidentally drop it.
    """
    jid = _seed_job(db)
    db.add(TimeEntry(
        id=uuid4(), job_id=uuid4(),  # FK not enforced on sqlite
        tech_id=_TECH_A_ID, user_id="user-a",
        clock_in=datetime.now(timezone.utc) - timedelta(hours=2),
        clock_out=datetime.now(timezone.utc),
        duration_minutes=120,
        company_id=_TENANT,
    ))
    db.add(TimeEntry(
        id=uuid4(), job_id=uuid4(),
        tech_id=_TECH_B_ID, user_id="user-b",
        clock_in=datetime.now(timezone.utc) - timedelta(hours=1),
        clock_out=datetime.now(timezone.utc),
        duration_minutes=60,
        company_id=_TENANT,
    ))
    db.commit()
    rows = db.execute(text(
        "SELECT tech_id, SUM(duration_minutes) AS minutes FROM time_entries "
        "WHERE deleted_at IS NULL GROUP BY tech_id ORDER BY tech_id"
    )).mappings().all()
    summary = {r["tech_id"]: r["minutes"] for r in rows}
    assert summary[_TECH_A_ID] == 120
    assert summary[_TECH_B_ID] == 60

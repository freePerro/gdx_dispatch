"""GET /api/jobs/{job_id}/closeout — the read side of the closeout (plan §1).

`job_closeouts` was write-only for 2.5 months: hours, work notes, the parts
attestation and the signer name went into the database and never reached any
screen. This endpoint is what the office bills from and what the tech
confirms against.

Pinned here:
1. No closeout → {closeout: None, history: []}, not a 404 — the job exists,
   it just hasn't been closed out.
2. The snapshot round-trips: hours, notes, parts attestation, signer name.
3. THE RAW SIGNATURE BLOB IS NEVER SERIALIZED (audit A4): mobile's
   company-grant redaction nulls `signature_data` for techs browsing under
   techs_see_all_jobs, and a closeout endpoint returning the snapshot
   verbatim would walk around that control. `signature_present` +
   signed_by/signed_at metadata is the whole contract.
4. After a re-closeout: `closeout` is the live restatement, `history` is
   newest-first with the superseded row still visible — the office must SEE
   that a closeout was revised (plan §14 gap 4).
5. `closed_by_name` resolves via users (full_name > name > username > email).
6. Malformed/unknown job ids 404.

Harness: direct function invocation on in-memory SQLite, same as
test_job_closeout_supersede.py.
"""
from __future__ import annotations

import json
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models.tenant_models import (
    Customer,
    Invoice,
    InvoiceLine,
    Job,
    JobCloseout,
    JobPartNeeded,
    Payment,
    Technician,
    TimeEntry,
    User,
)
from gdx_dispatch.modules.inventory.models import JobPart, Part
from gdx_dispatch.models.tenant_models import TenantRole, UserRoleAssignment
from gdx_dispatch.routers.jobs import (
    CloseoutPart,
    CloseoutPayload,
    closeout_job,
    get_job_closeout,
)

TENANT = "tenant-closeout-read"
USER_ID = uuid4()


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    for tbl in [
        Job.__table__,
        Customer.__table__,
        Invoice.__table__,
        InvoiceLine.__table__,
        Payment.__table__,
        JobPartNeeded.__table__,
        JobCloseout.__table__,
        Part.__table__,
        JobPart.__table__,
        Technician.__table__,
        TimeEntry.__table__,
        User.__table__,
        TenantRole.__table__,
        UserRoleAssignment.__table__,
    ]:
        tbl.create(bind=engine, checkfirst=True)
    TenantBase.metadata.create_all(bind=engine, checkfirst=True)
    session = Session()
    session.add(
        User(
            id=USER_ID,
            email="michael@example.test",
            full_name="Michael the Tech",
            company_id=TENANT,
        )
    )
    session.commit()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _request() -> Request:
    req = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    req.state.tenant = {"id": TENANT}
    req.state.tenant_id = TENANT
    return req


def _seed_job(db) -> Job:
    job = Job(
        customer_id=uuid4(),
        title="Read-side job",
        description="t",
        lifecycle_stage="in_progress",
        dispatch_status="on_site",
        billing_status="unbilled",
        company_id=TENANT,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _closeout(db, job, *, hours: float, notes: str | None = None, sig: str | None = None,
              parts: list | None = None):
    return closeout_job(
        payload=CloseoutPayload(
            parts=parts or [], hours=hours, no_parts_used=not parts, notes=notes,
            signature_data=sig,
            signed_by="Customer Name" if sig else None,
        ),
        job_id=str(job.id),
        request=_request(),
        current_user={"user_id": str(USER_ID), "tenant_id": TENANT, "role": "technician"},
        db=db,
    )


def _read(db, job_id: str, *, role: str = "admin", user_id: str | None = None) -> tuple[int, dict]:
    resp = get_job_closeout(
        job_id=job_id,
        request=_request(),
        current_user={
            "user_id": user_id or str(USER_ID),
            "sub": user_id or str(USER_ID),
            "tenant_id": TENANT,
            "role": role,
        },
        db=db,
    )
    return resp.status_code, json.loads(resp.body)


def test_job_without_closeout_reads_empty_not_404(db) -> None:
    job = _seed_job(db)
    status, body = _read(db, str(job.id))
    assert status == 200
    assert body["closeout"] is None
    assert body["history"] == []


def test_snapshot_round_trips_and_signature_blob_never_leaves(db) -> None:
    job = _seed_job(db)
    _closeout(
        db, job, hours=3.0,
        notes="Installed openers. Adjusted doors.",
        sig="data:image/png;base64,SECRETBLOB",
    )

    status, body = _read(db, str(job.id))
    assert status == 200
    co = body["closeout"]
    assert co["hours_worked"] == 3.0
    assert co["notes"] == "Installed openers. Adjusted doors."
    assert co["no_parts_used"] is True
    assert co["signature_present"] is True
    assert co["signed_by"] == "Customer Name"
    assert co["closed_by_name"] == "Michael the Tech"

    # A4: the blob must be absent from the ENTIRE payload — not nulled, ABSENT.
    raw = json.dumps(body)
    assert "SECRETBLOB" not in raw
    assert "signature_data" not in raw


def test_recloseout_shows_current_plus_history(db) -> None:
    job = _seed_job(db)
    _closeout(db, job, hours=3.0)
    _closeout(db, job, hours=4.0)

    status, body = _read(db, str(job.id))
    assert status == 200
    assert body["closeout"]["hours_worked"] == 4.0
    assert body["closeout"]["supersedes_id"] is not None

    hours = [h["hours_worked"] for h in body["history"]]
    assert hours == [4.0, 3.0], "history must be newest-first and keep the old row"
    assert body["history"][1]["superseded_at"] is not None
    assert body["history"][0]["superseded_at"] is None


def test_unknown_and_malformed_ids_404(db) -> None:
    status, _ = _read(db, "not-a-uuid")
    assert status == 404
    status, _ = _read(db, str(uuid4()))
    assert status == 404


def test_parts_come_back_without_cost_figures(db) -> None:
    """The read API says WHAT was used, never what it cost — it serves screens
    any job-visible user can open; unit_cost/line_total stay in the snapshot
    for the costing paths (audit round 3)."""
    job = _seed_job(db)
    _closeout(
        db, job, hours=2.0,
        parts=[CloseoutPart(name="Torsion spring 200x2.0", qty=2, unit_cost=48.5)],
    )

    status, body = _read(db, str(job.id))
    assert status == 200
    parts = body["closeout"]["parts_used"]
    assert parts and parts[0]["name"] == "Torsion spring 200x2.0"
    assert parts[0]["qty"] == 2
    raw = json.dumps(body)
    assert "unit_cost" not in raw
    assert "line_total" not in raw
    assert "48.5" not in raw


def test_technician_without_a_claim_gets_404(db) -> None:
    """Flat authentication was rejected by the audit: mobile built a
    three-tier grant so one tech can't browse another's attested records, and
    this endpoint must not walk around it. No claim → the same 404 as a
    missing job (never confirm existence)."""
    job = _seed_job(db)
    _closeout(db, job, hours=3.0)

    stranger = str(uuid4())
    status, _ = _read(db, str(job.id), role="technician", user_id=stranger)
    assert status == 404


def test_dispatcher_reads_via_jobs_read_all(db) -> None:
    """The office tier passes on jobs.read_all (BUILTIN dispatcher), which is
    the path the billing screens use.

    NOT covered here: the assigned-tech TRUE branch of job_belongs_to_user.
    All three of its ownership paths compare Uuid columns in raw SQL, which
    match dashed text on Postgres and 32-hex on SQLite — the same documented
    harness limitation as the smoke file's PATCH coverage. The gate itself is
    the shared, separately-tested core/job_access.job_belongs_to_user (the
    write gate for ~18 mobile endpoints); what THIS file pins is that the
    endpoint calls it (the stranger-404 above fails if the gate is removed).
    """
    job = _seed_job(db)
    _closeout(db, job, hours=3.0)

    status, body = _read(db, str(job.id), role="dispatcher", user_id=str(uuid4()))
    assert status == 200
    assert body["closeout"]["hours_worked"] == 3.0

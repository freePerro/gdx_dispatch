"""Closeout supersede model (plan §12) — one live snapshot per job, forever.

The bug this pins: `closeout_job` was append-only. It built
`JobCloseout(id=uuid4())` and `db.add`ed it unconditionally — no update, no
soft-delete, no unique constraint — so a re-closeout left TWO live rows. The
comment saying "one JobCloseout per job, re-closeout restates it" described
only the labor entry. Nobody had re-closed out a job in prod yet (8 closeouts,
8 distinct jobs), so it was latent; the first tech to fix a typo and resubmit
would have:

* made every `scalar_one_or_none()` reader keyed on job_id raise
  MultipleResultsFound — a 500 on the office closeout card (plan §1), and
* double-counted the job's hours in the tech-efficiency SUMs.

The contract, pinned here:
1. First closeout → one live row, `supersedes_id` NULL.
2. Re-closeout → prior row STAMPED superseded (never deleted, never edited —
   an attestation is evidence), new row links back, exactly one live row.
3. The chain survives a third restatement.
4. The partial unique index makes a forgotten stamp a loud IntegrityError,
   not a silent history fork.
5. A restatement emits `job_closeout_superseded` with OLD and NEW hours side
   by side (§13: never force a reader to reconstruct a delta from two events).
6. `tech_efficiency` filters to the live row (static pin — its DISTINCT ON is
   Postgres-only, so SQLite can't run it behaviorally).

Harness mirrors test_closeout_labor_trail.py: `closeout_job` invoked directly
as a function against in-memory SQLite.
"""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from gdx_dispatch.core.audit import AuditLog, TenantBase
from gdx_dispatch.core.closeouts import get_closeout_history, get_current_closeout
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
)
from gdx_dispatch.modules.inventory.models import JobPart, Part
from gdx_dispatch.routers.jobs import CloseoutPayload, closeout_job

TENANT = "tenant-supersede"
USER = "user-michael"


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
    ]:
        tbl.create(bind=engine, checkfirst=True)
    TenantBase.metadata.create_all(bind=engine, checkfirst=True)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _request() -> Request:
    req = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    req.state.tenant = {"id": TENANT}
    req.state.tenant_id = TENANT
    return req


def _seed_job(db) -> Job:
    job = Job(
        customer_id=uuid4(),
        title="Shed door adjustment",
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


def _closeout(db, job, *, hours: float):
    return closeout_job(
        payload=CloseoutPayload(parts=[], hours=hours, no_parts_used=True),
        job_id=str(job.id),
        request=_request(),
        current_user={"user_id": USER, "tenant_id": TENANT, "role": "technician"},
        db=db,
    )


def _rows(db, job) -> list[JobCloseout]:
    return list(
        db.execute(
            select(JobCloseout)
            .where(JobCloseout.job_id == job.id)
            .order_by(JobCloseout.created_at)
        ).scalars()
    )


def test_first_closeout_is_the_live_row(db) -> None:
    job = _seed_job(db)
    _closeout(db, job, hours=3.0)

    rows = _rows(db, job)
    assert len(rows) == 1
    assert rows[0].superseded_at is None
    assert rows[0].supersedes_id is None
    current = get_current_closeout(db, job.id)
    assert current is not None and current.id == rows[0].id


def test_recloseout_supersedes_instead_of_forking(db) -> None:
    """The core §12 contract: the prior attestation is stamped, not destroyed;
    the restatement links back; exactly one row is live."""
    job = _seed_job(db)
    _closeout(db, job, hours=3.0)
    _closeout(db, job, hours=4.0)

    rows = _rows(db, job)
    assert len(rows) == 2, "history must be kept — supersede, never overwrite"

    old, new = rows
    assert float(old.hours_worked) == 3.0
    assert old.superseded_at is not None, "prior row must be stamped superseded"
    assert float(new.hours_worked) == 4.0
    assert new.superseded_at is None
    assert new.supersedes_id == old.id, "restatement must link to what it replaced"

    live = [r for r in rows if r.superseded_at is None and r.deleted_at is None]
    assert len(live) == 1

    current = get_current_closeout(db, job.id)
    assert current is not None and float(current.hours_worked) == 4.0

    history = get_closeout_history(db, job.id)
    assert [float(r.hours_worked) for r in history] == [4.0, 3.0], "newest first"


def test_chain_survives_a_third_restatement(db) -> None:
    job = _seed_job(db)
    _closeout(db, job, hours=1.0)
    _closeout(db, job, hours=2.0)
    _closeout(db, job, hours=2.5)

    rows = _rows(db, job)
    assert len(rows) == 3
    live = [r for r in rows if r.superseded_at is None]
    assert len(live) == 1 and float(live[0].hours_worked) == 2.5
    # Walk the chain backwards: 2.5 → 2.0 → 1.0.
    by_id = {r.id: r for r in rows}
    chain = [float(live[0].hours_worked)]
    cursor = live[0]
    while cursor.supersedes_id:
        cursor = by_id[cursor.supersedes_id]
        chain.append(float(cursor.hours_worked))
    assert chain == [2.5, 2.0, 1.0]


def test_unique_index_makes_a_forgotten_stamp_loud(db) -> None:
    """Defense in depth: if a future write path inserts a second live snapshot
    without stamping the prior one, the partial unique index refuses — a loud
    IntegrityError instead of silently forked history."""
    job = _seed_job(db)
    _closeout(db, job, hours=3.0)

    from datetime import UTC, datetime

    now = datetime.now(UTC)
    db.add(
        JobCloseout(
            id=uuid4(),
            job_id=job.id,
            parts_used=[],
            no_parts_used=True,
            hours_worked=9.0,
            closed_by_user_id=USER,
            closed_at=now,
            created_at=now,
            updated_at=now,
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_supersede_emits_old_and_new_side_by_side(db) -> None:
    """§13: the restatement event carries OLD and NEW explicitly — a reader
    must never have to diff two events to learn what changed."""
    job = _seed_job(db)
    _closeout(db, job, hours=3.0)
    _closeout(db, job, hours=4.0)

    events = list(
        db.execute(
            select(AuditLog).where(AuditLog.action == "job_closeout_superseded")
        ).scalars()
    )
    assert len(events) == 1
    details = events[0].details or {}
    assert details["old"]["hours"] == 3.0
    assert details["new"]["hours"] == 4.0
    assert details["old"]["id"] != details["new"]["id"]

    # And the regular closeout event carries the linkage too.
    closeout_events = list(
        db.execute(
            select(AuditLog).where(AuditLog.action == "job_closeout")
        ).scalars()
    )
    assert len(closeout_events) == 2
    linked = [e for e in closeout_events if (e.details or {}).get("supersedes_id")]
    assert len(linked) == 1


def test_tech_efficiency_reads_only_the_live_row() -> None:
    """Static pin — tech_efficiency's SQL uses Postgres-only DISTINCT ON, so
    SQLite can't execute it; what must hold is that EVERY scan of
    job_closeouts in that file carries the live-row filter (the audit caught
    the first version of this pin checking only the first scan — a second,
    unfiltered scan added later would have passed it)."""
    src = (
        Path(__file__).resolve().parents[1] / "routers/tech_efficiency.py"
    ).read_text(encoding="utf-8")
    scans = src.count("FROM job_closeouts")
    assert scans >= 1, "tech_efficiency no longer scans job_closeouts — re-point this pin"
    filters = src.count("jc.superseded_at IS NULL")
    assert filters >= scans, (
        f"tech_efficiency has {scans} scan(s) of job_closeouts but only "
        f"{filters} supersede filter(s) — an unfiltered scan double-counts "
        "every restatement in the hours SUMs."
    )


def test_sqlite_partial_index_matches_migration(db) -> None:
    """The ORM's sqlite_where and migration 041's Postgres WHERE must express
    the same predicate, or tests enforce a constraint prod doesn't have.
    The migration check anchors on the CREATE INDEX statement itself, not the
    whole file (the audit caught the first version passing on a commented-out
    index)."""
    row = db.execute(
        text(
            "SELECT sql FROM sqlite_master "
            "WHERE type='index' AND name='uq_job_closeouts_live'"
        )
    ).scalar()
    assert row, "partial unique index missing from the ORM metadata"
    normalized = " ".join(row.split()).lower()
    assert "superseded_at is null" in normalized
    assert "deleted_at is null" in normalized

    mig = (
        Path(__file__).resolve().parents[1]
        / "migrations/versions/041_job_closeout_supersede.py"
    ).read_text(encoding="utf-8")
    create_at = mig.index("CREATE UNIQUE INDEX IF NOT EXISTS uq_job_closeouts_live")
    stmt = mig[create_at : create_at + 300]
    assert "WHERE superseded_at IS NULL AND deleted_at IS NULL" in stmt, (
        "migration 041's index no longer carries the live-row predicate in "
        "the CREATE INDEX statement itself"
    )


def test_concurrent_double_closeout_is_a_409_not_a_500(db, monkeypatch) -> None:
    """The race: tech B's get_current_closeout SELECT runs before tech A's
    commit, so B sees no prior snapshot and INSERTs a second live row — the
    partial unique index rejects it at flush. That is a coordination event,
    not breakage: B must get a 409 that says what happened, never a 500
    spraying driver internals onto a phone screen. Simulated by blinding the
    prior-snapshot lookup, exactly what the race does."""
    import gdx_dispatch.core.closeouts as closeouts_mod

    job = _seed_job(db)
    _closeout(db, job, hours=3.0)

    monkeypatch.setattr(closeouts_mod, "get_current_closeout", lambda _db, _jid: None)
    resp = _closeout(db, job, hours=4.0)

    assert resp.status_code == 409, (
        f"concurrent second closeout returned {resp.status_code} — the race "
        "must surface as a 409 coordination message"
    )
    body = resp.body.decode()
    assert "Someone else closed this job out" in body
    assert "psycopg2" not in body and "sqlite3" not in body

    # And the first closeout is untouched — the losing transaction rolled
    # back completely.
    db.rollback()
    rows = _rows(db, job)
    live = [r for r in rows if r.superseded_at is None and r.deleted_at is None]
    assert len(live) == 1 and float(live[0].hours_worked) == 3.0

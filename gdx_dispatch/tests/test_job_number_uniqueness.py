"""Job-number uniqueness guard — plan §5.

The prod bug: a stuck counter (next_seq=1) made next_job_number() hand out
JOB-2026-001…009 that were already in use — silent duplicates. These tests pin
the two guarantees that stop it recurring:
  1. a partial UNIQUE index rejects a duplicate live job_number at the DB, and
  2. next_job_number() is monotonic and never repeats a number for a tenant.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


# ---------------------------------------------------------------------------
# 1) The DB guard — the partial unique index (mirrors migration 048).
# ---------------------------------------------------------------------------
@pytest.fixture()
def jobs_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    with engine.begin() as c:
        c.exec_driver_sql(
            "CREATE TABLE jobs (id TEXT PRIMARY KEY, job_number TEXT, deleted_at TEXT)"
        )
        # The exact predicate migration 048 uses.
        c.exec_driver_sql(
            "CREATE UNIQUE INDEX ux_jobs_job_number_live ON jobs (job_number) "
            "WHERE deleted_at IS NULL AND job_number IS NOT NULL"
        )
    Session = sessionmaker(bind=engine)
    yield Session()
    engine.dispose()


def _add(db, number, *, deleted=None):
    db.execute(
        text("INSERT INTO jobs (id, job_number, deleted_at) VALUES (:i, :n, :d)"),
        {"i": str(uuid.uuid4()), "n": number, "d": deleted},
    )
    db.commit()


def test_duplicate_live_number_is_rejected(jobs_db):
    _add(jobs_db, "JOB-2026-006")
    with pytest.raises(IntegrityError):
        _add(jobs_db, "JOB-2026-006")


def test_null_numbers_do_not_collide(jobs_db):
    # Several un-numbered jobs coexist — the partial index excludes NULLs.
    _add(jobs_db, None)
    _add(jobs_db, None)
    assert jobs_db.execute(text("SELECT count(*) FROM jobs")).scalar() == 2


def test_deleted_job_frees_the_number(jobs_db):
    # A soft-deleted duplicate must not block reusing its number on a live job.
    _add(jobs_db, "JOB-2026-006", deleted="2026-07-30")
    _add(jobs_db, "JOB-2026-006")  # live — allowed, the deleted one is excluded
    live = jobs_db.execute(
        text("SELECT count(*) FROM jobs WHERE job_number='JOB-2026-006' AND deleted_at IS NULL")
    ).scalar()
    assert live == 1


# next_job_number()'s monotonicity can't be unit-tested here: it locks the row
# with SELECT ... FOR UPDATE, which SQLite rejects ("near FOR: syntax error").
# It's exercised against Postgres in the throwaway-container verification, and
# the partial unique index above is the DB-level backstop regardless.


# ---------------------------------------------------------------------------
# 2) apply_template renders unique numbers for distinct seqs (no FOR UPDATE).
# ---------------------------------------------------------------------------
def test_apply_template_is_injective_over_seq():
    from gdx_dispatch.modules.numbering.service import apply_template

    rendered = [apply_template("JOB-{year}-{seq:03d}", s, year=2026) for s in range(1, 40)]
    assert len(set(rendered)) == 39  # distinct seq -> distinct number
    assert rendered[0] == "JOB-2026-001" and rendered[-1] == "JOB-2026-039"


# ---------------------------------------------------------------------------
# 3) allocate_job_number() for real on Postgres (GDXA-834): its own session,
#    committed, monotonic; a failure returns None instead of raising.
# ---------------------------------------------------------------------------
def test_allocate_job_number_on_postgres(pg_test_db, monkeypatch):
    from datetime import UTC, datetime

    import gdx_dispatch.core.database as database
    from gdx_dispatch.core.tenant_settings import Tenant, TenantSettings
    from gdx_dispatch.modules.numbering import allocate_job_number

    schema = "alloc_834"
    engine = create_engine(pg_test_db, future=True, connect_args={"options": f"-csearch_path={schema}"})
    try:
        with engine.begin() as c:
            c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            c.exec_driver_sql(f"CREATE SCHEMA {schema}")
        Tenant.__table__.create(engine)
        TenantSettings.__table__.create(engine)
        tid = uuid.uuid4()
        year = datetime.now(UTC).year
        with sessionmaker(bind=engine)() as s:
            s.add(Tenant(id=tid, slug="t", name="t"))
            s.flush()
            s.add(TenantSettings(tenant_id=tid, job_number_next_seq=7, job_number_year_seen=year))
            s.commit()
        monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=engine))

        first = allocate_job_number(str(tid), customer_name="Becky Meinecke")
        second = allocate_job_number(tid)
        assert (first, second) == (f"JOB-{year}-007", f"JOB-{year}-008")
        with engine.connect() as c:
            assert c.execute(text("SELECT job_number_next_seq FROM tenant_settings")).scalar() == 9

        assert allocate_job_number("not-a-uuid") is None
    finally:
        with engine.begin() as c:
            c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        engine.dispose()


# ---------------------------------------------------------------------------
# 4) A counter behind live jobs heals itself (GDXA-933). Before this, the
#    allocator rendered from tenant_settings alone, committed the counter, and
#    the Job insert hit ux_jobs_job_number_live: POST /api/jobs answered 500
#    and every retry burned one more number.
# ---------------------------------------------------------------------------
def test_allocator_walks_past_live_numbers_on_postgres(pg_test_db, monkeypatch):
    from datetime import UTC, datetime

    import gdx_dispatch.core.database as database
    from gdx_dispatch.core.audit import TenantBase
    from gdx_dispatch.core.tenant_settings import Tenant, TenantSettings
    from gdx_dispatch.models.tenant_models import Job
    from gdx_dispatch.modules.numbering import allocate_job_number

    schema = "alloc_933"
    engine = create_engine(pg_test_db, future=True, connect_args={"options": f"-csearch_path={schema}"})
    try:
        with engine.begin() as c:
            c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            c.exec_driver_sql(f"CREATE SCHEMA {schema}")
        Tenant.__table__.create(engine)
        TenantSettings.__table__.create(engine)
        TenantBase.metadata.create_all(engine)
        with engine.begin() as c:
            # Migration 048's index; jobs is a create_all table, so the ORM lacks it.
            c.exec_driver_sql(
                "CREATE UNIQUE INDEX ux_jobs_job_number_live ON jobs (job_number) "
                "WHERE deleted_at IS NULL AND job_number IS NOT NULL"
            )
        tid = uuid.uuid4()
        year = datetime.now(UTC).year
        Session = sessionmaker(bind=engine)

        def job(number, *, deleted=None):
            return Job(title="t", company_id=str(tid), job_number=number, deleted_at=deleted)

        with Session() as s:
            s.add(Tenant(id=tid, slug="t", name="t"))
            s.flush()
            # The counter says 3, but 003, 004 and 006 are live; 005 was never
            # issued, and 007 belongs to a soft-deleted job whose invoices still
            # quote it, so it is not handed out again either.
            s.add(TenantSettings(tenant_id=tid, job_number_next_seq=3, job_number_year_seen=year))
            s.add_all([job(f"JOB-{year}-{n:03d}") for n in (3, 4, 6)])
            s.add(job(f"JOB-{year}-007", deleted=datetime.now(UTC)))
            s.commit()
        monkeypatch.setattr(database, "SessionLocal", Session)

        first = allocate_job_number(tid)
        second = allocate_job_number(tid)
        assert (first, second) == (f"JOB-{year}-005", f"JOB-{year}-008")
        with engine.connect() as c:
            assert c.execute(text("SELECT job_number_next_seq FROM tenant_settings")).scalar() == 9

        # The numbers it handed out are insertable: the index that used to
        # refuse the create now has nothing to refuse.
        with Session() as s:
            s.add_all([job(first), job(second)])
            s.commit()
    finally:
        with engine.begin() as c:
            c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        engine.dispose()


def test_highest_issued_seq_reads_the_numbers_of_this_template(jobs_db):
    from gdx_dispatch.modules.numbering.service import highest_issued_seq

    for number in ("JOB-2026-031", "JOB-2026-009", "JOB-2025-120", "LEGACY-9999", None):
        _add(jobs_db, number)

    assert highest_issued_seq(jobs_db, "JOB-{year}-{seq:03d}", year=2026) == 31
    assert highest_issued_seq(jobs_db, "JOB-{year}-{seq:03d}", year=2025) == 120
    assert highest_issued_seq(jobs_db, "JOB-{yy}-{seq}", year=2026) == 0
    # A token the renderer does not know, or no {seq}: nothing to compare against.
    assert highest_issued_seq(jobs_db, "JOB-{site}-{seq}", year=2026) == 0
    assert highest_issued_seq(jobs_db, "JOB-{year}", year=2026) == 0
    # A soft-deleted job's number still counts: it is never re-issued.
    _add(jobs_db, "JOB-2026-200", deleted="2026-07-30")
    assert highest_issued_seq(jobs_db, "JOB-{year}-{seq:03d}", year=2026) == 200


def test_highest_issued_seq_is_zero_without_a_jobs_table():
    from gdx_dispatch.modules.numbering.service import highest_issued_seq

    engine = create_engine("sqlite://")
    with sessionmaker(bind=engine)() as s:
        assert highest_issued_seq(s, "JOB-{year}-{seq:03d}", year=2026) == 0
    engine.dispose()


def test_settings_save_refuses_a_counter_at_or_below_an_issued_number():
    """The numbering PATCH may not rewind the counter onto issued numbers (GDXA-933)."""
    from datetime import UTC, datetime

    from gdx_dispatch.models.tenant_models import Job
    from gdx_dispatch.modules.numbering.router import _COLS
    from gdx_dispatch.modules.numbering.router import router as numbering_router
    from gdx_dispatch.tests.test_settings_audit import TID, _env

    client, Session, engine = _env(numbering_router, _COLS)
    try:
        year = datetime.now(UTC).year
        with Session() as s:
            s.add(Job(title="t", company_id=TID, job_number=f"JOB-{year}-031"))
            s.commit()

        fmt = "JOB-{year}-{seq:03d}"
        for seq in (1, 31):
            r = client.patch("/api/numbering/config", json={"job_number_format": fmt, "job_number_next_seq": seq})
            assert r.status_code == 409, r.text
            assert "32" in r.json()["detail"]
        with engine.connect() as c:
            assert c.execute(text("SELECT count(*) FROM tenant_settings WHERE job_number_next_seq IS NOT NULL")).scalar() == 0

        r = client.patch("/api/numbering/config", json={"job_number_format": fmt, "job_number_next_seq": 32})
        assert r.status_code == 200, r.text
        assert r.json()["job_number_next_seq"] == 32
        # A different format is measured against its own numbers, not this one's.
        r = client.patch("/api/numbering/config", json={"job_number_format": "W-{seq}", "job_number_next_seq": 1})
        assert r.status_code == 200, r.text
    finally:
        engine.dispose()

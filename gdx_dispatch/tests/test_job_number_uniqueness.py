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

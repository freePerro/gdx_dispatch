"""GDXA-1427: a crew row stored with a DASHED job_id must match its job on SQLite.

``job_assignments.job_id`` is a varchar the ORM writes as ``str(job.id)`` —
dashed. ``jobs.id`` is a ``Uuid`` column, which SQLite stores as 32 dashless
hex. ``core/job_access.py`` joined the two with a bare ``CAST = CAST``, so on
SQLite every ORM-written crew row missed its job: the crew tech was refused
(``job_belongs_to_user`` False, job absent from ``user_job_ids``) and, worse,
``creator_of_unassigned_job`` failed OPEN — the creator kept a pass-through on
a job dispatch had already crewed.

Real SQLite, schema from the ORM, no mocks. The dashless parametrisation is
the control: it matched before the fix too, so a failure there is the fixture,
not the defect.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.job_access import (
    creator_of_unassigned_job,
    job_belongs_to_user,
    user_job_ids,
)
from gdx_dispatch.models.tenant_models import Job, JobAssignment, Technician

TENANT = "tenant-crew-dashed"


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(bind=engine, checkfirst=True)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _job(db, created_by: str | None = None) -> Job:
    job = Job(
        customer_id=uuid4(),
        title="crew job",
        company_id=TENANT,
        assigned_to=None,
        created_by=created_by,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _crew(db, job: Job, tech_id: str, spelling: str) -> None:
    job_id = str(job.id) if spelling == "dashed" else job.id.hex
    db.add(JobAssignment(id=str(uuid4()), job_id=job_id, tech_id=tech_id))
    db.commit()


@pytest.mark.parametrize("spelling", ["dashed", "dashless"])
def test_crew_tech_owns_job_whatever_the_job_id_spelling(db, spelling) -> None:
    user_id = str(uuid4())
    tech = Technician(id=str(uuid4()), company_id=TENANT, user_id=user_id,
                      name="Crew", active=True)
    db.add(tech)
    db.commit()
    job = _job(db)
    _crew(db, job, tech.id, spelling)

    # The caller may pass either spelling; both must resolve.
    assert job_belongs_to_user(db, TENANT, str(job.id), user_id) is True
    assert job_belongs_to_user(db, TENANT, job.id.hex, user_id) is True

    # Spelled as jobs.id renders on this engine (dashless hex on SQLite), the
    # same as the assigned_to branch: callers feed these into IN (...) against
    # Uuid columns, so a crew row's own dashed spelling would miss there.
    assert user_job_ids(db, TENANT, user_id) == [job.id.hex]


@pytest.mark.parametrize("spelling", ["dashed", "dashless"])
def test_creator_loses_pass_through_once_a_crew_row_exists(db, spelling) -> None:
    creator = str(uuid4())
    job = _job(db, created_by=creator)
    assert creator_of_unassigned_job(db, TENANT, str(job.id), creator) is True

    _crew(db, job, str(uuid4()), spelling)
    assert creator_of_unassigned_job(db, TENANT, str(job.id), creator) is False
    assert creator_of_unassigned_job(db, TENANT, job.id.hex, creator) is False


def test_stranger_is_not_matched_by_the_normalised_join(db) -> None:
    """Dash-stripping widens the join key; it must not widen who matches."""
    owner, stranger = str(uuid4()), str(uuid4())
    tech = Technician(id=str(uuid4()), company_id=TENANT, user_id=owner,
                      name="Crew", active=True)
    db.add(tech)
    db.commit()
    job = _job(db)
    _crew(db, job, tech.id, "dashed")

    assert job_belongs_to_user(db, TENANT, str(job.id), stranger) is False
    assert user_job_ids(db, TENANT, stranger) == []

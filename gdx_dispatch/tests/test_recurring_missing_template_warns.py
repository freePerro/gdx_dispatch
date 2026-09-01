"""#468 (found on the way): a recurring schedule whose job template is gone
must say so, not vanish.

`materialize_due_recurring_jobs` skipped (`continue`) any due schedule whose
`JobTemplate` was missing or soft-deleted — no log, no counter, no signal. A
recurring customer whose template someone retired would silently stop
getting jobs forever, and nothing anywhere would show why.

The fix is one WARNING line naming the schedule and the template id. This
test drives the real function against SQLite and asserts the log record; the
counterfactual (remove the `log.warning`) fails on `caplog`.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.models.tenant_models import Base, JobTemplate, RecurringJobSchedule
from gdx_dispatch.routers.recurring_jobs import materialize_due_recurring_jobs


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine, checkfirst=True)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _due_schedule(db, *, template_id: str) -> RecurringJobSchedule:
    row = RecurringJobSchedule(
        id=str(uuid4()),
        created_at=_now_iso(),
        updated_at=_now_iso(),
        job_template_id=template_id,
        frequency="monthly",
        customer_id=str(uuid4()),
        next_run=(datetime.now(UTC) - timedelta(days=1)).isoformat(),
        status="active",
    )
    db.add(row)
    db.commit()
    return row


def test_a_schedule_with_a_soft_deleted_template_warns_instead_of_vanishing(db, caplog):
    tpl = JobTemplate(
        id=str(uuid4()),
        title="Spring tune-up",
        job_type="service",
        default_priority="normal",
        estimated_duration=60,
        created_at=_now_iso(),
        updated_at=_now_iso(),
        deleted_at=_now_iso(),
    )
    db.add(tpl)
    db.commit()
    sched = _due_schedule(db, template_id=tpl.id)

    with caplog.at_level(logging.WARNING, logger="gdx_dispatch.routers.recurring_jobs"):
        result = materialize_due_recurring_jobs(db, tenant_id="t-1")

    assert result.get("created_count", result.get("created", 0)) == 0
    hits = [r for r in caplog.records if "recurring_schedule_template_missing" in r.getMessage()]
    assert hits, "missing template was skipped silently"
    msg = hits[0].getMessage()
    assert f"schedule_id={sched.id}" in msg
    assert f"job_template_id={tpl.id}" in msg


def test_a_schedule_whose_template_never_existed_warns_too(db, caplog):
    sched = _due_schedule(db, template_id=str(uuid4()))

    with caplog.at_level(logging.WARNING, logger="gdx_dispatch.routers.recurring_jobs"):
        materialize_due_recurring_jobs(db, tenant_id="t-1")

    assert any(
        f"schedule_id={sched.id}" in r.getMessage() for r in caplog.records
    ), "dangling template id was skipped silently"

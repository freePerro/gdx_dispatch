"""Migration 126: a completed job's open visits are closed, by F1's rule.

On both engines, against an ORM-built schema seeded through the ORM: an
on-site visit and an open visit on or before the job's completion day (shop
time, read from ``app_settings``) are closed; an open visit on a later shop day
is retired; visits of open or soft-deleted jobs and visits already closed or
cancelled do not move. The audit event chains and records each prior value, a
rerun is a no-op, and the downgrade puts a visit back only where it still
holds what the migration wrote.
"""
from __future__ import annotations

import importlib.util
import pathlib
import re
import uuid
from datetime import UTC, datetime, timedelta

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

MIGRATION = pathlib.Path(__file__).resolve().parents[1] / "migrations/versions/126_completed_job_visits_closed.py"
REV = "126_completed_job_visits_closed"
TENANT = "t-126"
# 22:00 on 2026-09-01 in Los Angeles, already 2026-09-02 in New York and UTC.
JOB_DONE = datetime(2026, 9, 2, 5, 0, tzinfo=UTC)


def _load(conn):
    spec = importlib.util.spec_from_file_location("m126", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if conn is not None:
        mod.op = Operations(MigrationContext.configure(conn))
    return mod


def test_it_chains_onto_125_and_is_the_only_reviser() -> None:
    source = MIGRATION.read_text()
    assert f'revision = "{REV}"' in source
    assert 'down_revision = "125_planner_task_reminders"' in source
    assert len(REV) <= 32  # alembic_version.version_num is VARCHAR(32)
    revising = [
        p.name for p in MIGRATION.parent.glob("*.py")
        if re.search(r'^down_revision = "125_planner_task_reminders"', p.read_text(), re.M)
    ]
    assert revising == [MIGRATION.name], revising


def test_no_percent_signs_in_its_sql() -> None:
    m = _load(None)
    for stmt in (m._CANDIDATES, m._TZ, m._CLOSE, m._RETIRE, m._EVENTS, m._UNCLOSE, m._UNRETIRE):
        assert "%" not in stmt.text, stmt.text


def _seed(s: Session) -> dict[str, uuid.UUID]:
    from gdx_dispatch.models.tenant_models import Appointment, AppSettings, Job

    s.add(AppSettings(company_name="Shop", timezone="America/Los_Angeles"))
    jobs: dict[str, uuid.UUID] = {}

    def job(key, stage, completed_at=JOB_DONE, deleted=False):
        j = Job(
            title=key, company_id=TENANT, lifecycle_stage=stage,
            completed_at=completed_at if stage == "completed" else None,
            deleted_at=datetime.now(UTC) if deleted else None,
        )
        s.add(j)
        s.flush()
        jobs[key] = j.id

    job("done", "completed")
    job("done_undated", "completed", completed_at=None)
    job("done_deleted", "completed", deleted=True)
    job("open", "scheduled")

    ids: dict[str, uuid.UUID] = {}

    def visit(key, job_key, start, status="scheduled", arrived=False, completed_at=None):
        a = Appointment(
            company_id=TENANT, job_id=jobs[job_key], title=key, tech_id="tech-1",
            start_at=start, end_at=start + timedelta(hours=1), status=status,
            arrived_at=start if arrived else None, completed_at=completed_at,
        )
        s.add(a)
        s.flush()
        ids[key] = a.id

    # Earlier the same shop day as the finish: closed.
    visit("same_day", "done", datetime(2026, 9, 1, 16, 0, tzinfo=UTC), status="confirmed")
    # 01:00 on 09-02 in Los Angeles, a day after the finish there; in New York
    # or UTC it would be the finish day. Retired only when the shop zone is read.
    visit("next_shop_day", "done", datetime(2026, 9, 2, 8, 0, tzinfo=UTC))
    # On site, though on a later day: closed, never retired.
    visit("on_site_later", "done", datetime(2026, 9, 3, 15, 0, tzinfo=UTC), arrived=True)
    visit("arrived_status", "done", datetime(2026, 8, 30, 15, 0, tzinfo=UTC), status="arrived")
    # A job finished with no completion time: closed, completed_at stays NULL.
    visit("undated_job", "done_undated", datetime(2026, 8, 1, 15, 0, tzinfo=UTC))
    # Not touched.
    visit("already_closed", "done", datetime(2026, 9, 1, 14, 0, tzinfo=UTC), status="completed",
          completed_at=datetime(2026, 9, 1, 15, 0, tzinfo=UTC))
    visit("cancelled", "done", datetime(2026, 9, 1, 13, 0, tzinfo=UTC), status="cancelled")
    visit("deleted_job", "done_deleted", datetime(2026, 9, 1, 15, 0, tzinfo=UTC))
    visit("open_job", "open", datetime(2026, 9, 1, 15, 0, tzinfo=UTC))
    s.commit()
    return ids


def _state(s: Session, ids) -> dict[str, tuple]:
    from gdx_dispatch.models.tenant_models import Appointment

    s.expire_all()
    out = {}
    for k, v in ids.items():
        a = s.get(Appointment, v)
        done = a.completed_at.replace(tzinfo=a.completed_at.tzinfo or UTC) if a.completed_at else None
        out[k] = (a.status, done, a.deleted_at is not None)
    return out


def _events(s: Session, action: str = "completed_job_visits_closed"):
    from gdx_dispatch.core.audit import AuditLog

    return s.execute(select(AuditLog).where(AuditLog.action == action)).scalars().all()


def _exercise(engine) -> None:
    from gdx_dispatch.core.audit import AuditLog, log_audit_event_sync, verify_audit_chain
    from gdx_dispatch.models.tenant_models import Appointment

    with Session(engine) as s:
        log_audit_event_sync(s, tenant_id=TENANT, user_id="u", action="seed", entity_type="x", details={})
        s.commit()
        ids = _seed(s)
        before = _state(s, ids)
        fixed = {
            k: (a.arrived_at, a.tech_id, a.start_at, a.updated_at)
            for k, a in ((k, s.get(Appointment, v)) for k, v in ids.items())
        }

    with engine.begin() as c:
        _load(c).upgrade()

    with Session(engine) as s:
        after = _state(s, ids)
        assert after["same_day"] == ("completed", JOB_DONE, False)
        assert after["next_shop_day"] == ("scheduled", None, True)
        assert after["on_site_later"] == ("completed", JOB_DONE, False)
        assert after["arrived_status"] == ("completed", JOB_DONE, False)
        assert after["undated_job"] == ("completed", None, False)
        for k in ("already_closed", "cancelled", "deleted_job", "open_job"):
            assert after[k] == before[k], k
        for k, v in ids.items():
            a = s.get(Appointment, v)
            assert (a.arrived_at, a.tech_id, a.start_at, a.updated_at) == fixed[k], k

        (event,) = _events(s)
        assert event.user_id == "migration:126_completed_job_visits_closed"
        assert event.tenant_id == TENANT
        visits = event.details["visits"]
        assert set(visits) == {str(ids[k]) for k in (
            "same_day", "next_shop_day", "on_site_later", "arrived_status", "undated_job")}
        assert visits[str(ids["next_shop_day"])] == {"action": "retired", "status": "scheduled", "completed_at": None}
        assert visits[str(ids["same_day"])] == {"action": "closed", "status": "confirmed", "completed_at": None}
        assert verify_audit_chain(s) is True

    with engine.begin() as c:
        _load(c).upgrade()
    with Session(engine) as s:
        assert len(_events(s)) == 1

        # A closed visit is reopened by hand since: its new state is not the migration's.
        a = s.get(Appointment, ids["arrived_status"])
        a.status = "en_route"
        s.commit()
        count = s.scalar(select(func.count()).select_from(AuditLog))

    with engine.begin() as c:
        _load(c).downgrade()

    with Session(engine) as s:
        back = _state(s, ids)
        assert back["same_day"] == before["same_day"]
        assert back["next_shop_day"] == before["next_shop_day"]
        assert back["on_site_later"] == before["on_site_later"]
        assert back["undated_job"] == before["undated_job"]
        assert back["arrived_status"][0] == "en_route"
        for k in ("already_closed", "cancelled", "deleted_job", "open_job"):
            assert back[k] == before[k], k
        assert s.scalar(select(func.count()).select_from(AuditLog)) == count + 1
        (reverted,) = _events(s, "completed_job_visits_closed_reverted")
        assert reverted.details["restored"] == 4
        assert verify_audit_chain(s) is True


def test_sqlite(tmp_path) -> None:
    from gdx_dispatch.core.audit import TenantBase

    engine = create_engine(f"sqlite:///{tmp_path / 'm126.db'}", future=True)
    TenantBase.metadata.create_all(bind=engine)
    _exercise(engine)


def test_postgres(pg_test_db) -> None:
    """In a schema of its own built from the ORM, so ``lifecycle_stage`` and the
    timestamp columns are the native types prod has."""
    from gdx_dispatch.core.audit import TenantBase

    schema = "m126_test"
    engine = create_engine(pg_test_db, future=True, connect_args={"options": f"-csearch_path={schema}"})
    try:
        with engine.begin() as c:
            c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            c.exec_driver_sql(f"CREATE SCHEMA {schema}")
        TenantBase.metadata.create_all(engine)
        _exercise(engine)
    finally:
        with engine.begin() as c:
            c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        engine.dispose()


def test_on_a_database_without_the_tables_both_directions_are_no_ops(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'empty.db'}", future=True)
    with engine.begin() as c:
        m = _load(c)
        m.upgrade()
        m.downgrade()

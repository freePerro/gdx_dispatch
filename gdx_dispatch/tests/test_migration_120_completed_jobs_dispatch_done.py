"""Migration 120: completed jobs read dispatch_status ``done`` (GDXA-834).

On both engines, against an ORM-built schema seeded through the ORM: every
completed job not at ``done`` moves to ``done`` and nothing else on the row
moves, no other stage is touched, the audit event chains and records each
prior value, a rerun is a no-op, and the downgrade restores the prior value
only where the job is still completed and still ``done``.
"""
from __future__ import annotations

import importlib.util
import pathlib
import re
import uuid
from datetime import UTC, datetime

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

MIGRATION = pathlib.Path(__file__).resolve().parents[1] / "migrations/versions/120_completed_jobs_dispatch_done.py"
REV = "120_completed_jobs_dispatch_done"
TENANT = "t-120"


def _load(conn):
    spec = importlib.util.spec_from_file_location("m120", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if conn is not None:
        mod.op = Operations(MigrationContext.configure(conn))
    return mod


def test_it_chains_onto_119_and_is_the_only_reviser() -> None:
    source = MIGRATION.read_text()
    assert f'revision = "{REV}"' in source
    assert 'down_revision = "119_backfill_job_numbers"' in source
    assert len(REV) <= 32  # alembic_version.version_num is VARCHAR(32)
    revising = [
        p.name for p in MIGRATION.parent.glob("*.py")
        if re.search(r'^down_revision = "119_backfill_job_numbers"', p.read_text(), re.M)
    ]
    assert revising == [MIGRATION.name], revising


def test_no_percent_signs_in_its_sql() -> None:
    m = _load(None)
    for stmt in (m._CANDIDATES, m._DONE, m._EVENTS, m._RESTORE):
        assert "%" not in stmt.text, stmt.text


def _seed(s: Session) -> dict[str, uuid.UUID]:
    from gdx_dispatch.models.tenant_models import Job

    completed_at = datetime(2026, 9, 1, tzinfo=UTC)
    ids: dict[str, uuid.UUID] = {}

    def job(key, stage, dispatch, deleted=False):
        j = Job(
            title=key, company_id=TENANT, lifecycle_stage=stage, dispatch_status=dispatch,
            completed_at=completed_at if stage == "completed" else None,
            assigned_to="tech-1", deleted_at=datetime.now(UTC) if deleted else None,
        )
        s.add(j)
        s.flush()
        ids[key] = j.id

    job("completed_unassigned", "completed", "unassigned")
    job("completed_assigned", "completed", "assigned")
    job("completed_on_site", "completed", "on_site")
    job("completed_done", "completed", "done")
    job("completed_deleted", "completed", "unassigned", deleted=True)
    job("scheduled_assigned", "scheduled", "assigned")
    job("cancelled_unassigned", "cancelled", "unassigned")
    job("in_progress_on_site", "in_progress", "on_site")
    s.commit()
    return ids


def _dispatch(s: Session, ids) -> dict[str, str]:
    from gdx_dispatch.models.tenant_models import Job

    s.expire_all()
    return {k: s.get(Job, v).dispatch_status for k, v in ids.items()}


def _events(s: Session, action: str = "job_dispatch_done_backfill"):
    from gdx_dispatch.core.audit import AuditLog

    return s.execute(select(AuditLog).where(AuditLog.action == action)).scalars().all()


def _exercise(engine) -> None:
    from gdx_dispatch.core.audit import AuditLog, log_audit_event_sync, verify_audit_chain
    from gdx_dispatch.models.tenant_models import Job

    with Session(engine) as s:
        log_audit_event_sync(s, tenant_id=TENANT, user_id="u", action="seed", entity_type="x", details={})
        s.commit()
        ids = _seed(s)
        before = _dispatch(s, ids)
        snapshot = {
            k: (j.lifecycle_stage, j.completed_at, j.assigned_to, j.updated_at, j.billing_status)
            for k, j in ((k, s.get(Job, v)) for k, v in ids.items())
        }

    with engine.begin() as c:
        _load(c).upgrade()

    changed = ("completed_unassigned", "completed_assigned", "completed_on_site", "completed_deleted")
    with Session(engine) as s:
        after = _dispatch(s, ids)
        for k in changed:
            assert after[k] == "done", k
        for k in ("completed_done", "scheduled_assigned", "cancelled_unassigned", "in_progress_on_site"):
            assert after[k] == before[k], k
        for k, v in ids.items():
            j = s.get(Job, v)
            assert (j.lifecycle_stage, j.completed_at, j.assigned_to, j.updated_at, j.billing_status) == snapshot[k], k

        (event,) = _events(s)
        assert event.user_id == "migration:120_completed_jobs_dispatch_done"
        assert event.tenant_id == TENANT
        assert event.details["prior"] == {str(ids[k]): before[k] for k in changed}
        assert verify_audit_chain(s) is True

    with engine.begin() as c:
        _load(c).upgrade()
    with Session(engine) as s:
        assert len(_events(s)) == 1

        # A backfilled job is reopened since and dispatched again.
        j = s.get(Job, ids["completed_assigned"])
        j.lifecycle_stage, j.dispatch_status = "scheduled", "en_route"
        s.commit()
        count = s.scalar(select(func.count()).select_from(AuditLog))

    with engine.begin() as c:
        _load(c).downgrade()

    with Session(engine) as s:
        back = _dispatch(s, ids)
        assert back["completed_unassigned"] == "unassigned"
        assert back["completed_on_site"] == "on_site"
        assert back["completed_deleted"] == "unassigned"
        assert back["completed_assigned"] == "en_route", "reopened since: its new state is not the migration's"
        assert back["completed_done"] == "done"
        assert s.scalar(select(func.count()).select_from(AuditLog)) == count + 1
        (reverted,) = _events(s, "job_dispatch_done_backfill_reverted")
        assert reverted.details["restored"] == 3
        assert verify_audit_chain(s) is True


def test_sqlite(tmp_path) -> None:
    from gdx_dispatch.core.audit import TenantBase

    engine = create_engine(f"sqlite:///{tmp_path / 'm120.db'}", future=True)
    TenantBase.metadata.create_all(bind=engine)
    _exercise(engine)


def test_postgres(pg_test_db) -> None:
    """In a schema of its own built from the ORM, so ``dispatch_status`` is the
    native ``job_dispatch_status`` enum the restore writes into."""
    from gdx_dispatch.core.audit import TenantBase

    schema = "m120_test"
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

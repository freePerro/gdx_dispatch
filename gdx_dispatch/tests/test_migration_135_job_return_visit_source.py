"""Migration 135: a return-visit job records which path created it
(``jobs.return_visit_source``, GDXA-1946).

On both engines, against an ORM-built schema seeded through the ORM and the
real audit writer: the column is dropped (as at 134) and the upgrade adds it
back nullable, then backfills each return visit from the audit row its
creator logged (closeout / callback / follow_up). A child with no such row, a
child already set, and a job that is not a return visit are left alone; the
backfill's own audit event chains and lists every id; a rerun writes nothing;
the downgrade drops the column and keeps every row.
"""
from __future__ import annotations

import importlib.util
import pathlib
import re
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session

MIGRATION = pathlib.Path(__file__).resolve().parents[1] / "migrations/versions/135_job_return_visit_source.py"
REV = "135_job_return_visit_source"
TENANT = "13513513-5135-4135-8135-135135135135"


def _load(conn):
    spec = importlib.util.spec_from_file_location("m135", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if conn is not None:
        mod.op = Operations(MigrationContext.configure(conn))
    return mod


def test_it_chains_onto_134_and_is_the_only_reviser() -> None:
    source = MIGRATION.read_text()
    assert f'revision = "{REV}"' in source
    assert 'down_revision = "134_drop_tech_unavailability"' in source
    assert len(REV) <= 32  # alembic_version.version_num is VARCHAR(32)
    revising = [
        p.name for p in MIGRATION.parent.glob("*.py")
        if re.search(r'^down_revision = "134_drop_tech_unavailability"', p.read_text(), re.M)
    ]
    assert revising == [MIGRATION.name], revising
    m = _load(None)
    for stmt in (m._EVENTS, m._SET, m._STILL_NULL):
        assert "%" not in stmt.text, stmt.text


def test_the_orm_model_carries_the_column_nullable() -> None:
    from gdx_dispatch.models.tenant_models import Job

    col = Job.__table__.columns["return_visit_source"]
    assert col.nullable is True
    assert col.type.length == 20


def _has_column(conn) -> bool:
    return "return_visit_source" in {c["name"] for c in inspect(conn).get_columns("jobs")}


def _exercise(engine) -> None:
    from gdx_dispatch.core.audit import AuditLog, log_audit_event_sync, verify_audit_chain
    from gdx_dispatch.models.tenant_models import Job

    def job(s, **kw) -> uuid.UUID:
        j = Job(title=kw.pop("title"), company_id=TENANT, **kw)
        s.add(j)
        s.flush()
        return j.id

    def spawned(s, job_id, action, **details) -> None:
        log_audit_event_sync(s, tenant_id=TENANT, user_id="u", action=action,
                             entity_type="job", entity_id=str(job_id), details=details)

    with Session(engine) as s:
        parent = job(s, title="parent", lifecycle_stage="completed")
        ids = {
            "closeout": job(s, title="closeout", parent_job_id=parent, is_return_visit=True),
            "callback": job(s, title="callback", parent_job_id=parent, is_return_visit=True),
            "follow_up": job(s, title="follow_up", parent_job_id=parent, is_return_visit=True),
            "no_audit": job(s, title="no_audit", parent_job_id=parent, is_return_visit=True),
            "already": job(s, title="already", parent_job_id=parent, is_return_visit=True),
            "not_rv": job(s, title="not_rv", parent_job_id=parent, is_return_visit=False),
        }
        spawned(s, ids["closeout"], "return_visit_spawned", original_job_id=str(parent),
                reason="day 2", source="closeout")
        spawned(s, ids["callback"], "return_visit_spawned", original_job_id=str(parent), reason="warranty")
        spawned(s, ids["follow_up"], "follow_up_job_created", original_job_id=str(parent))
        # Already recorded by the writer: the backfill must not overwrite it.
        spawned(s, ids["already"], "return_visit_spawned", original_job_id=str(parent))
        # An audit row for a job that is not a return visit changes nothing.
        spawned(s, ids["not_rv"], "follow_up_job_created", original_job_id=str(parent))
        s.commit()
        s.get(Job, ids["already"]).return_visit_source = "closeout"
        s.commit()

    # Back to 134: no column.
    with engine.begin() as c:
        _load(c).downgrade()
        assert not _has_column(c)
        assert c.exec_driver_sql("SELECT COUNT(*) FROM jobs").scalar() == 7

    # The "already" row lost its value with the column; set it again in raw
    # SQL after the add so the no-overwrite rule is what is under test.
    with engine.begin() as c:
        m = _load(c)
        from sqlalchemy import Column, String

        with m.op.batch_alter_table("jobs") as batch:
            batch.add_column(Column("return_visit_source", String(20), nullable=True))
        stored = ids["already"].hex if engine.dialect.name == "sqlite" else str(ids["already"])
        c.execute(text("UPDATE jobs SET return_visit_source = 'closeout' WHERE id = :id"), {"id": stored})
        m.upgrade()  # column present: only the backfill runs

    with Session(engine) as s:
        got = {k: s.scalar(select(Job.return_visit_source).where(Job.id == v)) for k, v in ids.items()}
        assert got == {
            "closeout": "closeout",
            "callback": "callback",
            "follow_up": "follow_up",
            "no_audit": None,
            "already": "closeout",
            "not_rv": None,
        }
        (event,) = s.execute(
            select(AuditLog).where(AuditLog.action == "return_visit_source_backfill")
        ).scalars().all()
        assert event.user_id == "migration:135_job_return_visit_source"
        assert event.details["jobs"] == {
            "closeout": [str(ids["closeout"])],
            "callback": [str(ids["callback"])],
            "follow_up": [str(ids["follow_up"])],
        }
        assert event.details["left_null"] == 1
        assert verify_audit_chain(s) is True

    # Rerun: nothing NULL has an audit row left, so no second event.
    with engine.begin() as c:
        _load(c).upgrade()
    with Session(engine) as s:
        assert len(s.execute(
            select(AuditLog).where(AuditLog.action == "return_visit_source_backfill")
        ).scalars().all()) == 1

    # Down, and up from scratch: the add path, then the same backfill.
    with engine.begin() as c:
        _load(c).downgrade()
        assert not _has_column(c)
        _load(c).downgrade()  # rerun is a no-op
        _load(c).upgrade()
        assert _has_column(c)
    with Session(engine) as s:
        assert s.scalar(select(Job.return_visit_source).where(Job.id == ids["closeout"])) == "closeout"
        # The writer's value went with the column; its audit row rebuilds it.
        assert s.scalar(select(Job.return_visit_source).where(Job.id == ids["already"])) == "callback"
        assert verify_audit_chain(s) is True


def _schema(engine) -> None:
    from gdx_dispatch.core.audit import TenantBase

    TenantBase.metadata.create_all(engine)


def test_sqlite(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'm135.db'}", future=True)
    _schema(engine)
    _exercise(engine)
    engine.dispose()


def test_postgres(pg_test_db) -> None:
    """In a schema of its own built from the ORM, as 119's test does."""
    schema = "m135_test"
    engine = create_engine(pg_test_db, future=True, connect_args={"options": f"-csearch_path={schema}"})
    try:
        with engine.begin() as c:
            c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            c.exec_driver_sql(f"CREATE SCHEMA {schema}")
        _schema(engine)
        _exercise(engine)
    finally:
        with engine.begin() as c:
            c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        engine.dispose()


def test_on_a_database_without_jobs_both_directions_are_no_ops(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'empty.db'}", future=True)
    with engine.begin() as c:
        m = _load(c)
        m.upgrade()
        m.downgrade()
        assert inspect(c).get_table_names() == []
    engine.dispose()

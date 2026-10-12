"""Migration 119: live jobs created without a job_number are numbered oldest
first from the tenant's own counter (GDXA-834).

On both engines, against an ORM-built schema seeded through the ORM: the
numbers follow ``created_at``, a number already on any job (live or deleted) is
skipped, soft-deleted jobs stay unnumbered, the counter moves past the last
number handed out, the audit event chains and lists every id with its number,
a rerun is a no-op, and the downgrade clears only the numbers it wrote and
leaves the counter where it is.
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

MIGRATION = pathlib.Path(__file__).resolve().parents[1] / "migrations/versions/119_backfill_job_numbers.py"
REV = "119_backfill_job_numbers"
TENANT = uuid.UUID("11911911-9119-4119-8119-119119119119")
YEAR = datetime.now(UTC).year


def _load(conn):
    spec = importlib.util.spec_from_file_location("m119", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if conn is not None:
        mod.op = Operations(MigrationContext.configure(conn))
    return mod


def test_it_chains_onto_118_and_is_the_only_reviser() -> None:
    source = MIGRATION.read_text()
    assert f'revision = "{REV}"' in source
    assert 'down_revision = "118_closeout_billed_invoice"' in source
    assert len(REV) <= 32  # alembic_version.version_num is VARCHAR(32)
    revising = [
        p.name for p in MIGRATION.parent.glob("*.py")
        if re.search(r'^down_revision = "118_closeout_billed_invoice"', p.read_text(), re.M)
    ]
    assert revising == [MIGRATION.name], revising


def test_no_percent_signs_in_its_sql() -> None:
    m = _load(None)
    for stmt in (m._UNNUMBERED, m._USED, m._SETTINGS, m._NUMBER, m._COUNTER, m._EVENTS, m._UNNUMBER):
        assert "%" not in stmt.text, stmt.text


def _schema(engine) -> None:
    from gdx_dispatch.core.audit import TenantBase
    from gdx_dispatch.core.tenant_settings import Tenant, TenantSettings

    TenantBase.metadata.create_all(engine)
    Tenant.__table__.create(engine)
    TenantSettings.__table__.create(engine)


def _settings(s: Session, *, seq: int, year_seen: int | None, fmt: str = "JOB-{year}-{seq:03d}") -> None:
    from gdx_dispatch.core.tenant_settings import Tenant, TenantSettings

    s.add(Tenant(id=TENANT, slug="t-119", name="t-119"))
    s.flush()
    s.add(TenantSettings(
        tenant_id=TENANT, job_number_format=fmt, job_number_next_seq=seq, job_number_year_seen=year_seen,
    ))
    s.flush()


def _job(s: Session, *, number=None, age_days=0, deleted=False, customer=None) -> uuid.UUID:
    from gdx_dispatch.models.tenant_models import Job

    now = datetime.now(UTC)
    j = Job(
        title="j", company_id=str(TENANT), job_number=number,
        customer_id=customer.id if customer is not None else None,
        created_at=now - timedelta(days=age_days),
        deleted_at=now if deleted else None,
    )
    s.add(j)
    s.flush()
    return j.id


def _numbers(s: Session, ids) -> dict[str, str | None]:
    from gdx_dispatch.models.tenant_models import Job

    s.expire_all()
    return {k: s.get(Job, v).job_number for k, v in ids.items()}


def _counter(s: Session) -> tuple[int, int | None]:
    from gdx_dispatch.core.tenant_settings import TenantSettings

    s.expire_all()
    ts = s.get(TenantSettings, TENANT)
    return ts.job_number_next_seq, ts.job_number_year_seen


def _events(s: Session, action: str = "job_number_backfill"):
    from gdx_dispatch.core.audit import AuditLog

    return s.execute(select(AuditLog).where(AuditLog.action == action)).scalars().all()


def _exercise(engine) -> None:
    from gdx_dispatch.core.audit import AuditLog, log_audit_event_sync, verify_audit_chain
    from gdx_dispatch.models.tenant_models import Customer, Job

    with Session(engine) as s:
        log_audit_event_sync(s, tenant_id=str(TENANT), user_id="u", action="seed", entity_type="x", details={})
        s.commit()
        _settings(s, seq=5, year_seen=YEAR)
        cust = Customer(name="Becky Meinecke", company_id=str(TENANT))
        s.add(cust)
        s.flush()
        ids = {
            # Already numbered: 005 is the counter's next value, 007 sits on a
            # deleted job. Both must be skipped, never reissued.
            "has_005": _job(s, number=f"JOB-{YEAR}-005", age_days=50),
            "deleted_007": _job(s, number=f"JOB-{YEAR}-007", age_days=50, deleted=True),
            # Added newest first, so insertion order is not what orders them.
            "newest": _job(s, age_days=1),
            "middle": _job(s, age_days=10, customer=cust),
            "oldest": _job(s, age_days=30),
            "deleted_unnumbered": _job(s, age_days=40, deleted=True),
        }
        s.commit()

    with engine.begin() as c:
        _load(c).upgrade()

    with Session(engine) as s:
        after = _numbers(s, ids)
        assert after["oldest"] == f"JOB-{YEAR}-006"
        assert after["middle"] == f"JOB-{YEAR}-008"
        assert after["newest"] == f"JOB-{YEAR}-009"
        assert after["has_005"] == f"JOB-{YEAR}-005"
        assert after["deleted_007"] == f"JOB-{YEAR}-007"
        assert after["deleted_unnumbered"] is None
        assert _counter(s) == (10, YEAR)

        (event,) = _events(s)
        assert event.user_id == "migration:119_backfill_job_numbers"
        assert event.tenant_id == str(TENANT)
        assert event.details["jobs"] == {
            str(ids["oldest"]): f"JOB-{YEAR}-006",
            str(ids["middle"]): f"JOB-{YEAR}-008",
            str(ids["newest"]): f"JOB-{YEAR}-009",
        }
        assert verify_audit_chain(s) is True

    # Rerun: no unnumbered live job left, so nothing moves and no second event.
    with engine.begin() as c:
        _load(c).upgrade()
    with Session(engine) as s:
        assert len(_events(s)) == 1
        assert _counter(s) == (10, YEAR)

        # Someone renumbers one backfilled job by hand after the release.
        s.get(Job, ids["middle"]).job_number = "HAND-1"
        s.commit()
        count = s.scalar(select(func.count()).select_from(AuditLog))

    with engine.begin() as c:
        _load(c).downgrade()

    with Session(engine) as s:
        back = _numbers(s, ids)
        assert back["oldest"] is None and back["newest"] is None
        assert back["middle"] == "HAND-1", "a number set since is not the migration's to undo"
        assert back["has_005"] == f"JOB-{YEAR}-005"
        assert _counter(s) == (10, YEAR), "the counter is never wound back"
        assert s.scalar(select(func.count()).select_from(AuditLog)) == count + 1
        (reverted,) = _events(s, "job_number_backfill_reverted")
        assert reverted.details["restored"] == 2
        assert verify_audit_chain(s) is True


def test_sqlite(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'm119.db'}", future=True)
    _schema(engine)
    _exercise(engine)


def test_postgres(pg_test_db) -> None:
    """In a schema of its own built from the ORM, as 117's test does."""
    schema = "m119_test"
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


def test_a_year_change_restarts_the_series_and_initials_render(tmp_path) -> None:
    """The counter resets on a new year exactly as next_job_number resets it,
    and a ``{customer_initials}`` template gets the job's customer."""
    from gdx_dispatch.models.tenant_models import Customer

    engine = create_engine(f"sqlite:///{tmp_path / 'year.db'}", future=True)
    _schema(engine)
    with Session(engine) as s:
        _settings(s, seq=42, year_seen=YEAR - 1, fmt="{customer_initials}-{yy}-{seq:03d}")
        cust = Customer(name="Becky Meinecke", company_id=str(TENANT))
        s.add(cust)
        s.flush()
        ids = {"a": _job(s, customer=cust, age_days=2), "b": _job(s, age_days=1)}
        s.commit()
    with engine.begin() as c:
        _load(c).upgrade()
    with Session(engine) as s:
        yy = f"{YEAR % 100:02d}"
        assert _numbers(s, ids) == {"a": f"BM-{yy}-001", "b": f"-{yy}-002"}
        assert _counter(s) == (3, YEAR)


def test_without_a_counter_jobs_are_left_and_nothing_is_written(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'nocounter.db'}", future=True)
    _schema(engine)
    with Session(engine) as s:
        ids = {"a": _job(s)}
        s.commit()
    with engine.begin() as c:
        _load(c).upgrade()
    with Session(engine) as s:
        assert _numbers(s, ids) == {"a": None}
        assert _events(s) == []


def test_a_template_without_seq_numbers_one_job_and_stops(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'noseq.db'}", future=True)
    _schema(engine)
    with Session(engine) as s:
        _settings(s, seq=1, year_seen=YEAR, fmt="FIXED")
        ids = {"a": _job(s, age_days=2), "b": _job(s, age_days=1)}
        s.commit()
    with engine.begin() as c:
        _load(c).upgrade()
    with Session(engine) as s:
        assert _numbers(s, ids) == {"a": "FIXED", "b": None}
        (event,) = _events(s)
        assert event.details["left_as_is"] == [str(ids["b"])]


def test_on_a_database_without_the_tables_both_directions_are_no_ops(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'empty.db'}", future=True)
    with engine.begin() as c:
        m = _load(c)
        m.upgrade()
        m.downgrade()

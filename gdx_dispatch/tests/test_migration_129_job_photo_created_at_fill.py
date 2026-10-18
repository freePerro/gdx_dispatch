"""Migration 129 and the model default: ``job_photos.created_at``.

On both engines: a NULL ``created_at`` takes its row's ``uploaded_at``, a set
one is left alone, rerunnable, and a missing table or column is skipped. Then
the model: a photo written through the ORM has ``created_at`` filled.
"""
from __future__ import annotations

import importlib.util
import pathlib
import re
from datetime import datetime, timezone
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

MIGRATION = pathlib.Path(__file__).resolve().parents[1] / "migrations/versions/129_job_photo_created_at_fill.py"


def _load(conn):
    spec = importlib.util.spec_from_file_location("m129", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.op = Operations(MigrationContext.configure(conn))
    return mod


def _legacy_schema(conn) -> None:
    """The Flask-era table: one photo never stamped, one already stamped."""
    conn.exec_driver_sql(
        "CREATE TABLE job_photos (id VARCHAR(32) PRIMARY KEY, uploaded_at TIMESTAMP NOT NULL, created_at TIMESTAMP)"
    )
    conn.exec_driver_sql(
        "INSERT INTO job_photos (id, uploaded_at, created_at) VALUES "
        "('null', '2026-09-01 10:00:00', NULL), "
        "('set', '2026-09-02 10:00:00', '2026-01-01 00:00:00')"
    )


def _created(conn) -> dict[str, str]:
    rows = conn.exec_driver_sql("SELECT id, created_at FROM job_photos").all()
    return {r[0]: str(r[1]) for r in rows}


def test_it_chains_onto_128_and_is_the_only_reviser() -> None:
    source = MIGRATION.read_text()
    assert 'revision = "129_job_photo_created_at_fill"' in source
    assert 'down_revision = "128_on_my_way_sms_opt_in"' in source
    assert len("129_job_photo_created_at_fill") <= 32  # alembic_version.version_num is VARCHAR(32)
    revising = [
        p.name for p in MIGRATION.parent.glob("*.py")
        if re.search(r'^down_revision = "128_on_my_way_sms_opt_in"', p.read_text(), re.M)
    ]
    assert revising == ["129_job_photo_created_at_fill.py"], revising


def test_no_unescaped_percent_signs() -> None:
    assert "%" not in MIGRATION.read_text().replace("%%", "")


def test_sqlite_null_takes_uploaded_at_and_set_is_kept(tmp_path) -> None:
    eng = create_engine(f"sqlite:///{tmp_path / 'm129.db'}", future=True)
    with eng.begin() as c:
        _legacy_schema(c)
        m = _load(c)
        m.upgrade()
        assert _created(c) == {"null": "2026-09-01 10:00:00", "set": "2026-01-01 00:00:00"}
        m.upgrade()  # rerun is a no-op
        assert _created(c) == {"null": "2026-09-01 10:00:00", "set": "2026-01-01 00:00:00"}
        m.downgrade()  # keeps the fill; nothing to restore
    eng.dispose()


def test_sqlite_missing_table_or_column_is_skipped(tmp_path) -> None:
    eng = create_engine(f"sqlite:///{tmp_path / 'm129_skip.db'}", future=True)
    with eng.begin() as c:
        _load(c).upgrade()  # no table
        c.exec_driver_sql("CREATE TABLE job_photos (id VARCHAR(32) PRIMARY KEY, uploaded_at TIMESTAMP NOT NULL)")
        _load(c).upgrade()  # no created_at column
    eng.dispose()


def test_the_orm_fills_created_at_on_a_new_photo(tmp_path) -> None:
    from gdx_dispatch.core.audit import TenantBase
    from gdx_dispatch.models.tenant_models import JobPhoto

    eng = create_engine(f"sqlite:///{tmp_path / 'orm.db'}", future=True)
    TenantBase.metadata.create_all(bind=eng, tables=[JobPhoto.__table__])
    with Session(eng) as s:
        photo = JobPhoto(company_id="t", job_id=uuid4(), url="/api/documents/x/download")
        s.add(photo)
        s.commit()
        s.refresh(photo)
        assert photo.created_at is not None
        assert photo.uploaded_at is not None
        # Both stamped by the same insert; they differ by the time between two
        # utcnow() calls, never by more than a second.
        created = photo.created_at.replace(tzinfo=photo.created_at.tzinfo or timezone.utc)
        uploaded = photo.uploaded_at.replace(tzinfo=photo.uploaded_at.tzinfo or timezone.utc)
        assert abs((created - uploaded).total_seconds()) < 1
        assert created <= datetime.now(timezone.utc)
    eng.dispose()


def test_postgres_null_takes_uploaded_at_and_set_is_kept(pg_test_db) -> None:
    eng = create_engine(pg_test_db, future=True)
    schema = "m129_test"
    try:
        with eng.connect() as c:
            c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            c.exec_driver_sql(f"CREATE SCHEMA {schema}")
            c.exec_driver_sql(f"SET search_path TO {schema}")
            m = _load(c)
            m.upgrade()  # a missing table is skipped
            _legacy_schema(c)
            c.commit()
            m.upgrade()
            c.commit()
            assert _created(c) == {"null": "2026-09-01 10:00:00", "set": "2026-01-01 00:00:00"}
            m.upgrade()  # rerun is a no-op
            m.downgrade()
            c.commit()
            assert _created(c) == {"null": "2026-09-01 10:00:00", "set": "2026-01-01 00:00:00"}
    finally:
        with eng.connect() as c:
            c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            c.commit()
        eng.dispose()

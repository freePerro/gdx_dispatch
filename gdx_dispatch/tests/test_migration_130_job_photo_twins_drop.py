"""Migration 130 and the model: ``job_photos.content_type`` / ``file_size`` dropped.

On both engines: a NULL ``mime_type`` / ``size_bytes`` takes its twin, a set
one is kept, the twins are gone, a rerun is a no-op, and the downgrade puts
the twins back with the real values. The guard refuses, and drops nothing,
when a row is still stranded after the backfill. A missing table or column is
skipped. Then the model: the ORM no longer maps the twins.
"""
from __future__ import annotations

import importlib.util
import pathlib
import re

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect

MIGRATION = pathlib.Path(__file__).resolve().parents[1] / "migrations/versions/130_job_photo_twins_drop.py"


def _load(conn):
    spec = importlib.util.spec_from_file_location("m130", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.op = Operations(MigrationContext.configure(conn))
    return mod


def _legacy_schema(conn) -> None:
    """The Flask-era table, both pairs present.

    fill: real columns NULL, twins set -> real columns take the twins.
    keep: real columns set, twins disagree -> real columns kept.
    none: all four NULL -> stays NULL.
    """
    conn.exec_driver_sql(
        "CREATE TABLE job_photos (id VARCHAR(32) PRIMARY KEY, job_id VARCHAR(32), "
        "mime_type VARCHAR(100), size_bytes INTEGER, content_type VARCHAR(150), file_size INTEGER)"
    )
    conn.exec_driver_sql("CREATE INDEX ix_job_photos_job_id ON job_photos (job_id)")
    conn.exec_driver_sql(
        "INSERT INTO job_photos (id, job_id, mime_type, size_bytes, content_type, file_size) VALUES "
        "('fill', 'j', NULL, NULL, 'image/png', 11), "
        "('keep', 'j', 'image/jpeg', 22, 'image/gif', 99), "
        "('none', 'j', NULL, NULL, NULL, NULL)"
    )


_AFTER_UPGRADE = {
    "fill": ("image/png", 11),
    "keep": ("image/jpeg", 22),
    "none": (None, None),
}


def _real(conn) -> dict[str, tuple]:
    rows = conn.exec_driver_sql("SELECT id, mime_type, size_bytes FROM job_photos").all()
    return {r[0]: (r[1], r[2]) for r in rows}


def _twins(conn) -> dict[str, tuple]:
    rows = conn.exec_driver_sql("SELECT id, content_type, file_size FROM job_photos").all()
    return {r[0]: (r[1], r[2]) for r in rows}


def _cols(conn) -> set[str]:
    return {c["name"] for c in inspect(conn).get_columns("job_photos")}


def test_it_chains_onto_129_and_is_the_only_reviser() -> None:
    source = MIGRATION.read_text()
    assert 'revision = "130_job_photo_twins_drop"' in source
    assert 'down_revision = "129_job_photo_created_at_fill"' in source
    assert len("130_job_photo_twins_drop") <= 32  # alembic_version.version_num is VARCHAR(32)
    revising = [
        p.name for p in MIGRATION.parent.glob("*.py")
        if re.search(r'^down_revision = "129_job_photo_created_at_fill"', p.read_text(), re.M)
    ]
    assert revising == ["130_job_photo_twins_drop.py"], revising


def test_no_unescaped_percent_signs() -> None:
    assert "%" not in MIGRATION.read_text().replace("%%", "")


def test_sqlite_backfill_drop_rerun_and_downgrade(tmp_path) -> None:
    eng = create_engine(f"sqlite:///{tmp_path / 'm130.db'}", future=True)
    with eng.begin() as c:
        _legacy_schema(c)
        m = _load(c)
        m.upgrade()
        assert _real(c) == _AFTER_UPGRADE
        assert {"content_type", "file_size"}.isdisjoint(_cols(c))
        # The SQLite table rebuild keeps the table's index.
        assert "ix_job_photos_job_id" in {i["name"] for i in inspect(c).get_indexes("job_photos")}
        m.upgrade()  # rerun is a no-op
        assert _real(c) == _AFTER_UPGRADE

        m.downgrade()
        assert {"content_type", "file_size"} <= _cols(c)
        assert _twins(c) == _AFTER_UPGRADE
        assert _real(c) == _AFTER_UPGRADE
        m.upgrade()  # and back up again
        assert {"content_type", "file_size"}.isdisjoint(_cols(c))
    eng.dispose()


def test_sqlite_stranded_row_refuses_and_drops_nothing(tmp_path) -> None:
    """The guard is a post-condition on the backfill: on a correct backfill
    it cannot fire. A trigger that undoes the fill stands in for a backfill
    that went wrong, so the refusal is exercised for real."""
    eng = create_engine(f"sqlite:///{tmp_path / 'm130_refuse.db'}", future=True)
    with eng.connect() as c:
        _legacy_schema(c)
        c.exec_driver_sql(
            "CREATE TRIGGER undo_fill AFTER UPDATE OF mime_type ON job_photos "
            "BEGIN UPDATE job_photos SET mime_type = NULL WHERE id = NEW.id; END"
        )
        c.commit()
        with pytest.raises(RuntimeError, match=r"130: 1 job_photos row\(s\) still hold content_type"):
            _load(c).upgrade()
        c.rollback()
        assert {"content_type", "file_size"} <= _cols(c)
        assert _twins(c)["fill"] == ("image/png", 11)
    eng.dispose()


def test_sqlite_missing_table_or_columns_is_skipped(tmp_path) -> None:
    eng = create_engine(f"sqlite:///{tmp_path / 'm130_skip.db'}", future=True)
    with eng.begin() as c:
        _load(c).upgrade()  # no table
        c.exec_driver_sql(
            "CREATE TABLE job_photos (id VARCHAR(32) PRIMARY KEY, mime_type VARCHAR(100), size_bytes INTEGER)"
        )
        _load(c).upgrade()  # no twins: a database built by create_all from the model
        assert _cols(c) == {"id", "mime_type", "size_bytes"}
    eng.dispose()


def test_the_orm_no_longer_maps_the_twins() -> None:
    from gdx_dispatch.models.tenant_models import JobPhoto

    cols = set(JobPhoto.__table__.columns.keys())
    assert {"mime_type", "size_bytes"} <= cols
    assert {"content_type", "file_size"}.isdisjoint(cols)


def test_postgres_backfill_drop_rerun_and_downgrade(pg_test_db) -> None:
    eng = create_engine(pg_test_db, future=True)
    schema = "m130_test"
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
            assert _real(c) == _AFTER_UPGRADE
            assert {"content_type", "file_size"}.isdisjoint(_cols(c))
            m.upgrade()  # rerun is a no-op
            m.downgrade()
            c.commit()
            assert {"content_type", "file_size"} <= _cols(c)
            assert _twins(c) == _AFTER_UPGRADE
            assert _real(c) == _AFTER_UPGRADE
    finally:
        with eng.connect() as c:
            c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            c.commit()
        eng.dispose()

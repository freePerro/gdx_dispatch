"""Migration 107: the audit_logs immutability guard is installed at upgrade on
Postgres, removed on downgrade, rerunnable, a no-op on SQLite and without the
table, and a refused install (no privilege) logs instead of failing the
upgrade — a raise here crash-loops ``app`` under ``set -e``."""

from __future__ import annotations

import importlib.util
import pathlib
import re
import uuid

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from gdx_dispatch.core.audit import audit_guard_present

MIGRATION = pathlib.Path(__file__).resolve().parents[1] / "migrations/versions/107_audit_logs_guard.py"


def _load(conn):
    spec = importlib.util.spec_from_file_location("m107", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.op = Operations(MigrationContext.configure(conn))
    return mod


def test_it_chains_onto_106_and_is_the_only_reviser() -> None:
    source = MIGRATION.read_text()
    assert 'revision = "107_audit_logs_guard"' in source
    assert 'down_revision = "106_day_close_markers"' in source
    assert len("107_audit_logs_guard") <= 32  # alembic_version.version_num is VARCHAR(32)
    revising = [
        p.name for p in MIGRATION.parent.glob("*.py")
        if re.search(r'^down_revision = "106_day_close_markers"', p.read_text(), re.M)
    ]
    assert revising == ["107_audit_logs_guard.py"], revising


def test_no_unescaped_percent_signs() -> None:
    assert "%" not in MIGRATION.read_text().replace("%%", "")


def test_sqlite_is_a_no_op_both_ways(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    with eng.connect() as c:
        c.exec_driver_sql("CREATE TABLE audit_logs (id TEXT PRIMARY KEY)")
        m = _load(c)
        m.upgrade()
        m.downgrade()
        c.commit()
        assert c.exec_driver_sql("SELECT count(*) FROM sqlite_master WHERE type='trigger'").scalar() == 0
    eng.dispose()


def test_postgres_round_trip(pg_test_engine):
    with pg_test_engine.connect() as c:
        assert not audit_guard_present(c), "fixture DB must start guard-absent"
        c.execute(text(
            "INSERT INTO audit_logs (id, action, entity_type, row_hash, prev_hash, created_at) "
            "VALUES (:id, 't', 't', 'h', '', now())"
        ), {"id": uuid.uuid4()})
        c.commit()
        m = _load(c)
        m.upgrade()
        c.commit()
        assert audit_guard_present(c)
        # The existing row is untouched and now refuses UPDATE.
        assert c.execute(text("SELECT count(*) FROM audit_logs")).scalar() == 1
        with pytest.raises(Exception, match="audit_logs is immutable"):
            c.execute(text("UPDATE audit_logs SET action = 'x'"))
        c.rollback()
        m.upgrade()  # rerun: guard already present, no-op
        c.commit()
        m.downgrade()
        c.commit()
        assert not audit_guard_present(c)
        assert c.execute(text(
            "SELECT count(*) FROM pg_proc WHERE proname = 'audit_logs_immutable_guard'"
        )).scalar() == 0


def test_postgres_upgrade_resets_lock_timeout(pg_test_engine):
    with pg_test_engine.connect() as c:
        before = c.execute(text("SHOW lock_timeout")).scalar()
        m = _load(c)
        m.upgrade()
        assert c.execute(text("SHOW lock_timeout")).scalar() == before
        c.rollback()


def test_postgres_without_the_table_is_a_no_op(pg_test_engine):
    with pg_test_engine.begin() as c:
        c.execute(text("DROP TABLE audit_logs"))
    with pg_test_engine.connect() as c:
        _load(c).upgrade()
        c.commit()
        assert c.execute(text(
            "SELECT count(*) FROM pg_proc WHERE proname = 'audit_logs_immutable_guard'"
        )).scalar() == 0


def test_postgres_refused_install_logs_and_does_not_fail_the_upgrade(pg_test_engine, pg_test_db, caplog):
    role = f"m107_{uuid.uuid4().hex[:8]}"
    with pg_test_engine.begin() as c:
        c.execute(text(f"CREATE ROLE {role} LOGIN NOSUPERUSER PASSWORD 'x'"))
        c.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
        c.execute(text(f"REVOKE CREATE ON SCHEMA public FROM {role}"))
        c.execute(text(f"GRANT SELECT ON audit_logs TO {role}"))
    eng = create_engine(make_url(pg_test_db).set(username=role, password="x"))
    try:
        with eng.connect() as c:
            with caplog.at_level("ERROR", logger="alembic.runtime.migration"):
                _load(c).upgrade()  # must not raise
            # The savepoint contained it: the migration transaction is usable.
            assert c.execute(text("SELECT 1")).scalar() == 1
            c.rollback()
        assert any("could NOT install" in r.getMessage() for r in caplog.records)
        with pg_test_engine.connect() as c:
            assert not audit_guard_present(c)
    finally:
        eng.dispose()
        with pg_test_engine.begin() as c:
            c.execute(text(f"DROP OWNED BY {role}"))
            c.execute(text(f"DROP ROLE {role}"))

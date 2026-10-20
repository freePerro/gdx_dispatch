"""Migration 135 and the model: seven dead ``users`` columns dropped (GDXA-1994).

On both engines: the seven are gone and every other column and row is kept, a
rerun is a no-op, and the downgrade puts the seven back empty. The guard
refuses, and drops nothing, when any of them holds a value, a ``False``
included. A missing table or column is skipped. Then the model: the ORM no
longer maps the seven, and keeps the look-alikes that are live.
"""
from __future__ import annotations

import importlib.util
import pathlib
import re

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect

MIGRATION = pathlib.Path(__file__).resolve().parents[1] / "migrations/versions/135_drop_dead_user_columns.py"

DEAD = {
    "auth_email", "google_email", "availability_status", "mfa_enabled",
    "mfa_secret", "tc_permissions", "mcp_enabled",
}
KEPT = {
    "id", "email", "google_id", "locked_until", "department", "field_skills",
    "tc_can_approve", "tc_can_edit", "tc_can_view_others",
}


def _load(conn):
    spec = importlib.util.spec_from_file_location("m135", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.op = Operations(MigrationContext.configure(conn))
    return mod


def _legacy_schema(conn) -> None:
    """The production-shape table: the seven dead columns beside live ones,
    an index and a table that holds a foreign key to ``users``."""
    conn.exec_driver_sql(
        "CREATE TABLE users (id VARCHAR(36) PRIMARY KEY, email VARCHAR(254), "
        "google_id VARCHAR(200), locked_until TIMESTAMP, department VARCHAR(100), "
        "field_skills TEXT, tc_can_approve BOOLEAN, tc_can_edit BOOLEAN, "
        "tc_can_view_others BOOLEAN, auth_email VARCHAR(254), google_email VARCHAR(254), "
        "availability_status VARCHAR(30), mfa_enabled BOOLEAN, mfa_secret VARCHAR(200), "
        "tc_permissions TEXT, mcp_enabled BOOLEAN)"
    )
    conn.exec_driver_sql("CREATE INDEX ix_users_email ON users (email)")
    conn.exec_driver_sql(
        "CREATE TABLE sessions_ref (id VARCHAR(36) PRIMARY KEY, "
        "user_id VARCHAR(36) REFERENCES users (id))"
    )
    conn.exec_driver_sql(
        "INSERT INTO users (id, email, tc_can_edit) VALUES "
        "('u1', 'a@example.test', TRUE), ('u2', 'b@example.test', NULL)"
    )
    conn.exec_driver_sql("INSERT INTO sessions_ref (id, user_id) VALUES ('s1', 'u1')")


def _cols(conn) -> set[str]:
    return {c["name"] for c in inspect(conn).get_columns("users")}


def _rows(conn) -> list[tuple]:
    return conn.exec_driver_sql("SELECT id, email, tc_can_edit FROM users ORDER BY id").all()


_ROWS = [("u1", "a@example.test", True), ("u2", "b@example.test", None)]


def test_it_chains_onto_134_and_is_the_only_reviser() -> None:
    source = MIGRATION.read_text()
    assert 'revision = "135_drop_dead_user_columns"' in source
    assert 'down_revision = "134_drop_tech_unavailability"' in source
    assert len("135_drop_dead_user_columns") <= 32  # alembic_version.version_num is VARCHAR(32)
    revising = [
        p.name for p in MIGRATION.parent.glob("*.py")
        if re.search(r'^down_revision = "134_drop_tech_unavailability"', p.read_text(), re.M)
    ]
    assert revising == ["135_drop_dead_user_columns.py"], revising


def test_no_unescaped_percent_signs() -> None:
    assert "%" not in MIGRATION.read_text().replace("%%", "")


def test_sqlite_drop_rerun_and_downgrade(tmp_path) -> None:
    eng = create_engine(f"sqlite:///{tmp_path / 'm135.db'}", future=True)
    with eng.begin() as c:
        c.exec_driver_sql("PRAGMA foreign_keys = ON")
        _legacy_schema(c)
        m = _load(c)
        m.upgrade()
        assert _cols(c) == KEPT
        assert [(r[0], r[1], bool(r[2]) if r[2] is not None else None) for r in _rows(c)] == _ROWS
        # Native DROP COLUMN, not a table rebuild: the index and the
        # referencing row survive.
        assert "ix_users_email" in {i["name"] for i in inspect(c).get_indexes("users")}
        assert c.exec_driver_sql("SELECT user_id FROM sessions_ref").scalar() == "u1"
        m.upgrade()  # rerun is a no-op
        assert _cols(c) == KEPT

        m.downgrade()
        assert _cols(c) == KEPT | DEAD
        assert c.exec_driver_sql(
            "SELECT COUNT(*) FROM users WHERE mfa_secret IS NOT NULL OR mcp_enabled IS NOT NULL"
        ).scalar() == 0
        m.upgrade()  # and back up again
        assert _cols(c) == KEPT
    eng.dispose()


@pytest.mark.parametrize(
    ("column", "value"),
    [("mfa_secret", "'JBSWY3DPEHPK3PXP'"), ("mcp_enabled", "FALSE"), ("availability_status", "'busy'")],
)
def test_sqlite_a_held_value_refuses_and_drops_nothing(tmp_path, column, value) -> None:
    eng = create_engine(f"sqlite:///{tmp_path / 'm135_refuse.db'}", future=True)
    with eng.connect() as c:
        _legacy_schema(c)
        c.exec_driver_sql(f"UPDATE users SET {column} = {value} WHERE id = 'u2'")  # noqa: S608 — test constants
        c.commit()
        with pytest.raises(RuntimeError, match=rf"135: users\.{column} is set on 1 row\(s\)"):
            _load(c).upgrade()
        c.rollback()
        assert _cols(c) == KEPT | DEAD
    eng.dispose()


def test_sqlite_missing_table_or_columns_is_skipped(tmp_path) -> None:
    eng = create_engine(f"sqlite:///{tmp_path / 'm135_skip.db'}", future=True)
    with eng.begin() as c:
        _load(c).upgrade()  # no table
        _load(c).downgrade()  # no table either way
        c.exec_driver_sql("CREATE TABLE users (id VARCHAR(36) PRIMARY KEY, mfa_secret VARCHAR(200))")
        _load(c).upgrade()  # only one of the seven: a partial legacy schema
        assert _cols(c) == {"id"}
        _load(c).upgrade()  # none of them: a database built by create_all from the model
        assert _cols(c) == {"id"}
    eng.dispose()


def test_the_orm_no_longer_maps_the_dead_columns() -> None:
    from gdx_dispatch.models.tenant_models import Technician, User

    cols = set(User.__table__.columns.keys())
    assert DEAD.isdisjoint(cols), DEAD & cols
    assert cols >= KEPT, KEPT - cols
    # Same name, different table, live reader in routers/technicians.py.
    assert "availability_status" in Technician.__table__.columns


def test_postgres_drop_refuse_rerun_and_downgrade(pg_test_db) -> None:
    eng = create_engine(pg_test_db, future=True)
    schema = "m135_test"
    try:
        with eng.connect() as c:
            c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            c.exec_driver_sql(f"CREATE SCHEMA {schema}")
            c.exec_driver_sql(f"SET search_path TO {schema}")
            m = _load(c)
            m.upgrade()  # a missing table is skipped
            _legacy_schema(c)
            c.exec_driver_sql("UPDATE users SET mfa_enabled = FALSE WHERE id = 'u1'")
            c.commit()
            with pytest.raises(RuntimeError, match=r"135: users\.mfa_enabled is set on 1 row\(s\)"):
                m.upgrade()
            c.rollback()
            c.exec_driver_sql(f"SET search_path TO {schema}")
            assert _cols(c) == KEPT | DEAD
            c.exec_driver_sql("UPDATE users SET mfa_enabled = NULL")
            c.commit()

            m.upgrade()
            c.commit()
            assert _cols(c) == KEPT
            assert _rows(c) == _ROWS
            assert c.exec_driver_sql("SELECT user_id FROM sessions_ref").scalar() == "u1"
            m.upgrade()  # rerun is a no-op
            m.downgrade()
            c.commit()
            assert _cols(c) == KEPT | DEAD
            m.upgrade()
            c.commit()
            assert _cols(c) == KEPT
    finally:
        with eng.connect() as c:
            c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            c.commit()
        eng.dispose()

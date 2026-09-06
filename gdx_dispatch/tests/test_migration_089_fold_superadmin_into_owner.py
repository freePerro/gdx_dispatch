"""Migration 089: any user row still spelled as the retired platform
superadmin becomes an owner, on both engines, rerunnably."""
from __future__ import annotations

import importlib.util
import os
import pathlib
import sys
import types
from collections.abc import Generator

import pytest
from sqlalchemy import Engine, create_engine

MIGRATION = (
    pathlib.Path(__file__).resolve().parents[1]
    / "migrations/versions/089_fold_superadmin_into_owner.py"
)
SEED = [
    "CREATE TABLE users (id VARCHAR(36) PRIMARY KEY, email VARCHAR(255), role VARCHAR(50))",
    "INSERT INTO users VALUES ('u1', 'a@example.com', 'super_admin')",
    "INSERT INTO users VALUES ('u2', 'b@example.com', 'SuperAdmin')",
    "INSERT INTO users VALUES ('u3', 'c@example.com', ' super-admin ')",
    "INSERT INTO users VALUES ('u4', 'd@example.com', 'admin')",
    "INSERT INTO users VALUES ('u5', 'e@example.com', 'technician')",
    "INSERT INTO users VALUES ('u6', 'f@example.com', 'owner')",
]
EXPECTED = {"u1": "owner", "u2": "owner", "u3": "owner", "u4": "admin", "u5": "technician", "u6": "owner"}


def _load(conn):
    spec = importlib.util.spec_from_file_location("m089", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    fake = types.ModuleType("alembic")

    class _Op:
        def get_bind(self):
            return conn

    fake.op = _Op()
    saved = sys.modules.get("alembic")
    sys.modules["alembic"] = fake
    try:
        spec.loader.exec_module(mod)
    finally:
        if saved is not None:
            sys.modules["alembic"] = saved
        else:
            sys.modules.pop("alembic", None)
    return mod


def _roles(conn) -> dict[str, str]:
    return dict(conn.exec_driver_sql("SELECT id, role FROM users").fetchall())


def test_it_chains_onto_088() -> None:
    source = MIGRATION.read_text()
    assert 'revision = "089_fold_superadmin_into_owner"' in source
    assert 'down_revision = "088_drop_supplier_portal_tables"' in source
    assert len("089_fold_superadmin_into_owner") <= 32


def test_no_unescaped_percent_signs() -> None:
    assert "%" not in MIGRATION.read_text().replace("%%", "")


def test_the_role_registry_no_longer_knows_the_spelling() -> None:
    """The migration exists because normalize_role stopped mapping these."""
    from gdx_dispatch.core import roles

    for spelling in ("super_admin", "superadmin", "super-admin"):
        assert not roles.is_admin_tier(spelling)
        assert not roles.is_role_admin_actor(spelling)
        assert not roles.is_dispatch_manager(spelling)
    assert not hasattr(roles, "SUPER_ADMIN")


@pytest.fixture()
def sqlite_conn(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'm089.db'}", future=True)
    with eng.begin() as c:
        for stmt in SEED:
            c.exec_driver_sql(stmt)
    with eng.begin() as c:
        yield c
    eng.dispose()


def test_sqlite_folds_every_spelling_and_nothing_else(sqlite_conn):
    m = _load(sqlite_conn)
    m.upgrade()
    assert _roles(sqlite_conn) == EXPECTED
    m.upgrade()  # rerun: no-op
    assert _roles(sqlite_conn) == EXPECTED
    m.downgrade()  # declared no-op
    assert _roles(sqlite_conn) == EXPECTED


def test_sqlite_fresh_install_without_users_table(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}", future=True)
    with eng.begin() as c:
        _load(c).upgrade()  # must not crash
    eng.dispose()


_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
_requires_pg = pytest.mark.skipif(
    "postgresql" not in _URL,
    reason="the Postgres arm of 089 needs a real Postgres; set DATABASE_URL "
    "or TEST_DATABASE_URL to a postgres url",
)


def test_these_tests_actually_run_in_ci() -> None:
    if os.environ.get("CI"):
        assert "postgresql" in _URL, "CI is set but no postgres URL — the Postgres arm would skip."


@pytest.fixture()
def pg_conn() -> Generator[Engine, None, None]:
    """The scratch `users` lives in its own schema — the shared CI database
    keeps its real one."""
    schema = "m089_scratch"
    admin = create_engine(_URL, future=True)
    with admin.begin() as c:
        c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        c.exec_driver_sql(f"CREATE SCHEMA {schema}")
    admin.dispose()
    eng = create_engine(_URL, future=True, connect_args={"options": f"-c search_path={schema}"})
    with eng.begin() as c:
        for stmt in SEED:
            c.exec_driver_sql(stmt)
    with eng.begin() as c:
        yield c
    eng.dispose()
    admin = create_engine(_URL, future=True)
    with admin.begin() as c:
        c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    admin.dispose()


@_requires_pg
def test_pg_folds_every_spelling(pg_conn):
    m = _load(pg_conn)
    m.upgrade()
    assert _roles(pg_conn) == EXPECTED
    m.upgrade()
    assert _roles(pg_conn) == EXPECTED

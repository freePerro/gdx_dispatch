"""Migration 134: drop ``tech_unavailability`` — on both engines, reversibly,
rerunnably, and only when it is empty. Time off is one record,
``time_off_requests``; the table's writers and its one reader left in
GDXA-1828 and GDXA-1829, and the model leaves with this change (GDXA-1830).
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import re
import sys
import types
from collections.abc import Generator

import pytest
from sqlalchemy import Engine, create_engine, inspect

MIGRATION = pathlib.Path(__file__).resolve().parents[1] / "migrations/versions/134_drop_tech_unavailability.py"
TABLES = ("tech_unavailability",)

SEED = [
    "CREATE TABLE tenants (id VARCHAR(36) PRIMARY KEY, slug VARCHAR(100))",
    "INSERT INTO tenants (id, slug) VALUES ('t1', 'gdx')",
    # The shape create_all made (tests/fixtures/structure.sql before this
    # change), minus Postgres-only types, with both of its indexes.
    "CREATE TABLE tech_unavailability (id VARCHAR(36) PRIMARY KEY, company_id VARCHAR(64) NOT NULL, "
    "tech_id VARCHAR(64) NOT NULL, start_at TIMESTAMP NOT NULL, end_at TIMESTAMP NOT NULL, "
    "reason VARCHAR(200), created_by VARCHAR(200), created_at TIMESTAMP NOT NULL, deleted_at TIMESTAMP)",
    "CREATE INDEX ix_tech_unavailability_company_id ON tech_unavailability (company_id)",
    "CREATE INDEX ix_tech_unavailability_tech_id ON tech_unavailability (tech_id)",
]

_OCCUPY = (
    "INSERT INTO tech_unavailability (id, company_id, tech_id, start_at, end_at, reason, created_at) "
    "VALUES ('u1', 't1', 'tech-1', '2026-10-20 08:00:00', '2026-10-20 17:00:00', 'vacation', '2026-10-19 12:00:00')"
)

EXPECTED_COLUMNS = {
    "tech_unavailability": {"id", "company_id", "tech_id", "start_at", "end_at", "reason", "created_by",
                            "created_at", "deleted_at"},
}


def _load(conn):
    """Import the migration with `op.get_bind()` pointed at our connection."""
    spec = importlib.util.spec_from_file_location("m134", MIGRATION)
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


def _tables(conn) -> set[str]:
    return set(inspect(conn).get_table_names())


def _cols(conn, table: str) -> set[str]:
    return {c["name"] for c in inspect(conn).get_columns(table)}


# ── Static ───────────────────────────────────────────────────────────────────

def test_it_chains_onto_133_and_133_exists() -> None:
    source = MIGRATION.read_text()
    assert 'revision = "134_drop_tech_unavailability"' in source
    assert 'down_revision = "133_drop_van_inventory"' in source
    assert len("134_drop_tech_unavailability") <= 32, "alembic_version.version_num is varchar(32)"
    declaring = [
        p.name for p in MIGRATION.parent.glob("*.py")
        if re.search(r'^revision = "133_drop_van_inventory"', p.read_text(), re.M)
    ]
    assert declaring == ["133_drop_van_inventory.py"], f"exactly one migration must declare 133, found {declaring}"


def test_nothing_else_revises_133() -> None:
    """Two heads would stop `alembic upgrade head` on the next deploy."""
    revising = [
        p.name for p in MIGRATION.parent.glob("*.py")
        if re.search(r'^down_revision = "133_drop_van_inventory"', p.read_text(), re.M)
    ]
    assert revising == ["134_drop_tech_unavailability.py"], revising


def test_no_unescaped_percent_signs() -> None:
    body = MIGRATION.read_text()
    assert "%" not in body.replace("%%", "")


def test_no_model_declares_the_dropped_table() -> None:
    """create_all runs at every boot: the ORM must not re-create what this drops."""
    import gdx_dispatch.models as models
    from gdx_dispatch.models.tenant_models import Base

    still_declared = [t for t in TABLES if t in Base.metadata.tables]
    assert not still_declared, still_declared
    assert not hasattr(models, "TechUnavailability")


def test_recreate_covers_every_table_it_drops() -> None:
    m = _load(None)
    assert set(m.TABLES) == set(TABLES)
    assert set(m._RECREATE) == set(TABLES)


def test_no_route_serves_tech_unavailability() -> None:
    routes = (pathlib.Path(__file__).resolve().parents[1] / "openapi_routes.txt").read_text()
    assert "/api/tech-unavailability" not in routes
    assert "/unavailability" not in routes


# ── SQLite ───────────────────────────────────────────────────────────────────

@pytest.fixture()
def sqlite_conn(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'm134.db'}", future=True)
    with eng.begin() as c:
        for stmt in SEED:
            c.exec_driver_sql(stmt)
    with eng.begin() as c:
        yield c
    eng.dispose()


def test_sqlite_upgrade_drops_the_table_and_nothing_else(sqlite_conn):
    m = _load(sqlite_conn)
    assert set(TABLES) <= _tables(sqlite_conn)
    m.upgrade()
    assert _tables(sqlite_conn) == {"tenants"}
    assert sqlite_conn.exec_driver_sql("SELECT COUNT(*) FROM tenants").scalar() == 1
    indexes = sqlite_conn.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND name LIKE 'ix_tech_unavailability_%%'"
    ).fetchall()
    assert indexes == []


def test_sqlite_an_occupied_table_refuses_the_drop(sqlite_conn):
    sqlite_conn.exec_driver_sql(_OCCUPY)
    m = _load(sqlite_conn)
    with pytest.raises(RuntimeError, match=r"tech_unavailability holds 1 row"):
        m.upgrade()
    assert set(TABLES) <= _tables(sqlite_conn)
    assert sqlite_conn.exec_driver_sql("SELECT COUNT(*) FROM tech_unavailability").scalar() == 1


def test_sqlite_a_soft_deleted_row_still_refuses(sqlite_conn):
    """The guard counts every row; a soft-deleted one is still data a person
    has to rule on."""
    sqlite_conn.exec_driver_sql(_OCCUPY)
    sqlite_conn.exec_driver_sql("UPDATE tech_unavailability SET deleted_at = '2026-10-19 13:00:00'")
    m = _load(sqlite_conn)
    with pytest.raises(RuntimeError, match=r"tech_unavailability holds 1 row"):
        m.upgrade()


def test_sqlite_upgrade_is_rerunnable_and_tolerates_absence(sqlite_conn):
    m = _load(sqlite_conn)
    m.upgrade()
    m.upgrade()
    assert _tables(sqlite_conn) == {"tenants"}


def test_sqlite_downgrade_recreates_the_model_shape(sqlite_conn):
    m = _load(sqlite_conn)
    m.upgrade()
    m.downgrade()
    for t in TABLES:
        assert _cols(sqlite_conn, t) == EXPECTED_COLUMNS[t], t
        assert sqlite_conn.exec_driver_sql(f"SELECT COUNT(*) FROM {t}").scalar() == 0  # noqa: S608 — test constant
    m.downgrade()  # rerun must not abort either
    m.upgrade()    # and the round trip closes
    assert _tables(sqlite_conn) == {"tenants"}


def test_sqlite_fresh_install_without_the_table(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}", future=True)
    with eng.begin() as c:
        c.exec_driver_sql(SEED[0])
    with eng.begin() as c:
        m = _load(c)
        m.upgrade()
        assert _tables(c) == {"tenants"}
    eng.dispose()


# ── Postgres ─────────────────────────────────────────────────────────────────

_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
_requires_pg = pytest.mark.skipif(
    "postgresql" not in _URL,
    reason="the Postgres arm of 134 needs a real Postgres; set DATABASE_URL "
    "or TEST_DATABASE_URL to a postgres url",
)


def test_these_tests_actually_run_in_ci() -> None:
    """A skipif that fails open is how "tested on both engines" quietly becomes
    false — the Postgres arm skips and the build stays green (#440)."""
    if os.environ.get("CI"):
        assert "postgresql" in _URL, "CI is set but no postgres URL — the Postgres arm would skip."


@pytest.fixture()
def pg_conn() -> Generator[Engine, None, None]:
    """Every table lives in a scratch schema — the shared CI database keeps its
    real tables."""
    schema = "m134_scratch"
    eng = create_engine(_URL, future=True)
    with eng.begin() as c:
        c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        c.exec_driver_sql(f"CREATE SCHEMA {schema}")
        c.exec_driver_sql(f"SET search_path TO {schema}")
        for stmt in SEED:
            c.exec_driver_sql(stmt)
    with eng.begin() as c:
        c.exec_driver_sql(f"SET search_path TO {schema}")
        yield c
    with eng.begin() as c:
        c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    eng.dispose()


@_requires_pg
def test_pg_upgrade_then_downgrade_round_trip(pg_conn):
    m = _load(pg_conn)
    assert set(TABLES) <= _tables(pg_conn)
    m.upgrade()
    assert _tables(pg_conn) == {"tenants"}
    m.upgrade()  # rerunnable
    m.downgrade()
    for t in TABLES:
        assert _cols(pg_conn, t) == EXPECTED_COLUMNS[t], t
        assert pg_conn.exec_driver_sql(f"SELECT COUNT(*) FROM {t}").scalar() == 0  # noqa: S608 — test constant
    m.upgrade()
    assert _tables(pg_conn) == {"tenants"}


@_requires_pg
def test_pg_an_occupied_table_refuses_the_drop(pg_conn):
    pg_conn.exec_driver_sql(_OCCUPY)
    m = _load(pg_conn)
    with pytest.raises(RuntimeError, match="tech_unavailability holds 1 row"):
        m.upgrade()
    assert set(TABLES) <= _tables(pg_conn)

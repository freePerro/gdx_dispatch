"""Migration 099: leads.follow_up_date, leads.estimate_id, leads.origin_ref,
leads.intake permission grant, and default lead custom fields.
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import re
from typing import Generator
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

from gdx_dispatch.core.permissions import BUILTIN_ROLES
from gdx_dispatch.migrations.grant_helpers import grant_permission_to_seeded_roles
from gdx_dispatch.tests.fixtures.pg import _skip_unless_ci

MIGRATION = (
    pathlib.Path(__file__).resolve().parents[1]
    / "migrations/versions/099_lead_intake.py"
)


def _load(conn):
    spec = importlib.util.spec_from_file_location("m099", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.op = Operations(MigrationContext.configure(conn))
    return mod


def _load_raw():
    spec = importlib.util.spec_from_file_location("_mig_099", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_MIG = _load_raw()
_KEY = _MIG._KEY
_ROLES = _MIG._ROLES


def _cols(conn, table: str) -> set[str]:
    return {c["name"] for c in inspect(conn).get_columns(table)}


def _indexes(conn, table: str) -> set[str]:
    return {ix["name"] for ix in inspect(conn).get_indexes(table)}


# ── Static tests ─────────────────────────────────────────────────────────────


def test_it_chains_onto_098_and_is_the_only_reviser() -> None:
    source = MIGRATION.read_text()
    assert 'revision = "099_lead_intake"' in source
    assert 'down_revision = "098_planner_today"' in source
    assert len("099_lead_intake") <= 32
    revising = [
        p.name
        for p in MIGRATION.parent.glob("*.py")
        if re.search(r'^down_revision = "098_planner_today"', p.read_text(), re.M)
    ]
    assert revising == [
        "099_lead_intake.py"
    ], f"two heads would stop the next deploy: {revising}"


def test_no_unescaped_percent_signs() -> None:
    assert "%" not in MIGRATION.read_text().replace("%%", "")


def test_migration_roles_match_builtin_roles_exactly() -> None:
    expected = {
        role
        for role, perms in BUILTIN_ROLES.items()
        if _KEY in perms and role not in ("admin", "owner")
    }
    assert set(_ROLES) == expected, (
        f"migration grants {sorted(_ROLES)} but BUILTIN_ROLES (minus admin/owner) "
        f"has {sorted(expected)} — update _ROLES or permissions.py so they agree"
    )


def test_admin_and_owner_are_not_in_the_migration() -> None:
    assert "admin" not in _ROLES
    assert "owner" not in _ROLES


# ── SQLite tests ─────────────────────────────────────────────────────────────

SEED_SQLITE = [
    """CREATE TABLE leads (
        id VARCHAR(36) PRIMARY KEY,
        company_id VARCHAR(64) NOT NULL,
        name VARCHAR(200) NOT NULL,
        stage VARCHAR(30) NOT NULL
    )""",
    """INSERT INTO leads (id, company_id, name, stage)
       VALUES ('l1', 't1', 'Test Lead', 'new')""",
    """CREATE TABLE custom_field_definitions (
        id VARCHAR(36) PRIMARY KEY,
        company_id VARCHAR(64) NOT NULL,
        entity_type VARCHAR(30) NOT NULL,
        field_key VARCHAR(80) NOT NULL,
        label VARCHAR(200) NOT NULL,
        field_type VARCHAR(30) NOT NULL,
        options TEXT,
        required INTEGER NOT NULL DEFAULT 0,
        sort_order INTEGER NOT NULL DEFAULT 0,
        deleted_at DATETIME,
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL
    )""",
]


@pytest.fixture()
def sqlite_conn(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'm099.db'}", future=True)
    with eng.begin() as c:
        for stmt in SEED_SQLITE:
            c.exec_driver_sql(stmt)
    with eng.connect() as c:
        yield c
    eng.dispose()


def test_sqlite_upgrade_and_downgrade(sqlite_conn) -> None:
    mod = _load(sqlite_conn)

    # 1. Upgrade
    mod.upgrade()

    cols = _cols(sqlite_conn, "leads")
    assert "follow_up_date" in cols
    assert "estimate_id" in cols
    assert "origin_ref" in cols

    indexes = _indexes(sqlite_conn, "leads")
    assert "ix_leads_follow_up_date" in indexes
    assert "ix_leads_estimate_id" in indexes

    # Verify default custom fields seeded
    field_keys = {
        row[0]
        for row in sqlite_conn.exec_driver_sql(
            "SELECT field_key FROM custom_field_definitions WHERE entity_type = 'lead'"
        ).fetchall()
    }
    assert field_keys == {"job_kind", "door_count", "door_size", "door_options", "opener"}

    # 2. Downgrade
    mod.downgrade()

    cols_after = _cols(sqlite_conn, "leads")
    assert "follow_up_date" not in cols_after
    assert "estimate_id" not in cols_after
    assert "origin_ref" not in cols_after


# ── Behavior against a real Postgres ─────────────────────────────────────────

_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
_requires_pg = pytest.mark.skipif(
    "postgresql" not in _URL,
    reason="grant helper is Postgres-only (jsonb / pg_input_is_valid); set DATABASE_URL",
)


@pytest.fixture()
def pg() -> Generator[Engine, None, None]:
    try:
        eng = create_engine(_URL)
        with eng.connect() as c:
            c.execute(text("SELECT 1"))
    except Exception as exc:
        _skip_unless_ci(f"postgres not reachable: {exc}")
    yield eng
    eng.dispose()


def _seed_pg(conn) -> None:
    conn.execute(
        text(
            """
            CREATE TEMP TABLE tenant_roles (
                id uuid PRIMARY KEY,
                company_id varchar,
                name varchar,
                permissions text,
                is_system boolean,
                created_at timestamptz,
                updated_at timestamptz
            ) ON COMMIT DROP
            """
        )
    )
    conn.execute(
        text(
            """
            INSERT INTO tenant_roles (id, company_id, name, permissions, is_system)
            VALUES
                (:tech_seeded, 't1', 'technician', '["jobs.read_own"]', true),
                (:tech_custom, 't1', 'technician', '["jobs.read_own"]', false)
            """
        ),
        {"tech_seeded": uuid4(), "tech_custom": uuid4()},
    )


@_requires_pg
def test_postgres_grant_adds_key_to_seeded_technician(pg) -> None:
    with pg.begin() as conn:
        _seed_pg(conn)
        mod = _load(conn)
        mod.upgrade()

        # Seeded technician gets leads.intake
        row = conn.execute(
            text("SELECT permissions FROM tenant_roles WHERE is_system = true AND name = 'technician'")
        ).scalar()
        assert "leads.intake" in row

        # Custom technician left untouched
        row_custom = conn.execute(
            text("SELECT permissions FROM tenant_roles WHERE is_system = false AND name = 'technician'")
        ).scalar()
        assert "leads.intake" not in row_custom

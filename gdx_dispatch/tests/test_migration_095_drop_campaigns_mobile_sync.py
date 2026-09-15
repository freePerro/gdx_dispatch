"""Migration 095: drop campaigns, campaign_sends and mobile_sync_actions on both
engines, reversibly, rerunnably, all-or-nothing, and only when every one of
them is empty. All three held 0 rows on production and demo (2026-09-13 and
2026-09-14); the models leave in the same change (see
test_campaigns_retired.py). On Postgres the three enum types those tables used
go too, unless something else (a column, array, domain or function) depends on one.
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

MIGRATION = pathlib.Path(__file__).resolve().parents[1] / "migrations/versions/095_drop_campaigns_mobile_sync.py"
TABLES = ("campaign_sends", "campaigns", "mobile_sync_actions")
ENUM_TYPES = ("campaign_send_status", "campaign_trigger", "campaign_channel")

# The shapes create_all made on prod (information_schema, 2026-09-14), minus
# indexes and the Postgres-only types: enough to prove the drop and the refusal.
SEED_SQLITE = [
    "CREATE TABLE customers (id VARCHAR(36) PRIMARY KEY, name VARCHAR(200))",
    "INSERT INTO customers (id, name) VALUES ('c1', 'Pat')",
    "CREATE TABLE campaigns (id VARCHAR(36) PRIMARY KEY, name VARCHAR(200) NOT NULL, \"trigger\" VARCHAR(30) NOT NULL, "
    "delay_days INTEGER NOT NULL, message_template TEXT NOT NULL, channel VARCHAR(10) NOT NULL, is_active BOOLEAN NOT NULL, "
    "send_count INTEGER NOT NULL, created_at TIMESTAMP NOT NULL)",
    "CREATE TABLE campaign_sends (id VARCHAR(36) PRIMARY KEY, campaign_id VARCHAR(36) NOT NULL REFERENCES campaigns(id), "
    "customer_id VARCHAR(36) NOT NULL REFERENCES customers(id), entity_type VARCHAR(50) NOT NULL, entity_id VARCHAR(50) NOT NULL, "
    "scheduled_at TIMESTAMP NOT NULL, sent_at TIMESTAMP, status VARCHAR(20) NOT NULL, idempotency_key VARCHAR(100) NOT NULL UNIQUE)",
    "CREATE TABLE mobile_sync_actions (id TEXT PRIMARY KEY, company_id TEXT NOT NULL, fingerprint TEXT, action_type TEXT, "
    "entity_id TEXT, queued_at TEXT, created_at TEXT)",
]

# Postgres: the real column types, enum types included, as prod has them.
SEED_PG = [
    "CREATE TABLE customers (id UUID PRIMARY KEY, name VARCHAR(200))",
    "INSERT INTO customers (id, name) VALUES ('11111111-1111-1111-1111-111111111111', 'Pat')",
    "CREATE TYPE campaign_trigger AS ENUM ('estimate_not_accepted', 'job_completed', 'manual')",
    "CREATE TYPE campaign_channel AS ENUM ('sms', 'email', 'both')",
    "CREATE TYPE campaign_send_status AS ENUM ('pending', 'sent', 'failed', 'cancelled')",
    "CREATE TABLE campaigns (id UUID PRIMARY KEY, name VARCHAR(200) NOT NULL, trigger campaign_trigger NOT NULL, "
    "delay_days INTEGER NOT NULL, message_template TEXT NOT NULL, channel campaign_channel NOT NULL, is_active BOOLEAN NOT NULL, "
    "send_count INTEGER NOT NULL, created_at TIMESTAMPTZ NOT NULL)",
    "CREATE TABLE campaign_sends (id UUID PRIMARY KEY, campaign_id UUID NOT NULL REFERENCES campaigns(id), "
    "customer_id UUID NOT NULL REFERENCES customers(id), entity_type VARCHAR(50) NOT NULL, entity_id VARCHAR(50) NOT NULL, "
    "scheduled_at TIMESTAMPTZ NOT NULL, sent_at TIMESTAMPTZ, status campaign_send_status NOT NULL, "
    "idempotency_key VARCHAR(100) NOT NULL UNIQUE)",
    "CREATE TABLE mobile_sync_actions (id TEXT PRIMARY KEY, company_id TEXT NOT NULL, fingerprint TEXT, action_type TEXT, "
    "entity_id TEXT, queued_at TEXT, created_at TEXT)",
]

_OCCUPY = {
    "campaigns": "INSERT INTO campaigns (id, name, \"trigger\", delay_days, message_template, channel, is_active, send_count, created_at) "
    "VALUES ('{cid}', 'Spring', 'manual', 3, 'Hi', 'sms', true, 0, '2026-09-14')",
    "campaign_sends": "INSERT INTO campaign_sends (id, campaign_id, customer_id, entity_type, entity_id, scheduled_at, status, idempotency_key) "
    "VALUES ('{sid}', '{cid}', '{cust}', 'estimate', 'e1', '2026-09-14', 'pending', 'k1')",
    "mobile_sync_actions": "INSERT INTO mobile_sync_actions (id, company_id) VALUES ('m1', 't1')",
}
_IDS_SQLITE = {"cid": "camp1", "sid": "send1", "cust": "c1"}
_IDS_PG = {
    "cid": "22222222-2222-2222-2222-222222222222",
    "sid": "33333333-3333-3333-3333-333333333333",
    "cust": "11111111-1111-1111-1111-111111111111",
}

EXPECTED_COLUMNS = {
    "campaigns": {"id", "name", "trigger", "delay_days", "message_template", "channel", "is_active", "send_count", "created_at"},
    "campaign_sends": {"id", "campaign_id", "customer_id", "entity_type", "entity_id", "scheduled_at", "sent_at", "status",
                       "idempotency_key"},
    "mobile_sync_actions": {"id", "company_id", "fingerprint", "action_type", "entity_id", "queued_at", "created_at"},
}


def _occupy(conn, table: str, ids: dict[str, str]) -> None:
    """A send needs its campaign, so occupying campaign_sends occupies both."""
    if table == "campaign_sends":
        conn.exec_driver_sql(_OCCUPY["campaigns"].format(**ids))
    conn.exec_driver_sql(_OCCUPY[table].format(**ids))


def _load(conn):
    """Import the migration with `op.get_bind()` pointed at our connection."""
    spec = importlib.util.spec_from_file_location("m095", MIGRATION)
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

def test_it_chains_onto_094_and_094_exists() -> None:
    source = MIGRATION.read_text()
    assert 'revision = "095_drop_campaigns_mobile_sync"' in source
    assert 'down_revision = "094_not_null_to_match_orm"' in source
    assert len("095_drop_campaigns_mobile_sync") <= 32, "alembic_version.version_num is varchar(32)"
    declaring = [
        p.name for p in MIGRATION.parent.glob("*.py")
        if re.search(r'^revision = "094_not_null_to_match_orm"', p.read_text(), re.M)
    ]
    assert declaring == ["094_not_null_to_match_orm.py"], f"exactly one migration must declare 094, found {declaring}"


def test_nothing_else_revises_094() -> None:
    """Two heads would stop `alembic upgrade head` on the next deploy."""
    revising = [
        p.name for p in MIGRATION.parent.glob("*.py")
        if re.search(r'^down_revision = "094_not_null_to_match_orm"', p.read_text(), re.M)
    ]
    assert revising == ["095_drop_campaigns_mobile_sync.py"], revising


def test_no_unescaped_percent_signs() -> None:
    body = MIGRATION.read_text()
    assert "%" not in body.replace("%%", "")


def test_no_model_declares_a_dropped_table() -> None:
    """create_all runs at every boot, before alembic: the ORM must not re-create
    what this drops. Checked in a fresh interpreter through the app's own
    registration path, since conftest imports models itself."""
    import subprocess

    script = (
        "import gdx_dispatch.models\n"
        "from gdx_dispatch.core.audit import TenantBase\n"
        "from gdx_dispatch.models.tenant_models import Base\n"
        "print(' '.join(sorted(set(TenantBase.metadata.tables) | set(Base.metadata.tables))))\n"
    )
    env = {**os.environ, "JWT_SECRET": os.environ.get("JWT_SECRET") or "test-jwt-secret-at-least-32-bytes-long-for-hs256"}
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120, env=env)
    assert out.returncode == 0, out.stderr[-2000:]
    registered = set(out.stdout.split())
    assert "marketing_campaigns" in registered, "control: the registry read is empty or wrong"
    still_declared = [t for t in TABLES if t in registered]
    assert not still_declared, still_declared


def test_recreate_covers_every_table_it_drops() -> None:
    m = _load(None)
    assert set(m.TABLES) == set(TABLES)
    assert set(m._RECREATE) == set(TABLES)
    assert set(m.PG_ENUM_TYPES) == set(ENUM_TYPES)


# ── SQLite ───────────────────────────────────────────────────────────────────

@pytest.fixture()
def sqlite_conn(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'm095.db'}", future=True)
    with eng.begin() as c:
        for stmt in SEED_SQLITE:
            c.exec_driver_sql(stmt)
    with eng.begin() as c:
        yield c
    eng.dispose()


def test_sqlite_upgrade_drops_all_three_and_nothing_else(sqlite_conn):
    m = _load(sqlite_conn)
    assert set(TABLES) <= _tables(sqlite_conn)
    m.upgrade()
    assert _tables(sqlite_conn) == {"customers"}
    assert sqlite_conn.exec_driver_sql("SELECT COUNT(*) FROM customers").scalar() == 1


@pytest.mark.parametrize("occupied", TABLES)
def test_sqlite_one_occupied_table_refuses_the_whole_drop(sqlite_conn, occupied):
    _occupy(sqlite_conn, occupied, _IDS_SQLITE)
    m = _load(sqlite_conn)
    with pytest.raises(RuntimeError, match=rf"{occupied} holds 1 row"):
        m.upgrade()
    # All-or-nothing: the empty siblings survive too.
    assert set(TABLES) <= _tables(sqlite_conn)
    assert sqlite_conn.exec_driver_sql(f"SELECT COUNT(*) FROM {occupied}").scalar() == 1  # noqa: S608 — test constant


def test_sqlite_upgrade_is_rerunnable_and_tolerates_absence(sqlite_conn):
    m = _load(sqlite_conn)
    m.upgrade()
    m.upgrade()
    assert _tables(sqlite_conn) == {"customers"}


def test_sqlite_upgrade_tolerates_a_partial_set(sqlite_conn):
    """A fresh install never had these; a half-migrated one may have some."""
    sqlite_conn.exec_driver_sql("DROP TABLE mobile_sync_actions")
    m = _load(sqlite_conn)
    m.upgrade()
    assert _tables(sqlite_conn) == {"customers"}


def test_sqlite_downgrade_recreates_the_model_shapes(sqlite_conn):
    m = _load(sqlite_conn)
    m.upgrade()
    m.downgrade()
    for t in TABLES:
        assert _cols(sqlite_conn, t) == EXPECTED_COLUMNS[t], t
        assert sqlite_conn.exec_driver_sql(f"SELECT COUNT(*) FROM {t}").scalar() == 0  # noqa: S608 — test constant
    m.downgrade()  # rerun must not abort either
    m.upgrade()    # and the round trip closes
    assert _tables(sqlite_conn) == {"customers"}


def test_sqlite_fresh_install_without_the_tables(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}", future=True)
    with eng.begin() as c:
        c.exec_driver_sql(SEED_SQLITE[0])
    with eng.begin() as c:
        m = _load(c)
        m.upgrade()
        assert _tables(c) == {"customers"}
    eng.dispose()


# ── Postgres ─────────────────────────────────────────────────────────────────

_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
_requires_pg = pytest.mark.skipif(
    "postgresql" not in _URL,
    reason="the Postgres arm of 095 needs a real Postgres; set DATABASE_URL "
    "or TEST_DATABASE_URL to a postgres url",
)


def test_these_tests_actually_run_in_ci() -> None:
    """A skipif that fails open is how "tested on both engines" quietly becomes
    false — the Postgres arm skips and the build stays green (#440)."""
    if os.environ.get("CI"):
        assert "postgresql" in _URL, "CI is set but no postgres URL — the Postgres arm would skip."


@pytest.fixture()
def pg_conn() -> Generator[Engine, None, None]:
    """Every table and type lives in a scratch schema — the shared CI database
    keeps its real ones."""
    schema = "m095_scratch"
    eng = create_engine(_URL, future=True)
    with eng.begin() as c:
        c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        c.exec_driver_sql(f"CREATE SCHEMA {schema}")
        c.exec_driver_sql(f"SET search_path TO {schema}")
        for stmt in SEED_PG:
            c.exec_driver_sql(stmt)
    with eng.begin() as c:
        c.exec_driver_sql(f"SET search_path TO {schema}")
        yield c
    with eng.begin() as c:
        c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    eng.dispose()


def _pg_types(conn) -> set[str]:
    return {
        r[0] for r in conn.exec_driver_sql(
            "SELECT t.typname FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace "
            "WHERE n.nspname = current_schema() AND t.typtype = 'e'"
        )
    }


@_requires_pg
def test_pg_upgrade_drops_tables_and_their_enum_types_then_round_trips(pg_conn):
    m = _load(pg_conn)
    assert set(TABLES) <= _tables(pg_conn)
    assert set(ENUM_TYPES) <= _pg_types(pg_conn)
    m.upgrade()
    assert _tables(pg_conn) == {"customers"}
    assert not (set(ENUM_TYPES) & _pg_types(pg_conn)), "enum types left behind"
    m.upgrade()  # rerunnable
    m.downgrade()
    for t in TABLES:
        assert _cols(pg_conn, t) == EXPECTED_COLUMNS[t], t
        assert pg_conn.exec_driver_sql(f"SELECT COUNT(*) FROM {t}").scalar() == 0  # noqa: S608 — test constant
    m.upgrade()
    assert _tables(pg_conn) == {"customers"}


@_requires_pg
@pytest.mark.parametrize("occupied", TABLES)
def test_pg_one_occupied_table_refuses_the_whole_drop(pg_conn, occupied):
    _occupy(pg_conn, occupied, _IDS_PG)
    m = _load(pg_conn)
    with pytest.raises(RuntimeError, match=rf"{occupied} holds 1 row"):
        m.upgrade()
    assert set(TABLES) <= _tables(pg_conn)
    assert set(ENUM_TYPES) <= _pg_types(pg_conn), "a refused migration must not drop types either"


_ADOPTERS = {
    "column": ["CREATE TABLE adopter (id INTEGER PRIMARY KEY, ch campaign_channel)"],
    "array-column": ["CREATE TABLE adopter (id INTEGER PRIMARY KEY, chs campaign_channel[])"],
    "domain": ["CREATE DOMAIN channel_domain AS campaign_channel"],
    "function-argument": [
        "CREATE FUNCTION channel_label(c campaign_channel) RETURNS text LANGUAGE sql AS 'SELECT c::text'"
    ],
}


@_requires_pg
@pytest.mark.parametrize("kind", sorted(_ADOPTERS))
def test_pg_an_enum_type_something_else_uses_is_kept_and_does_not_fail(pg_conn, kind):
    """DROP TYPE on a type something else depends on raises, and a raise here
    crash-loops the app. Postgres decides what a dependency is; the migration
    keeps that type and drops the rest. An information_schema.columns check
    missed the array, domain and function cases."""
    for stmt in _ADOPTERS[kind]:
        pg_conn.exec_driver_sql(stmt)
    m = _load(pg_conn)
    m.upgrade()
    assert set(TABLES).isdisjoint(_tables(pg_conn)), "the tables must still be dropped"
    remaining = set(ENUM_TYPES) & _pg_types(pg_conn)
    assert remaining == {"campaign_channel"}, remaining


@_requires_pg
def test_pg_any_other_drop_type_error_still_raises(pg_conn):
    """Only "something still depends on it" (2BP01) is tolerated. A different
    failure, here a permission error (42501), must surface rather than be
    swallowed into a migration that reports success with a type left behind."""
    from sqlalchemy.exc import DBAPIError

    class _PermissionDenied(Exception):
        pgcode = "42501"

    class _Proxy:
        def __init__(self, conn):
            self._conn = conn
            self.dialect = conn.dialect

        def begin_nested(self):
            return self._conn.begin_nested()

        def exec_driver_sql(self, sql, *args, **kwargs):
            if sql.startswith("DROP TYPE IF EXISTS campaign_channel"):
                raise DBAPIError(sql, None, _PermissionDenied("must be owner of type campaign_channel"))
            return self._conn.exec_driver_sql(sql, *args, **kwargs)

    m = _load(_Proxy(pg_conn))
    m.inspect = lambda _bind: inspect(pg_conn)
    with pytest.raises(DBAPIError, match="must be owner"):
        m.upgrade()

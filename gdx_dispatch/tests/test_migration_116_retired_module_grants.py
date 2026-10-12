"""Migration 116: delete the grant rows of retired module keys, on both
engines, rerunnably. Plus the class guard: a key that leaves the module
registry must be listed as retired, and some migration must delete its grants
(GDXA-663 / GDXA-706).
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
from sqlalchemy import Engine, create_engine

from gdx_dispatch.core.modules import (
    LEGACY_MODULE_ALIASES,
    MODULE_KEYS,
    MODULES,
    RETIRED_MODULE_KEYS,
)

VERSIONS = pathlib.Path(__file__).resolve().parents[1] / "migrations/versions"
MIGRATION = VERSIONS / "116_retired_module_grants.py"

# Every key (canonical or alias) the registry has ever held, read from git
# history on 2026-10-11. Only append to this. Removing a key from MODULES or
# LEGACY_MODULE_ALIASES without moving it to RETIRED_MODULE_KEYS turns
# test_every_key_ever_registered_is_live_or_retired red.
EVER_REGISTERED = frozenset({
    # MODULES
    "jobs", "customers", "estimates", "invoices", "dispatch", "timeclock",
    "inventory", "quickbooks", "customer_portal", "gps_dispatch",
    "ai_estimates", "ai_dispatch", "ai_communication", "llm", "loyalty",
    "warranties", "automations", "documents", "communications",
    "reports_advanced", "mobile", "segments", "google_maps", "phone_com",
    "email", "bank_feeds",
    # LEGACY_MODULE_ALIASES
    "advanced_reports", "warranty", "workflows", "proposals", "change_orders",
    "maintenance_plans", "purchase_orders", "service_areas",
    "service_agreements",
    # retired
    "stripe_connect", "chrome_extension", "equipment_tracking", "fleet",
    "campaigns", "contractors",
})

CREATE = (
    "CREATE TABLE company_module_grants (id VARCHAR(36) PRIMARY KEY, "
    "company_id VARCHAR(36) NOT NULL, module_key VARCHAR(100) NOT NULL, "
    "CONSTRAINT uq_cmg UNIQUE (company_id, module_key))"
)


def _load(path: pathlib.Path, conn):
    """Import a migration with `op.get_bind()` pointed at our connection."""
    spec = importlib.util.spec_from_file_location(f"m_{path.stem}", path)
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


def _seed(conn, keys) -> None:
    conn.exec_driver_sql(CREATE)
    for i, key in enumerate(sorted(keys)):
        conn.exec_driver_sql(
            f"INSERT INTO company_module_grants VALUES ('g{i}', 't1', '{key}')"  # noqa: S608 — keys from test constants
        )


def _grant_keys(conn) -> set[str]:
    return {k for (k,) in conn.exec_driver_sql("SELECT module_key FROM company_module_grants").fetchall()}


# ── the class guard ──────────────────────────────

def test_every_key_ever_registered_is_live_or_retired() -> None:
    lost = EVER_REGISTERED - set(MODULE_KEYS) - RETIRED_MODULE_KEYS
    assert not lost, (
        f"{sorted(lost)} left the module registry without being retired. Add the key(s) "
        "to RETIRED_MODULE_KEYS in core/modules.py and ship a migration that deletes "
        "their company_module_grants rows."
    )


def test_a_retired_key_is_not_live() -> None:
    assert RETIRED_MODULE_KEYS.isdisjoint(MODULE_KEYS)


def test_registry_snapshot_is_current() -> None:
    """A key added to the registry belongs in EVER_REGISTERED, or its later
    retirement would slip past the guard above."""
    missing = (set(MODULES) | set(LEGACY_MODULE_ALIASES)) - EVER_REGISTERED
    assert not missing, f"append {sorted(missing)} to EVER_REGISTERED"


def test_the_migrations_delete_the_grants_of_every_retired_key(tmp_path) -> None:
    """Behaviour, not text: seed a grant for every live and every retired key,
    run the upgrade of every migration that touches company_module_grants, in
    revision order, and only the live grants may remain. A new retirement whose
    migration forgets the grants fails here."""
    touching = sorted(
        p for p in VERSIONS.glob("*.py")
        if "company_module_grants" in p.read_text() and re.match(r"\d{3}_", p.name)
    )
    assert MIGRATION in touching
    eng = create_engine(f"sqlite:///{tmp_path / 'all.db'}", future=True)
    with eng.begin() as c:
        _seed(c, set(MODULE_KEYS) | RETIRED_MODULE_KEYS)
    with eng.begin() as c:
        for path in touching:
            _load(path, c).upgrade()
        assert _grant_keys(c) == set(MODULE_KEYS)
    eng.dispose()


# ── migration 116 itself ─────────────────────────

def test_it_chains_onto_115_and_115_exists() -> None:
    source = MIGRATION.read_text()
    assert 'revision = "116_retired_module_grants"' in source
    assert 'down_revision = "115_vacation_allowance"' in source
    assert len("116_retired_module_grants") <= 32, "alembic_version.version_num is varchar(32)"
    declaring = [
        p.name for p in VERSIONS.glob("*.py")
        if re.search(r'^revision = "115_vacation_allowance"', p.read_text(), re.M)
    ]
    assert declaring == ["115_vacation_allowance.py"]
    revising = [
        p.name for p in VERSIONS.glob("*.py")
        if re.search(r'^down_revision = "115_vacation_allowance"', p.read_text(), re.M)
    ]
    assert revising == [MIGRATION.name], f"two heads off 115: {revising}"


def test_its_list_matches_the_registry() -> None:
    eng = create_engine("sqlite://", future=True)
    with eng.begin() as c:
        assert set(_load(MIGRATION, c)._RETIRED) == RETIRED_MODULE_KEYS
    eng.dispose()


@pytest.fixture()
def sqlite_conn(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'm116.db'}", future=True)
    with eng.begin() as c:
        _seed(c, {"jobs", "communications", "warranty", "equipment_tracking", "fleet", "campaigns", "stripe_connect"})
    with eng.begin() as c:
        yield c
    eng.dispose()


def test_sqlite_upgrade_deletes_only_retired_grants(sqlite_conn) -> None:
    m = _load(MIGRATION, sqlite_conn)
    m.upgrade()
    # `warranty` is a live alias: not this migration's business.
    assert _grant_keys(sqlite_conn) == {"jobs", "communications", "warranty"}


def test_sqlite_rerun_and_downgrade_are_harmless(sqlite_conn) -> None:
    m = _load(MIGRATION, sqlite_conn)
    m.upgrade()
    m.upgrade()
    m.downgrade()
    assert _grant_keys(sqlite_conn) == {"jobs", "communications", "warranty"}


def test_sqlite_no_grants_table_is_a_no_op(tmp_path) -> None:
    eng = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}", future=True)
    with eng.begin() as c:
        _load(MIGRATION, c).upgrade()
    eng.dispose()


# ── Postgres ─────────────────────────────────────

_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
_requires_pg = pytest.mark.skipif(
    "postgresql" not in _URL,
    reason="the Postgres arm of 116 needs a real Postgres; set DATABASE_URL "
    "or TEST_DATABASE_URL to a postgres url",
)


def test_these_tests_actually_run_in_ci() -> None:
    if os.environ.get("CI"):
        assert "postgresql" in _URL, "CI is set but no postgres URL — the Postgres arm would skip."


@pytest.fixture()
def pg_conn() -> Generator[Engine, None, None]:
    schema = "m116_scratch"
    admin = create_engine(_URL, future=True)
    with admin.begin() as c:
        c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        c.exec_driver_sql(f"CREATE SCHEMA {schema}")
    admin.dispose()
    eng = create_engine(_URL, future=True, connect_args={"options": f"-c search_path={schema}"})
    with eng.begin() as c:
        _seed(c, {"jobs", "warranty", "equipment_tracking", "fleet", "campaigns", "contractors"})
    with eng.begin() as c:
        yield c
    eng.dispose()
    admin = create_engine(_URL, future=True)
    with admin.begin() as c:
        c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    admin.dispose()


@_requires_pg
def test_pg_upgrade_deletes_only_retired_grants_and_reruns(pg_conn) -> None:
    m = _load(MIGRATION, pg_conn)
    m.upgrade()
    m.upgrade()
    m.downgrade()
    assert _grant_keys(pg_conn) == {"jobs", "warranty"}
    # the transaction is still usable after the rerun
    assert pg_conn.exec_driver_sql("SELECT COUNT(*) FROM company_module_grants").scalar() == 2

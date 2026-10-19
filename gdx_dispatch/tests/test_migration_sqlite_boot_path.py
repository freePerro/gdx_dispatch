"""The SQLite dev boot path, as far as it runs today (GDXA-1615).

The entrypoint's three steps (create_orm_tables, ``alembic upgrade head``,
``bootstrap_app``) run here as subprocesses against a temp SQLite file, so
``core.database`` binds that file rather than the suite's test database.

Nothing ran alembic on SQLite before this test, and three things were broken
unseen: ``migrations/env.py`` took ``pg_advisory_xact_lock`` on every dialect,
migration 001 fed a multi-statement Postgres dump to sqlite3, and
``bootstrap_app`` bound a str to the Uuid ``tenants.id`` and ``users.id``.

NOT fixed: migrations 002 to 065 are raw Postgres SQL, so the real entrypoint
still fails at 002 on SQLite (deferred, GDXA-1615). The boot test builds 001
for real, stamps across that stretch, runs every later migration, and pins the
columns the skipped stretch leaves missing. A new migration that errors on
SQLite turns it red, and so does a new column on one of the alembic-built
baseline tables (tenant_settings, tenants, game_*) that no migration adds.
The TenantBase tables come from create_all before alembic, in their final
shape, so an unmigrated column on one of them stays green here, and later
migrations never ALTER an older shape of them. It checks column names only:
not types, indexes, triggers, or the work of a migration that returns early on
SQLite (107).
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

_REPO = Path(__file__).resolve().parents[2]
_PKG = _REPO / "gdx_dispatch"
_LAST_PG_ONLY = "065_vendor_bill_payments"
_TENANT_HEX = "00000000000000000000000000000001"
# Columns only the stamped-over 002-065 add: the SQLite schema lacks them, so
# an ORM read of TenantSettings fails on this database. The stamp runs none of
# 002-065, so these entries go only when the whole stretch is ported and the
# stamp is dropped.
_KNOWN_GAP = {
    ("tenant_settings", "session_idle_timeout_minutes"),  # 002
    ("tenant_settings", "workflow_require_invoice_on_complete"),  # 015
    ("tenant_settings", "estimates_hide_line_prices"),  # 019
    ("tenant_settings", "closeout_billing_reconciliation"),  # 047
    ("tenant_settings", "qb_money_pull_paused"),  # 049
}


def _env(db: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.pop("ALEMBIC_DATABASE_URL", None)
    env.pop("GDX_SKIP_BOOTSTRAP", None)
    for key in ("GDX_TENANT_ID", "GDX_DEFAULT_TENANT_ID", "GDX_ADMIN_EMAIL"):
        env.pop(key, None)
    env["DATABASE_URL"] = f"sqlite:///{db}"
    env["GDX_ADMIN_PASSWORD"] = "sqlite-boot-path-test-pw"
    env["PYTHONPATH"] = str(_REPO)
    return env


def _run(env: dict[str, str], *args: str, cwd: Path = _REPO) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=600
    )


def _ok(proc: subprocess.CompletedProcess) -> None:
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]


def _alembic(env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    return _run(env, "-m", "alembic", "-c", "alembic.ini", *args, cwd=_PKG)


def _head() -> str:
    cfg = Config(str(_PKG / "alembic.ini"))
    cfg.set_main_option("script_location", str(_PKG / "migrations"))
    return ScriptDirectory.from_config(cfg).get_current_head()


# Runs in a subprocess: in the suite, earlier tests have imported module models
# (modules/locations, purchase_orders, ...) that gdx_dispatch.models does not,
# so the test process's metadata maps tables create_orm_tables never saw.
_SCHEMA_GAP = """
import json, sqlite3, sys
import gdx_dispatch.models  # noqa: F401 — the same models create_orm_tables registers
from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.tenant_settings import Base

con = sqlite3.connect(sys.argv[1])
gap = []
for metadata in (TenantBase.metadata, Base.metadata):
    for table in metadata.tables.values():
        have = {row[1] for row in con.execute(f'PRAGMA table_info("{table.name}")')}
        gap += [[table.name, c.name] for c in table.columns if c.name not in have]
print(json.dumps(gap))
"""


def _schema_gap(env: dict[str, str], db: Path) -> set[tuple[str, str]]:
    """Every (table, column) the boot path's ORM maps that the migrated database lacks."""
    proc = _run(env, "-c", _SCHEMA_GAP, str(db))
    _ok(proc)
    return {tuple(pair) for pair in json.loads(proc.stdout.strip().splitlines()[-1])}


def _create_orm_tables(env: dict[str, str]) -> None:
    _ok(_run(env, "-c", "from gdx_dispatch.tools.bootstrap_app import create_orm_tables; create_orm_tables()"))


def test_sqlite_boot_path_past_the_pg_only_stretch(tmp_path):
    db = tmp_path / "boot.db"
    env = _env(db)
    _create_orm_tables(env)
    _ok(_alembic(env, "upgrade", "001_squashed_baseline"))
    _ok(_alembic(env, "stamp", _LAST_PG_ONLY))
    _ok(_alembic(env, "upgrade", "head"))
    _ok(_run(env, "-m", "gdx_dispatch.tools.bootstrap_app"))
    # Every container start runs bootstrap again; the second pass reads the
    # rows the first one wrote.
    _ok(_run(env, "-m", "gdx_dispatch.tools.bootstrap_app"))

    con = sqlite3.connect(db)
    try:
        assert con.execute("SELECT version_num FROM alembic_version").fetchall() == [(_head(),)]
        # Stored as SQLAlchemy's Uuid stores it, not coerced to an integer.
        assert con.execute("SELECT id, typeof(id) FROM tenants").fetchall() == [(_TENANT_HEX, "text")]
        assert con.execute("SELECT id FROM companies").fetchall() == [
            ("00000000-0000-0000-0000-000000000001",)
        ]
        users = con.execute("SELECT id, role, deleted_at FROM users").fetchall()
        assert len(users) == 1 and users[0][1] == "owner" and users[0][2] is None
        assert len(users[0][0]) == 32
        assert _schema_gap(env, db) == _KNOWN_GAP
    finally:
        con.close()


def test_unported_stretch_ends_at_the_stamp(tmp_path):
    """The real entrypoint sequence still fails on SQLite, no later than 065.

    This pins the deferred gap; it is not a claim that the path works. When
    002 to 065 are ported it goes red: drop the stamp, _KNOWN_GAP and this test.
    """
    env = _env(tmp_path / "plain.db")
    _create_orm_tables(env)
    proc = _alembic(env, "upgrade", "head")
    assert proc.returncode != 0, "002-065 now run on SQLite: drop the _LAST_PG_ONLY stamp"
    started = [ln.split("Running upgrade ", 1)[1] for ln in proc.stderr.splitlines() if "Running upgrade " in ln]
    failed_at = started[-1].split(" -> ", 1)[1].split(",", 1)[0]
    assert failed_at <= _LAST_PG_ONLY, proc.stderr[-3000:]

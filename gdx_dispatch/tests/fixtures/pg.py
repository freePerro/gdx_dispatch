"""
PG-truth test fixtures.

Loads gdx_dispatch/tests/fixtures/structure.sql (pg_dump --schema-only of a fresh
TenantBase.metadata.create_all() — i.e. the ORM is truth) into a template
database once per session. Each test that requests `pg_test_db` gets a
fresh database cloned from that template via
`CREATE DATABASE ... TEMPLATE gdx_test_template` (~50ms, fully isolated).

Regenerate structure.sql via gdx_dispatch/tools/refresh_test_schema.sh whenever the
ORM changes. GDX prod (post-pave) has the same shape as the ORM, so
"prod-like" is automatic — no per-tenant schema dump needed.

Activated by adding `gdx_dispatch.tests.fixtures.pg` to `pytest_plugins` in
gdx_dispatch/tests/conftest.py. Fixtures are opt-in — existing SQLite-based tests
are unaffected unless they request `pg_test_db` or `pg_test_engine`.

Connection target: local docker container `gdx-test-postgres` (PG 15.17),
exposed on 127.0.0.1:5433 as user `gdx`. Override via env:
  GDX_TEST_PG_HOST, GDX_TEST_PG_PORT, GDX_TEST_PG_USER, GDX_TEST_PG_PASSWORD
"""
from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path

import psycopg2
import pytest
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

PG_HOST = os.environ.get("GDX_TEST_PG_HOST", "127.0.0.1")
PG_PORT = int(os.environ.get("GDX_TEST_PG_PORT", "5433"))
PG_USER = os.environ.get("GDX_TEST_PG_USER", "gdx")
PG_PASSWORD = os.environ.get("GDX_TEST_PG_PASSWORD", "gdx")
PG_ADMIN_DB = os.environ.get("GDX_TEST_PG_ADMIN_DB", "gdx")

# Template name is per-pid so that under pytest-split each independent
# pytest invocation gets its own template (no cross-process coordination
# needed). Falls back to a single name in serial mode.
TEMPLATE_DB = f"gdx_test_template_{os.getpid()}"
STRUCTURE_SQL = Path(__file__).parent / "structure.sql"


def _admin_conn(dbname: str = PG_ADMIN_DB):
    conn = psycopg2.connect(
        host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD, dbname=dbname
    )
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    return conn


def _drop_db(name: str) -> None:
    # psycopg2's `with conn` opens a transaction; DROP/CREATE DATABASE require
    # autocommit + no surrounding transaction, so manage the connection by hand.
    conn = _admin_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (name,),
        )
        cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
        cur.close()
    finally:
        conn.close()


def _cleanup_template(name: str) -> None:
    """Un-flag template + drop. Called at session end."""
    conn = _admin_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE pg_database SET datistemplate = false WHERE datname = %s",
            (name,),
        )
        cur.close()
    finally:
        conn.close()
    _drop_db(name)


def _create_db(name: str, template: str | None = None) -> None:
    conn = _admin_conn()
    try:
        cur = conn.cursor()
        if template:
            cur.execute(f'CREATE DATABASE "{name}" TEMPLATE "{template}"')
        else:
            cur.execute(f'CREATE DATABASE "{name}"')
        cur.close()
    finally:
        conn.close()


def _drop_stale_templates() -> None:
    """Drop any gdx_test_template_<pid> DBs whose PID no longer exists.

    Each pytest invocation (including each pytest-split shard) creates its
    own per-pid template. Crashed runs leave them behind. Sweep them at
    session start so they don't accumulate on developer laptops.
    """
    conn = _admin_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT datname FROM pg_database WHERE datname LIKE 'gdx_test_template_%'"
        )
        names = [r[0] for r in cur.fetchall()]
        cur.close()
    finally:
        conn.close()

    for name in names:
        suffix = name.removeprefix("gdx_test_template_")
        if not suffix.isdigit():
            continue
        pid = int(suffix)
        # /proc/<pid> existence = process is alive. Don't touch live PIDs
        # (could be a parallel split shard mid-run).
        if Path(f"/proc/{pid}").exists():
            continue
        # Multiple shards may race here. Swallow concurrent-drop errors —
        # whichever shard wins, the row goes away, which is the goal.
        try:
            c = _admin_conn()
            try:
                cur = c.cursor()
                cur.execute(
                    "UPDATE pg_database SET datistemplate = false WHERE datname = %s",
                    (name,),
                )
                cur.close()
            finally:
                c.close()
            _drop_db(name)
        except (
            psycopg2.errors.DeadlockDetected,
            psycopg2.errors.InternalError_,
            psycopg2.errors.UndefinedObject,
            psycopg2.errors.ObjectInUse,
        ):
            # Another shard is dropping the same row, or it's already gone.
            continue


@pytest.fixture(scope="session")
def pg_template_db(request) -> str:
    """Create the template DB once per session and load structure.sql into it."""
    _drop_stale_templates()
    request.addfinalizer(lambda: _cleanup_template(TEMPLATE_DB))
    if not STRUCTURE_SQL.exists():
        pytest.skip(f"{STRUCTURE_SQL} not present — run gdx_dispatch/tools/refresh_test_schema.sh")

    # Templates can't be dropped while flagged as templates — un-flag first.
    conn = _admin_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE pg_database SET datistemplate = false WHERE datname = %s",
            (TEMPLATE_DB,),
        )
        cur.close()
    finally:
        conn.close()

    _drop_db(TEMPLATE_DB)
    _create_db(TEMPLATE_DB)

    env = {**os.environ, "PGPASSWORD": PG_PASSWORD}
    result = subprocess.run(
        [
            "psql",
            "-h", PG_HOST,
            "-p", str(PG_PORT),
            "-U", PG_USER,
            "-d", TEMPLATE_DB,
            "-v", "ON_ERROR_STOP=1",
            "-q",
            "-f", str(STRUCTURE_SQL),
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.fail(
            f"Failed to load {STRUCTURE_SQL.name} into {TEMPLATE_DB}\n"
            f"stderr (last 2000 chars):\n{result.stderr[-2000:]}"
        )

    conn = _admin_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE pg_database SET datistemplate = true WHERE datname = %s",
            (TEMPLATE_DB,),
        )
        cur.close()
    finally:
        conn.close()
    return TEMPLATE_DB


@pytest.fixture
def pg_test_db(pg_template_db) -> str:
    """Per-test database cloned from the template. Returns the SQLAlchemy URL."""
    name = f"gdx_test_{uuid.uuid4().hex[:12]}"
    _create_db(name, template=pg_template_db)
    url = f"postgresql+psycopg2://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{name}"
    try:
        yield url
    finally:
        _drop_db(name)


@pytest.fixture
def pg_test_engine(pg_test_db):
    engine = create_engine(pg_test_db, future=True)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def pg_test_session(pg_test_engine) -> Session:
    with Session(pg_test_engine, future=True) as s:
        yield s

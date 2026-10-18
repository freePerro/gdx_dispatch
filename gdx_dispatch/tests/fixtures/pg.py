"""
PG-truth test fixtures.

Loads gdx_dispatch/tests/fixtures/structure.sql (pg_dump --schema-only of a fresh
TenantBase.metadata.create_all() — i.e. the ORM is truth) into a template
database once per session. Each test that requests `pg_test_db` gets a
fresh database cloned from that template via
`CREATE DATABASE ... TEMPLATE gdx_test_template_<pid>_<hex8>` (~50ms, fully
isolated).

Regenerate structure.sql via gdx_dispatch/tools/refresh_test_schema.sh whenever the
ORM changes; gdx_dispatch/tests/test_structure_sql_drift.py is red until you do
(GDXA-1558: the script was named here for months before it existed). GDX prod (post-pave) has the same shape as the ORM, so
"prod-like" is automatic — no per-tenant schema dump needed.

Activated by adding `gdx_dispatch.tests.fixtures.pg` to `pytest_plugins` in
gdx_dispatch/tests/conftest.py. Fixtures are opt-in — existing SQLite-based tests
are unaffected unless they request `pg_test_db` or `pg_test_engine`.

Connection target: local docker container `gdx-test-postgres` (PG 15.17),
exposed on 127.0.0.1:5433 as user `gdx`. Override via env:
  GDX_TEST_PG_HOST, GDX_TEST_PG_PORT, GDX_TEST_PG_USER, GDX_TEST_PG_PASSWORD,
  GDX_TEST_PG_ADMIN_DB
CI sets all five in `.github/workflows/ci.yml`. With no reachable PG the chain
skips on a laptop and FAILS under CI (see `_skip_unless_ci`, #440).
"""
from __future__ import annotations

import os
import re
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

TEMPLATE_PREFIX = "gdx_test_template_"


def _template_name(pid: int) -> str:
    """``gdx_test_template_<pid>_<hex8>`` — unique per pytest process.

    The pid alone is not unique: every docker-app shard runs pytest as PID 1,
    so shards sharing one Postgres all picked ``gdx_test_template_1`` and
    dropped the template another shard was cloning (GDXA-660). The random
    suffix makes the name unique across PID namespaces; the pid stays for
    whoever is reading ``pg_database``. At most 45 bytes, so it also fits
    ``application_name`` and is never truncated at Postgres's 63.
    """
    return f"{TEMPLATE_PREFIX}{pid}_{uuid.uuid4().hex[:8]}"


# One template per pytest process, so each pytest-split shard has its own
# and no cross-process coordination is needed beyond the owner connection.
TEMPLATE_DB = _template_name(os.getpid())
STRUCTURE_SQL = Path(__file__).parent / "structure.sql"


def _admin_conn(dbname: str = PG_ADMIN_DB, application_name: str | None = None):
    kwargs = {"application_name": application_name} if application_name else {}
    conn = psycopg2.connect(
        host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD, dbname=dbname,
        **kwargs,
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


_TEMPLATE_NAME_RE = re.compile(rf"^{TEMPLATE_PREFIX}\d+_[0-9a-f]{{8}}$")


def _hold_template_owner(name: str, dbname: str = PG_ADMIN_DB):
    """Open the connection that marks ``name`` as owned; return it.

    It carries the template name as its ``application_name`` and lives as long
    as the session, so a crashed or killed pytest disowns its template with no
    cleanup of its own. ``pg_stat_activity`` is server-wide, so the mark is
    seen from any database: a sweeper with a different GDX_TEST_PG_ADMIN_DB
    still sees it, which an advisory lock (per-database) would not.
    """
    return _admin_conn(dbname, application_name=name)


def _drop_stale_templates() -> None:
    """Drop every gdx_test_template_<pid>_<hex8> DB that no session owns.

    Each pytest invocation (including each pytest-split shard) creates its
    own template. Crashed runs leave them behind. Sweep them at session start
    so they don't accumulate on developer laptops.

    Liveness is the owner connection from ``_hold_template_owner``, asked of
    the server, not ``/proc/<pid>``: shards run in their own PID namespaces,
    so ``/proc/1`` always exists inside a container and a host pid is
    invisible from one. Names in the old ``gdx_test_template_<pid>`` form are
    left alone: an older checkout may still be running them, and it holds no
    owner connection to ask.
    """
    conn = _admin_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT d.datname FROM pg_database d "
            "WHERE d.datname LIKE 'gdx_test_template_%' AND NOT EXISTS ("
            "  SELECT 1 FROM pg_stat_activity a WHERE a.application_name = d.datname)"
        )
        names = [r[0] for r in cur.fetchall() if _TEMPLATE_NAME_RE.match(r[0])]
        cur.close()
    finally:
        conn.close()

    for name in names:
        if name == TEMPLATE_DB:
            continue
        # Multiple shards may race here. Swallow concurrent-drop errors —
        # whichever shard wins, the row goes away, which is the goal.
        try:
            _cleanup_template(name)
        except (
            psycopg2.errors.DeadlockDetected,
            psycopg2.errors.InternalError_,
            psycopg2.errors.UndefinedObject,
            psycopg2.errors.ObjectInUse,
        ):
            continue


def _skip_unless_ci(reason: str) -> None:
    """Skip on a laptop; fail under CI.

    Every test on this fixture used to skip green in CI (#440): the defaults
    above point at a laptop's 5433 while CI's service is on 5432, and nothing
    noticed for weeks — a skip is indistinguishable from a pass in a summary.
    CI provides a Postgres, so there a skip here can only mean the wiring is
    broken, and it must say so. GitHub Actions sets ``CI=true``.
    """
    if os.environ.get("CI"):
        pytest.fail(f"CI is set, so the Postgres arm must run, not skip: {reason}")
    pytest.skip(reason)


@pytest.fixture(scope="session")
def pg_template_db(request) -> str:
    """Create the template DB once per session and load structure.sql into it."""
    # requires_pg gate: on a laptop with no test PG, skip (not error) the whole
    # pg-fixture chain. Set GDX_TEST_PG_HOST/PORT/USER/PASSWORD to run these.
    try:
        _admin_conn().close()
    except psycopg2.OperationalError as exc:
        _skip_unless_ci(
            f"PostgreSQL not reachable at {PG_HOST}:{PG_PORT} "
            f"(set GDX_TEST_PG_* to run requires-pg tests): {exc}"
        )
    # Own it before it exists, so no sweeper ever sees it unowned.
    owner = _hold_template_owner(TEMPLATE_DB)

    def _finalize() -> None:
        try:
            _cleanup_template(TEMPLATE_DB)
        finally:
            owner.close()

    request.addfinalizer(_finalize)
    _drop_stale_templates()
    if not STRUCTURE_SQL.exists():
        _skip_unless_ci(f"{STRUCTURE_SQL} not present — run gdx_dispatch/tools/refresh_test_schema.sh")

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

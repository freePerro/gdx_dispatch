"""structure.sql must be the ORM (GDXA-1558).

``fixtures/pg.py`` loads ``fixtures/structure.sql`` as the schema every
PG-truth test runs against, and its docstring calls the ORM the source of
truth. Nothing held it there: by 2026-10-18 the dump lacked 103 TenantBase
tables (``job_chat_messages`` among them, so the 658 chat test built its own
database) and carried 11 the ORM had dropped, and tests patched columns into
their clones one by one to get past the lag.

Two halves, so the guard runs everywhere:

* the text half parses the dump and compares table and column names with
  ``TenantBase.metadata``. It needs no database, so it runs in every shard
  and on every laptop.
* the Postgres half builds a fresh database with ``create_all`` and compares
  the catalog (column types, nullability, defaults, indexes, constraints)
  with a clone of the template. It runs wherever the PG arm runs, and under
  CI an unreachable Postgres fails rather than skips (``pg._skip_unless_ci``).

Fix a red here with ``gdx_dispatch/tools/refresh_test_schema.sh``, never by
editing structure.sql by hand.
"""
from __future__ import annotations

import functools
import re
import subprocess
import sys
import uuid

from sqlalchemy import create_engine, text

import gdx_dispatch.models  # noqa: F401 — registers every model on TenantBase.metadata
from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.tests.fixtures import pg

_REFRESH = "run gdx_dispatch/tools/refresh_test_schema.sh"

_TABLE_RE = re.compile(r"^CREATE TABLE public\.(\w+) \($(.*?)^\);$", re.M | re.S)
_COLUMN_RE = re.compile(r'^    (?:"([^"]+)"|(\w+)) ', re.M)


def _dump_columns() -> dict[str, set[str]]:
    sql = pg.STRUCTURE_SQL.read_text()
    tables: dict[str, set[str]] = {}
    for name, body in _TABLE_RE.findall(sql):
        cols = {q or bare for q, bare in _COLUMN_RE.findall(body)}
        cols.discard("CONSTRAINT")
        tables[name] = cols
    return tables


@functools.cache
def _app_table_names() -> frozenset[str]:
    """The tables the app itself registers on TenantBase.

    Asked of a fresh interpreter doing the refresh script's own import, not of
    this process: by the time a test runs, conftest and other test modules
    have imported models no app code imports (``modules/distributor/
    onboarding.py``'s ``dealer_invitations``), so this process's metadata is a
    superset of the ORM the app runs.
    """
    out = subprocess.run(
        [sys.executable, "-c",
         "import gdx_dispatch.models\n"
         "from gdx_dispatch.core.audit import TenantBase\n"
         "for t in TenantBase.metadata.tables: print('TABLE', t)"],
        capture_output=True, text=True, check=True,
    ).stdout
    return frozenset(line[6:] for line in out.splitlines() if line.startswith("TABLE "))


def _orm_columns() -> dict[str, set[str]]:
    return {
        name: {c.name for c in TenantBase.metadata.tables[name].columns}
        for name in _app_table_names()
    }


def test_the_parser_reads_the_dump():
    """A parser that matched nothing would make the comparisons below vacuous."""
    dump = _dump_columns()
    assert len(dump) > 200, f"parsed only {len(dump)} tables from {pg.STRUCTURE_SQL.name}"
    assert len(_app_table_names()) > 200, "the app-ORM probe returned almost nothing"
    assert {"id", "job_id", "created_at"} <= dump["job_chat_messages"]


def test_structure_sql_has_exactly_the_orm_tables():
    dump, orm = set(_dump_columns()), set(_orm_columns())
    assert not orm - dump, f"ORM tables missing from structure.sql: {sorted(orm - dump)}; {_REFRESH}"
    assert not dump - orm, f"structure.sql tables the ORM no longer has: {sorted(dump - orm)}; {_REFRESH}"


def test_structure_sql_has_exactly_the_orm_columns():
    dump, orm = _dump_columns(), _orm_columns()
    drift = {
        table: {"missing": sorted(cols - dump[table]), "extra": sorted(dump[table] - cols)}
        for table, cols in orm.items()
        if table in dump and cols != dump[table]
    }
    assert not drift, f"column drift between the ORM and structure.sql: {drift}; {_REFRESH}"


_CATALOG = {
    "columns": """
        SELECT table_name, column_name, data_type, udt_name,
               character_maximum_length, numeric_precision, numeric_scale,
               is_nullable, column_default
        FROM information_schema.columns WHERE table_schema = 'public'
    """,
    "indexes": "SELECT tablename, indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'",
    "constraints": """
        SELECT c.conrelid::regclass::text, c.conname, pg_get_constraintdef(c.oid)
        FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace
        WHERE n.nspname = 'public'
    """,
    "enums": """
        SELECT t.typname, e.enumlabel, e.enumsortorder
        FROM pg_type t JOIN pg_enum e ON e.enumtypid = t.oid
        JOIN pg_namespace n ON n.oid = t.typnamespace WHERE n.nspname = 'public'
    """,
}


def _catalog(engine) -> dict[str, set[tuple]]:
    with engine.connect() as conn:
        return {k: {tuple(r) for r in conn.execute(text(q))} for k, q in _CATALOG.items()}


def test_template_catalog_equals_a_fresh_create_all(pg_test_engine):
    name = f"gdx_test_orm_{uuid.uuid4().hex[:12]}"
    pg._create_db(name)
    fresh = create_engine(
        f"postgresql+psycopg2://{pg.PG_USER}:{pg.PG_PASSWORD}@{pg.PG_HOST}:{pg.PG_PORT}/{name}",
        future=True,
    )
    try:
        TenantBase.metadata.create_all(
            fresh, tables=[TenantBase.metadata.tables[n] for n in _app_table_names()],
        )
        want, got = _catalog(fresh), _catalog(pg_test_engine)
    finally:
        fresh.dispose()
        pg._drop_db(name)

    drift = {
        kind: {"missing": sorted(map(str, want[kind] - got[kind]))[:20],
               "extra": sorted(map(str, got[kind] - want[kind]))[:20]}
        for kind in _CATALOG
        if want[kind] != got[kind]
    }
    assert not drift, f"structure.sql's catalog differs from create_all's: {drift}; {_REFRESH}"

"""#681 — `GET /api/segments` 500s on PostgreSQL.

`_customer_stats` listed every selected column in its `GROUP BY`, which put
`c.metadata` there. On PostgreSQL `customers.metadata` is `json`, a type with no
equality operator, so the statement raised
`UndefinedFunction: could not identify an equality operator for type json`.

Provenance, stated precisely because the first draft of this file got it wrong:
the *query* has been malformed since the initial public release, but the *list*
endpoint only started calling `_customer_stats` in f2b38ef (PR #675, shipped in
v1.118.3). Before that the bad query was reachable only through
`/{id}/customers` and `/{id}/count`. So the Segments **page** breaking is a
v1.118.3 regression; the broken SQL underneath is older.

Three guards, deliberately layered:

* `test_group_by_names_only_the_primary_key` runs **everywhere**, including the
  default SQLite suite. It asserts the shape of the generated SQL, so it fails
  the moment a payload column goes back into the GROUP BY — on the engine where
  that mistake is otherwise invisible. Mutation-proved.
* `test_the_orm_still_declares_the_primary_key_the_fix_depends_on` guards the
  assumption underneath the fix. `GROUP BY c.id` is only legal because `id` is a
  *declared* primary key; Postgres's functional-dependency rule recognises
  nothing weaker. Drop that and the fix trades one 500 for another
  (`column "c.name" must appear in the GROUP BY clause`).
* `test_the_query_actually_runs_on_postgres` is the end-to-end proof and skips
  without a Postgres URL. A skipping test is not a regression net (#440), which
  is why the two above do not skip.

Absence-assertions on generated SQL are the point here: asserting a string is
*missing* is meaningful, where asserting one is *present* would only prove
somebody typed it.
"""

from __future__ import annotations

import os
import re
from collections.abc import Generator

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Connection

from gdx_dispatch.models.tenant_models import Customer
from gdx_dispatch.routers.segments import _customer_stats_sql

_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
_requires_pg = pytest.mark.skipif(
    "postgresql" not in _URL,
    reason="the Postgres arm of #681 needs a real Postgres; set DATABASE_URL "
    "or TEST_DATABASE_URL to a postgres url",
)


def _group_by_clause(sql: str) -> str:
    m = re.search(r"GROUP BY(.*?)ORDER BY", sql, re.S | re.I)
    assert m, f"no GROUP BY ... ORDER BY found in:\n{sql}"
    return m.group(1).strip()


@pytest.mark.parametrize("has_customer_type", [True, False])
@pytest.mark.parametrize("has_metadata", [True, False])
def test_group_by_names_only_the_primary_key(has_customer_type, has_metadata):
    """The GROUP BY must be the PK alone, in every column-detection combination.

    This is the guard that fails on SQLite for a Postgres-only defect. Put
    `, c.metadata` back and this goes red on a laptop, instead of staying green
    until the page 500s in production.
    """
    clause = _group_by_clause(_customer_stats_sql(has_customer_type, has_metadata))
    assert clause == "c.id", (
        "GROUP BY must name only the primary key — every other c.* column is "
        "functionally dependent on it, and a json column there raises "
        f"UndefinedFunction on PostgreSQL (#681). Got: {clause!r}"
    )


def test_metadata_is_still_selected_even_though_it_is_not_grouped():
    """The other half of the bound.

    Dropping `metadata` from the GROUP BY must not mean dropping it from the
    result — `_customer_stats` unpacks it into per-customer keys the rule engine
    matches on. Without this, "keep json out of GROUP BY" could be implemented
    as "stop selecting metadata" and the test above would still pass.
    """
    sql = _customer_stats_sql(has_customer_type=True, has_metadata=True)
    select_list = sql.split("FROM customers")[0]
    assert "c.metadata AS metadata" in select_list
    assert "c.customer_type AS customer_type" in select_list

    absent = _customer_stats_sql(has_customer_type=False, has_metadata=False)
    absent_select = absent.split("FROM customers")[0]
    assert "NULL AS metadata" in absent_select
    assert "NULL AS customer_type" in absent_select
    assert _group_by_clause(absent) == "c.id"


def test_the_orm_still_declares_the_primary_key_the_fix_depends_on():
    """`GROUP BY c.id` is legal only because `id` is a DECLARED primary key.

    Postgres's functional-dependency shortcut recognises nothing weaker — not a
    unique index, not a NOT NULL. Verified against PG 16.15: with the same table
    and no PK, the new SQL fails with
    `column "c.name" must appear in the GROUP BY clause`. So this fix would
    trade one 500 for another if the key were ever dropped from the model.
    """
    pk = [c.name for c in inspect(Customer).mapper.primary_key]
    assert pk == ["id"], (
        f"customers' primary key is {pk}, not ['id'] — _customer_stats_sql "
        "groups by c.id alone and relies on that being the declared PK (#681)"
    )


@pytest.fixture()
def pg_conn() -> Generator[Connection, None, None]:
    """Every table lives in a scratch schema.

    `customers`, `jobs` and `invoices` are REAL table names. An earlier draft of
    this file ran `DROP TABLE IF EXISTS invoices, jobs, customers CASCADE`
    against `$DATABASE_URL` — which CI sets for all seven shards
    (`ci.yml`), and which points at the dev database inside `docker-app-1`. It
    dropped all three. Same property the 091/092/093 migration tests hold, and
    for the same reason.
    """
    schema = "seg681_scratch"
    eng = create_engine(_URL, future=True)
    with eng.begin() as c:
        c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        c.exec_driver_sql(f"CREATE SCHEMA {schema}")
    with eng.begin() as c:
        c.exec_driver_sql(f"SET search_path TO {schema}")
        # `metadata json` and `id ... PRIMARY KEY` are the shapes measured on
        # the production database 2026-09-09.
        c.exec_driver_sql(
            "CREATE TABLE customers (id uuid PRIMARY KEY, name text, email text,"
            " phone text, address text, customer_type varchar, metadata json,"
            " created_at timestamptz, deleted_at timestamptz)"
        )
        c.exec_driver_sql(
            "CREATE TABLE jobs (id uuid PRIMARY KEY, customer_id uuid,"
            " created_at timestamptz, deleted_at timestamptz)"
        )
        c.exec_driver_sql(
            "CREATE TABLE invoices (id uuid PRIMARY KEY, job_id uuid,"
            " total numeric, deleted_at timestamptz)"
        )
        yield c
    with eng.begin() as c:
        c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    eng.dispose()


@_requires_pg
def test_the_query_actually_runs_on_postgres(pg_conn):
    """End-to-end proof against the engine that rejected the old query.

    Pre-fix this raised `UndefinedFunction: could not identify an equality
    operator for type json` — the exact error behind the production 500.
    """
    pg_conn.exec_driver_sql(
        "INSERT INTO customers VALUES (gen_random_uuid(), 'A', 'a@example.test',"
        " '1', 'x', 'Residential', '{\"tier\":\"gold\"}'::json, now(), NULL)"
    )
    rows = pg_conn.execute(
        text(_customer_stats_sql(has_customer_type=True, has_metadata=True))
    ).mappings().all()
    assert len(rows) == 1, rows
    assert rows[0]["metadata"] == {"tier": "gold"}, (
        "metadata must survive into the payload even though it is not grouped"
    )


@_requires_pg
def test_a_customer_with_several_jobs_collapses_to_one_row(pg_conn):
    """Grouping by the PK alone must still aggregate, not fan out.

    Without this, "GROUP BY c.id" could be mistaken for "no grouping needed" and
    a customer with three jobs would appear three times in the segment list.
    """
    pg_conn.exec_driver_sql(
        "INSERT INTO customers VALUES ('22222222-2222-4222-8222-222222222222',"
        " 'B', 'b@example.test', '2', 'y', 'Commercial', '{}'::json, now(), NULL)"
    )
    for _ in range(3):
        pg_conn.exec_driver_sql(
            "INSERT INTO jobs VALUES (gen_random_uuid(),"
            " '22222222-2222-4222-8222-222222222222', now(), NULL)"
        )
    rows = pg_conn.execute(
        text(_customer_stats_sql(has_customer_type=True, has_metadata=True))
    ).mappings().all()
    assert len(rows) == 1, f"three jobs must not fan the customer out: {rows}"

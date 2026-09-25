"""GDXA-13 — a real pave of a real migrated database keeps its rows.

`test_pave_strict.py` is entirely `unittest.mock` over `subprocess.run`: it
never runs a `pg_dump`/`psql` pair, so it could not fail for the defect this
file guards. That defect was that pave dumped **every** table in the database
and recreated only `TenantBase.metadata`'s, so the reload hit
`alembic_version` — a table in every Alembic-managed database and in no ORM —
and `ON_ERROR_STOP=on` halted there. Nothing after the 4th COPY landed: a
245-table, 0-row database, the audit chain and the admin account gone.

So this test does the whole thing for real: it builds a database the way
`docker/entrypoint.sh` does (create_all → `alembic upgrade head` → bootstrap),
adds a plugin-shaped table that neither the ORM nor a migration owns, paves
it, and asserts the rows are still there afterwards.

It also invokes pave the way its own docstring tells an operator to — **by
script path, from an unrelated working directory, with `PYTHONPATH` scrubbed**
— because that is the invocation that used to `DROP SCHEMA public CASCADE` and
only then discover it could not import `gdx_dispatch` to rebuild it.

Connection target is the same `GDX_TEST_PG_*` env the rest of the pg harness
uses (`tests/fixtures/pg.py`). With no reachable Postgres this skips on a
laptop and FAILS under CI — a skip here is indistinguishable from a pass, and
that is exactly how #440 hid 33 tests for weeks.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import psycopg2
import pytest

from gdx_dispatch.tests.fixtures.pg import (
    PG_HOST,
    PG_PASSWORD,
    PG_PORT,
    PG_USER,
    _admin_conn,
    _create_db,
    _drop_db,
    _skip_unless_ci,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_DIR = REPO_ROOT / "gdx_dispatch"
PAVE_SCRIPT = PACKAGE_DIR / "tools" / "pave_tenant_db.py"

# Plugin tables: created at boot by `schema_reconcile`, owned by neither the
# ORM nor a migration. Prod carries 13 of these holding 632 rows. Shaped to
# make the replay do real work rather than one bare CREATE TABLE:
#
#   * serial primary keys, so each table's captured file carries its own
#     `CREATE SEQUENCE` (no IF NOT EXISTS — pg_dump does not emit one);
#   * `plug_fake_aline` sorts BEFORE the `plug_fake_catalog` it references, so
#     its file is guaranteed to lose its first replay pass on the missing FK
#     target and has to be retried;
#   * a trigger on `plug_fake_catalog`, which `pg_dump -t` emits inside that
#     table's file while the catalog also reports it as a standalone object.
#
# The last two are where replaying an already-present object would be fatal:
# a second CREATE of either is an "already exists" error no pass can clear, so
# step 4c would exit "no progress" with the schema already dropped.
PLUGIN_TABLE_DDL = """
CREATE TABLE plug_fake_catalog (
    id    SERIAL PRIMARY KEY,
    sku   text NOT NULL,
    price numeric(10, 2)
);
CREATE INDEX ix_plug_fake_catalog_sku ON plug_fake_catalog (sku);
INSERT INTO plug_fake_catalog (sku, price) VALUES ('A-1', 10), ('B-2', 20), ('C-3', 30);

CREATE TABLE plug_fake_aline (
    id     SERIAL PRIMARY KEY,
    cat_id integer NOT NULL REFERENCES plug_fake_catalog (id),
    note   text
);
INSERT INTO plug_fake_aline (cat_id, note) VALUES (1, 'x'), (2, 'y');

CREATE FUNCTION plug_fake_touch() RETURNS trigger AS $$
BEGIN
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER plug_fake_catalog_touch BEFORE UPDATE ON plug_fake_catalog
    FOR EACH ROW EXECUTE FUNCTION plug_fake_touch();

-- An unowned sequence with non-default parameters. `gl_journal_entry_no_seq`
-- is the real one: owned by no column, so nothing rebuilds it and nothing in
-- step 6 can reset it. A bare `CREATE SEQUENCE` would silently return it to
-- INCREMENT 1 / NO CYCLE.
CREATE SEQUENCE plug_fake_unowned_seq INCREMENT BY 5 START WITH 100 CYCLE MAXVALUE 900;
SELECT nextval('plug_fake_unowned_seq');
"""

# A view on a matview: two captured objects with a real dependency between
# them, so the replay's multi-pass ordering is exercised by something other
# than a mock. The matview holds rows of its own, which the catalog cannot
# tell apart from an empty one — `relispopulated` is true either way once
# something has refreshed it, and a matview is not a base table so no row-count
# check ever looked at it.
#
# `v_cheap_plug_parts` carries a CHECK OPTION, which lives in
# `pg_class.reloptions` and not in `pg_get_viewdef()`: restoring the view
# without it turns a view that refuses a bad write into one that accepts it.
EXTRA_OBJECTS_DDL = """
CREATE MATERIALIZED VIEW mv_plug_skus AS SELECT id, sku FROM plug_fake_catalog;
-- A UNIQUE index on a matview. Nothing rebuilds one — and losing it breaks
-- REFRESH MATERIALIZED VIEW CONCURRENTLY permanently.
CREATE UNIQUE INDEX ux_mv_plug_skus_id ON mv_plug_skus (id);
CREATE VIEW v_plug_skus AS SELECT sku FROM mv_plug_skus;
CREATE VIEW v_cheap_plug_parts AS
    SELECT * FROM plug_fake_catalog WHERE price < 100 WITH CASCADED CHECK OPTION;
-- security_invoker lives in the same reloptions array as check_option. Losing
-- it silently flips a view from the caller's rights to the definer's.
CREATE VIEW v_invoker_plug_parts WITH (security_invoker = true) AS
    SELECT sku FROM plug_fake_catalog;
-- Never refreshed. Restoring the prior state means leaving it that way: a pave
-- that populates it has changed the database, and step 7 would then report a
-- mismatch for a difference the pave itself introduced.
CREATE MATERIALIZED VIEW mv_never_refreshed AS SELECT sku FROM plug_fake_catalog
    WITH NO DATA;
-- The matview above is now STALE: a 4th catalog row exists that its stored
-- result does not know about. Stale is the normal state of a matview, and a
-- correct pave must not report it as lost rows.
INSERT INTO plug_fake_catalog (sku, price) VALUES ('D-4', 40);
"""

# `DROP SCHEMA public CASCADE` takes these with everything else, no migration
# installs them, and `core/audit.py` only ever installs them once — so a pave
# that does not put them back leaves audit_logs mutable with ARCHITECTURAL
# INVARIANT #2 silently unenforced.
AUDIT_GUARD_OBJECTS = (
    ("function", "audit_logs_immutable_guard()"),
    ("trigger", "audit_logs_no_update ON audit_logs"),
    ("trigger", "audit_logs_no_delete ON audit_logs"),
)

COUNTED_TABLES = (
    "users", "companies", "tenants", "tags", "audit_logs",
    "plug_fake_catalog", "plug_fake_aline",
)

# migrations/grant_helpers.py (migration 029) calls pg_input_is_valid(), which
# is PostgreSQL 16+. Every other pg test loads fixtures/structure.sql instead
# of running the migration chain, so this is the first one that needs a real
# 16. The repo's documented laptop container `gdx-test-postgres` is 15; CI's
# service is postgres:16, so this gate skips on a laptop and never in CI.
MIN_SERVER_MAJOR = 16


def _pg_dump_major() -> int:
    out = subprocess.run(["pg_dump", "--version"], capture_output=True, text=True).stdout
    return int(re.search(r"(\d+)", out.split()[-1]).group(1))


def _child_env(url: str, **extra: str) -> dict[str, str]:
    env = {**os.environ, "DATABASE_URL": url, "ALEMBIC_DATABASE_URL": url}
    env.setdefault("JWT_SECRET", "test-secret-key-at-least-32-bytes-long-x")
    env.update(extra)
    return env


def _run_step(label: str, cmd: list[str], cwd: Path, env: dict[str, str]) -> None:
    r = subprocess.run(cmd, cwd=str(cwd), env=env, capture_output=True, text=True)
    if r.returncode != 0:
        pytest.fail(
            f"rig setup step '{label}' failed (rc={r.returncode})\n"
            f"stdout:\n{r.stdout[-2000:]}\nstderr:\n{r.stderr[-2000:]}"
        )


def _sql(url: str, query: str):
    conn = psycopg2.connect(url)
    try:
        cur = conn.cursor()
        cur.execute(query)
        rows = cur.fetchall()
        cur.close()
        return rows
    finally:
        conn.close()


def _exec(url: str, statements: str) -> None:
    conn = psycopg2.connect(url)
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(statements)
        cur.close()
    finally:
        conn.close()


def _table_names(url: str) -> set[str]:
    return {
        r[0] for r in _sql(
            url,
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'",
        )
    }


def _inventory(url: str) -> set[tuple[str, str]]:
    """(kind, identity) for every non-table object in `public`.

    Deliberately written here rather than imported from the tool: a guard that
    asks the code under test what the right answer is cannot fail for a wrong
    answer.
    """
    rows = _sql(url, """
        SELECT CASE c.relkind WHEN 'v' THEN 'view' ELSE 'matview' END, c.relname::text
          FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public' AND c.relkind IN ('v', 'm')
        UNION ALL
        SELECT 'function', p.oid::regprocedure::text
          FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
         WHERE n.nspname = 'public' AND p.prokind IN ('f', 'p')
        UNION ALL
        SELECT 'trigger', t.tgname || ' ON ' || c.relname
          FROM pg_trigger t
          JOIN pg_class c ON c.oid = t.tgrelid
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public' AND NOT t.tgisinternal
        UNION ALL
        SELECT 'enum', t.typname::text
          FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
         WHERE n.nspname = 'public' AND t.typtype = 'e'
    """)
    return {(r[0], r[1]) for r in rows}


def _counts(url: str) -> dict[str, int]:
    select = " UNION ALL ".join(
        f"SELECT '{t}' AS t, count(*) FROM \"{t}\""  # noqa: S608 — names come from COUNTED_TABLES, a literal tuple
        for t in COUNTED_TABLES
    )
    return {r[0]: r[1] for r in _sql(url, select)}


def _install_audit_guard_if_absent(url: str) -> None:
    """Make sure the rig carries the audit-immutability guard.

    `core/audit.py` installs it on the first audit write when the role has
    CREATE. The rig's role is a superuser, so bootstrap normally leaves it in
    place; install it here if not, because a rig without it cannot notice a
    pave that drops it.
    """
    present = _sql(url, "SELECT 1 FROM pg_proc WHERE proname = 'audit_logs_immutable_guard'")
    if present:
        return
    _exec(url, """
        CREATE OR REPLACE FUNCTION audit_logs_immutable_guard()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit_logs is immutable (op=%)', TG_OP
                USING HINT = 'audit rows are append-only; see D45';
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER audit_logs_no_update BEFORE UPDATE ON audit_logs
            FOR EACH ROW EXECUTE FUNCTION audit_logs_immutable_guard();
        CREATE TRIGGER audit_logs_no_delete BEFORE DELETE ON audit_logs
            FOR EACH ROW EXECUTE FUNCTION audit_logs_immutable_guard();
    """)


@pytest.fixture
def migrated_pg_db():
    """A throwaway database built exactly the way `entrypoint.sh` builds one.

    That pairing — `create_all()` then `alembic upgrade head` then bootstrap —
    is what produces the Alembic-base tables (`alembic_version`, `tenants`,
    `tenant_settings`, `server_errors`, the game tables) that `create_all()`
    alone never makes. A database built from the ORM only cannot reproduce
    this defect, which is why the pg template fixture is not reused here.

    Deliberately not built by calling anything in `pave_tenant_db`: the rig
    has to stand up whether or not the code under test works.
    """
    if shutil.which("pg_dump") is None or shutil.which("psql") is None:
        _skip_unless_ci("pg_dump/psql not on PATH — pave shells out to both")
    try:
        conn = _admin_conn()
    except psycopg2.OperationalError as exc:
        _skip_unless_ci(
            f"PostgreSQL not reachable at {PG_HOST}:{PG_PORT} "
            f"(set GDX_TEST_PG_* to run the pave rebuild guard): {exc}"
        )
    try:
        server_major = conn.server_version // 10000
    finally:
        conn.close()
    if server_major < MIN_SERVER_MAJOR:
        _skip_unless_ci(
            f"server at {PG_HOST}:{PG_PORT} is PostgreSQL {server_major}; this test runs the "
            f"migration chain and migration 029 needs pg_input_is_valid() (PG "
            f"{MIN_SERVER_MAJOR}+). The repo's laptop container gdx-test-postgres is 15; "
            f"point GDX_TEST_PG_* at a 16 to run it."
        )
    client_major = _pg_dump_major()
    if client_major != server_major:
        # Dockerfile pins postgresql-client-16 for exactly this: a newer client
        # emits GUCs (transaction_timeout) that ON_ERROR_STOP=on refuses on an
        # older server, and the failure reads as a pave bug rather than a
        # harness one. ubuntu-latest ships 16.15 against CI's postgres:16.
        _skip_unless_ci(
            f"HARNESS MISMATCH, NOT A PAVE DEFECT: pg_dump is {client_major} but the server is "
            f"{server_major}, and pave's dump/reload pair needs matching majors. Fix the "
            f"harness, not the tool: align gdx_dispatch/docker/Dockerfile's postgresql-client "
            f"pin, ci.yml's postgres service image, and the runner's bundled client."
        )

    name = f"gdx_pave_{uuid.uuid4().hex[:12]}"
    url = f"postgresql://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{name}"
    _create_db(name)
    try:
        env = _child_env(url)
        _run_step(
            "create_all",
            [sys.executable, "-c",
             "from gdx_dispatch.tools.bootstrap_app import create_orm_tables; create_orm_tables()"],
            REPO_ROOT, env,
        )
        _run_step(
            "alembic upgrade head",
            [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"],
            PACKAGE_DIR, env,
        )
        _run_step(
            "bootstrap",
            [sys.executable, "-m", "gdx_dispatch.tools.bootstrap_app"],
            REPO_ROOT, env,
        )
        _exec(url, PLUGIN_TABLE_DDL)
        _exec(url, EXTRA_OBJECTS_DDL)
        # bootstrap_app writes audit rows, which is what installs the
        # immutability guard via core/audit.py. Assert rather than assume —
        # if it is absent the test proves nothing about preserving it.
        _install_audit_guard_if_absent(url)
        yield url
    finally:
        _drop_db(name)


def test_pave_rebuilds_every_table_it_dumped(migrated_pg_db, tmp_path):
    url = migrated_pg_db

    before_tables = _table_names(url)
    before_counts = _counts(url)
    before_objects = _inventory(url)
    before_stamp = _sql(url, "SELECT version_num FROM alembic_version")[0][0]

    # The rig itself has to be capable of catching the defect: an
    # ORM-only database has nothing for pave to lose.
    assert "alembic_version" in before_tables
    assert "server_errors" in before_tables
    assert before_counts["users"] >= 1, "no admin row — bootstrap did not run"
    assert before_counts["tags"] >= 1, "no seeded rows — bootstrap did not run"
    assert before_counts["plug_fake_catalog"] == 4
    assert before_counts["plug_fake_aline"] == 2
    assert ("trigger", "plug_fake_catalog_touch ON plug_fake_catalog") in before_objects
    # A BEFORE ... FOR EACH ROW trigger only fires on a row, so an empty
    # audit_logs would make the enforcement assertion below vacuous.
    assert before_counts["audit_logs"] >= 1, "no audit rows — the guard assertion would be vacuous"
    for obj in AUDIT_GUARD_OBJECTS:
        assert obj in before_objects, f"rig lacks {obj} — it cannot prove the guard survives"
    assert ("matview", "mv_plug_skus") in before_objects
    assert ("view", "v_plug_skus") in before_objects
    assert ("view", "v_cheap_plug_parts") in before_objects
    # A matview restored empty is indistinguishable from a restored one by
    # name alone, so the rig's has to hold rows for the check below to bite.
    before_mv = _sql(url, "SELECT count(*) FROM mv_plug_skus")[0][0]
    assert before_mv == 3, "matview is empty — the row-preservation check would be vacuous"
    # Deliberately stale: 3 stored rows against 4 in the base table.
    assert before_counts["plug_fake_catalog"] != before_mv

    # BY SCRIPT PATH, from an unrelated cwd, with PYTHONPATH scrubbed: the
    # documented invocation, and the one that dropped the schema and then
    # died on `import gdx_dispatch.models`.
    env = {k: v for k, v in _child_env(url).items() if k != "PYTHONPATH"}
    r = subprocess.run(
        [sys.executable, str(PAVE_SCRIPT), "--yes", url],
        cwd=str(tmp_path), env=env, capture_output=True, text=True,
    )
    output = f"{r.stdout}\n{r.stderr}"
    assert r.returncode == 0, f"pave exited {r.returncode}\n{output[-6000:]}"

    after_tables = _table_names(url)
    lost = sorted(before_tables - after_tables)
    assert not lost, f"pave dumped these tables and did not rebuild them: {lost}"

    assert _counts(url) == before_counts, f"rows lost\n{output[-6000:]}"

    # `alembic upgrade head` re-stamps; paving from the same tree must land on
    # the same revision, and never on no revision at all.
    assert _sql(url, "SELECT version_num FROM alembic_version")[0][0] == before_stamp

    # The plugin table has to come back whole, not just as a bare CREATE TABLE.
    indexes = {r[0] for r in _sql(
        url, "SELECT indexname FROM pg_indexes WHERE tablename = 'plug_fake_catalog'"
    )}
    assert "ix_plug_fake_catalog_sku" in indexes
    assert (_sql(url, "SELECT last_value FROM plug_fake_catalog_id_seq")[0][0]
            == before_counts["plug_fake_catalog"])

    # The FK between the two plugin tables is part of "whole": without it the
    # child table is back but nothing stops an orphan row.
    assert _sql(url, """
        SELECT count(*) FROM pg_constraint
         WHERE conname = 'plug_fake_aline_cat_id_fkey' AND contype = 'f'
    """)[0][0] == 1

    # An unowned sequence has no table to be rebuilt with and no owner for step
    # 6 to reset it from, so its parameters and its value both have to survive
    # the capture/replay round trip.
    increment, cycles, maxvalue = _sql(url, """
        SELECT increment_by, cycle, max_value FROM pg_sequences
         WHERE schemaname = 'public' AND sequencename = 'plug_fake_unowned_seq'
    """)[0]
    assert (increment, cycles, maxvalue) == (5, True, 900)
    assert _sql(url, "SELECT last_value FROM plug_fake_unowned_seq")[0][0] == 100

    # `DROP SCHEMA public CASCADE` takes more than tables. Nothing in the ORM
    # or in a migration puts the audit-immutability guard back, and the
    # runtime role cannot, so losing it here is permanent.
    after_objects = _inventory(url)
    lost_objects = sorted(before_objects - after_objects)
    assert not lost_objects, f"pave dropped these non-table objects: {lost_objects}"

    # Present is not the same as enforcing — ARCHITECTURAL INVARIANT #2 is the
    # refusal, not the row in pg_trigger.
    for statement in ("UPDATE audit_logs SET action = 'tampered'", "DELETE FROM audit_logs"):
        with pytest.raises(psycopg2.errors.RaiseException, match="audit_logs is immutable"):
            _exec(url, statement)

    # A matview is derived data, but it is data: step 4c can only create it
    # before the rows are back, so something has to refresh it afterwards.
    # Nothing here would notice otherwise — a matview is not a base table, so
    # no row-count check covers it, and the catalog reports the empty one as
    # present and populated.
    # Populated, and refreshed against the reloaded base table. NOT compared
    # to the stale stored count: a matview is a cache, and the pave above had
    # to exit 0 despite that count changing — asserting 3 here would demand the
    # false alarm this check was rewritten to stop reporting.
    assert _sql(url, "SELECT count(*) FROM mv_plug_skus")[0][0] == before_counts["plug_fake_catalog"]
    assert _sql(url, "SELECT count(*) FROM v_plug_skus")[0][0] == before_counts["plug_fake_catalog"]

    # A matview's index is rebuilt by nothing else; UNIQUE is what makes
    # REFRESH ... CONCURRENTLY possible at all.
    mv_indexes = {r[0] for r in _sql(
        url, "SELECT indexname FROM pg_indexes WHERE tablename = 'mv_plug_skus'")}
    assert "ux_mv_plug_skus_id" in mv_indexes
    _exec(url, "REFRESH MATERIALIZED VIEW CONCURRENTLY mv_plug_skus")

    # security_invoker shares reloptions with check_option; carrying one and
    # dropping the other flips the view to the definer's rights in silence.
    assert _sql(url, """
        SELECT 'security_invoker=true' = ANY(c.reloptions)
          FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public' AND c.relname = 'v_invoker_plug_parts'
    """)[0][0] is True
    # …and the one that was never populated stays that way.
    assert _sql(url, """
        SELECT relispopulated FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public' AND c.relname = 'mv_never_refreshed'
    """)[0][0] is False

    # A CHECK OPTION lives in reloptions, not in pg_get_viewdef(). Assert the
    # refusal, not the catalog row: restoring the view without it leaves
    # something that looks identical and silently accepts a bad write.
    with pytest.raises(psycopg2.errors.WithCheckOptionViolation):
        _exec(url, "INSERT INTO v_cheap_plug_parts (sku, price) VALUES ('TOO-DEAR', 999)")

    # "⚠ PAVE COMPLETE WITH MISMATCHES" also contains "PAVE COMPLETE".
    assert "✅ PAVE COMPLETE" in output
    assert "MISMATCH" not in output
    assert "MISSING " not in output


def test_pave_refuses_an_object_kind_it_cannot_rebuild(migrated_pg_db, tmp_path):
    """The preflight is the whole reason the database is still there to lose.

    `unsupported_objects()` is what turns "pave cannot reproduce this" into a
    refusal while the schema still exists. Every other test of the replay
    patches `schema_inventory` out, so a typo in that 40-line catalog query
    would disarm the tool's headline safety feature with every test green.
    """
    url = migrated_pg_db
    # A domain type: `DROP SCHEMA public CASCADE` takes it, no migration and no
    # ORM puts it back, and pave has no DDL for one.
    _exec(url, "CREATE DOMAIN plug_fake_postcode AS text CHECK (VALUE ~ '^[0-9]{5}$')")

    before_tables = _table_names(url)
    before_counts = _counts(url)

    env = {k: v for k, v in _child_env(url).items() if k != "PYTHONPATH"}
    r = subprocess.run(
        [sys.executable, str(PAVE_SCRIPT), "--yes", url],
        cwd=str(tmp_path), env=env, capture_output=True, text=True,
    )
    output = f"{r.stdout}\n{r.stderr}"

    assert r.returncode == 1, f"pave should have refused\n{output[-4000:]}"
    assert "domain type plug_fake_postcode" in output
    assert "NOTHING has been dropped" in output
    # The refusal is only worth anything if it really refused.
    assert _table_names(url) == before_tables
    assert _counts(url) == before_counts
    assert "Dropping schema" not in output

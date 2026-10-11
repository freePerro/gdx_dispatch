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


# --- GDXA-531 ---------------------------------------------------------------


@pytest.fixture
def bare_pg_db():
    """An empty throwaway database, for the refusals that never reach a rebuild."""
    try:
        _admin_conn().close()
    except psycopg2.OperationalError as exc:
        _skip_unless_ci(f"PostgreSQL not reachable at {PG_HOST}:{PG_PORT}: {exc}")
    name = f"gdx_pave_{uuid.uuid4().hex[:12]}"
    _create_db(name)
    try:
        yield f"postgresql://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{name}"
    finally:
        _drop_db(name)


def _pave(url: str, cwd: Path, **env: str) -> tuple[int, str]:
    child = {k: v for k, v in _child_env(url, **env).items() if k != "PYTHONPATH"}
    r = subprocess.run([sys.executable, str(PAVE_SCRIPT), "--yes", url],
                       cwd=str(cwd), env=child, capture_output=True, text=True)
    return r.returncode, f"{r.stdout}\n{r.stderr}"


def test_an_extension_in_public_is_not_refused_as_its_own_members(bare_pg_db):
    """`CREATE EXTENSION pg_trgm` puts 10 operators, 2 operator classes and 2
    families in `public`. The unsupported-object list used to name every one
    of them, so any database with the extension could never be paved — while
    the replay already had the one statement that brings them all back."""
    from sqlalchemy import create_engine

    from gdx_dispatch.tools import pave_tenant_db

    url = bare_pg_db
    _exec(url, "CREATE EXTENSION pg_trgm SCHEMA public")
    # The rig has to hold the member kinds the old list refused.
    assert _sql(url, """
        SELECT count(*) FROM pg_operator o
         WHERE o.oprnamespace = 'public'::regnamespace
    """)[0][0] > 0
    engine = create_engine(url)
    try:
        assert pave_tenant_db.unsupported_objects(engine) == []
        inventory = pave_tenant_db.schema_inventory(engine)
        assert ("extension", "pg_trgm") in inventory
        assert not [o for o in inventory if o[0] == "function" and "similarity" in o[1]]
    finally:
        engine.dispose()


def _seed_unpaveable(url: str) -> None:
    _exec(url, "CREATE TABLE keep_me (id int); INSERT INTO keep_me VALUES (1), (2)")


def _untouched(url: str) -> bool:
    return _sql(url, "SELECT count(*) FROM keep_me")[0][0] == 2


@pytest.mark.parametrize("stamp", [None, "001_initial"])
def test_a_database_behind_head_is_refused_with_nothing_dropped(bare_pg_db, tmp_path, stamp):
    url = bare_pg_db
    _seed_unpaveable(url)
    if stamp:
        _exec(url, "CREATE TABLE alembic_version (version_num varchar(32) PRIMARY KEY);"  # noqa: S608 — stamp is a parametrize literal
                   f"INSERT INTO alembic_version VALUES ('{stamp}')")
    rc, output = _pave(url, tmp_path)
    assert rc == 1, output[-4000:]
    assert "this tree's head is" in output
    assert "NOTHING has been dropped" in output
    assert "Dropping schema" not in output
    assert _untouched(url)


@pytest.mark.parametrize("case", ["index_comment", "extension_member_acl", "defaults_for_role"])
def test_state_the_rebuild_would_lose_is_refused_with_nothing_dropped(bare_pg_db, tmp_path, case):
    """Each of these used to be lost after the DROP:
    - a hand-made index is not rebuilt, so its COMMENT ON failed and sent the
      operator to a full restore;
    - CREATE EXTENSION puts back a member's original privileges, undoing a
      REVOKE, and the pave passed;
    - default privileges FOR ROLE another role never apply to the tables the
      paving role creates."""
    from gdx_dispatch.tools import pave_tenant_db

    url = bare_pg_db
    _seed_unpaveable(url)
    # At head, so the stamp refusal that runs first lets it through to this one.
    values = ", ".join(f"('{h}')" for h in sorted(pave_tenant_db.alembic_heads()))
    _exec(url, "CREATE TABLE alembic_version (version_num varchar(32) PRIMARY KEY);"  # noqa: S608 — revision ids from this tree
               f"INSERT INTO alembic_version VALUES {values}")
    other = f"gdx_pave_other_{uuid.uuid4().hex[:8]}"
    setup, expected = {
        "index_comment": ("CREATE INDEX ix_hand_made_keep_me ON keep_me (id);"
                          "COMMENT ON INDEX ix_hand_made_keep_me IS 'hand'",
                          "comment on index public.ix_hand_made_keep_me"),
        "extension_member_acl": ("CREATE EXTENSION pg_trgm SCHEMA public;"
                                 "REVOKE EXECUTE ON FUNCTION similarity(text, text) FROM PUBLIC",
                                 "privileges on extension member function similarity(text,text) differ"),
        "defaults_for_role": (f"CREATE ROLE {other} NOLOGIN;"
                              f"ALTER DEFAULT PRIVILEGES FOR ROLE {other} IN SCHEMA public "
                              "GRANT SELECT ON TABLES TO PUBLIC",
                              f"default privileges FOR ROLE {other}"),
    }[case]
    try:
        _exec(url, setup)
        rc, output = _pave(url, tmp_path)
        assert rc == 1, output[-4000:]
        assert expected in output
        assert "NOTHING has been dropped" in output
        assert "Dropping schema" not in output
        assert _untouched(url)
    finally:
        if case == "defaults_for_role":
            _exec(url, f"DROP OWNED BY {other}; DROP ROLE {other}")


@pytest.mark.parametrize("in_schema, flagged", [(True, True), (False, False)])
def test_extension_members_are_flagged_only_when_recreating_them_differs(
        bare_pg_db, in_schema, flagged):
    """Default privileges in place at CREATE EXTENSION reach its members. A
    schema's are dropped with it and step 3b re-creates the extension before
    restoring them, so the members' grants would be reset; database-wide ones
    survive the DROP and come back identical, so refusing those would only
    make the database unpaveable."""
    from sqlalchemy import create_engine

    from gdx_dispatch.tools import pave_tenant_db

    url = bare_pg_db
    reader = f"gdx_pave_reader_{uuid.uuid4().hex[:8]}"
    scope = "IN SCHEMA public " if in_schema else ""
    try:
        _exec(url, f"CREATE ROLE {reader} NOLOGIN;"
                   f"ALTER DEFAULT PRIVILEGES {scope}GRANT EXECUTE ON FUNCTIONS TO {reader};"
                   "CREATE EXTENSION pg_trgm SCHEMA public")
        engine = create_engine(url)
        try:
            out = pave_tenant_db.privileges_pave_cannot_rebuild(
                engine, {"default_acl": set()})
        finally:
            engine.dispose()
        members = [line for line in out if "extension member function similarity(text,text)" in line]
        assert bool(members) == flagged, out
    finally:
        _exec(url, f"DROP OWNED BY {reader}; DROP ROLE {reader}")


def test_a_column_grant_on_an_extension_member_is_flagged(bare_pg_db):
    """Re-creating the extension drops a column grant on its view, and the
    member is not compared after the pave."""
    from sqlalchemy import create_engine

    from gdx_dispatch.tools import pave_tenant_db

    url = bare_pg_db
    reader = f"gdx_pave_reader_{uuid.uuid4().hex[:8]}"
    try:
        _exec(url, f"CREATE ROLE {reader} NOLOGIN;"
                   "CREATE EXTENSION pg_stat_statements SCHEMA public;"
                   f"GRANT SELECT (userid) ON public.pg_stat_statements TO {reader}")
        engine = create_engine(url)
        try:
            out = pave_tenant_db.privileges_pave_cannot_rebuild(engine, {"default_acl": set()})
        finally:
            engine.dispose()
        assert any("column userid of view pg_stat_statements" in line for line in out), out
    finally:
        _exec(url, f"DROP OWNED BY {reader}; DROP ROLE {reader}")


def test_an_extension_comes_back_at_the_version_it_was(bare_pg_db):
    """Without VERSION the replay installed default_version: an upgrade that
    nothing reported, since members are not compared after the pave."""
    from sqlalchemy import create_engine, text

    from gdx_dispatch.tools import pave_tenant_db

    url = bare_pg_db
    _exec(url, "CREATE EXTENSION pg_stat_statements SCHEMA public VERSION '1.4'")
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            ddl = {(k, key): d for k, key, d in conn.execute(pave_tenant_db._CAPTURE_OBJECTS_SQL)}
        _exec(url, "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
                   + ddl[("extension", "pg_stat_statements")])
        with engine.connect() as conn:
            assert conn.execute(text("SELECT extversion FROM pg_extension "
                                     "WHERE extname = 'pg_stat_statements'")).scalar() == "1.4"
    finally:
        engine.dispose()


def test_an_extension_this_server_cannot_reinstall_is_refused(bare_pg_db):
    """A version left behind by a major upgrade: step 3b's CREATE ... VERSION
    would fail after the DROP. The catalog row is edited to stand in for one."""
    from sqlalchemy import create_engine

    from gdx_dispatch.tools import pave_tenant_db

    url = bare_pg_db
    _exec(url, "CREATE EXTENSION pg_trgm SCHEMA public")
    engine = create_engine(url)
    try:
        assert pave_tenant_db.extensions_pave_cannot_reinstall(engine) == []
        _exec(url, "UPDATE pg_extension SET extversion = '0.9' WHERE extname = 'pg_trgm'")
        assert [item.split(" (")[0] for item in
                pave_tenant_db.extensions_pave_cannot_reinstall(engine)] == [
            "extension pg_trgm at version 0.9"]
    finally:
        engine.dispose()


def test_an_edited_extension_comment_is_refused(bare_pg_db):
    """COMMENT ON EXTENSION is invisible to the comment capture, and step 3b's
    CREATE EXTENSION would reset it: refused before the DROP instead."""
    from sqlalchemy import create_engine

    from gdx_dispatch.tools import pave_tenant_db

    url = bare_pg_db
    _exec(url, "CREATE EXTENSION pg_trgm SCHEMA public")
    engine = create_engine(url)
    try:
        assert pave_tenant_db.extensions_pave_cannot_reinstall(engine) == []
        _exec(url, "COMMENT ON EXTENSION pg_trgm IS 'mine'")
        assert [item.split(" (")[0] for item in
                pave_tenant_db.extensions_pave_cannot_reinstall(engine)] == [
            "extension pg_trgm comment"]
    finally:
        engine.dispose()


def test_the_late_exit_restore_gives_the_app_its_database_back(bare_pg_db, tmp_path):
    """What every exit after the DROP tells the operator to run, run as
    printed (`restore_steps`). The database is left as a late exit leaves it:
    the schema re-created by the paving role with no ACL, where pg_dump writes
    the public schema's ACL only as a diff from a default it no longer has,
    and a rebuilt table already holding some of its rows, which the full
    backup cannot reload over. Every row, a role that could read the table
    before, and the schema's owner and comment must be back."""
    from sqlalchemy import create_engine, text

    from gdx_dispatch.tools import pave_tenant_db

    if shutil.which("pg_dump") is None or shutil.which("psql") is None:
        _skip_unless_ci("pg_dump/psql not on PATH — the restore shells out to both")
    url = bare_pg_db
    reader = f"gdx_pave_reader_{uuid.uuid4().hex[:8]}"
    db_info = pave_tenant_db.parse_db_url(url)
    full = tmp_path / "d_full.sql"
    try:
        _exec(url, "CREATE TABLE kept (id int PRIMARY KEY); INSERT INTO kept VALUES (1), (2);"
                   "COMMENT ON SCHEMA public IS 'the shop'")
        _exec(url, f"CREATE ROLE {reader} NOLOGIN; GRANT SELECT ON kept TO {reader}")
        engine = create_engine(url)
        try:
            with engine.connect() as conn:
                before = conn.execute(text(
                    "SELECT pg_get_userbyid(nspowner), has_schema_privilege(:r, 'public', 'USAGE') "
                    "FROM pg_namespace WHERE nspname = 'public'").bindparams(r=reader)).one()
            assert before[1] is True, before
            pre = pave_tenant_db.privilege_state(engine)
        finally:
            engine.dispose()
        pave_tenant_db.dump_full(db_info, str(full))
        schema = pave_tenant_db.schema_restore_path(str(full))
        Path(schema).write_text(pave_tenant_db.schema_restore_sql(pre))
        _exec(url, "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
                   "CREATE TABLE kept (id int PRIMARY KEY); INSERT INTO kept VALUES (1)")
        steps = pave_tenant_db.restore_steps(db_info, str(full))
        assert [s[-1] for s in steps[1:]] == [schema, str(full)]
        for step in steps:
            r = subprocess.run(step, env={**os.environ, "PGPASSWORD": db_info["password"]},
                               capture_output=True, text=True)
            assert r.returncode == 0, r.stderr
        engine = create_engine(url)
        try:
            with engine.connect() as conn:
                after = conn.execute(text(
                    "SELECT pg_get_userbyid(nspowner), has_schema_privilege(:r, 'public', 'USAGE'), "
                    "has_table_privilege(:r, 'public.kept', 'SELECT'), "
                    "obj_description('public'::regnamespace, 'pg_namespace'), "
                    "(SELECT count(*) FROM public.kept) "
                    "FROM pg_namespace WHERE nspname = 'public'").bindparams(r=reader)).one()
        finally:
            engine.dispose()
        assert tuple(after) == (before[0], True, True, "the shop", 2)
    finally:
        _exec(url, f"DROP OWNED BY {reader}; DROP ROLE {reader}")


def test_a_member_grant_and_its_revoke_is_not_flagged(bare_pg_db):
    """GRANT then REVOKE leaves the ACL written out where it was NULL: the same
    privileges, which refusing would make the database unpaveable for good."""
    from sqlalchemy import create_engine

    from gdx_dispatch.tools import pave_tenant_db

    url = bare_pg_db
    reader = f"gdx_pave_reader_{uuid.uuid4().hex[:8]}"
    try:
        _exec(url, f"CREATE ROLE {reader} NOLOGIN; CREATE EXTENSION pg_trgm SCHEMA public;"
                   f"GRANT EXECUTE ON FUNCTION similarity(text, text) TO {reader};"
                   f"REVOKE EXECUTE ON FUNCTION similarity(text, text) FROM {reader}")
        assert _sql(url, "SELECT proacl IS NOT NULL FROM pg_proc "
                         "WHERE oid = 'public.similarity(text,text)'::regprocedure")[0] == (True,)
        engine = create_engine(url)
        try:
            assert pave_tenant_db.privileges_pave_cannot_rebuild(engine, {"default_acl": set()}) == []
        finally:
            engine.dispose()
    finally:
        _exec(url, f"DROP OWNED BY {reader}; DROP ROLE {reader}")


def test_ai_role_passwords_are_refused_with_nothing_dropped(bare_pg_db, tmp_path):
    url = bare_pg_db
    _seed_unpaveable(url)
    rc, output = _pave(url, tmp_path, TENANT_AI_READONLY_PASSWORD="r", TENANT_AI_WRITE_PASSWORD="w")
    assert rc == 1, output[-4000:]
    assert "ai_roles_bootstrap" in output and "NOTHING has been dropped" in output
    assert "Dropping schema" not in output
    assert _untouched(url)


def test_pave_keeps_owners_grants_comments_and_extensions(migrated_pg_db, tmp_path):
    """`DROP SCHEMA public CASCADE` takes the schema's owner, its ACL and its
    comment, every GRANT and owner in it, and every COMMENT; the rebuild puts
    back none of it and pave used to report success. Measured on 16.15: the
    re-created schema is owned by the paving role and PUBLIC has no USAGE, so
    a NOSUPERUSER runtime role could no longer see a single table.

    Asserted with catalog queries written here, not with the tool's own
    privilege_state(), so a capture that misses something cannot also be the
    thing that says nothing was missed."""
    url = migrated_pg_db
    role = f"gdx_pave_r_{uuid.uuid4().hex[:8]}"
    _exec(url, f"CREATE ROLE {role} NOLOGIN")
    try:
        _exec(url, f"""
            CREATE EXTENSION pg_trgm SCHEMA public;
            GRANT SELECT, INSERT ON TABLE users TO {role};
            GRANT SELECT (sku) ON TABLE plug_fake_catalog TO {role} WITH GRANT OPTION;
            ALTER TABLE plug_fake_aline OWNER TO {role};
            REVOKE EXECUTE ON FUNCTION plug_fake_touch() FROM PUBLIC;
            GRANT EXECUTE ON FUNCTION plug_fake_touch() TO {role};
            GRANT USAGE ON SEQUENCE plug_fake_unowned_seq TO {role};
            ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO {role};
            ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT EXECUTE ON FUNCTIONS TO {role};
            COMMENT ON TABLE plug_fake_catalog IS 'the plugin''s catalog \\ parts';
            COMMENT ON COLUMN plug_fake_catalog.sku IS 'stock keeping unit';
            COMMENT ON FUNCTION plug_fake_touch() IS 'touch';
            COMMENT ON TRIGGER plug_fake_catalog_touch ON plug_fake_catalog IS 'trg';
            COMMENT ON TABLE users IS 'staff accounts';
            DROP TABLE vendor_statement_lines CASCADE;
        """)  # noqa: S608 — role is generated above from a uuid

        def state():
            q = lambda s: _sql(url, s)[0]  # noqa: E731
            # An ACL is a set: replay grants before ALTER OWNER, which reorders it.
            return {
                "schema": q("""SELECT pg_get_userbyid(nspowner),
                                      (SELECT array_agg(a::text ORDER BY a::text)
                                         FROM unnest(nspacl) a)::text,
                                      obj_description(oid, 'pg_namespace')
                                 FROM pg_namespace WHERE nspname = 'public'"""),
                "aline_owner": q("""SELECT pg_get_userbyid(relowner) FROM pg_class
                                     WHERE oid = 'public.plug_fake_aline'::regclass"""),
                "privs": q(f"""SELECT
                    has_table_privilege('{role}', 'public.users', 'INSERT'),
                    has_table_privilege('{role}', 'public.users', 'UPDATE'),
                    has_column_privilege('{role}', 'public.plug_fake_catalog', 'sku',
                                         'SELECT WITH GRANT OPTION'),
                    has_column_privilege('{role}', 'public.plug_fake_catalog', 'price', 'SELECT'),
                    has_function_privilege('{role}', 'public.plug_fake_touch()', 'EXECUTE'),
                    has_function_privilege('public', 'public.plug_fake_touch()', 'EXECUTE'),
                    has_sequence_privilege('{role}', 'public.plug_fake_unowned_seq', 'USAGE')"""),
                "defacl": q("""SELECT count(*) FROM pg_default_acl
                                WHERE defaclnamespace = 'public'::regnamespace"""),
                "comments": q("""SELECT
                    obj_description('public.plug_fake_catalog'::regclass, 'pg_class'),
                    col_description('public.plug_fake_catalog'::regclass, 2),
                    obj_description('public.plug_fake_touch()'::regprocedure, 'pg_proc'),
                    (SELECT obj_description(oid, 'pg_trigger') FROM pg_trigger
                      WHERE tgname = 'plug_fake_catalog_touch'),
                    obj_description('public.users'::regclass, 'pg_class')"""),
                # Created before the default privileges, so its members hold
                # none of them; a CREATE EXTENSION after them would.
                "trgm": q("""SELECT count(*), bool_and(p.proacl IS NULL)
                               FROM pg_extension e
                               JOIN pg_depend d ON d.refobjid = e.oid AND d.deptype = 'e'
                               JOIN pg_proc p ON d.classid = 'pg_proc'::regclass
                                             AND p.oid = d.objid
                              WHERE e.extname = 'pg_trgm'"""),
            }

        before = state()
        # The rig must hold every thing the drop takes, or the test proves nothing.
        assert before["schema"][0] == "pg_database_owner"
        assert "=U/" in before["schema"][1]
        assert before["schema"][2] == "standard public schema"
        assert before["aline_owner"] == (role,)
        assert before["privs"] == (True, False, True, False, True, False, True)
        assert before["defacl"] == (2,)
        assert before["comments"] == ("the plugin's catalog \\ parts", "stock keeping unit",
                                      "touch", "trg", "staff accounts")
        assert before["trgm"][0] > 0 and before["trgm"][1] is True

        rc, output = _pave(url, tmp_path)
        assert rc == 0, output[-6000:]
        assert state() == before, output[-6000:]
        _sql(url, "SELECT similarity('door', 'doors')")
        assert "✅ PAVE COMPLETE" in output
        assert "MISMATCH" not in output
        assert "FAILED" not in output
        # A table the old database lacked is created by the rebuild under the
        # schema's default privileges, so they have to be back before it is.
        assert _sql(url, f"SELECT has_table_privilege('{role}', "  # noqa: S608 — role is generated above
                         "'public.vendor_statement_lines', 'SELECT')")[0] == (True,)
        # And what one pave leaves, the next accepts and keeps.
        rc, output = _pave(url, tmp_path)
        assert rc == 0, output[-6000:]
        assert state() == before, output[-6000:]
    finally:
        _exec(url, f"REASSIGN OWNED BY {role} TO CURRENT_USER; DROP OWNED BY {role}")
        _exec(url, f"DROP ROLE {role}")


@pytest.mark.parametrize("grant_options, schema_create, blocked", [
    ("WITH INHERIT TRUE, SET TRUE", True, False),
    # Passes pg_has_role(..., 'MEMBER') and still cannot ALTER ... OWNER TO.
    ("WITH INHERIT FALSE, SET FALSE", True, True),
    # ALTER ... OWNER TO also needs the new owner to hold CREATE on the schema.
    ("WITH INHERIT TRUE, SET TRUE", False, True),
])
def test_a_non_superuser_paver_restores_owners_or_refuses_first(
        bare_pg_db, grant_options, schema_create, blocked):
    """Every other test paves as a superuser and so never reaches this check.

    Where the preflight lets the pave through, the same non-superuser then
    drops the schema, re-creates the one table by hand and runs the step-4e
    replay. That proves the replay of owners and ACLs, not a whole pave:
    create_all, alembic, the reload and the rest never run as this role here.
    """
    from sqlalchemy import create_engine, text

    from gdx_dispatch.tools import pave_tenant_db

    url = bare_pg_db
    dbname = url.rsplit("/", 1)[1]
    tag = uuid.uuid4().hex[:8]
    owner, paver = f"gdx_pave_owner_{tag}", f"gdx_pave_paver_{tag}"
    # The public schema is owned by pg_database_owner, which only the
    # database's owner holds, so a paver has to own the database.
    _exec(url, f"CREATE ROLE {owner} NOLOGIN; CREATE ROLE {paver} LOGIN PASSWORD 'x';"
               f"ALTER DATABASE {dbname} OWNER TO {paver};"
               f"GRANT CREATE ON SCHEMA public TO {owner};"
               f"CREATE TABLE owned_elsewhere (id int); ALTER TABLE owned_elsewhere OWNER TO {owner};"
               f"GRANT {owner} TO {paver} {grant_options}")  # noqa: S608 — names generated above
    if not schema_create:
        _exec(url, f"REVOKE CREATE ON SCHEMA public FROM {owner}")
    engine = create_engine(url.replace(f"{PG_USER}:{PG_PASSWORD}@", f"{paver}:x@"))
    try:
        pre = pave_tenant_db.privilege_state(engine)
        assert pre["owners"][("table", "owned_elsewhere")][1] == owner
        assert pave_tenant_db.roles_pave_cannot_act_as(engine, pre) == ([owner] if blocked else [])
        if blocked:
            return
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
            conn.execute(text("CREATE TABLE public.owned_elsewhere (id int)"))
        assert pave_tenant_db.replay_privilege_state(engine, pre) == []
        post = pave_tenant_db.privilege_state(engine)
        assert pave_tenant_db.privilege_mismatches(pre, post) == []
        assert post["owners"][("schema", "public")][1] == "pg_database_owner"
    finally:
        engine.dispose()
        _exec(url, f"ALTER DATABASE {dbname} OWNER TO CURRENT_USER; DROP OWNED BY {owner};"
                   f"DROP OWNED BY {paver}; DROP ROLE {paver}; DROP ROLE {owner}")

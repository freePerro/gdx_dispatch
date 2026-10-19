#!/usr/bin/env python3
"""D1 Phase 2 — Nuke-and-pave the application database.

Drops the public schema and rebuilds it the way a container boot does —
``TenantBase.metadata.create_all()`` **then** ``alembic upgrade head`` — then
reloads the data dump it took first.

``DROP SCHEMA public CASCADE`` takes the whole schema. ``create_all()`` rebuilds
one slice of it. Everything between those two is what pave has to put back:

  1. **TenantBase tables** — every business table, rebuilt by ``create_all()``.
  2. **The Alembic base** — ``alembic_version``, ``tenants``,
     ``tenant_settings``, ``server_errors``, the game tables. Rebuilt by
     ``alembic upgrade head`` (step 4b), never by ``create_all()``.
  3. **Plugin tables** (``plug_*``, ``plugin_*``) — created at boot by
     ``schema_reconcile``, owned by neither the ORM nor a migration.
  4. **Non-table objects** — functions, triggers, views, enums, sequences,
     extensions. Some come back with a migration (the GL trigger set); some
     come back with nothing at all. ``audit_logs_immutable_guard`` and its two
     triggers are the ones that matter. Before migration 107 nothing but the
     runtime installed them, and the runtime role cannot, so a pave that
     dropped them left ``audit_logs`` UPDATE/DELETE-able with ARCHITECTURAL
     INVARIANT #2 unenforced. Step 4b now runs 107 on the empty schema; 107
     logs and carries on when its DDL is refused, so the step 4c replay is
     still what guarantees they come back.

Kinds 3 and 4 have their DDL captured BEFORE the drop (step 0b) and replayed
after it (step 4c); an object kind pave has no DDL for refuses the run before
anything is dropped. Pave used to rebuild only kind 1 while dumping all of
them, so the reload hit a table that no longer existed and —
``ON_ERROR_STOP=on`` — stopped there, leaving an empty database.

Usage (in a one-off app container, with every other database client stopped —
see USAGE below; the preflight refuses while any other client is connected):
    python -m gdx_dispatch.tools.pave_tenant_db --yes                 # pave DATABASE_URL
    python -m gdx_dispatch.tools.pave_tenant_db --yes <database_url>  # pave one DB by direct URL

The script-path form (``python gdx_dispatch/tools/pave_tenant_db.py --yes``)
works too — see the sys.path bootstrap below — but the module form is the
documented one because it cannot be run from the wrong directory.

Connect as a superuser. The data dump is taken with ``--disable-triggers``, so
its reload (step 5) runs ``ALTER TABLE ... DISABLE TRIGGER ALL``, which needs
superuser on any table with a foreign key, even for the table's owner. The
preflight refuses any other role before anything is dropped. In
``docker/docker-compose.yml`` the ``DATABASE_URL`` role is the database's
``POSTGRES_USER``, which is a superuser. A deployment that connects the app as
a narrower role has to pass a superuser URL as the argument.

Flags:
    --strict     (default) ON_ERROR_STOP=on; aborts on first reload error. Safe.
    --no-strict  legacy ON_ERROR_STOP=off; logs errors and continues. Risk: silent data loss.
    --terminate-others  CONNECTION LIMIT 0 for the run and terminate other
                 clients, instead of refusing while they are connected. Not a
                 fence against a superuser client (see "Other writers" below).

Steps per database:
    0.  Pre-pave row counts and table list
    0b. Preflight: refuse outright when TENANT_AI_*_PASSWORD is set (there is
        no role bootstrap to consume it), when the paving role is not a
        superuser (step 5 needs it, see below), when the database's alembic
        stamp is not this tree's head, while any other client is connected to
        the database, on an object kind or property pave has
        no DDL for, or on a comment it cannot re-issue. Then record the schema inventory, the owners, ACLs,
        schema default privileges and comments, and capture the DDL of every
        table create_all() will not rebuild plus every non-table object.
        Every failure here refuses with NOTHING dropped.
    1.  Dump data to /tmp/<dbname>_data.sql (alembic_version excluded — step 4b
        restamps it, and reloading the old stamp on top is a duplicate key)
    2.  Full backup to /tmp/<dbname>_full.sql
    3.  DROP SCHEMA public CASCADE; CREATE SCHEMA public — in one transaction
        that first refuses if a client has connected since 0b, and takes its
        locks under lock_timeout (a refusal there drops nothing)
    4.  TenantBase.metadata.create_all() — the ORM's tables
    4b. alembic upgrade head — the Alembic-base tables and the stamp
    4c. Replay captured DDL for whatever is still missing (plugin tables, the
        audit-immutability guard, anything else nothing else owns)
    4d. Refuse to reload if any pre-pave object is still missing
    4e. Restore owners, GRANTs (object and column), schema default privileges
        and comments to what step 0b recorded
    5.  Reload data from dump
    6.  Fix sequences (setval to max id)
    6b. REFRESH the materialized views step 4c had to create empty — they are
        built before the rows come back, so the catalog would report a
        populated-looking, empty matview as restored
    7.  Verify the schema inventory, the row counts, whether each matview is
        populated, and the owners, privileges, default privileges and
        comments against the pre-pave state, and any client connected at that
        moment (it came in during the rebuild; one that has already left is
        not seen); mismatches exit non-zero

Steps 4c, 4d and 4e run AFTER the drop. The preflight removes what can be known in
advance — an object of a kind pave has no DDL for — not the possibility of
failing late. Every late exit preserves /tmp/<dbname>_full.sql and prints the
psql commands that restore it (restore_steps: empty the schema first).
"""
from __future__ import annotations

import logging
import os
import re
import secrets
import shlex
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import create_engine, inspect, make_url, text
from sqlalchemy.exc import OperationalError

REPO_ROOT = Path(__file__).resolve().parents[2]

# Running this file BY PATH puts `tools/` on sys.path, not the repo root, so
# every `gdx_dispatch.*` import dies with ModuleNotFoundError. That mattered
# more here than in the sibling scanners carrying this same bootstrap: the
# model import used to sit INSIDE create_all_from_orm(), i.e. AFTER
# `DROP SCHEMA public CASCADE`, so the documented invocation dropped the
# schema and only then discovered it could not import the ORM to rebuild it.
# Nothing downstream of a DROP may have an unresolved import ahead of it —
# hence both the bootstrap and the module-scope imports below.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

USAGE = """Usage (in a one-off app container, every other database client stopped).
Use the compose invocation of the deployment that owns the database; for the
self-hosted stack that is update.sh's, from the repo root:
  C="docker compose -p gdx --env-file ./.env -f gdx_dispatch/docker/docker-compose.yml \\
     -f gdx_dispatch/docker/docker-compose.selfhost.yml"
  $C stop app plugin-host celery-high celery-low celery-beat   # every DB client it defines
  $C run --no-deps --entrypoint python app \\
      -m gdx_dispatch.tools.pave_tenant_db --yes        # no --rm: /tmp holds the backup
  $C start app plugin-host celery-high celery-low celery-beat
A `stop` aimed at the wrong project stops nothing and still exits 0; the
preflight then refuses, naming the clients still connected.

  python -m gdx_dispatch.tools.pave_tenant_db [--strict|--no-strict] [--terminate-others] --yes
  python -m gdx_dispatch.tools.pave_tenant_db [--strict|--no-strict] [--terminate-others] --yes <database_url>

  --yes        REQUIRED. This tool DROPS EVERY TABLE in the target database and
               rebuilds it from the ORM + `alembic upgrade head` (data reloaded
               from the dump it takes first). With no URL the target IS the
               application database.
               The URL's role must be a superuser: the reload runs
               `ALTER TABLE ... DISABLE TRIGGER ALL` (pg_dump --disable-triggers).
               Any other role is refused before anything is dropped.
  --strict     (default) abort on any reload error; preserves full backup.
  --no-strict  legacy behavior; logs errors but continues. Risk: silent data loss.
  --terminate-others
               Any other client connected to the database refuses the pave,
               with nothing dropped: it could write into the empty tables
               before the reload. This flag sets CONNECTION LIMIT 0 for the
               run and terminates them instead. Superusers ignore a
               connection limit, and every compose service connects as one,
               so a service still running reconnects and is refused again:
               stop the services."""

# Answered before the ORM import below, because that import pulls in the app's
# routers and so needs JWT_SECRET et al: `--help` is the one invocation that
# has to work in a bare shell. Nothing destructive lives above this line.
if __name__ == "__main__" and any(a in ("-h", "--help") for a in sys.argv[1:]):
    print(USAGE)
    raise SystemExit(1)

from alembic.config import Config as AlembicConfig  # noqa: E402
from alembic.script import ScriptDirectory  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import gdx_dispatch  # noqa: E402
import gdx_dispatch.models  # noqa: E402, F401 — registers every model on TenantBase.metadata
from gdx_dispatch.core.audit import TenantBase  # noqa: E402
from gdx_dispatch.models.pricing_engine import seed_default_pricing  # noqa: E402

# alembic.ini lives in the package root and its script_location is relative,
# so `alembic upgrade head` has to run from there — same as entrypoint.sh.
PACKAGE_DIR = Path(gdx_dispatch.__file__).resolve().parent

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


def parse_db_url(url: str) -> dict:
    """Extract components from a database URL."""
    parsed = urlparse(url)
    return {
        "host": parsed.hostname or "localhost",
        "port": str(parsed.port or 5432),
        "user": parsed.username or "gdx",
        "password": parsed.password or "",
        "dbname": parsed.path.lstrip("/"),
    }


def get_row_counts(engine) -> dict[str, int]:
    """Get row count for every table."""
    insp = inspect(engine)
    counts = {}
    with engine.connect() as conn:
        for table in sorted(insp.get_table_names()):
            try:
                row = conn.execute(text(f'SELECT COUNT(*) FROM "{table}"'))  # noqa: S608 — name comes from the database catalog (inspector), double-quoted; operator tool, no request path
                counts[table] = row.scalar()
            except Exception as e:
                log.warning("Could not count %s: %s", table, e)
                counts[table] = -1
    return counts


def get_table_names(engine) -> list[str]:
    """Get all table names."""
    return sorted(inspect(engine).get_table_names())


_MATVIEWS_SQL = text("""
    SELECT c.relname::text, c.relispopulated
      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public' AND c.relkind = 'm'
     ORDER BY c.relname
""")


def get_matview_states(engine) -> dict[str, bool]:
    """Populated-or-not per materialized view.

    Populated-ness, deliberately, not a row count. `inspect().get_table_names()`
    returns base tables only, so a matview is invisible to `get_row_counts` and
    therefore to `verify_counts` — a pave used to return one present and empty
    with a `✅`. But a matview is a *cache*: the normal state of one is stale,
    and step 6b refreshes it against the reloaded base tables, so its row count
    legitimately changes across a pave. Comparing counts made a correct pave
    report lost rows and exit non-zero, which for `pave && restart` is worse
    than the silence it replaced.
    """
    with engine.connect() as conn:
        return {name: bool(populated)
                for name, populated in conn.execute(_MATVIEWS_SQL).fetchall()}


def refresh_matviews(engine, populated_before: set[str]) -> list[str]:
    """REFRESH the matviews step 4c left unpopulated. Returns failures.

    Only the ones that held data before the pave. A matview that was already
    unpopulated is left that way: the job is to restore the database's prior
    state, and refreshing it would make step 7 report a mismatch for a
    difference pave itself introduced.

    In passes: a matview selecting from another matview cannot be refreshed
    until its source is populated, and the catalog records no ordering. A pass
    that makes no progress stops — better a named failure than a matview that
    is present, empty, and reported as restored.
    """
    with engine.connect() as conn:
        pending = [name for name, populated in conn.execute(_MATVIEWS_SQL).fetchall()
                   if not populated and name in populated_before]
    if not pending:
        return []
    log.info("--- Step 6b: refreshing %d materialized view(s) ---", len(pending))
    while pending:
        done, errors = [], {}
        for name in pending:
            try:
                with engine.connect() as conn:
                    conn.execute(text(f'REFRESH MATERIALIZED VIEW public."{name}"'))
                    conn.commit()
                done.append(name)
            except Exception as e:
                errors[name] = str(e).splitlines()[0]
        if not done:
            for name, err in errors.items():
                log.error("  REFRESH FAILED %s: %s", name, err)
            return sorted(pending)
        pending = [n for n in pending if n not in done]
    log.info("Materialized views refreshed.")
    return []


def dump_data(db_info: dict, output_path: str,
              exclude_tables: tuple[str, ...] = ("alembic_version",)) -> None:
    """pg_dump --data-only to a file.

    `alembic_version` is excluded by default because step 4b's
    `alembic upgrade head` writes that row itself, at the head the *code being
    paved from* defines. Reloading the dumped stamp on top of it is a
    duplicate-key ERROR, and under --strict that aborts the whole reload on
    the 4th COPY — nothing after it lands.
    """
    flags = ["--data-only", "--disable-triggers", "--no-owner", "--no-privileges"]
    for table in exclude_tables:
        flags += ["--exclude-table", f"public.{table}"]
    log.info("Dumping data to %s (excluding %s) ...",
             output_path, ", ".join(exclude_tables) or "nothing")
    _pg_dump(db_info, flags, output_path, "pg_dump failed")
    log.info("Data dump complete: %s", output_path)


def _pg_env(db_info: dict) -> dict[str, str]:
    """The environment every pg_dump/psql child runs with.

    PATH rides along on purpose. Without it the bare `pg_dump`/`psql` name
    resolves through os.defpath (`/bin:/usr/bin`), where Debian's pg_wrapper,
    given `-h`, always picks the newest client installed. Whoever runs pave can
    then never choose the client that matches the server's major, and a newer
    pg_dump writes GUCs (transaction_timeout) that an older server refuses
    under ON_ERROR_STOP (GDXA-1169).
    """
    return {"PGPASSWORD": db_info["password"], "PATH": os.environ.get("PATH", os.defpath)}


def _pg_dump(db_info: dict, flags: list[str], output_path: str, failure: str) -> None:
    """Run pg_dump with `flags` into `output_path`; exit on failure (nothing is dropped yet)."""
    cmd = ["pg_dump", "-h", db_info["host"], "-p", db_info["port"], "-U", db_info["user"],
           *flags, db_info["dbname"], "-f", output_path]
    r = subprocess.run(cmd, env=_pg_env(db_info),
                       capture_output=True, text=True)
    if r.returncode != 0:
        log.error("%s: %s", failure, r.stderr)
        sys.exit(1)


def unrecreatable_tables(engine) -> list[str]:
    """Live tables `TenantBase.metadata.create_all()` will not rebuild.

    That is the Alembic base AND the plugin tables together, deliberately:
    capturing the DDL of a table `alembic upgrade head` rebuilds anyway costs
    one pg_dump and is thrown away by step 4c (which only replays what is
    still missing), while *missing* one is how a pave loses a table. Erring
    towards over-capture keeps this list from being load-bearing.
    """
    return sorted(set(get_table_names(engine)) - set(TenantBase.metadata.tables))


def capture_table_schemas(db_info: dict, tables: list[str]) -> dict[tuple[str, str], str]:
    """`pg_dump --schema-only -t <table>`, one file per table, BEFORE any drop.

    One file per table rather than one file for all of them so step 4c can
    replay a subset — the Alembic-base tables come back on their own and must
    not be replayed on top of themselves.

    A capture failure exits here, with the database untouched. This is the
    preflight: the tool discovers what it cannot rebuild *before* it destroys
    anything, not after.
    """
    captured: dict[tuple[str, str], str] = {}
    if not tables:
        return captured
    log.info("  capturing DDL for %d table(s) create_all() will not rebuild", len(tables))
    env = _pg_env(db_info)
    for table in tables:
        path = f"/tmp/gdxpave_schema_{db_info['dbname']}_{table}.sql"
        cmd = [
            "pg_dump",
            "-h", db_info["host"],
            "-p", db_info["port"],
            "-U", db_info["user"],
            "--schema-only",
            "--no-owner",
            "--no-privileges",
            "-t", f"public.{table}",
            db_info["dbname"],
            "-f", path,
        ]
        r = subprocess.run(cmd, env=env, capture_output=True, text=True)
        if r.returncode != 0:
            log.error("pg_dump --schema-only failed for %s: %s", table, r.stderr)
            log.error("Refusing to pave: NOTHING has been dropped. The database is untouched.")
            sys.exit(1)
        captured[("table", table)] = path
    log.info("  captured: %s", ", ".join(tables))
    return captured


# `DROP SCHEMA public CASCADE` takes everything in the schema, not just tables,
# and only tables are rebuilt by create_all()/Alembic. On a real tenant database
# that difference is `audit_logs_immutable_guard` and its two triggers — the
# DB-level enforcement of ARCHITECTURAL INVARIANT #2 (audit immutability).
# Step 4b's `alembic upgrade head` runs migration 107, which installs them —
# but 107 logs and carries on when its DDL is refused rather than fail the
# upgrade, and `core/audit.py`'s runtime install (retried every few minutes
# since GDXA-352, logged at ERROR when it fails) expects a NOSUPERUSER runtime
# role, `gdx_app`, which cannot issue the DDL at all — whether a given
# deployment has made that switch is D97 Phase 1, still open in
# docs/d97_rls_runbook.md. Before 107 a pave that dropped them reported
# `✅ PAVE COMPLETE` with audit_logs freely UPDATE/DELETE-able.
#
# Every non-table object therefore gets its DDL read out of the catalog before
# the drop, and step 4c puts back whatever the rebuild did not. Over-capture is
# free: an object the rebuild recreates is simply skipped.


def _not_owned_sequence(oid: str) -> str:
    """SQL: the relation is not a sequence owned by a column (deptype 'a'/'i').

    Such a sequence belongs to its table: it is rebuilt with it and re-owned
    with it, and PostgreSQL refuses to re-own it on its own.
    """
    return (f"NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = {oid} "  # noqa: S608 — oid/catalog are this module's literals
            "AND d.classid = 'pg_class'::regclass AND d.refclassid = 'pg_class'::regclass "
            "AND d.deptype IN ('a', 'i'))")


def _not_extension_member(catalog: str, oid: str) -> str:
    """SQL: the object is not a member of an extension (pg_depend deptype 'e').

    An extension's members are the extension's, not the schema's: replaying
    the captured `CREATE EXTENSION` brings every one of them back, and none of
    them has DDL of its own that replays. `CREATE EXTENSION pg_trgm SCHEMA
    public` puts 31 functions, 10 operators, 2 operator classes and 2 operator
    families into `public` (PostgreSQL 16.15, measured 2026-10-11). Counted as
    the schema's own objects, the operators made the preflight refuse the pave
    while `schema_inventory()` listed the extension itself as rebuildable, and
    the refusal told the operator to export them by hand.
    """
    return (f"NOT EXISTS (SELECT 1 FROM pg_depend e WHERE e.classid = '{catalog}'::regclass "  # noqa: S608 — oid/catalog are this module's literals
            f"AND e.objid = {oid} AND e.deptype = 'e')")


_CAPTURE_OBJECTS_SQL = text(f"""
    -- pg_get_viewdef() returns the SELECT and nothing else. Everything that
    -- makes a view more than a query lives in pg_class.reloptions:
    -- `check_option` (a view that refuses a bad write vs one that accepts it)
    -- and `security_invoker` / `security_barrier` (caller's rights vs the
    -- definer's). All of them are carried through verbatim as a WITH list
    -- rather than one clause at a time, so a reloption nobody thought of here
    -- is preserved instead of silently dropped. The viewdef's trailing
    -- semicolon is stripped so `AS` stays last.
    SELECT 'view' AS kind, c.relname::text AS key,
           'CREATE VIEW public.' || quote_ident(c.relname) ||
           COALESCE(' WITH (' || array_to_string(c.reloptions, ', ') || ')', '') || ' AS ' ||
           regexp_replace(pg_get_viewdef(c.oid, true), ';\\s*$', '') AS ddl
      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public' AND c.relkind = 'v'
       AND {_not_extension_member('pg_class', 'c.oid')}
    UNION ALL
    -- An index on a matview. Nothing else rebuilds one: create_all() does not
    -- know the matview exists and the capture above is a bare definition. A
    -- lost UNIQUE index also permanently breaks REFRESH ... CONCURRENTLY.
    -- (Indexes on TABLES are deliberately excluded — rebuilding those from the
    -- ORM, under the ORM's names, is the point of a pave; a captured table's
    -- own pg_dump file carries its indexes.)
    SELECT 'matview index', i.relname::text, pg_get_indexdef(i.oid)
      FROM pg_index x
      JOIN pg_class i ON i.oid = x.indexrelid
      JOIN pg_class t ON t.oid = x.indrelid
      JOIN pg_namespace n ON n.oid = i.relnamespace
     WHERE n.nspname = 'public' AND t.relkind = 'm'
    UNION ALL
    -- WITH NO DATA deliberately. Step 4c runs before the data reload, so a
    -- populated CREATE would scan empty tables and store an empty result that
    -- looks identical to a correct one in the catalog. Step 6b refreshes every
    -- unpopulated matview once the rows are back.
    SELECT 'matview', c.relname::text,
           'CREATE MATERIALIZED VIEW public.' || quote_ident(c.relname) || ' AS ' ||
           regexp_replace(pg_get_viewdef(c.oid, true), ';\\s*$', '') || ' WITH NO DATA'
      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public' AND c.relkind = 'm'
       AND {_not_extension_member('pg_class', 'c.oid')}
    UNION ALL
    -- Only UNOWNED sequences. A sequence owned by a column comes back with its
    -- table — from create_all(), from Alembic, or from that table's captured
    -- pg_dump file, which carries its own `CREATE SEQUENCE` with no
    -- IF NOT EXISTS. Capturing it here as well used to create it first and
    -- then make the table's file unreplayable ("relation ..._id_seq already
    -- exists") on every subsequent pass: step 4c ran out of progress and
    -- exited, after the DROP. Verified against PostgreSQL 16.15, 2026-09-24.
    -- Parameters are carried because a bare CREATE SEQUENCE silently resets a
    -- non-default increment or cycle; the value itself comes back with the
    -- data reload's setval.
    SELECT 'sequence', c.relname::text,
           'CREATE SEQUENCE IF NOT EXISTS public.' || quote_ident(c.relname) ||
           ' INCREMENT BY ' || s.seqincrement ||
           ' MINVALUE ' || s.seqmin || ' MAXVALUE ' || s.seqmax ||
           ' START WITH ' || s.seqstart || ' CACHE ' || s.seqcache ||
           CASE WHEN s.seqcycle THEN ' CYCLE' ELSE ' NO CYCLE' END
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
      JOIN pg_sequence s ON s.seqrelid = c.oid
     WHERE n.nspname = 'public' AND c.relkind = 'S'
       AND {_not_extension_member('pg_class', 'c.oid')}
       AND {_not_owned_sequence('c.oid')}
    UNION ALL
    SELECT 'function', p.oid::regprocedure::text, pg_get_functiondef(p.oid)
      FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname = 'public' AND p.prokind IN ('f', 'p')
       AND {_not_extension_member('pg_proc', 'p.oid')}
    UNION ALL
    SELECT 'trigger', t.tgname || ' ON ' || c.relname, pg_get_triggerdef(t.oid)
      FROM pg_trigger t
      JOIN pg_class c ON c.oid = t.tgrelid
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public' AND NOT t.tgisinternal
    UNION ALL
    SELECT 'enum', t.typname::text,
           'CREATE TYPE public.' || quote_ident(t.typname) || ' AS ENUM (' ||
           (SELECT string_agg(quote_literal(e.enumlabel), ', ' ORDER BY e.enumsortorder)
              FROM pg_enum e WHERE e.enumtypid = t.oid) || ')'
      FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
     WHERE n.nspname = 'public' AND t.typtype = 'e'
       AND {_not_extension_member('pg_type', 't.oid')}
    UNION ALL
    SELECT 'extension', e.extname::text,
           -- At its installed version: without VERSION the rebuild installs
           -- default_version, an upgrade nothing reports (members are not compared).
           'CREATE EXTENSION IF NOT EXISTS ' || quote_ident(e.extname) || ' SCHEMA public'
             || ' VERSION ' || quote_literal(e.extversion)
      FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
     WHERE n.nspname = 'public'
""")  # noqa: S608 — interpolates only this module's SQL fragments, no input; operator tool

# The same object kinds, plus tables, as bare identities — used before the drop
# to record what has to come back and after the rebuild to prove it did.
_INVENTORY_SQL = text(f"""
    SELECT 'table' AS kind, c.relname::text AS key
      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
    UNION ALL
    SELECT CASE c.relkind WHEN 'v' THEN 'view' WHEN 'm' THEN 'matview' ELSE 'sequence' END,
           c.relname::text
      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public' AND c.relkind IN ('v', 'm', 'S')
       AND {_not_extension_member('pg_class', 'c.oid')}
    UNION ALL
    SELECT 'matview index', i.relname::text
      FROM pg_index x
      JOIN pg_class i ON i.oid = x.indexrelid
      JOIN pg_class t ON t.oid = x.indrelid
      JOIN pg_namespace n ON n.oid = i.relnamespace
     WHERE n.nspname = 'public' AND t.relkind = 'm'
    UNION ALL
    SELECT 'function', p.oid::regprocedure::text
      FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname = 'public' AND p.prokind IN ('f', 'p')
       AND {_not_extension_member('pg_proc', 'p.oid')}
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
       AND {_not_extension_member('pg_type', 't.oid')}
    UNION ALL
    SELECT 'extension', e.extname::text
      FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
     WHERE n.nspname = 'public'
""")  # noqa: S608 — interpolates only this module's SQL fragments, no input; operator tool

# Object kinds this tool has no DDL for. Their presence refuses the pave
# BEFORE the drop rather than discovering the loss after it.
#
# This is an ENUMERATED list, not a complement: a schema-scoped kind that is
# neither captured above nor named here is silently lost, and PostgreSQL adds
# kinds. A new one belongs in this list (refuse) or in the capture (rebuild) —
# never in neither. Table indexes and constraints are the one deliberate
# omission from both this list and the inventory: rebuilding them from the ORM,
# under the ORM's names, is the point of a pave.
_UNSUPPORTED_OBJECTS_SQL = text(f"""
    SELECT 'domain type' AS kind, t.typname::text AS key
      FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
     WHERE n.nspname = 'public' AND t.typtype = 'd'
       AND {_not_extension_member('pg_type', 't.oid')}
    UNION ALL
    SELECT 'range type', t.typname::text
      FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
     WHERE n.nspname = 'public' AND t.typtype = 'r'
       AND {_not_extension_member('pg_type', 't.oid')}
    UNION ALL
    SELECT 'standalone composite type', t.typname::text
      FROM pg_type t
      JOIN pg_namespace n ON n.oid = t.typnamespace
      LEFT JOIN pg_class c ON c.oid = t.typrelid
     WHERE n.nspname = 'public' AND t.typtype = 'c'
       AND (c.relkind IS NULL OR c.relkind = 'c')
       AND {_not_extension_member('pg_type', 't.oid')}
    UNION ALL
    SELECT 'aggregate or window function', p.oid::regprocedure::text
      FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname = 'public' AND p.prokind IN ('a', 'w')
       AND {_not_extension_member('pg_proc', 'p.oid')}
    UNION ALL
    SELECT 'row-level security policy', pol.polname || ' ON ' || c.relname
      FROM pg_policy pol
      JOIN pg_class c ON c.oid = pol.polrelid
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public'
    UNION ALL
    SELECT 'rule', r.rulename || ' ON ' || c.relname
      FROM pg_rewrite r
      JOIN pg_class c ON c.oid = r.ev_class
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public' AND r.rulename <> '_RETURN'
    UNION ALL
    SELECT 'foreign table', c.relname::text
      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public' AND c.relkind = 'f'
       AND {_not_extension_member('pg_class', 'c.oid')}
    UNION ALL
    SELECT 'collation', c.collname::text
      FROM pg_collation c JOIN pg_namespace n ON n.oid = c.collnamespace
     WHERE n.nspname = 'public'
       AND {_not_extension_member('pg_collation', 'c.oid')}
    UNION ALL
    SELECT 'text search configuration', t.cfgname::text
      FROM pg_ts_config t JOIN pg_namespace n ON n.oid = t.cfgnamespace
     WHERE n.nspname = 'public'
       AND {_not_extension_member('pg_ts_config', 't.oid')}
    UNION ALL
    SELECT 'text search dictionary', t.dictname::text
      FROM pg_ts_dict t JOIN pg_namespace n ON n.oid = t.dictnamespace
     WHERE n.nspname = 'public'
       AND {_not_extension_member('pg_ts_dict', 't.oid')}
    UNION ALL
    SELECT 'text search parser', t.prsname::text
      FROM pg_ts_parser t JOIN pg_namespace n ON n.oid = t.prsnamespace
     WHERE n.nspname = 'public'
       AND {_not_extension_member('pg_ts_parser', 't.oid')}
    UNION ALL
    SELECT 'text search template', t.tmplname::text
      FROM pg_ts_template t JOIN pg_namespace n ON n.oid = t.tmplnamespace
     WHERE n.nspname = 'public'
       AND {_not_extension_member('pg_ts_template', 't.oid')}
    UNION ALL
    SELECT 'operator', o.oid::regoperator::text
      FROM pg_operator o JOIN pg_namespace n ON n.oid = o.oprnamespace
     WHERE n.nspname = 'public'
       AND {_not_extension_member('pg_operator', 'o.oid')}
    UNION ALL
    SELECT 'operator class', c.opcname::text
      FROM pg_opclass c JOIN pg_namespace n ON n.oid = c.opcnamespace
     WHERE n.nspname = 'public'
       AND {_not_extension_member('pg_opclass', 'c.oid')}
    UNION ALL
    SELECT 'operator family', f.opfname::text
      FROM pg_opfamily f JOIN pg_namespace n ON n.oid = f.opfnamespace
     WHERE n.nspname = 'public'
       AND {_not_extension_member('pg_opfamily', 'f.oid')}
    UNION ALL
    SELECT 'extended statistics', s.stxname::text
      FROM pg_statistic_ext s JOIN pg_namespace n ON n.oid = s.stxnamespace
     WHERE n.nspname = 'public'
       AND {_not_extension_member('pg_statistic_ext', 's.oid')}
    UNION ALL
    SELECT 'conversion', c.conname::text
      FROM pg_conversion c JOIN pg_namespace n ON n.oid = c.connamespace
     WHERE n.nspname = 'public'
       AND {_not_extension_member('pg_conversion', 'c.oid')}
    UNION ALL
    -- Not objects but properties of one, which the DROP takes and nothing
    -- rebuilds: create_all() and the migrations never ENABLE row security, so
    -- a table that had it switched on comes back open to every role with a
    -- grant on it, and a pave cannot see the difference by name.
    SELECT 'row-level security switched on for table', c.relname::text
      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public' AND (c.relrowsecurity OR c.relforcerowsecurity)
    UNION ALL
    SELECT 'security label', o.identity
      FROM pg_seclabel l
      CROSS JOIN LATERAL pg_identify_object(l.classoid, l.objoid, l.objsubid) o
     WHERE o.schema = 'public' OR o.identity = 'public'
""")  # noqa: S608 — interpolates only this module's SQL fragments, no input; operator tool


def schema_inventory(engine) -> set[tuple[str, str]]:
    """Every (kind, identity) in `public` that a pave has to put back."""
    with engine.connect() as conn:
        return {(row[0], row[1]) for row in conn.execute(_INVENTORY_SQL)}


def unsupported_objects(engine) -> list[str]:
    """Objects in `public` this tool cannot rebuild. Non-empty means refuse."""
    with engine.connect() as conn:
        return sorted(f"{row[0]} {row[1]}" for row in conn.execute(_UNSUPPORTED_OBJECTS_SQL))


def capture_schema_objects(db_info: dict, engine) -> dict[tuple[str, str], str]:
    """Read every non-table object's DDL out of the catalog, before any drop."""
    captured: dict[tuple[str, str], str] = {}
    with engine.connect() as conn:
        rows = conn.execute(_CAPTURE_OBJECTS_SQL).fetchall()
    for kind, key, ddl in rows:
        if not ddl:
            # Nothing to replay it with; the pre-drop refusal is the honest
            # answer, not a half-capture that fails after the schema is gone.
            log.error("No DDL available for %s %s", kind, key)
            log.error("Refusing to pave: NOTHING has been dropped. The database is untouched.")
            sys.exit(1)
        safe = re.sub(r"[^A-Za-z0-9_]+", "_", f"{kind}_{key}")
        path = f"/tmp/gdxpave_obj_{db_info['dbname']}_{safe}.sql"
        with open(path, "w") as fh:
            fh.write(ddl.rstrip().rstrip(";") + ";\n")
        captured[(kind, key)] = path
    log.info("  captured DDL for %d non-table object(s) in public", len(captured))
    return captured


# --------------------------------------------------------------------------
# Who owns what, who may do what, and what it says about itself (GDXA-531).
#
# Every object the replay and the ORM rebuild bring back is owned by the role
# running the pave, carries that role's default privileges and has no comment.
# `pg_dump --no-owner --no-privileges` (every dump above) records none of it,
# and the name-only inventory could not tell the difference, so a pave that
# stripped every GRANT logged "PAVE COMPLETE" and exited 0. The schema itself
# goes with it: on PostgreSQL 16.15 (measured 2026-10-11) `CREATE SCHEMA
# public` comes back owned by the paving role with no ACL at all, where the
# dropped one was owned by pg_database_owner and granted USAGE to PUBLIC. Once
# the runtime role is NOSUPERUSER (D97), that is every query refused.
#
# Captured as ABSOLUTE state, not as pg_dump's diff against a default:
# pg_dump writes the public schema's ACL relative to its pg_init_privs entry,
# and the re-created schema has none, so replaying that diff would restore
# exactly nothing in the case that locks the application out. Each ACL below is
# COALESCEd with acldefault() so a NULL ("the defaults for this owner") and the
# same privileges spelled out compare equal.
#
# Extension members are left out of owners and ACLs: CREATE EXTENSION sets
# them, and an extension's members cannot be re-owned one by one. A member
# whose privileges were changed since refuses the pave instead
# (privileges_pave_cannot_rebuild).
# --------------------------------------------------------------------------

_RELKIND_KIND = ("CASE c.relkind WHEN 'v' THEN 'view' WHEN 'm' THEN 'matview' "
                 "WHEN 'S' THEN 'sequence' ELSE 'table' END")
_ROUTINE_TARGET = ("'public.' || quote_ident(p.proname) || '(' || "
                   "pg_get_function_identity_arguments(p.oid) || ')'")

# (kind, key, ALTER target, owner). Keys match _INVENTORY_SQL's. A sequence
# owned by a column is left out: PostgreSQL moves it with its table and refuses
# to re-own it on its own.
_OWNERS_SQL = text(f"""
    SELECT 'schema' AS kind, n.nspname::text AS key, 'SCHEMA public' AS target,
           quote_ident(pg_get_userbyid(n.nspowner)) AS owner
      FROM pg_namespace n WHERE n.nspname = 'public'
    UNION ALL
    SELECT {_RELKIND_KIND}, c.relname::text,
           CASE c.relkind WHEN 'v' THEN 'VIEW ' WHEN 'm' THEN 'MATERIALIZED VIEW '
                          WHEN 'S' THEN 'SEQUENCE ' ELSE 'TABLE ' END
             || 'public.' || quote_ident(c.relname),
           quote_ident(pg_get_userbyid(c.relowner))
      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm', 'S')
       AND {_not_extension_member('pg_class', 'c.oid')}
       AND {_not_owned_sequence('c.oid')}
    UNION ALL
    SELECT 'function', p.oid::regprocedure::text, 'ROUTINE ' || {_ROUTINE_TARGET},
           quote_ident(pg_get_userbyid(p.proowner))
      FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname = 'public' AND p.prokind IN ('f', 'p')
       AND {_not_extension_member('pg_proc', 'p.oid')}
    UNION ALL
    SELECT 'enum', t.typname::text, 'TYPE public.' || quote_ident(t.typname),
           quote_ident(pg_get_userbyid(t.typowner))
      FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
     WHERE n.nspname = 'public' AND t.typtype = 'e'
       AND {_not_extension_member('pg_type', 't.oid')}
""")  # noqa: S608 — interpolates only this module's SQL fragments, no input; operator tool

# (kind, key, GRANT target, column clause, grantee, privilege, grantable), one
# row per privilege. An object whose ACL is empty still yields one row with a
# NULL grantee, so "this object existed and nobody could use it" is recorded
# rather than indistinguishable from "this object was not looked at". Every
# column of every relation is listed for the same reason; a column with no
# column-level grant is the usual case.
_ACL_SQL = text(f"""
    WITH objs(kind, key, target, cols, acl) AS (
        SELECT 'schema', n.nspname::text, 'SCHEMA public', '',
               COALESCE(n.nspacl, acldefault('n', n.nspowner))
          FROM pg_namespace n WHERE n.nspname = 'public'
        UNION ALL
        SELECT {_RELKIND_KIND}, c.relname::text,
               CASE WHEN c.relkind = 'S' THEN 'SEQUENCE ' ELSE 'TABLE ' END
                 || 'public.' || quote_ident(c.relname), '',
               COALESCE(c.relacl, acldefault(
                   CASE WHEN c.relkind = 'S' THEN 's' ELSE 'r' END::"char", c.relowner))
          FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm', 'S')
           AND {_not_extension_member('pg_class', 'c.oid')}
        UNION ALL
        SELECT 'column', c.relname::text || '.' || a.attname::text,
               'TABLE public.' || quote_ident(c.relname), ' (' || quote_ident(a.attname) || ')',
               a.attacl
          FROM pg_attribute a
          JOIN pg_class c ON c.oid = a.attrelid
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm')
           AND a.attnum > 0 AND NOT a.attisdropped
           AND {_not_extension_member('pg_class', 'c.oid')}
        UNION ALL
        SELECT 'function', p.oid::regprocedure::text, 'ROUTINE ' || {_ROUTINE_TARGET}, '',
               COALESCE(p.proacl, acldefault('f', p.proowner))
          FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
         WHERE n.nspname = 'public' AND p.prokind IN ('f', 'p')
           AND {_not_extension_member('pg_proc', 'p.oid')}
        UNION ALL
        SELECT 'enum', t.typname::text, 'TYPE public.' || quote_ident(t.typname), '',
               COALESCE(t.typacl, acldefault('T', t.typowner))
          FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
         WHERE n.nspname = 'public' AND t.typtype = 'e'
           AND {_not_extension_member('pg_type', 't.oid')}
    )
    SELECT o.kind, o.key, o.target, o.cols,
           CASE WHEN x.grantee IS NULL THEN NULL
                WHEN x.grantee = 0 THEN 'PUBLIC'
                ELSE quote_ident(pg_get_userbyid(x.grantee)) END,
           x.privilege_type, x.is_grantable
      FROM objs o
      -- aclexplode() raises on an empty array, which is a real ACL (an owner
      -- who revoked their own privileges and granted none): no grants, no rows.
      LEFT JOIN LATERAL aclexplode(CASE WHEN cardinality(o.acl) > 0 THEN o.acl END) x ON true
""")  # noqa: S608 — interpolates only this module's SQL fragments, no input; operator tool

# `ALTER DEFAULT PRIVILEGES ... IN SCHEMA public` rows. They depend on the
# schema and the DROP takes them (measured, PostgreSQL 16.15). Global defaults
# (defaclnamespace = 0) are untouched by a pave and are not read here.
_DEFAULT_ACL_SQL = text("""
    SELECT quote_ident(pg_get_userbyid(d.defaclrole)), d.defaclobjtype::text,
           CASE WHEN x.grantee = 0 THEN 'PUBLIC'
                ELSE quote_ident(pg_get_userbyid(x.grantee)) END,
           x.privilege_type, x.is_grantable
      FROM pg_default_acl d
      JOIN pg_namespace n ON n.oid = d.defaclnamespace
      CROSS JOIN LATERAL aclexplode(d.defaclacl) x
     WHERE n.nspname = 'public'
""")
_DEFAULT_ACL_OBJECTS = {"r": "TABLES", "S": "SEQUENCES", "f": "FUNCTIONS", "T": "TYPES"}

# Every comment on the schema or on something in it, with the COMMENT ON
# keyword its pg_identify_object() type needs. pg_identify_object()'s `schema`
# column is NULL for a trigger and for the schema itself, so membership is
# decided per catalog instead. A comment whose type has no keyword here comes
# back with `kw` NULL and refuses the pave before the drop.
_COMMENTS_SQL = text("""
    SELECT o.type, o.identity, d.description,
           CASE o.type
             WHEN 'schema' THEN 'SCHEMA' WHEN 'table' THEN 'TABLE'
             WHEN 'table column' THEN 'COLUMN' WHEN 'view column' THEN 'COLUMN'
             WHEN 'materialized view column' THEN 'COLUMN'
             WHEN 'view' THEN 'VIEW' WHEN 'materialized view' THEN 'MATERIALIZED VIEW'
             WHEN 'sequence' THEN 'SEQUENCE' WHEN 'index' THEN 'INDEX'
             WHEN 'function' THEN 'FUNCTION' WHEN 'procedure' THEN 'PROCEDURE'
             WHEN 'trigger' THEN 'TRIGGER' WHEN 'table constraint' THEN 'CONSTRAINT'
             WHEN 'type' THEN 'TYPE'
           END AS kw
      FROM pg_description d
      CROSS JOIN LATERAL pg_identify_object(d.classoid, d.objoid, d.objsubid) o
     WHERE (d.classoid = 'pg_namespace'::regclass AND d.objoid = 'public'::regnamespace)
        OR (d.classoid = 'pg_class'::regclass AND EXISTS (
              SELECT 1 FROM pg_class c
               WHERE c.oid = d.objoid AND c.relnamespace = 'public'::regnamespace))
        OR (d.classoid = 'pg_proc'::regclass AND EXISTS (
              SELECT 1 FROM pg_proc p
               WHERE p.oid = d.objoid AND p.pronamespace = 'public'::regnamespace))
        OR (d.classoid = 'pg_type'::regclass AND EXISTS (
              SELECT 1 FROM pg_type t
               WHERE t.oid = d.objoid AND t.typnamespace = 'public'::regnamespace))
        OR (d.classoid = 'pg_trigger'::regclass AND EXISTS (
              SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
               WHERE t.oid = d.objoid AND c.relnamespace = 'public'::regnamespace))
        OR (d.classoid = 'pg_constraint'::regclass AND EXISTS (
              SELECT 1 FROM pg_constraint k
               WHERE k.oid = d.objoid AND k.connamespace = 'public'::regnamespace))
        OR (d.classoid NOT IN ('pg_namespace'::regclass, 'pg_class'::regclass,
                               'pg_proc'::regclass, 'pg_type'::regclass,
                               'pg_trigger'::regclass, 'pg_constraint'::regclass)
            AND o.schema = 'public')
""")


def privilege_state(engine) -> dict:
    """Owners, ACLs, schema default ACLs and comments of `public`, as data.

    The shape is what `replay_privilege_state` and `privilege_mismatches` take:
      owners       {(kind, key): (alter_target, owner)}
      acl          {(kind, key, grantee): (target, cols, frozenset((priv, grantable)))}
      objects      {(kind, key)} — every object the ACL query looked at
      default_acl  {(role, objtype, grantee, priv, grantable)}
      comments     {(type, identity): (kw, description)}
    Role names are kept quote_ident()ed, the form the replay writes them in.
    """
    state: dict = {"owners": {}, "acl": {}, "objects": set(),
                   "default_acl": set(), "comments": {}}
    with engine.connect() as conn:
        for kind, key, target, owner in conn.execute(_OWNERS_SQL):
            state["owners"][(kind, key)] = (target, owner)
        privs: dict[tuple, set] = {}
        targets: dict[tuple, tuple[str, str]] = {}
        for kind, key, target, cols, grantee, priv, grantable in conn.execute(_ACL_SQL):
            state["objects"].add((kind, key))
            if grantee is None:
                continue
            privs.setdefault((kind, key, grantee), set()).add((priv, bool(grantable)))
            targets[(kind, key, grantee)] = (target, cols)
        state["acl"] = {k: (*targets[k], frozenset(v)) for k, v in privs.items()}
        for role, objtype, grantee, priv, grantable in conn.execute(_DEFAULT_ACL_SQL):
            state["default_acl"].add((role, objtype, grantee, priv, bool(grantable)))
        for type_, identity, description, kw in conn.execute(_COMMENTS_SQL):
            state["comments"][(type_, identity)] = (kw, description)
    return state


# Comment types whose object the rebuild re-creates under a name pave cannot
# promise: create_all and Alembic name indexes and constraints their own way,
# and a hand-made index is not captured at all. Its COMMENT ON would fail only
# after the DROP. Nothing in the tree comments one (no `comment=` and no
# `COMMENT ON` in models or migrations), so this refuses only hand-made ones.
_COMMENT_TYPES_NOT_KEPT = ("index", "table constraint")


def unreplayable_privilege_state(state: dict) -> list[str]:
    """What pave captured but has no statement to put back. Non-empty means refuse."""
    out = [f"comment on {type_} {identity}"
           for (type_, identity), (kw, _d) in sorted(state["comments"].items()) if kw is None]
    out += [f"comment on {type_} {identity} (the rebuild does not keep its name; drop the "
            "comment and re-add it after the pave)"
            for (type_, identity) in sorted(state["comments"]) if type_ in _COMMENT_TYPES_NOT_KEPT]
    out += [f"default privileges for {role} on object type {objtype!r}"
            for role, objtype, *_ in sorted(state["default_acl"])
            if objtype not in _DEFAULT_ACL_OBJECTS]
    return out


# Extension members in public whose ACL differs from what re-creating the
# extension in step 3b gives them: what its script recorded (pg_init_privs),
# else the database-wide default ACL for the owner (`ALTER DEFAULT PRIVILEGES`
# without IN SCHEMA, which survives the DROP), else the built-in default, NULL.
# Either someone changed them since, or the schema had default privileges
# when the extension was created and the members took them; step 3b creates
# the extension before those, so either way the pave would reset the ACL:
# a REVOKE on an extension function came back undone and the pave passed
# (measured, PostgreSQL 16.15).
#
# NOT caught, and lost silently (at least this case): a member the extension's script itself
# GRANTs on, created while the schema had default privileges. Its
# pg_init_privs row already includes those defaults, so it matches here and
# the re-created member lacks them (measured: pg_stat_statements under
# TABLES defaults, 2026-10-11). Comparing against a prediction cannot close
# that; capturing the members' ACLs and comparing them in step 7 would.
# Nothing in this tree creates an extension or schema default privileges.
_EXTENSION_MEMBER_ACL_CHANGES_SQL = text("""
    SELECT pg_describe_object(d.classid, d.objid, 0)
      FROM pg_depend d
      LEFT JOIN pg_class c ON d.classid = 'pg_class'::regclass AND c.oid = d.objid
      LEFT JOIN pg_proc p ON d.classid = 'pg_proc'::regclass AND p.oid = d.objid
      LEFT JOIN pg_type t ON d.classid = 'pg_type'::regclass AND t.oid = d.objid
      LEFT JOIN pg_init_privs i ON i.objoid = d.objid AND i.classoid = d.classid
                               AND i.objsubid = 0 AND i.privtype = 'e'
      CROSS JOIN LATERAL (SELECT COALESCE(c.relowner, p.proowner, t.typowner) AS owner,
                                 CASE WHEN p.oid IS NOT NULL THEN 'f'
                                      WHEN t.oid IS NOT NULL THEN 'T'
                                      WHEN c.relkind = 'S' THEN 's'
                                      ELSE 'r' END::"char" AS objtype) m
      LEFT JOIN pg_default_acl g
             ON g.defaclnamespace = 0 AND g.defaclrole = m.owner
            -- pg_default_acl spells a sequence 'S'; acldefault() wants 's'.
            AND g.defaclobjtype = CASE m.objtype WHEN 's' THEN 'S' ELSE m.objtype END
     WHERE d.deptype = 'e' AND d.refclassid = 'pg_extension'::regclass
       AND COALESCE(c.relnamespace, p.pronamespace, t.typnamespace) = 'public'::regnamespace
       -- Both sides spelled out with acldefault(), so a NULL and the same
       -- privileges written out (a GRANT and its REVOKE) compare equal.
       AND COALESCE(c.relacl, p.proacl, t.typacl, acldefault(m.objtype, m.owner))::text
           IS DISTINCT FROM COALESCE(i.initprivs, g.defaclacl,
                                     acldefault(m.objtype, m.owner))::text
    UNION ALL
    -- Column grants on a member's table or view: _ACL_SQL leaves them out
    -- with the rest of the member, and re-creating the extension drops them.
    SELECT pg_describe_object(d.classid, d.objid, a.attnum)
      FROM pg_depend d
      JOIN pg_class c ON d.classid = 'pg_class'::regclass AND c.oid = d.objid
      JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
      LEFT JOIN pg_init_privs i ON i.objoid = c.oid AND i.classoid = 'pg_class'::regclass
                               AND i.objsubid = a.attnum AND i.privtype = 'e'
     WHERE d.deptype = 'e' AND d.refclassid = 'pg_extension'::regclass
       AND c.relnamespace = 'public'::regnamespace
       AND a.attacl::text IS DISTINCT FROM i.initprivs::text
     ORDER BY 1
""")


def privileges_pave_cannot_rebuild(engine, state: dict) -> list[str]:
    """Privilege state a replay could write back and the rebuild would still lose.

    - A changed extension member's privileges (see the SQL above).
    - Default privileges `FOR ROLE` someone other than the paving role: they
      apply only to objects that role creates, and the rebuild creates every
      table as the paving role, so step 3b would restore the row and no table
      the rebuild adds would ever get the grants.
    """
    with engine.connect() as conn:
        out = [f"privileges on extension member {name} differ from what its extension "
               "records (a GRANT or REVOKE on it, or schema default privileges in place "
               "when the extension was created); the pave would reset them"
               for (name,) in conn.execute(_EXTENSION_MEMBER_ACL_CHANGES_SQL)]
        me = conn.execute(text("SELECT quote_ident(current_user)")).scalar()
    out += [f"default privileges FOR ROLE {role} (the rebuild creates its tables as {me})"
            for role in sorted({row[0] for row in state["default_acl"]} - {me})]
    return out


def extensions_pave_cannot_reinstall(engine) -> list[str]:
    """Extensions in public this server cannot CREATE at their installed version.

    Step 3b re-creates each one `VERSION <extversion>`; a cluster carried across
    a major upgrade can hold a version the new server has no script for, and
    that CREATE would fail after the DROP.

    Also an extension whose comment is not the one CREATE EXTENSION sets from
    its control file: _COMMENTS_SQL cannot see `COMMENT ON EXTENSION`
    (pg_identify_object gives an extension no schema), so the edit would be
    lost and step 7 would not notice.
    """
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT e.extname, e.extversion
              FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
             WHERE n.nspname = 'public'
               AND NOT EXISTS (SELECT 1 FROM pg_available_extension_versions v
                                WHERE v.name = e.extname AND v.version = e.extversion)
             ORDER BY 1""")).all()
        commented = conn.execute(text("""
            SELECT e.extname
              FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
              JOIN pg_available_extension_versions v
                ON v.name = e.extname AND v.version = e.extversion
             WHERE n.nspname = 'public'
               AND obj_description(e.oid, 'pg_extension') IS DISTINCT FROM v.comment
             ORDER BY 1""")).scalars().all()
    return ([f"extension {name} at version {version} (this server cannot install that "
             "version; ALTER EXTENSION ... UPDATE it first)" for name, version in rows]
            + [f"extension {name} comment (differs from its control file's; CREATE "
               "EXTENSION would reset it — restore the default, re-apply after)"
               for name in commented])


def _run_each(engine, statements: list[str]) -> list[str]:
    """Run each statement in its own transaction; return the ones that failed.

    One failure must not take the rest with it — a shared transaction would
    abort on the first error and every statement after it would fail with
    InFailedSqlTransaction, the shape `fix_sequences` was fixed for.
    """
    failures = []
    for stmt in statements:
        try:
            with engine.begin() as conn:
                conn.execute(text(stmt))
        except Exception as e:
            failures.append(stmt)
            log.error("  FAILED %s: %s", stmt, str(e).splitlines()[0])
    return failures


def replay_default_privileges(engine, pre: dict) -> list[str]:
    """Step 3b: the schema's `ALTER DEFAULT PRIVILEGES`, straight after CREATE SCHEMA.

    They decide what a NEW object is granted, so they have to exist before the
    rebuild creates anything: put back in step 4e, a table the rebuild added
    (one the old database lacked) came back without them and the pave passed,
    because step 7 only compares objects the database had. Step 4e runs this
    again, where it finds nothing to do unless something removed one since.
    """
    post = privilege_state(engine)
    stmts = []
    for role, objtype, grantee, priv, grantable in sorted(pre["default_acl"] - post["default_acl"]):
        stmts.append(f"ALTER DEFAULT PRIVILEGES FOR ROLE {role} IN SCHEMA public GRANT {priv} "
                     f"ON {_DEFAULT_ACL_OBJECTS[objtype]} TO {grantee}"
                     + (" WITH GRANT OPTION" if grantable else ""))
    for role, objtype, grantee, priv, _g in sorted(post["default_acl"] - pre["default_acl"]):
        if objtype in _DEFAULT_ACL_OBJECTS:
            stmts.append(f"ALTER DEFAULT PRIVILEGES FOR ROLE {role} IN SCHEMA public REVOKE {priv} "
                         f"ON {_DEFAULT_ACL_OBJECTS[objtype]} FROM {grantee}")
    return _run_each(engine, stmts)


def replay_privilege_state(engine, pre: dict) -> list[str]:
    """Step 4e: put the captured owners, ACLs, default ACLs and comments back.

    In phases, re-reading the catalog before each, because each phase changes
    what the next one sees: `ALTER ... OWNER` needs the new owner to hold
    CREATE on the schema, so the schema's ACL goes first; it rewrites the old
    owner's entries in each ACL, so every ACL is restored (the schema's again)
    after it; and a table-level `REVOKE ALL` also revokes that grantee's
    column-level privileges, so columns go last. Only objects the database had before the pave
    are touched; a table the rebuild added keeps whatever it was created with.
    Returns the statements that failed — step 7 then reports the difference.
    """
    log.info("--- Step 4e: restoring ownership, privileges and comments ---")
    failures: list[str] = []

    def _phase(kind: str) -> str:
        return {"schema": "schema", "column": "columns"}.get(kind, "objects")

    for phase in ("schema", "owners", "schema", "objects", "columns"):
        post = privilege_state(engine)
        if phase == "owners":
            failures += _run_each(engine, [
                f"ALTER {target} OWNER TO {owner}"
                for key, (target, owner) in sorted(pre["owners"].items())
                if key in post["owners"] and post["owners"][key][1] != owner])
            continue
        stmts = []
        groups = {k for k in set(pre["acl"]) | set(post["acl"])
                  if (k[0], k[1]) in pre["objects"] and _phase(k[0]) == phase}
        for kind, key, grantee in sorted(groups):
            have = post["acl"].get((kind, key, grantee))
            want = pre["acl"].get((kind, key, grantee))
            have_privs = have[2] if have else frozenset()
            want_privs = want[2] if want else frozenset()
            if have_privs == want_privs:
                continue
            target, cols = (want or have)[:2]
            if have_privs:
                stmts.append(f"REVOKE ALL{cols} ON {target} FROM {grantee}")
            for priv, grantable in sorted(want_privs):
                stmts.append(f"GRANT {priv}{cols} ON {target} TO {grantee}"
                             + (" WITH GRANT OPTION" if grantable else ""))
        failures += _run_each(engine, stmts)

    failures += replay_default_privileges(engine, pre)
    post = privilege_state(engine)
    stmts = []
    for (type_, identity), (kw, description) in sorted(pre["comments"].items()):
        if post["comments"].get((type_, identity), (None, None))[1] != description:
            stmts.append(f"COMMENT ON {kw} {identity} IS {_sql_literal(description)}")
    failures += _run_each(engine, stmts)

    if failures:
        log.error("Step 4e: %d statement(s) failed; step 7 will report what is still different.",
                  len(failures))
    else:
        log.info("Ownership, privileges and comments restored.")
    return failures


def _sql_literal(value: str) -> str:
    """An escape-string literal: reads the same whatever standard_conforming_strings is."""
    return "E'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def privilege_mismatches(pre: dict, post: dict) -> list[str]:
    """Every captured owner, privilege and comment the paved database lacks.

    Scoped to what existed before the pave: an object the rebuild added is not
    a mismatch. A comment the rebuild added (an ORM `comment=`) is not one
    either; only a pre-pave comment that is gone or different counts.
    """
    out = []
    for key, (_t, owner) in sorted(pre["owners"].items()):
        now = post["owners"].get(key, (None, None))[1]
        if now != owner:
            out.append(f"OWNER {key[0]} {key[1]}: {owner} before, {now} after")
    groups = {k for k in set(pre["acl"]) | set(post["acl"]) if (k[0], k[1]) in pre["objects"]}
    for kind, key, grantee in sorted(groups):
        want = pre["acl"].get((kind, key, grantee), (None, None, frozenset()))[2]
        have = post["acl"].get((kind, key, grantee), (None, None, frozenset()))[2]
        if want != have:
            out.append(f"PRIVILEGES {kind} {key} for {grantee}: "
                       f"{_fmt_privs(want)} before, {_fmt_privs(have)} after")
    for row in sorted(pre["default_acl"] ^ post["default_acl"]):
        side = "lost" if row in pre["default_acl"] else "gained"
        out.append(f"DEFAULT PRIVILEGES {side}: for {row[0]} on {row[1]!r} {row[3]} to {row[2]}")
    for (type_, identity), (_kw, description) in sorted(pre["comments"].items()):
        now = post["comments"].get((type_, identity), (None, None))[1]
        if now != description:
            out.append(f"COMMENT on {type_} {identity}: {description!r} before, {now!r} after")
    return out


def _fmt_privs(privs: frozenset) -> str:
    return ",".join(p + ("*" if g else "") for p, g in sorted(privs)) or "none"


# --------------------------------------------------------------------------
# The Alembic stamp (GDXA-531). Step 4b runs `alembic upgrade head` on an
# empty schema and step 5 reloads the old rows under the head stamp, so every
# data migration between the database's stamp and head runs against empty
# tables and never touches the rows. And a table a later migration drops on
# purpose is captured by unrecreatable_tables() and resurrected by step 4c.
# A database already at head has had both done to it for real, so pave
# accepts only that.
# --------------------------------------------------------------------------


def alembic_heads() -> set[str]:
    """This tree's head revision(s), read from the migration scripts."""
    cfg = AlembicConfig(str(PACKAGE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(PACKAGE_DIR / "migrations"))
    return set(ScriptDirectory.from_config(cfg).get_heads())


def alembic_stamp(engine) -> set[str] | None:
    """The database's alembic_version rows, or None when it has no such table."""
    if "alembic_version" not in get_table_names(engine):
        return None
    with engine.connect() as conn:
        return {row[0] for row in conn.execute(text("SELECT version_num FROM alembic_version"))}


def refuse_unless_at_head(engine) -> None:
    stamp = alembic_stamp(engine)
    heads = alembic_heads()
    if stamp == heads:
        log.info("  alembic stamp %s is this tree's head", ", ".join(sorted(stamp)))
        return
    shown = "no alembic_version table" if stamp is None else (", ".join(sorted(stamp)) or "empty")
    log.error("The database is stamped %s; this tree's head is %s.", shown, ", ".join(sorted(heads)))
    log.error("Pave rebuilds at head and reloads the rows unmigrated: every data migration "
              "in between would run against empty tables, and a table a migration dropped "
              "would be put back.")
    log.error("Refusing to pave: NOTHING has been dropped. The database is untouched.")
    log.error("Bring it to head first (`alembic upgrade head`, which every app boot runs), "
              "or pave with the image whose head matches the stamp.")
    sys.exit(1)


# Step 5 replays a `pg_dump --disable-triggers` file, which wraps every table's
# rows in `ALTER TABLE ... DISABLE TRIGGER ALL`. On a table with a foreign key
# or a deferrable unique constraint that statement needs superuser, even for
# the table's owner, because those constraints are enforced by internal
# triggers. The pave reaches it after `DROP SCHEMA`, so a non-superuser used
# to stop there under ON_ERROR_STOP with an empty schema and the full backup as
# the only copy. Every step after the drop also creates extensions, re-owns
# objects to other roles and sets default privileges for them. A superuser
# holds every privilege those steps need, so this one check covers them all.
def refuse_unless_superuser(engine) -> None:
    with engine.connect() as conn:
        me, is_super = conn.execute(text(
            "SELECT current_user, rolsuper FROM pg_roles WHERE rolname = current_user")).one()
    if is_super:
        log.info("  paving role %s is a superuser", me)
        return
    log.error("The paving role %s is not a superuser.", me)
    log.error("Step 5 reloads a `pg_dump --disable-triggers` dump: its `ALTER TABLE ... "
              "DISABLE TRIGGER ALL` needs superuser on any table with a foreign key, and "
              "it runs after `DROP SCHEMA public CASCADE`.")
    log.error("Refusing to pave: NOTHING has been dropped. The database is untouched.")
    log.error("Re-run with a DATABASE_URL (or URL argument) for a superuser role, such as "
              "the POSTGRES_USER the database container was created with.")
    sys.exit(1)


# TENANT_AI_*_PASSWORD used to drive a step 4e that imported
# gdx_dispatch.tools.sql.ai_roles_bootstrap — a module that has never existed
# in this tree's history. It ran after the DROP, so setting either variable
# turned every pave into an ImportError on an empty database. The roles are
# cluster-level and survive a pave; what a pave used to lose is their GRANTs,
# and step 4e now restores those from the capture. Nothing here sets a
# password, so a variable that asks for one is refused while that refusal is
# still free.
AI_ROLE_PASSWORD_ENV = ("TENANT_AI_READONLY_PASSWORD", "TENANT_AI_WRITE_PASSWORD")


def refuse_ai_role_bootstrap_env() -> None:
    # The messages are literals: CodeQL taints anything derived from a
    # *_PASSWORD variable, names included (py/clear-text-logging-sensitive-data).
    if not any(os.environ.get(name) for name in AI_ROLE_PASSWORD_ENV):
        return
    log.error("TENANT_AI_READONLY_PASSWORD or TENANT_AI_WRITE_PASSWORD is set, but pave has "
              "no AI-role bootstrap: the module it once imported "
              "(gdx_dispatch.tools.sql.ai_roles_bootstrap) has never existed.")
    log.error("Grants to existing roles such as gdx_ai_readonly are carried across the pave "
              "by step 4e; role passwords are cluster-level and a pave does not touch them.")
    log.error("Refusing to pave: NOTHING has been dropped. The database is untouched.")
    log.error("Unset TENANT_AI_READONLY_PASSWORD and TENANT_AI_WRITE_PASSWORD and re-run.")
    sys.exit(1)


def alembic_upgrade_head(db_url: str) -> None:
    """`alembic upgrade head` against the freshly created schema.

    entrypoint.sh pairs create_all() with this on every boot and pave has to
    do the same. create_all() builds only TenantBase's tables; without this
    step the paved database has no `alembic_version` stamp at all and none of
    the Alembic-base tables the data dump is about to reload into.
    """
    cmd = [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"]
    env = {**os.environ, "ALEMBIC_DATABASE_URL": db_url}
    log.info("--- Step 4b: alembic upgrade head ---")
    r = subprocess.run(cmd, cwd=str(PACKAGE_DIR), env=env, capture_output=True, text=True)
    if r.returncode != 0:
        log.error("alembic upgrade head FAILED (rc=%s):", r.returncode)
        for line in (r.stderr or "").splitlines()[-30:]:
            log.error("  %s", line)
        sys.exit(1)
    log.info("alembic upgrade head complete.")


def _replay_order(items: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Tables first, then everything else, then by identity.

    Not correctness — the retry loop converges from any order — but a captured
    table's file brings its own indexes, constraints and triggers with it, so
    doing tables first makes most of the rest present without a failed attempt,
    and makes the log read in the order an operator would expect.
    """
    return sorted(items, key=lambda item: (item[0] != "table", item[0], item[1]))


def replay_table_schemas(db_info: dict, engine, captured: dict[tuple[str, str], str],
                         step: str = "4c", full_backup_path: str | None = None) -> None:
    """Recreate captured objects the ORM/Alembic rebuild did not bring back.

    Replayed in passes, because nothing here has a reliable creation order: a
    captured table can carry a foreign key to another captured table and
    `pg_dump -t` gives no cross-table ordering, and a trigger cannot be created
    before the function it calls. A file that fails in one pass is retried in
    the next. Each runs `--single-transaction` with ON_ERROR_STOP=on, so a
    failed attempt leaves nothing half-applied for the retry to trip over. A
    pass that makes no progress is fatal — a real dependency cycle fails loudly
    here rather than silently half-restoring.

    The catalog is re-read after every pass, because applying one file can
    satisfy another item outright: `pg_dump -t <table>` carries that table's
    own triggers. Replaying such a trigger on top of itself is an "already
    exists" error that no number of passes can clear, and the loop would then
    exit as "no progress" with the schema already dropped.
    """
    present = schema_inventory(engine)
    remaining = _replay_order([k for k in captured if k not in present])
    if not remaining:
        return
    log.info("--- Step %s: replaying DDL for %d object(s) neither the ORM nor Alembic rebuilt ---",
             step, len(remaining))
    for kind, key in remaining:
        log.info("  %s %s", kind, key)
    env = _pg_env(db_info)
    while remaining:
        applied, errors = [], {}
        for item in remaining:
            cmd = [
                "psql",
                "-h", db_info["host"],
                "-p", db_info["port"],
                "-U", db_info["user"],
                "-d", db_info["dbname"],
                "-f", captured[item],
                "--single-transaction",
                "--set", "ON_ERROR_STOP=on",
                "-q",
            ]
            r = subprocess.run(cmd, env=env, capture_output=True, text=True)
            if r.returncode == 0:
                applied.append(item)
            else:
                errors[item] = (r.stderr or "").strip().splitlines()[-1:] or ["(no stderr)"]
        if not applied:
            log.error("Schema replay made no progress; %d object(s) could not be recreated:",
                      len(remaining))
            for item, err in errors.items():
                log.error("  %s %s: %s", item[0], item[1], err[0])
            if full_backup_path:
                log_restore(db_info, full_backup_path)
            sys.exit(1)
        present = schema_inventory(engine)
        remaining = [k for k in remaining if k not in applied and k not in present]
    log.info("Schema replay complete.")


def dump_full(db_info: dict, output_path: str) -> None:
    """Full pg_dump as safety backup — the file every late exit tells the operator to restore.

    Unlike the other dumps it keeps owners and GRANTs: step 4e restores them
    from memory, so an exit before 4e (3b, 4c, 4d) would otherwise leave a
    restore that brings the rows back owned by the paving role with no grants
    for gdx_app or gdx_ai_readonly. Restored as the paving role, its
    `ALTER ... OWNER` lines succeed because refuse_unless_superuser() has
    already refused a paving role that is not a superuser — once
    schema_restore_sql() has given the schema back its ACL; the file alone
    does not restore the public schema's owner, ACL or comment.
    """
    log.info("Full backup to %s ...", output_path)
    _pg_dump(db_info, [], output_path, "pg_dump (full) failed")
    log.info("Full backup complete: %s", output_path)


def schema_restore_path(full_backup_path: str) -> str:
    return full_backup_path.removesuffix("_full.sql") + "_schema.sql"


def schema_restore_sql(pre: dict) -> str:
    """The public schema's owner, ACL and comment, as SQL, from the step 0b capture.

    The full backup cannot carry them: pg_dump writes the public schema's ACL
    relative to its pg_init_privs entry, so the file says nothing about a
    schema left at its defaults — and the schema a late exit leaves behind is
    the paving role's, with no ACL (see the note above _RELKIND_KIND). Restored
    from the full backup alone, the rows come back and no app role has USAGE.
    Run before the full backup: its `ALTER ... OWNER TO r` needs r to hold
    CREATE on the schema, which only this file gives back.
    """
    _target, owner = pre["owners"][("schema", "public")]
    grants = {grantee: privs for (kind, _k, grantee), (_t, _c, privs) in pre["acl"].items()
              if kind == "schema"}
    lines = [f"ALTER SCHEMA public OWNER TO {owner};"]
    lines += [f"REVOKE ALL ON SCHEMA public FROM {g};" for g in sorted({"PUBLIC", owner} | set(grants))]
    lines += [f"GRANT {priv} ON SCHEMA public TO {g}" + (" WITH GRANT OPTION;" if grantable else ";")
              for g, privs in sorted(grants.items()) for priv, grantable in sorted(privs)]
    comment = next((d for (type_, _i), (_kw, d) in pre["comments"].items() if type_ == "schema"), None)
    lines.append("COMMENT ON SCHEMA public IS "
                 + (_sql_literal(comment) if comment is not None else "NULL") + ";")
    return "\n".join(lines) + "\n"


def restore_steps(db_info: dict, full_backup_path: str) -> list[list[str]]:
    """The psql commands that put the pre-pave database back, in order.

    The schema is emptied first: an exit after the rebuild leaves tables, some
    with rows, and the full backup's CREATE TABLE then fails on them (or, with
    errors ignored, its COPY into a non-empty table aborts and the rows stay
    the rebuild's). Then the schema file, then the full backup.
    """
    psql = ["psql", "-h", db_info["host"], "-p", db_info["port"], "-U", db_info["user"],
            "-d", db_info["dbname"], "-v", "ON_ERROR_STOP=1"]
    steps = [psql + ["-c", "DROP SCHEMA public CASCADE; CREATE SCHEMA public"]]
    schema_path = schema_restore_path(full_backup_path)
    if os.path.exists(schema_path):
        steps.append(psql + ["-f", schema_path])
    steps.append(psql + ["-f", full_backup_path])
    return steps


def log_restore(db_info: dict, full_backup_path: str, level=logging.ERROR,
                rows_are_back: bool = False) -> None:
    """The restore instructions every exit after the DROP prints.

    rows_are_back: step 7 found every row count matching, and the mismatch is
    elsewhere (a grant, a comment, an object). The steps then throw away a
    working database, and every write made since, so they are offered only
    as a last resort. A failed reload, or lost rows, never passes it.
    """
    log.log(level, "Pre-pave full backup preserved: %s", full_backup_path)
    if rows_are_back:
        log.log(level, "The data is reloaded. Only if this database cannot be used, replace it "
                       "with the backup; the first step DISCARDS EVERYTHING in it, including "
                       "every write made since the pave:")
    else:
        log.log(level, "Restore it by running, in order (the first discards what the pave rebuilt):")
    for step in restore_steps(db_info, full_backup_path):
        log.log(level, "  %s", shlex.join(step))


# --------------------------------------------------------------------------
# Other writers (GDXA-1811). From step 3 to the end of step 5 every table
# exists and is empty, so anything else connected to the database — the app,
# plugin-host, the celery workers, celery-beat's every-minute outbox drains —
# can write a row the step 5 COPY then collides with, and ON_ERROR_STOP aborts
# the reload after the DROP. Pave therefore refuses, with nothing dropped,
# while any other client backend is connected, checks again inside the DROP's
# own transaction, and reports at step 7 any client that connected meanwhile.
#
# --terminate-others fences with CONNECTION LIMIT 0 and terminates the rest.
# A connection limit does not apply to superusers, and neither does REVOKE
# CONNECT; ALLOW_CONNECTIONS false would apply, but it also refuses pave's own
# pg_dump, alembic and psql connections. Every service in
# docker/docker-compose.yml connects as POSTGRES_USER, a superuser, so against
# them the fence is detection, not prevention: a terminated client that comes
# back before the DROP is refused like any other. Stop them first.
# --------------------------------------------------------------------------

# The database clients the compose files define; demo has only the first two,
# and the customer file adds a read-only `backup` sidecar.
DB_CLIENT_SERVICES = ("app", "plugin-host", "celery-high", "celery-low", "celery-beat", "backup")
DROP_LOCK_TIMEOUT = "10s"
TERMINATE_WAIT_SECONDS = 10

# Pave's own sessions carry this name. One pid is not enough to exclude: an
# engine can hold two pooled connections at once (get_row_counts inspects
# inside an open connection). The random part keeps another client from
# passing as pave by name.
SELF_APPLICATION_NAME = f"gdx_pave_{os.getpid()}_{secrets.token_hex(4)}"


def _own_url(db_url: str):
    return make_url(db_url).update_query_dict({"application_name": SELF_APPLICATION_NAME})


_OTHER_BACKENDS_SQL = text("""
    SELECT pid,
           COALESCE(NULLIF(application_name, ''), '(no application_name)'),
           usename,
           COALESCE(host(client_addr), 'local socket')
      FROM pg_stat_activity
     WHERE datname = current_database()
       AND pid <> pg_backend_pid()
       AND application_name IS DISTINCT FROM :me
       AND backend_type = 'client backend'
     ORDER BY pid
""").bindparams(me=SELF_APPLICATION_NAME)


def other_backends(engine) -> list[tuple[int, str, str, str]]:
    """(pid, application_name, role, client address) of every other client on this database.

    Read as the paving role, which refuse_unless_superuser() has made a
    superuser, so pg_stat_activity shows every session and not just its own.
    """
    with engine.connect() as conn:
        return [tuple(row) for row in conn.execute(_OTHER_BACKENDS_SQL)]


def _log_backends(backends: list[tuple[int, str, str, str]]) -> None:
    for pid, app, role, addr in backends:
        log.error("  pid %s  application_name=%s  role=%s  client=%s", pid, app, role, addr)


def refuse_while_others_connected(engine) -> None:
    others = other_backends(engine)
    if not others:
        log.info("  no other client is connected to the database")
        return
    log.error("%d other client(s) are connected to the database:", len(others))
    _log_backends(others)
    log.error("Any of them can write into the empty tables between the DROP and the reload, "
              "and a row the reload's COPY collides with aborts it with the schema dropped.")
    log.error("Refusing to pave: NOTHING has been dropped. The database is untouched.")
    log.error("Stop every client first — in the compose project that owns this database, "
              "the services among %s (run --help for the commands) — and re-run, or pass "
              "--terminate-others to fence the database and terminate them; a client that "
              "connects as a superuser is not fenced and will be refused again if it reconnects.",
              ", ".join(DB_CLIENT_SERVICES))
    sys.exit(1)


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _connlimit_sql(dbname: str, limit: int) -> str:
    return f"ALTER DATABASE {_quote_ident(dbname)} CONNECTION LIMIT {int(limit)}"


def fence_database(engine, db_info: dict) -> tuple[str, int]:
    """CONNECTION LIMIT 0 on the target database. Returns (dbname, the limit it had)."""
    with engine.connect() as conn:
        dbname, limit = conn.execute(text(
            "SELECT datname, datconnlimit FROM pg_database "
            "WHERE datname = current_database()")).one()
        conn.execute(text(_connlimit_sql(dbname, 0)))
        conn.commit()
    restore = ["psql", "-h", db_info["host"], "-p", db_info["port"], "-U", db_info["user"],
               "-d", dbname, "-c", _connlimit_sql(dbname, limit)]
    log.warning("Fenced %s: CONNECTION LIMIT 0 (was %s); superusers are not held off by it.",
                dbname, limit)
    log.warning("Pave lifts the fence when it exits. If it is killed first, lift it with:")
    log.warning("  %s", shlex.join(restore))
    return dbname, limit


def lift_fence(db_url: str, dbname: str, limit: int) -> bool:
    engine = create_engine(_own_url(db_url))
    try:
        with engine.connect() as conn:
            conn.execute(text(_connlimit_sql(dbname, limit)))
            conn.commit()
        log.info("Fence lifted: %s CONNECTION LIMIT %s", dbname, limit)
        return True
    except Exception as e:
        log.error("Could not lift the fence on %s (%s). Run: %s;", dbname, e,
                  _connlimit_sql(dbname, limit))
        return False
    finally:
        engine.dispose()


def terminate_other_backends(engine) -> None:
    others = other_backends(engine)
    if not others:
        return
    log.warning("Terminating %d other client(s):", len(others))
    for pid, app, role, addr in others:
        log.warning("  pid %s  application_name=%s  role=%s  client=%s", pid, app, role, addr)
    with engine.connect() as conn:
        for pid, *_ in others:
            conn.execute(text("SELECT pg_terminate_backend(:pid)").bindparams(pid=pid))
        conn.commit()
    # pg_terminate_backend returns once the signal is sent, not once the
    # backend has gone; wait for exactly those pids to leave.
    targets = {pid for pid, *_ in others}
    for _ in range(TERMINATE_WAIT_SECONDS * 10):
        if not targets & {pid for pid, *_ in other_backends(engine)}:
            return
        time.sleep(0.1)


def drop_and_recreate_schema(engine) -> None:
    """DROP SCHEMA public CASCADE; CREATE SCHEMA public;

    One transaction. It first re-checks for other clients, since the dumps
    give one time to connect, and then takes the DROP's locks under
    lock_timeout: a session holding a lock on any table would otherwise park
    the DROP indefinitely. Either refusal rolls back with nothing dropped.
    """
    log.info("Dropping schema...")
    with engine.connect() as conn:
        conn.execute(text(f"SET LOCAL lock_timeout = '{DROP_LOCK_TIMEOUT}'"))
        others = [tuple(row) for row in conn.execute(_OTHER_BACKENDS_SQL)]
        if others:
            conn.rollback()
            log.error("%d client(s) connected while the dumps were taken:", len(others))
            _log_backends(others)
            log.error("Refusing to pave: NOTHING has been dropped. The database is untouched.")
            log.error("Stop every client (the services among %s; run --help) and re-run.",
                      ", ".join(DB_CLIENT_SERVICES))
            sys.exit(1)
        try:
            conn.execute(text("DROP SCHEMA public CASCADE"))
        except OperationalError as e:
            if getattr(e.orig, "pgcode", None) != "55P03":  # lock_not_available
                raise
            conn.rollback()
            log.error("DROP SCHEMA could not take its locks within %s: another session holds "
                      "a lock on a table in public.", DROP_LOCK_TIMEOUT)
            log.error("Refusing to pave: NOTHING has been dropped. The database is untouched.")
            sys.exit(1)
        conn.execute(text("CREATE SCHEMA public"))
        conn.commit()
    log.info("Schema dropped and recreated.")


def create_all_from_orm(engine) -> None:
    """Run TenantBase.metadata.create_all().

    The ORM imports are at module scope, not here: this runs immediately after
    `DROP SCHEMA public CASCADE`, and an import that fails at this point leaves
    an empty database behind (the original defect).
    """
    log.info("Running create_all() with %d tables in metadata...",
             len(TenantBase.metadata.tables))
    TenantBase.metadata.create_all(engine, checkfirst=False)
    log.info("create_all() complete.")


def reload_data(db_info: dict, dump_path: str, strict: bool = True, full_backup_path: str | None = None) -> None:
    """psql < data.sql to reload data.

    strict=True (default): ON_ERROR_STOP=on. Any reload error aborts the pave
    via sys.exit(1). The pre-pave full backup is preserved at full_backup_path
    for restore. This is the safe default — it prevents the silent-failure
    mode (D-PE-7) where type-incompatible rows were dropped and pave still
    reported success.

    strict=False: legacy ON_ERROR_STOP=off behavior. Use only when you've
    accepted that some rows may be lost (e.g. mid-migration intentional
    column drops). Errors are logged loud regardless.
    """
    cmd = [
        "psql",
        "-h", db_info["host"],
        "-p", db_info["port"],
        "-U", db_info["user"],
        "-d", db_info["dbname"],
        "-f", dump_path,
        "--set", f"ON_ERROR_STOP={'on' if strict else 'off'}",
        "-q",
    ]
    env = _pg_env(db_info)
    log.info("Reloading data from %s (strict=%s) ...", dump_path, strict)
    r = subprocess.run(cmd, env=env, capture_output=True, text=True)

    stderr_lines = (r.stderr or "").splitlines()
    error_lines = [line for line in stderr_lines if "ERROR" in line]

    if r.returncode != 0 or (strict and error_lines):
        log.error("Data reload FAILED (rc=%s, %d error lines):", r.returncode, len(error_lines))
        for e in error_lines[:30]:
            log.error("  %s", e)
        if full_backup_path:
            log_restore(db_info, full_backup_path)
        if strict:
            log.error("Aborting under --strict. Re-run with --no-strict only if data loss is acceptable.")
            sys.exit(1)

    if error_lines:
        log.warning("Data reload had %d error lines (non-strict mode, continuing):", len(error_lines))
        for e in error_lines[:20]:
            log.warning("  %s", e)
    else:
        log.info("Data reload complete.")


# Which table and column a sequence belongs to, from the catalog rather than
# from its name. Splitting the name gets `gl_journal_entry_no_seq` wrong —
# table `gl_journal_entry`, column `no`, neither of which exists — and an
# unowned sequence (that one has no owner at all) has no table to read a max
# from and is correctly absent from this result.
_OWNED_SEQUENCES_SQL = text("""
    SELECT s.relname AS seq_name, t.relname AS tbl, a.attname AS col
    FROM pg_class s
    JOIN pg_namespace n ON n.oid = s.relnamespace
    JOIN pg_depend d
      ON d.objid = s.oid
     AND d.classid = 'pg_class'::regclass
     AND d.refclassid = 'pg_class'::regclass
     AND d.deptype IN ('a', 'i')
    JOIN pg_class t ON t.oid = d.refobjid
    JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = d.refobjsubid
    WHERE s.relkind = 'S' AND n.nspname = 'public'
    ORDER BY s.relname
""")


def fix_sequences(engine) -> list[str]:
    """Reset every owned sequence to max(<its own column>). Returns failures.

    Belt and braces over the reload — `pg_dump --data-only` emits its own
    `setval` lines — but it used to be broken belt and braces: the owner came
    from splitting the sequence name, the one sequence that does not fit the
    convention raised, that error aborted the *shared* connection transaction,
    and every sequence after it in the loop then failed with
    InFailedSqlTransaction. All of it was swallowed into warnings and the step
    still logged "Sequences fixed." A pave that reports success and then hands
    the next INSERT a duplicate key is the same fake-success shape this tool
    is being fixed for, so each setval now runs in its own SAVEPOINT and the
    failures are returned to the caller instead of disappearing.
    """
    log.info("Fixing sequences...")
    failures: list[str] = []
    with engine.connect() as conn:
        rows = conn.execute(_OWNED_SEQUENCES_SQL).fetchall()
        for seq_name, table, col in rows:
            try:
                with conn.begin_nested():
                    max_val = conn.execute(
                        text(f'SELECT MAX("{col}") FROM "{table}"')  # noqa: S608 — names come from the database catalog (pg_depend), double-quoted; operator tool, no request path
                    ).scalar()
                    if max_val is not None:
                        conn.execute(
                            text("SELECT setval(CAST(:seq AS regclass), :val)")
                            .bindparams(seq=f"public.{seq_name}", val=int(max_val))
                        )
            except Exception as e:
                failures.append(seq_name)
                log.warning("pave_tenant_db: sequence setval FAILED for %s (table=%s col=%s): %s",
                            seq_name, table, col, e)
        conn.commit()

    if failures:
        log.error("Sequences: %d of %d could not be reset: %s",
                  len(failures), len(rows), ", ".join(failures))
        log.error("The next INSERT into those tables can collide on its primary key.")
    else:
        log.info("Sequences fixed (%d owned sequences).", len(rows))
    return failures


def verify_counts(pre_counts: dict[str, int], post_counts: dict[str, int]) -> bool:
    """Compare the table list AND the row counts. Returns True if all match.

    The old check compared row counts only, under a `pre > 0` guard. A dropped
    table that had held rows did fail it — as "MISMATCH x: 139 -> 0", which
    reads as lost rows rather than a lost table — but a dropped **empty** table
    passed clean, and the drop itself was only ever an INFO line ("Tables no
    longer in ORM (data lost)"). A missing table is now its own error, whether
    or not it held anything.
    """
    ok = True
    for table in sorted(set(pre_counts) - set(post_counts)):
        log.error("  MISSING TABLE %s — %d row(s) before the pave, table gone after it",
                  table, pre_counts[table])
        ok = False
    for table in sorted(set(pre_counts) | set(post_counts)):
        pre = pre_counts.get(table, 0)
        post = post_counts.get(table, 0)
        if pre != post and pre > 0:
            log.warning("  MISMATCH %s: %d -> %d (lost %d rows)",
                        table, pre, post, pre - post)
            ok = False
    return ok


def default_target() -> tuple[str, str]:
    """The one (label, db_url) pair this install has: DATABASE_URL."""
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        log.error("DATABASE_URL not set")
        sys.exit(1)
    from gdx_dispatch.core.tenant import single_tenant
    return str(single_tenant()["slug"]), db_url


def _bypass_proxy_host(db_url: str) -> str:
    """Rewrite URL host to bypass connection-pool proxies (pgbouncer).

    Pave does DDL (DROP SCHEMA, create_all). In transaction-mode pgbouncer,
    pooled backends keep schema state from before the drop and Step 5b's
    seed query intermittently lands on a stale backend, raising
    `relation "pricing_settings" does not exist` against a freshly created
    table. Pave is an admin op — it must not share a pool.

    Env override `PAVE_DIRECT_TENANT_HOST` sets the replacement host.
    Default: rewrite "pgbouncer" → "tenant-db" (lab compose default).
    """
    parsed = urlparse(db_url)
    if not parsed.hostname:
        return db_url
    proxy_hosts = {"pgbouncer"}
    if parsed.hostname not in proxy_hosts:
        return db_url
    direct_host = os.getenv("PAVE_DIRECT_TENANT_HOST", "tenant-db")
    new_netloc = parsed.netloc.replace(parsed.hostname, direct_host, 1)
    rewritten = parsed._replace(netloc=new_netloc).geturl()
    log.info("pave: rewrote host %s → %s (bypass pool)", parsed.hostname, direct_host)
    return rewritten


def pave_one(db_url: str, label: str, strict: bool = True, terminate_others: bool = False) -> bool:
    """Pave a single database. Returns True on success.

    terminate_others: fence the database (CONNECTION LIMIT 0) and terminate
    every other client before the preflight checks for them; the fence is
    lifted on every exit that runs Python's `finally`, success or not.
    """
    db_url = _bypass_proxy_host(db_url)
    if not terminate_others:
        return _pave_one(db_url, label, strict, terminate_others=False)
    engine = create_engine(_own_url(db_url))
    try:
        # ALTER DATABASE and pg_terminate_backend on another role's session
        # both need it, and it is what exempts pave from its own fence.
        refuse_unless_superuser(engine)
        dbname, limit = fence_database(engine, parse_db_url(db_url))
    finally:
        engine.dispose()
    ok = False
    try:
        ok = _pave_one(db_url, label, strict, terminate_others=True)
    finally:
        # A fence left up refuses every non-superuser connection afterwards:
        # not a successful pave, whatever step 7 found.
        lifted = lift_fence(db_url, dbname, limit)
    return ok and lifted


def _pave_one(db_url: str, label: str, strict: bool, terminate_others: bool) -> bool:
    db_info = parse_db_url(db_url)
    dbname = db_info["dbname"]

    log.info("=" * 60)
    log.info("PAVE TARGET: %s (%s)", label, dbname)
    log.info("=" * 60)

    engine = create_engine(_own_url(db_url))

    # Step 0: Pre-pave row counts
    log.info("--- Step 0: Pre-pave row counts ---")
    pre_counts = get_row_counts(engine)
    pre_matviews = get_matview_states(engine)
    total = sum(v for v in pre_counts.values() if v > 0)
    log.info("  %d tables, %d total rows", len(pre_counts), total)
    if pre_matviews:
        log.info("  %d materialized view(s): %s", len(pre_matviews),
                 ", ".join(f"{k}={'populated' if v else 'not populated'}"
                           for k, v in sorted(pre_matviews.items())))
    for t, c in sorted(pre_counts.items(), key=lambda x: -x[1])[:10]:
        log.info("  %-40s %6d", t, c)

    # Step 0b: Preflight. Everything below happens while the database still
    # exists, and every failure path here refuses with nothing dropped.
    #
    # It does NOT make failure-after-the-drop impossible, and must not be
    # described as if it did: this proves an object's kind is one pave knows
    # and that DDL for it exists, not that the DDL replays. Step 4c (replay
    # made no progress) and step 4d (rebuilt schema is missing something) both
    # exit after `DROP SCHEMA`, with the full backup preserved and its restore
    # line printed. What the preflight removes is the *discoverable* half —
    # an object kind pave has no DDL for at all.
    log.info("--- Step 0b: Preflight ---")
    refuse_ai_role_bootstrap_env()
    refuse_unless_superuser(engine)
    refuse_unless_at_head(engine)
    if terminate_others:
        terminate_other_backends(engine)
    refuse_while_others_connected(engine)
    uncounted = sorted(t for t, c in pre_counts.items() if c == -1)
    if uncounted:
        # Step 7 cannot compare a table it could not count, so the pave would
        # end in MISMATCHES with every row back; refuse while nothing is dropped.
        log.error("Could not count %d table(s) before the pave: %s",
                  len(uncounted), ", ".join(uncounted))
        log.error("Refusing to pave: NOTHING has been dropped. The database is untouched.")
        sys.exit(1)
    unsupported = unsupported_objects(engine)
    if unsupported:
        log.error("This database holds %d object(s) or propert(ies) pave cannot rebuild:",
                  len(unsupported))
        for obj in unsupported:
            log.error("  %s", obj)
        log.error("Refusing to pave: NOTHING has been dropped. The database is untouched.")
        # Extension members never reach this list (_not_extension_member), so
        # everything here really is the schema's own and really is lost.
        log.error("Each of these is lost by DROP SCHEMA public CASCADE and rebuilt by nothing. "
                  "Remove it (and recreate it by hand after the pave), or teach "
                  "capture_schema_objects() to reproduce it, then re-run.")
        engine.dispose()
        sys.exit(1)

    pre_privileges = privilege_state(engine)
    unreplayable = (unreplayable_privilege_state(pre_privileges)
                    + privileges_pave_cannot_rebuild(engine, pre_privileges)
                    + extensions_pave_cannot_reinstall(engine))
    if unreplayable:
        for item in unreplayable:
            log.error("  pave cannot restore: %s", item)
        log.error("Refusing to pave: NOTHING has been dropped. The database is untouched.")
        engine.dispose()
        sys.exit(1)
    log.info("  captured %d owner(s), %d privilege grant(s), %d schema default privilege(s), "
             "%d comment(s)", len(pre_privileges["owners"]),
             sum(len(v[2]) for v in pre_privileges["acl"].values()),
             len(pre_privileges["default_acl"]), len(pre_privileges["comments"]))

    pre_inventory = schema_inventory(engine)
    foreign = unrecreatable_tables(engine)
    if foreign:
        log.info("  %d table(s) are not in the ORM: %s", len(foreign), ", ".join(foreign))
    captured = capture_table_schemas(db_info, foreign)
    captured.update(capture_schema_objects(db_info, engine))

    # Step 1: Data-only dump
    data_path = f"/tmp/{dbname}_data.sql"
    dump_data(db_info, data_path)

    # Step 2: Full backup
    full_path = f"/tmp/{dbname}_full.sql"
    dump_full(db_info, full_path)
    Path(schema_restore_path(full_path)).write_text(schema_restore_sql(pre_privileges))

    # Step 3: Drop and recreate schema
    drop_and_recreate_schema(engine)

    # Step 3b: default privileges, before anything is created under them. A
    # failure here is reported by step 7 with everything else. Extensions go
    # first: CREATE EXTENSION after them would hand an extension's functions
    # and types the schema's default grants, which the preflight has just
    # established the old members did not hold, and step 7 cannot see it
    # (members are not compared). Step 4c then finds them present.
    replay_table_schemas(db_info, engine,
                         {k: v for k, v in captured.items() if k[0] == "extension"},
                         step="3b", full_backup_path=full_path)
    default_acl_failures = replay_default_privileges(engine, pre_privileges)

    # Step 4: create_all from ORM
    engine.dispose()
    engine = create_engine(_own_url(db_url))
    create_all_from_orm(engine)

    # Step 4b: alembic upgrade head — the other half of entrypoint.sh's pairing.
    engine.dispose()
    alembic_upgrade_head(db_url)
    engine = create_engine(_own_url(db_url))

    # Step 4c: replay the DDL of anything neither of those brought back.
    replay_table_schemas(db_info, engine, captured, full_backup_path=full_path)
    engine.dispose()
    engine = create_engine(_own_url(db_url))

    # Step 4d: refuse to reload into a schema that is missing anything the
    # pre-pave database had. For a table the reload would abort on its COPY
    # anyway; for a function or a trigger nothing would ever notice, which is
    # how a pave used to remove the audit-immutability guard and still report
    # success. Checked against the whole inventory, not just the table list.
    missing = sorted(pre_inventory - schema_inventory(engine))
    if missing:
        log.error("Rebuilt schema is missing %d object(s) the database had before the pave:",
                  len(missing))
        for kind, key in missing:
            log.error("  %s %s", kind, key)
        log_restore(db_info, full_path)
        engine.dispose()
        sys.exit(1)

    # Step 4e: owners, GRANTs (gdx_app's, gdx_ai_readonly's, PUBLIC's USAGE on
    # the schema), schema default privileges and comments. After 4b/4c, so it
    # covers the Alembic-base and plugin tables, and before the reload, so a
    # failure is reported by step 7 alongside everything else.
    priv_failures = replay_privilege_state(engine, pre_privileges)

    # Step 5: Reload data
    reload_data(db_info, data_path, strict=strict, full_backup_path=full_path)

    # Step 5b: Sprint 1.0.5 — seed pricing engine defaults AFTER reload.
    # Order matters: reload restores any pre-existing tier rows first; the
    # seeder is idempotent and only fills missing (category, class) sets so
    # user-edited tiers always win over stubs. Fail-loud — a paved tenant
    # without a working pricing engine is a worse outcome than a failed pave.
    # Both imports are at module scope: this runs after the DROP.
    try:
        _S = sessionmaker(bind=engine, future=True)
        with _S() as _s:
            seed_default_pricing(_s)
        log.info("pricing_engine_seeded label=%s", label)
    except Exception as e:
        log.error("pricing_engine_seed_failed label=%s err=%s", label, e)
        engine.dispose()
        raise

    # Step 6: Fix sequences
    engine.dispose()
    engine = create_engine(_own_url(db_url))
    seq_failures = fix_sequences(engine)

    # Step 6b: matviews were created WITH NO DATA before the rows existed.
    # Last mutation before verification, so what step 7 counts is final.
    mv_failures = refresh_matviews(
        engine, {name for name, populated in pre_matviews.items() if populated})

    # Step 7: Verify
    log.info("--- Step 7: Verify schema inventory and row counts ---")
    post_counts = get_row_counts(engine)
    post_total = sum(v for v in post_counts.values() if v > 0)
    log.info("  %d tables, %d total rows", len(post_counts), post_total)

    lost_objects = sorted(pre_inventory - schema_inventory(engine))
    for kind, key in lost_objects:
        log.error("  MISSING %s %s — present before the pave, gone after it", kind, key)

    # A matview is in the inventory by name, so the checks above pass on an
    # empty one. What has to match is whether it holds data — not how many
    # rows, which step 6b's refresh legitimately changes.
    post_matviews = get_matview_states(engine)
    mv_mismatch = False
    for name, was_populated in sorted(pre_matviews.items()):
        if was_populated and not post_matviews.get(name, False):
            log.error("  MATVIEW NOT POPULATED %s — it held data before the pave", name)
            mv_mismatch = True

    priv_mismatches = privilege_mismatches(pre_privileges, privilege_state(engine))
    for line in priv_mismatches:
        log.error("  MISMATCH %s", line)

    # The DROP's transaction found no other client, so anyone here now
    # connected during the rebuild and may have written into it: a row in a
    # table that was empty before passes verify_counts unnoticed. A snapshot:
    # a client that connected, wrote and left in between is not seen here.
    intruders = other_backends(engine)
    if intruders:
        log.error("  %d client(s) connected during the pave and may have written to it:",
                  len(intruders))
        _log_backends(intruders)

    # get_row_counts records -1 for a table it could not count, and on Postgres
    # every later table on that connection goes -1 too; verify_counts skips
    # them, so an uncounted table is not a match.
    counts_match = (verify_counts(pre_counts, post_counts)
                    and -1 not in pre_counts.values() and -1 not in post_counts.values())
    ok = (counts_match
          and not seq_failures and not lost_objects and not intruders
          and not mv_failures and not mv_mismatch
          and not default_acl_failures and not priv_failures and not priv_mismatches)
    if ok:
        # Deliberately narrower than "everything is back": this is name
        # equality over the kinds in _INVENTORY_SQL, row counts, and the
        # owners, privileges, schema default privileges and comments of the
        # objects that existed before. It does not prove fidelity of an
        # object's definition.
        log.info("✅ PAVE COMPLETE — every pre-pave object is present by name with its "
                 "owner, privileges and comments, and all row counts match for %s", label)
    else:
        log.warning("⚠ PAVE COMPLETE WITH MISMATCHES for %s", label)
        log_restore(db_info, full_path, level=logging.WARNING, rows_are_back=counts_match)

    new_tables = set(post_counts) - set(pre_counts)
    if new_tables:
        log.info("  New tables the rebuild added: %s", ", ".join(sorted(new_tables)))

    engine.dispose()
    return ok


def main():
    args = sys.argv[1:]
    strict = True
    if "--no-strict" in args:
        strict = False
        args.remove("--no-strict")
    if "--strict" in args:
        args.remove("--strict")  # default; flag is accepted for explicitness
    terminate_others = "--terminate-others" in args
    if terminate_others:
        args.remove("--terminate-others")

    confirmed = "--yes" in args
    if confirmed:
        args.remove("--yes")
    if args and args[0] in ("-h", "--help"):
        print(USAGE)
        sys.exit(1)
    if args and args[0] in ("--all-tenants", "--tenant"):
        log.error("%s was removed 2026-09-06: there is one database. Run with --yes (DATABASE_URL) or --yes <database_url>.", args[0])
        sys.exit(2)
    if not confirmed:
        log.error("Refusing to pave without --yes: this drops every table in the target database. Run with --help for usage.")
        sys.exit(2)

    # A pave that finished with mismatches exits non-zero. It used to exit 0,
    # so `pave && restart` treated "⚠ PAVE COMPLETE WITH MISMATCHES" — rows
    # lost, a sequence unset, a matview empty — as success.
    if args:
        ok = pave_one(args[0], "direct", strict=strict, terminate_others=terminate_others)
    else:
        label, url = default_target()
        ok = pave_one(url, label, strict=strict, terminate_others=terminate_others)
        log.info("  %-20s %s", label, "✅" if ok else "⚠ MISMATCHES")
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()

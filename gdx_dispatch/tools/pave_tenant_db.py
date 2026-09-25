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
     triggers are the ones that matter: no migration installs them, and the
     runtime role cannot, so a pave that dropped them left ``audit_logs``
     UPDATE/DELETE-able with ARCHITECTURAL INVARIANT #2 unenforced.

Kinds 3 and 4 have their DDL captured BEFORE the drop (step 0b) and replayed
after it (step 4c); an object kind pave has no DDL for refuses the run before
anything is dropped. Pave used to rebuild only kind 1 while dumping all of
them, so the reload hit a table that no longer existed and —
``ON_ERROR_STOP=on`` — stopped there, leaving an empty database.

Usage (inside the app container):
    python -m gdx_dispatch.tools.pave_tenant_db --yes                 # pave DATABASE_URL
    python -m gdx_dispatch.tools.pave_tenant_db --yes <database_url>  # pave one DB by direct URL

The script-path form (``python gdx_dispatch/tools/pave_tenant_db.py --yes``)
works too — see the sys.path bootstrap below — but the module form is the
documented one because it cannot be run from the wrong directory.

Flags:
    --strict     (default) ON_ERROR_STOP=on; aborts on first reload error. Safe.
    --no-strict  legacy ON_ERROR_STOP=off; logs errors and continues. Risk: silent data loss.

Steps per database:
    0.  Pre-pave row counts and table list
    0b. Preflight: refuse outright on an object kind pave has no DDL for, then
        record the schema inventory and capture the DDL of every table
        create_all() will not rebuild plus every non-table object. Every
        failure here refuses with NOTHING dropped.
    1.  Dump data to /tmp/<dbname>_data.sql (alembic_version excluded — step 4b
        restamps it, and reloading the old stamp on top is a duplicate key)
    2.  Full backup to /tmp/<dbname>_full.sql
    3.  DROP SCHEMA public CASCADE; CREATE SCHEMA public
    4.  TenantBase.metadata.create_all() — the ORM's tables
    4b. alembic upgrade head — the Alembic-base tables and the stamp
    4c. Replay captured DDL for whatever is still missing (plugin tables, the
        audit-immutability guard, anything else nothing else owns)
    4d. Refuse to reload if any pre-pave object is still missing
    5.  Reload data from dump
    6.  Fix sequences (setval to max id)
    6b. REFRESH the materialized views step 4c had to create empty — they are
        built before the rows come back, so the catalog would report a
        populated-looking, empty matview as restored
    7.  Verify the schema inventory, the row counts AND the matview row counts
        against the pre-pave state; mismatches exit non-zero

Steps 4c and 4d run AFTER the drop. The preflight removes what can be known in
advance — an object of a kind pave has no DDL for — not the possibility of
failing late. Every late exit preserves /tmp/<dbname>_full.sql and prints the
psql line that restores it.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import create_engine, inspect, text

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

USAGE = """Usage (inside the app container):
  python -m gdx_dispatch.tools.pave_tenant_db [--strict|--no-strict] --yes                 # DATABASE_URL
  python -m gdx_dispatch.tools.pave_tenant_db [--strict|--no-strict] --yes <database_url>

  --yes        REQUIRED. This tool DROPS EVERY TABLE in the target database and
               rebuilds it from the ORM + `alembic upgrade head` (data reloaded
               from the dump it takes first). With no URL the target IS the
               application database.
  --strict     (default) abort on any reload error; preserves full backup.
  --no-strict  legacy behavior; logs errors but continues. Risk: silent data loss."""

# Answered before the ORM import below, because that import pulls in the app's
# routers and so needs JWT_SECRET et al: `--help` is the one invocation that
# has to work in a bare shell. Nothing destructive lives above this line.
if __name__ == "__main__" and any(a in ("-h", "--help") for a in sys.argv[1:]):
    print(USAGE)
    raise SystemExit(1)

import gdx_dispatch  # noqa: E402
import gdx_dispatch.models  # noqa: E402, F401 — registers every model on TenantBase.metadata
from gdx_dispatch.core.audit import TenantBase  # noqa: E402

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
    cmd = [
        "pg_dump",
        "-h", db_info["host"],
        "-p", db_info["port"],
        "-U", db_info["user"],
        "--data-only",
        "--disable-triggers",
        "--no-owner",
        "--no-privileges",
    ]
    for table in exclude_tables:
        cmd += ["--exclude-table", f"public.{table}"]
    cmd += [db_info["dbname"], "-f", output_path]
    env = {"PGPASSWORD": db_info["password"]}
    log.info("Dumping data to %s (excluding %s) ...",
             output_path, ", ".join(exclude_tables) or "nothing")
    r = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if r.returncode != 0:
        log.error("pg_dump failed: %s", r.stderr)
        sys.exit(1)
    log.info("Data dump complete: %s", output_path)


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
    env = {"PGPASSWORD": db_info["password"]}
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
# DB-level enforcement of ARCHITECTURAL INVARIANT #2 (audit immutability). No
# migration installs them, and `core/audit.py` installs them once per engine
# and then skips forever — it adds the engine to `_AUDIT_GUARD_INITIALIZED`
# whether or not the DDL succeeded, so a boot that could not create them will
# not try again. (That code also expects a NOSUPERUSER runtime role, `gdx_app`,
# which cannot issue the DDL at all; whether a given deployment has made that
# switch — D97 Phase 1, still open in docs/d97_rls_runbook.md — only changes
# whether the loss is recoverable by restart, not whether pave caused it.) A
# pave that dropped them used to report `✅ PAVE COMPLETE` with audit_logs
# freely UPDATE/DELETE-able.
#
# Every non-table object therefore gets its DDL read out of the catalog before
# the drop, and step 4c puts back whatever the rebuild did not. Over-capture is
# free: an object the rebuild recreates is simply skipped.
_CAPTURE_OBJECTS_SQL = text("""
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
       AND NOT EXISTS (
             SELECT 1 FROM pg_depend d
              WHERE d.objid = c.oid
                AND d.classid = 'pg_class'::regclass
                AND d.refclassid = 'pg_class'::regclass
                AND d.deptype IN ('a', 'i'))
    UNION ALL
    SELECT 'function', p.oid::regprocedure::text, pg_get_functiondef(p.oid)
      FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname = 'public' AND p.prokind IN ('f', 'p')
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
    UNION ALL
    SELECT 'extension', e.extname::text,
           'CREATE EXTENSION IF NOT EXISTS ' || quote_ident(e.extname) || ' SCHEMA public'
      FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
     WHERE n.nspname = 'public'
""")

# The same object kinds, plus tables, as bare identities — used before the drop
# to record what has to come back and after the rebuild to prove it did.
_INVENTORY_SQL = text("""
    SELECT 'table' AS kind, c.relname::text AS key
      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
    UNION ALL
    SELECT CASE c.relkind WHEN 'v' THEN 'view' WHEN 'm' THEN 'matview' ELSE 'sequence' END,
           c.relname::text
      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public' AND c.relkind IN ('v', 'm', 'S')
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
    UNION ALL
    SELECT 'extension', e.extname::text
      FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
     WHERE n.nspname = 'public'
""")

# Object kinds this tool has no DDL for. Their presence refuses the pave
# BEFORE the drop rather than discovering the loss after it.
#
# This is an ENUMERATED list, not a complement: a schema-scoped kind that is
# neither captured above nor named here is silently lost, and PostgreSQL adds
# kinds. A new one belongs in this list (refuse) or in the capture (rebuild) —
# never in neither. Table indexes and constraints are the one deliberate
# omission from both this list and the inventory: rebuilding them from the ORM,
# under the ORM's names, is the point of a pave.
_UNSUPPORTED_OBJECTS_SQL = text("""
    SELECT 'domain type' AS kind, t.typname::text AS key
      FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
     WHERE n.nspname = 'public' AND t.typtype = 'd'
    UNION ALL
    SELECT 'range type', t.typname::text
      FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
     WHERE n.nspname = 'public' AND t.typtype = 'r'
    UNION ALL
    SELECT 'standalone composite type', t.typname::text
      FROM pg_type t
      JOIN pg_namespace n ON n.oid = t.typnamespace
      LEFT JOIN pg_class c ON c.oid = t.typrelid
     WHERE n.nspname = 'public' AND t.typtype = 'c'
       AND (c.relkind IS NULL OR c.relkind = 'c')
    UNION ALL
    SELECT 'aggregate or window function', p.oid::regprocedure::text
      FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname = 'public' AND p.prokind IN ('a', 'w')
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
    UNION ALL
    SELECT 'collation', c.collname::text
      FROM pg_collation c JOIN pg_namespace n ON n.oid = c.collnamespace
     WHERE n.nspname = 'public'
    UNION ALL
    SELECT 'text search configuration', t.cfgname::text
      FROM pg_ts_config t JOIN pg_namespace n ON n.oid = t.cfgnamespace
     WHERE n.nspname = 'public'
    UNION ALL
    SELECT 'text search dictionary', t.dictname::text
      FROM pg_ts_dict t JOIN pg_namespace n ON n.oid = t.dictnamespace
     WHERE n.nspname = 'public'
    UNION ALL
    SELECT 'text search parser', t.prsname::text
      FROM pg_ts_parser t JOIN pg_namespace n ON n.oid = t.prsnamespace
     WHERE n.nspname = 'public'
    UNION ALL
    SELECT 'text search template', t.tmplname::text
      FROM pg_ts_template t JOIN pg_namespace n ON n.oid = t.tmplnamespace
     WHERE n.nspname = 'public'
    UNION ALL
    SELECT 'operator', o.oid::regoperator::text
      FROM pg_operator o JOIN pg_namespace n ON n.oid = o.oprnamespace
     WHERE n.nspname = 'public'
    UNION ALL
    SELECT 'operator class', c.opcname::text
      FROM pg_opclass c JOIN pg_namespace n ON n.oid = c.opcnamespace
     WHERE n.nspname = 'public'
    UNION ALL
    SELECT 'operator family', f.opfname::text
      FROM pg_opfamily f JOIN pg_namespace n ON n.oid = f.opfnamespace
     WHERE n.nspname = 'public'
    UNION ALL
    SELECT 'extended statistics', s.stxname::text
      FROM pg_statistic_ext s JOIN pg_namespace n ON n.oid = s.stxnamespace
     WHERE n.nspname = 'public'
    UNION ALL
    SELECT 'conversion', c.conname::text
      FROM pg_conversion c JOIN pg_namespace n ON n.oid = c.connamespace
     WHERE n.nspname = 'public'
""")


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


def replay_table_schemas(db_info: dict, engine, captured: dict[tuple[str, str], str]) -> None:
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
    log.info("--- Step 4c: replaying DDL for %d object(s) neither the ORM nor Alembic rebuilt ---",
             len(remaining))
    for kind, key in remaining:
        log.info("  %s %s", kind, key)
    env = {"PGPASSWORD": db_info["password"]}
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
            sys.exit(1)
        present = schema_inventory(engine)
        remaining = [k for k in remaining if k not in applied and k not in present]
    log.info("Schema replay complete.")


def dump_full(db_info: dict, output_path: str) -> None:
    """Full pg_dump as safety backup."""
    cmd = [
        "pg_dump",
        "-h", db_info["host"],
        "-p", db_info["port"],
        "-U", db_info["user"],
        "--no-owner",
        "--no-privileges",
        db_info["dbname"],
        "-f", output_path,
    ]
    env = {"PGPASSWORD": db_info["password"]}
    log.info("Full backup to %s ...", output_path)
    r = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if r.returncode != 0:
        log.error("pg_dump (full) failed: %s", r.stderr)
        sys.exit(1)
    log.info("Full backup complete: %s", output_path)


def drop_and_recreate_schema(engine) -> None:
    """DROP SCHEMA public CASCADE; CREATE SCHEMA public;"""
    log.info("Dropping schema...")
    with engine.connect() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
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
    env = {"PGPASSWORD": db_info["password"]}
    log.info("Reloading data from %s (strict=%s) ...", dump_path, strict)
    r = subprocess.run(cmd, env=env, capture_output=True, text=True)

    stderr_lines = (r.stderr or "").splitlines()
    error_lines = [line for line in stderr_lines if "ERROR" in line]

    if r.returncode != 0 or (strict and error_lines):
        log.error("Data reload FAILED (rc=%s, %d error lines):", r.returncode, len(error_lines))
        for e in error_lines[:30]:
            log.error("  %s", e)
        if full_backup_path:
            log.error("Pre-pave full backup preserved: %s", full_backup_path)
            log.error("Restore: psql -h %s -U %s -d %s -f %s",
                      db_info["host"], db_info["user"], db_info["dbname"], full_backup_path)
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


def pave_one(db_url: str, label: str, strict: bool = True) -> bool:
    """Pave a single database. Returns True on success."""
    db_url = _bypass_proxy_host(db_url)
    db_info = parse_db_url(db_url)
    dbname = db_info["dbname"]

    log.info("=" * 60)
    log.info("PAVE TARGET: %s (%s)", label, dbname)
    log.info("=" * 60)

    engine = create_engine(db_url)

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
    unsupported = unsupported_objects(engine)
    if unsupported:
        log.error("This database holds %d object(s) pave cannot rebuild:", len(unsupported))
        for obj in unsupported:
            log.error("  %s", obj)
        log.error("Refusing to pave: NOTHING has been dropped. The database is untouched.")
        log.error("Export them by hand (pg_dump --schema-only), or teach "
                  "capture_schema_objects() how to reproduce them, then re-run.")
        engine.dispose()
        sys.exit(1)

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

    # Step 3: Drop and recreate schema
    drop_and_recreate_schema(engine)

    # Step 4: create_all from ORM
    engine.dispose()
    engine = create_engine(db_url)
    create_all_from_orm(engine)

    # Step 4b: alembic upgrade head — the other half of entrypoint.sh's pairing.
    engine.dispose()
    alembic_upgrade_head(db_url)
    engine = create_engine(db_url)

    # Step 4c: replay the DDL of anything neither of those brought back.
    replay_table_schemas(db_info, engine, captured)
    engine.dispose()
    engine = create_engine(db_url)

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
        log.error("Pre-pave full backup preserved: %s", full_path)
        log.error("Restore: psql -h %s -U %s -d %s -f %s",
                  db_info["host"], db_info["user"], dbname, full_path)
        engine.dispose()
        sys.exit(1)

    # Step 4e: Bootstrap AI-tier connection roles (D105 Layer 2 / Sprint 1.x-S1).
    # After 4b/4c, so the grants cover the Alembic-base and plugin tables too
    # and not just the slice create_all() had made at this point.
    # Skip silently when TENANT_AI_READONLY_PASSWORD is unset (dev-machine path);
    # raise loudly when set-but-fails so a misconfigured prod pave halts here
    # rather than producing a tenant DB without the readonly role AI tools rely on.
    ai_readonly_pw = os.environ.get("TENANT_AI_READONLY_PASSWORD")
    ai_write_pw = os.environ.get("TENANT_AI_WRITE_PASSWORD")
    if ai_readonly_pw:
        from gdx_dispatch.tools.sql.ai_roles_bootstrap import apply_ai_readonly_role
        apply_ai_readonly_role(engine, ai_readonly_pw)
    else:
        log.warning("TENANT_AI_READONLY_PASSWORD not set; skipping gdx_ai_readonly role bootstrap")
    if ai_write_pw:
        from gdx_dispatch.tools.sql.ai_roles_bootstrap import apply_ai_write_role
        apply_ai_write_role(engine, ai_write_pw)
    else:
        log.warning("TENANT_AI_WRITE_PASSWORD not set; skipping gdx_ai_write role bootstrap")

    # Step 5: Reload data
    reload_data(db_info, data_path, strict=strict, full_backup_path=full_path)

    # Step 5b: Sprint 1.0.5 — seed pricing engine defaults AFTER reload.
    # Order matters: reload restores any pre-existing tier rows first; the
    # seeder is idempotent and only fills missing (category, class) sets so
    # user-edited tiers always win over stubs. Fail-loud — a paved tenant
    # without a working pricing engine is a worse outcome than a failed pave.
    try:
        from sqlalchemy.orm import sessionmaker as _sm

        from gdx_dispatch.models.pricing_engine import seed_default_pricing as _seed
        _S = _sm(bind=engine, future=True)
        with _S() as _s:
            _seed(_s)
        log.info("pricing_engine_seeded label=%s", label)
    except Exception as e:
        log.error("pricing_engine_seed_failed label=%s err=%s", label, e)
        engine.dispose()
        raise

    # Step 6: Fix sequences
    engine.dispose()
    engine = create_engine(db_url)
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

    ok = (verify_counts(pre_counts, post_counts)
          and not seq_failures and not lost_objects
          and not mv_failures and not mv_mismatch)
    if ok:
        # Deliberately narrower than "everything is back": this is name
        # equality over the kinds in _INVENTORY_SQL plus row counts. It does
        # not prove fidelity of an object's definition.
        log.info("✅ PAVE COMPLETE — every pre-pave object is present by name and "
                 "all row counts match for %s", label)
    else:
        log.warning("⚠ PAVE COMPLETE WITH MISMATCHES for %s", label)
        log.warning("  Full backup at: %s", full_path)
        log.warning("  Restore if needed: psql -d %s -f %s", dbname, full_path)

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
        ok = pave_one(args[0], "direct", strict=strict)
    else:
        label, url = default_target()
        ok = pave_one(url, label, strict=strict)
        log.info("  %-20s %s", label, "✅" if ok else "⚠ MISMATCHES")
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()

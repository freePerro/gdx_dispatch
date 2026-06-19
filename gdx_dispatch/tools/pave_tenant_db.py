#!/usr/bin/env python3
"""D1 Phase 2 — Nuke-and-pave a tenant database.

Drops all tables, recreates from ORM create_all(), reloads data.
The schema will match the ORM exactly — no more drift.

Usage (inside docker-app-1):
    python gdx_dispatch/tools/pave_tenant_db.py --all-tenants   # pave all CC-registered tenants
    python gdx_dispatch/tools/pave_tenant_db.py --tenant GDX     # pave one tenant by company_code
    python gdx_dispatch/tools/pave_tenant_db.py <database_url>   # pave one DB by direct URL

Flags:
    --strict     (default) ON_ERROR_STOP=on; aborts on first reload error. Safe.
    --no-strict  legacy ON_ERROR_STOP=off; logs errors and continues. Risk: silent data loss.

Steps per database:
    1. Dump data to /tmp/<dbname>_data.sql
    2. Full backup to /tmp/<dbname>_full.sql
    3. DROP SCHEMA public CASCADE; CREATE SCHEMA public
    4. TenantBase.metadata.create_all() — fresh schema from ORM
    5. Reload data from dump
    6. Fix sequences (setval to max id)
    7. Verify row counts match pre-pave counts
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from urllib.parse import urlparse

from sqlalchemy import create_engine, inspect, text

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
                row = conn.execute(text(f'SELECT COUNT(*) FROM "{table}"'))
                counts[table] = row.scalar()
            except Exception as e:
                log.warning("Could not count %s: %s", table, e)
                counts[table] = -1
    return counts


def get_table_names(engine) -> list[str]:
    """Get all table names."""
    return sorted(inspect(engine).get_table_names())


def dump_data(db_info: dict, output_path: str) -> None:
    """pg_dump --data-only to a file."""
    cmd = [
        "pg_dump",
        "-h", db_info["host"],
        "-p", db_info["port"],
        "-U", db_info["user"],
        "--data-only",
        "--disable-triggers",
        "--no-owner",
        "--no-privileges",
        db_info["dbname"],
        "-f", output_path,
    ]
    env = {"PGPASSWORD": db_info["password"]}
    log.info("Dumping data to %s ...", output_path)
    r = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if r.returncode != 0:
        log.error("pg_dump failed: %s", r.stderr)
        sys.exit(1)
    log.info("Data dump complete: %s", output_path)


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
    """Run TenantBase.metadata.create_all()."""
    # Import registers all models on TenantBase.metadata
    import gdx_dispatch.models  # noqa: F401
    from gdx_dispatch.core.audit import TenantBase

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


def fix_sequences(engine) -> None:
    """Reset all sequences to max(id) for their table."""
    log.info("Fixing sequences...")
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT sequencename, sequenceowner
            FROM pg_sequences
            WHERE schemaname = 'public'
        """)).fetchall()

        for seq_name, _ in rows:
            # Convention: sequence name is <table>_id_seq or <table>_<col>_seq
            # Try to find the table and column
            parts = seq_name.rsplit("_seq", 1)[0]
            # Try <table>_id pattern
            if parts.endswith("_id"):
                table = parts[:-3]
                col = "id"
            else:
                # Try last segment as column
                segments = parts.rsplit("_", 1)
                if len(segments) == 2:
                    table, col = segments
                else:
                    table = parts
                    col = "id"

            try:
                result = conn.execute(
                    text(f'SELECT MAX("{col}") FROM "{table}"')
                )
                max_val = result.scalar()
                if max_val is not None:
                    conn.execute(
                        text(f"SELECT setval('{seq_name}', :val)")
                        .bindparams(val=int(max_val))
                    )
            except Exception as e:
                log.warning("pave_tenant_db: sequence setval skipped for %s (table=%s col=%s): %s",
                            seq_name, table, col, e)

        conn.commit()
    log.info("Sequences fixed.")


def verify_counts(pre_counts: dict[str, int], post_counts: dict[str, int]) -> bool:
    """Compare row counts. Returns True if all match."""
    ok = True
    for table in sorted(set(pre_counts) | set(post_counts)):
        pre = pre_counts.get(table, 0)
        post = post_counts.get(table, 0)
        if pre != post and pre > 0:
            log.warning("  MISMATCH %s: %d -> %d (lost %d rows)",
                        table, pre, post, pre - post)
            ok = False
    return ok


def resolve_tenant_urls() -> list[tuple[str, str]]:
    """Read all tenant DB URLs from the control plane. Returns [(slug, db_url)].

    The control DB (docker-control-db-1:gdx_control) has a `tenants` table with
    slug and db_url_enc. db_url_enc may be Fernet-encrypted or plaintext
    depending on GDX_FERNET_KEY.
    """
    control_url = os.getenv("CONTROL_DATABASE_URL")
    if not control_url:
        log.error("CONTROL_DATABASE_URL not set")
        sys.exit(1)

    # Import the decrypt helper from the app
    from gdx_dispatch.core.database import _decrypt_db_url

    control_engine = create_engine(control_url)
    results = []
    with control_engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT slug, db_url_enc
            FROM tenants
            WHERE deleted_at IS NULL
              AND db_url_enc IS NOT NULL
            ORDER BY slug
        """)).fetchall()

        for slug, db_url_enc in rows:
            try:
                url = _decrypt_db_url(db_url_enc)
                results.append((slug, url))
            except Exception as e:
                log.warning("Could not decrypt URL for tenant %s: %s", slug, e)

    control_engine.dispose()
    return results


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
    total = sum(v for v in pre_counts.values() if v > 0)
    log.info("  %d tables, %d total rows", len(pre_counts), total)
    for t, c in sorted(pre_counts.items(), key=lambda x: -x[1])[:10]:
        log.info("  %-40s %6d", t, c)

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

    # Step 4b: Bootstrap AI-tier connection roles (D105 Layer 2 / Sprint 1.x-S1).
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
    fix_sequences(engine)

    # Step 7: Verify
    log.info("--- Step 7: Verify row counts ---")
    post_counts = get_row_counts(engine)
    post_total = sum(v for v in post_counts.values() if v > 0)
    log.info("  %d tables, %d total rows", len(post_counts), post_total)

    ok = verify_counts(pre_counts, post_counts)
    if ok:
        log.info("✅ PAVE COMPLETE — all row counts match for %s", label)
    else:
        log.warning("⚠ PAVE COMPLETE WITH MISMATCHES for %s", label)
        log.warning("  Full backup at: %s", full_path)
        log.warning("  Restore if needed: psql -d %s -f %s", dbname, full_path)

    new_tables = set(post_counts) - set(pre_counts)
    if new_tables:
        log.info("  New tables from ORM: %s", ", ".join(sorted(new_tables)))

    dropped = set(pre_counts) - set(post_counts)
    if dropped:
        log.info("  Tables no longer in ORM (data lost): %s", ", ".join(sorted(dropped)))

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

    if not args:
        print("Usage:")
        print("  python pave_tenant_db.py [--strict|--no-strict] --all-tenants")
        print("  python pave_tenant_db.py [--strict|--no-strict] --tenant <CODE>")
        print("  python pave_tenant_db.py [--strict|--no-strict] <database_url>")
        print("")
        print("  --strict     (default) abort on any reload error; preserves full backup.")
        print("  --no-strict  legacy behavior; logs errors but continues. Risk: silent data loss.")
        sys.exit(1)

    if args[0] == "--all-tenants":
        tenants = resolve_tenant_urls()
        log.info("Found %d tenants to pave (strict=%s)", len(tenants), strict)
        tenants.sort(key=lambda x: (0 if x[0].lower() == "gdx" else 1, x[0]))
        results = {}
        for code, url in tenants:
            results[code] = pave_one(url, code, strict=strict)
        log.info("")
        log.info("=" * 60)
        log.info("SUMMARY")
        log.info("=" * 60)
        for code, ok in results.items():
            status = "✅" if ok else "⚠ MISMATCHES"
            log.info("  %-20s %s", code, status)

    elif args[0] == "--tenant":
        if len(args) < 2:
            print("Usage: python pave_tenant_db.py --tenant <COMPANY_CODE>")
            sys.exit(1)
        code = args[1].lower()
        tenants = resolve_tenant_urls()
        match = [(c, u) for c, u in tenants if c.lower() == code]
        if not match:
            log.error("Tenant %s not found. Available: %s",
                      code, ", ".join(c for c, _ in tenants))
            sys.exit(1)
        pave_one(match[0][1], match[0][0], strict=strict)

    else:
        # Direct URL
        pave_one(args[0], "direct", strict=strict)


if __name__ == "__main__":
    main()

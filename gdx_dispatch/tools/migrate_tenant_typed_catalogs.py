"""Non-destructive tenant-plane migration for typed-catalog sprint.

Adds:
  1. custom_catalogs.product_class      (varchar(40) NOT NULL DEFAULT 'parts')
  2. custom_catalog_items.product_class (varchar(40) NOT NULL DEFAULT 'parts')
  3. door_specs                          (new table — created by create_all)

Idempotent: ADD COLUMN IF NOT EXISTS skips already-applied tenants. The
door_specs table comes from TenantBase.metadata.create_all(checkfirst=True)
so re-runs are safe.

Why this exists: tenant plane has no alembic. CLAUDE.md says
TenantBase.metadata.create_all() at signup repairs new tables, but it does
NOT add columns to existing tables. This script handles that gap for one
sprint; the proper fix (tenant-plane migration framework) is parking-lot.

Per feedback_pave_is_destructive.md — this is NOT pave. No DROP SCHEMA.
ADD COLUMN with IF NOT EXISTS only. Safe to re-run.

Usage:
    # Single tenant (for testing):
    python -m gdx_dispatch.tools.migrate_tenant_typed_catalogs --tenant-uuid <uuid>

    # All tenants:
    python -m gdx_dispatch.tools.migrate_tenant_typed_catalogs --all

    # Dry run (print SQL without executing):
    python -m gdx_dispatch.tools.migrate_tenant_typed_catalogs --all --dry-run
"""
from __future__ import annotations

import argparse
import sys

from sqlalchemy import create_engine, text

# Importing gdx_dispatch.models registers EVERY tenant-plane model on TenantBase.metadata
# so that create_all(checkfirst=True) below picks up new tables (door_specs).
# Without this import, TenantBase.metadata only contains whatever was already
# imported by the time the migrator ran, and create_all silently no-ops on
# the new table.
import gdx_dispatch.models  # noqa: F401 — side-effect: registers tenant models
from gdx_dispatch.core.audit import TenantBase

ALTER_CATALOGS_SQL = """
ALTER TABLE custom_catalogs
  ADD COLUMN IF NOT EXISTS product_class varchar(40) NOT NULL DEFAULT 'parts';
"""

ALTER_ITEMS_SQL = """
ALTER TABLE custom_catalog_items
  ADD COLUMN IF NOT EXISTS product_class varchar(40) NOT NULL DEFAULT 'parts';
"""

INDEX_CATALOGS_SQL = """
CREATE INDEX IF NOT EXISTS ix_custom_catalogs_product_class
  ON custom_catalogs (product_class);
"""

INDEX_ITEMS_SQL = """
CREATE INDEX IF NOT EXISTS ix_custom_catalog_items_product_class
  ON custom_catalog_items (product_class);
"""


def migrate_one(db_url: str, dry_run: bool = False) -> None:
    engine = create_engine(db_url)
    statements = [
        ("ALTER custom_catalogs", ALTER_CATALOGS_SQL),
        ("ALTER custom_catalog_items", ALTER_ITEMS_SQL),
        ("INDEX product_class on catalogs", INDEX_CATALOGS_SQL),
        ("INDEX product_class on items", INDEX_ITEMS_SQL),
    ]
    if dry_run:
        for label, sql in statements:
            print(f"-- {label}\n{sql.strip()}\n")
        print("-- door_specs created via TenantBase.metadata.create_all(checkfirst=True)")
        return

    with engine.begin() as conn:
        for label, sql in statements:
            print(f"  applying: {label}")
            conn.execute(text(sql))
    # New tables (door_specs) — non-destructive, only creates missing tables
    TenantBase.metadata.create_all(engine, checkfirst=True)
    engine.dispose()
    print("  ✅ migration complete")


def list_tenant_db_urls() -> list[tuple[str, str]]:
    """Return (tenant_label, db_url) for every active tenant in control plane."""
    from gdx_dispatch.core.database import CONTROL_DATABASE_URL, _decrypt_db_url

    control_engine = create_engine(CONTROL_DATABASE_URL)
    rows: list[tuple[str, str]] = []
    with control_engine.begin() as conn:
        result = conn.execute(text(
            "SELECT id, slug, db_url_enc FROM tenants WHERE deleted_at IS NULL"
        )).mappings().all()
        for r in result:
            if not r["db_url_enc"]:
                continue
            try:
                url = _decrypt_db_url(r["db_url_enc"])
            except Exception as exc:
                print(f"  ! decrypt failed for {r['slug']}: {exc}")
                continue
            rows.append((f"{r['slug']} ({r['id']})", url))
    control_engine.dispose()
    return rows


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-uuid", help="Migrate one tenant by UUID")
    parser.add_argument("--db-url", help="Migrate one tenant by db_url directly")
    parser.add_argument("--all", action="store_true", help="Migrate all tenants")
    parser.add_argument("--dry-run", action="store_true", help="Print SQL without executing")
    args = parser.parse_args(argv[1:])

    if args.db_url:
        print(f"Migrating direct db_url ({args.db_url[:60]}...)")
        migrate_one(args.db_url, dry_run=args.dry_run)
        return 0

    if args.tenant_uuid:
        from sqlalchemy import create_engine as ce
        from gdx_dispatch.core.database import CONTROL_DATABASE_URL, _decrypt_db_url

        eng = ce(CONTROL_DATABASE_URL)
        with eng.begin() as conn:
            row = conn.execute(
                text("SELECT slug, db_url_enc FROM tenants WHERE id = :id"),
                {"id": args.tenant_uuid},
            ).mappings().first()
        eng.dispose()
        if not row:
            print(f"tenant {args.tenant_uuid} not found")
            return 2
        url = _decrypt_db_url(row["db_url_enc"])
        print(f"Migrating tenant {row['slug']} ({args.tenant_uuid})")
        migrate_one(url, dry_run=args.dry_run)
        return 0

    if args.all:
        tenants = list_tenant_db_urls()
        print(f"Found {len(tenants)} tenants")
        for label, url in tenants:
            print(f"\n→ {label}")
            try:
                migrate_one(url, dry_run=args.dry_run)
            except Exception as exc:
                print(f"  ! FAILED: {exc}")
        return 0

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))

#!/usr/bin/env python3
"""Sprint role-permissions 1.5 — backfill builtin role permissions.

For each tenant (or a specific one via --tenant), upserts the 7 builtin roles
in tenant_roles to match the canonical seed in gdx_dispatch.core.permissions.BUILTIN_ROLES.
Pre-fix, tenants either had no rows (lazy seed never fired) or had rows whose
`permissions` JSON was customized by admins on the old (non-enforcing) UI.

Strategy — UPSERT by (company_id, name):
- If row exists: overwrite `permissions`, `description`, set `is_system=True`,
  bump `updated_at`. Preserves `id` so existing UserRoleAssignment rows keep
  pointing at the same role.
- If row missing: insert fresh.
- Custom (non-builtin) roles are LEFT ALONE; admin reviews them at /role-permissions.

Banner: writes/updates `tenant_feature_flags.role_permissions_reset_pending = 1`
so the role-permissions banner endpoint shows "your permissions were reset"
until an admin acknowledges.

Usage:
    python gdx_dispatch/tools/backfill_role_permissions.py --tenant gdx --dry-run
    python gdx_dispatch/tools/backfill_role_permissions.py --tenant gdx
    python gdx_dispatch/tools/backfill_role_permissions.py --all
    python gdx_dispatch/tools/backfill_role_permissions.py <database_url>

Idempotent: rerunning resets builtin perms again (safe — they're canonical).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Iterable

# When invoked directly as `python /app/gdx_dispatch/tools/backfill_role_permissions.py`
# the script's parent (tools/) is on sys.path[0], NOT the project root, so
# `from gdx_dispatch.core.permissions import ...` fails with ModuleNotFoundError
# (S114 ssh-via-VPS attempt hit this). Self-heal by inserting the project
# root onto sys.path. Idempotent — duplicate entries are deduped by Python.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from gdx_dispatch.core.permissions import BUILTIN_DESCRIPTIONS, BUILTIN_ROLES

log = logging.getLogger(__name__)


def _resolve_db_urls(tenant: str | None, url: str | None, all_tenants: bool) -> list[tuple[str, str]]:
    if url:
        return [("<url>", url)]
    from gdx_dispatch.control.models import Tenant
    from gdx_dispatch.core.database import SessionLocal, _decrypt_db_url

    with SessionLocal() as cdb:
        if all_tenants:
            rows = cdb.execute(
                select(Tenant).where(Tenant.deleted_at.is_(None))
            ).scalars().all()
        elif tenant:
            row = cdb.execute(
                select(Tenant).where(Tenant.slug == tenant.strip().lower())
            ).scalar_one_or_none()
            if row is None:
                raise SystemExit(f"No tenant with slug={tenant!r}")
            rows = [row]
        else:
            raise SystemExit("Provide --tenant <slug>, --all, or a database URL.")
        return [(r.slug, _decrypt_db_url(str(r.db_url_enc))) for r in rows if r.db_url_enc]


def backfill_one(slug: str, db_url: str, *, dry_run: bool) -> dict[str, int]:
    """Reset builtin roles + raise the banner flag for one tenant."""
    from gdx_dispatch.models.tenant_models import TenantRole
    from gdx_dispatch.provisioning.models import TenantFeatureFlag

    eng = create_engine(db_url, future=True)
    counts = {"upserted": 0, "inserted": 0, "updated": 0, "banner_set": 0, "banner_skipped": 0}
    try:
        with Session(eng, future=True) as db:
            # Tenant DB stores company_id as the tenant slug in practice for some
            # legacy paths but the canonical value is the UUID — both shapes have
            # been seen in prod (D97). Read whatever is already used in this DB
            # so the upsert lines up; fall back to the supplied slug.
            existing_company_id = db.execute(
                select(TenantRole.company_id).limit(1)
            ).scalar_one_or_none()
            company_id = existing_company_id or slug

            for name, perms in BUILTIN_ROLES.items():
                row = db.execute(
                    select(TenantRole).where(
                        TenantRole.company_id == company_id,
                        TenantRole.name == name,
                    )
                ).scalar_one_or_none()
                perms_json = json.dumps(perms)
                if row is None:
                    counts["inserted"] += 1
                    if not dry_run:
                        db.add(
                            TenantRole(
                                company_id=company_id,
                                name=name,
                                description=BUILTIN_DESCRIPTIONS.get(name),
                                permissions=perms_json,
                                is_system=True,
                            )
                        )
                else:
                    if (
                        row.permissions != perms_json
                        or not row.is_system
                        or row.description != BUILTIN_DESCRIPTIONS.get(name)
                    ):
                        counts["updated"] += 1
                    if not dry_run:
                        row.permissions = perms_json
                        row.description = BUILTIN_DESCRIPTIONS.get(name)
                        row.is_system = True
                counts["upserted"] += 1

            if not dry_run:
                # Commit the role upserts BEFORE the banner write. If the banner
                # path fails (e.g. pre-existing tenant DB without the
                # tenant_feature_flags table), the role data still lands.
                db.commit()

            # Banner flag — non-essential. Older tenant DBs may not have the
            # tenant_feature_flags table at all; swallow that and continue.
            try:
                flag = db.execute(
                    select(TenantFeatureFlag).where(
                        TenantFeatureFlag.flag_key == "role_permissions_reset_pending"
                    )
                ).scalar_one_or_none()
                if flag is None:
                    counts["banner_set"] = 1
                    if not dry_run:
                        db.add(
                            TenantFeatureFlag(
                                flag_key="role_permissions_reset_pending",
                                rollout_pct=1,
                            )
                        )
                        db.commit()
                elif flag.rollout_pct != 1:
                    counts["banner_set"] = 1
                    if not dry_run:
                        flag.rollout_pct = 1
                        db.commit()
            except Exception as e:
                if not dry_run:
                    db.rollback()
                log.warning("banner_write_skipped tenant=%s reason=%s", slug, e.__class__.__name__)
                counts["banner_skipped"] = 1
    finally:
        eng.dispose()
    return counts


def run(targets: Iterable[tuple[str, str]], *, dry_run: bool) -> int:
    rc = 0
    for slug, db_url in targets:
        try:
            c = backfill_one(slug, db_url, dry_run=dry_run)
            mode = "DRY-RUN" if dry_run else "APPLIED"
            print(
                f"backfill_role_permissions {mode} tenant={slug} "
                f"upserted={c['upserted']} inserted={c['inserted']} "
                f"updated={c['updated']} banner_set={c['banner_set']} "
                f"banner_skipped={c.get('banner_skipped', 0)}"
            )
        except Exception as e:
            log.exception("backfill_failed tenant=%s", slug)
            print(f"backfill_role_permissions FAILED tenant={slug} error={e!r}")
            rc = 2
    return rc


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--tenant", help="tenant slug (resolved via control plane)")
    p.add_argument("--all", action="store_true", help="apply to every active tenant")
    p.add_argument("--dry-run", action="store_true", help="report counts; do not modify rows")
    p.add_argument("url", nargs="?", help="direct database URL (skips control plane lookup)")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    targets = _resolve_db_urls(args.tenant, args.url, args.all)
    return run(targets, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())

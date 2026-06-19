#!/usr/bin/env python3
"""D-leads-authz-sweep — additive `leads.*` snapshot migration.

Paired rollout step for the leads authz sweep (Approach D). The leads
router now gates on `require_permission("leads.{read,write,delete}")`.
The resolver is INTENTIONALLY unchanged (it still honors a tenant's
TenantRole snapshot verbatim — the documented `permissions.py:177-178`
"resolver honors their DB row" contract for the 5 editable builtin
roles is preserved; no UNION, no silent escalation).

So a snapshotted non-admin builtin role only gains `leads.*` when its
snapshot gains it. This tool does exactly that — and ONLY that:

  - ADD-ONLY: it never removes a key, never touches a non-`leads.*`
    key, never touches custom roles, admin, owner, or technician.
  - Per the matrix (mirrors BUILTIN_ROLES additions in this sweep):
        dispatcher, sales → leads.read, leads.write, leads.delete
        accounting, viewer → leads.read
    admin/owner resolve via live BUILTIN (resolver step 3) so they get
    `leads.*` the moment the code deploys — no snapshot edit needed.
    technician is excluded by the sweep matrix.
  - is_system + exact-name gated: a tenant's CUSTOM role that happens
    to be named "sales" is left untouched (only the seeded builtin row
    is amended).
  - Idempotent: a key already present is a no-op; safe to re-run.

Because it is purely additive and only touches brand-new keys (no
existing snapshot could have *deliberately removed* `leads.*` — the
keys did not exist when any current snapshot was written), running it
cannot escalate privilege or reverse any tenant customization. A tenant
skipped (stale connect string) simply doesn't see leads until a re-run;
nothing else breaks. This is the generalizable discipline: every new
BUILTIN key ships with its paired additive snapshot migration.

Usage:
    python gdx_dispatch/tools/add_leads_perms_to_role_snapshots.py --tenant gdx --dry-run
    python gdx_dispatch/tools/add_leads_perms_to_role_snapshots.py --tenant gdx
    python gdx_dispatch/tools/add_leads_perms_to_role_snapshots.py --all --dry-run
    python gdx_dispatch/tools/add_leads_perms_to_role_snapshots.py --all
    python gdx_dispatch/tools/add_leads_perms_to_role_snapshots.py <database_url>
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Iterable

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

# Reuse the proven control-plane enumeration + db_url decrypt path.
from gdx_dispatch.tools.backfill_role_permissions import _resolve_db_urls

log = logging.getLogger(__name__)

# Mirrors exactly the BUILTIN_ROLES additions made in this sweep
# (gdx_dispatch/core/permissions.py). Keep in lockstep with that file.
LEADS_ROLE_GRANTS: dict[str, list[str]] = {
    "dispatcher": ["leads.read", "leads.write", "leads.delete"],
    "sales": ["leads.read", "leads.write", "leads.delete"],
    "accounting": ["leads.read"],
    "viewer": ["leads.read"],
}


def amend_one(slug: str, db_url: str, *, dry_run: bool) -> dict[str, int]:
    """Add (only) the matrix `leads.*` keys to the seeded builtin role
    snapshots in one tenant DB. Add-only, idempotent, is_system+name
    gated."""
    from gdx_dispatch.models.tenant_models import TenantRole

    eng = create_engine(db_url, future=True)
    counts = {"roles_amended": 0, "keys_added": 0, "roles_noop": 0, "roles_absent": 0}
    try:
        with Session(eng, future=True) as db:
            existing_company_id = db.execute(
                select(TenantRole.company_id).limit(1)
            ).scalar_one_or_none()
            company_id = existing_company_id or slug

            for role_name, keys in LEADS_ROLE_GRANTS.items():
                row = db.execute(
                    select(TenantRole).where(
                        TenantRole.company_id == company_id,
                        TenantRole.name == role_name,
                    )
                ).scalar_one_or_none()
                if row is None:
                    counts["roles_absent"] += 1
                    continue
                # Only the SEEDED builtin row — never a tenant's custom
                # role that happens to share the name.
                if not bool(row.is_system):
                    counts["roles_absent"] += 1
                    continue

                try:
                    cur = json.loads(row.permissions) if isinstance(row.permissions, str) else (row.permissions or [])
                    if not isinstance(cur, list):
                        cur = list(cur)
                except (ValueError, TypeError):
                    cur = []

                have = set(cur)
                missing = [k for k in keys if k not in have]
                if not missing:
                    counts["roles_noop"] += 1
                    continue

                counts["roles_amended"] += 1
                counts["keys_added"] += len(missing)
                if not dry_run:
                    # Append (preserve existing order); ADD-only.
                    row.permissions = json.dumps(cur + missing)

            if not dry_run:
                db.commit()
    finally:
        eng.dispose()
    return counts


def run(targets: Iterable[tuple[str, str]], *, dry_run: bool) -> int:
    rc = 0
    for slug, db_url in targets:
        try:
            c = amend_one(slug, db_url, dry_run=dry_run)
            mode = "DRY-RUN" if dry_run else "APPLIED"
            print(
                f"add_leads_perms {mode} tenant={slug} "
                f"roles_amended={c['roles_amended']} keys_added={c['keys_added']} "
                f"roles_noop={c['roles_noop']} roles_absent={c['roles_absent']}"
            )
        except Exception as e:
            log.exception("add_leads_perms_failed tenant=%s", slug)
            print(f"add_leads_perms FAILED tenant={slug} error={e!r}")
            rc = 2
    return rc


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    p = argparse.ArgumentParser(description="Additive leads.* snapshot migration (D-leads-authz-sweep).")
    p.add_argument("--tenant", help="tenant slug (single)")
    p.add_argument("--all", action="store_true", help="every non-deleted tenant")
    p.add_argument("--dry-run", action="store_true", help="report only; no writes")
    p.add_argument("url", nargs="?", help="direct database URL (skips control-plane lookup)")
    args = p.parse_args()
    targets = _resolve_db_urls(args.tenant, args.url, args.all)
    return run(targets, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())

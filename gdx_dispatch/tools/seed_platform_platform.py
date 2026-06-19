"""Seed synthetic platform-internal platform rows (SS-4 slice A).

Order matters because downstream backfills depend on these FK targets.
This module is idempotent and safe to re-run.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from uuid import NAMESPACE_DNS, uuid4, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.models.platform import CapabilitySet, Identity
from gdx_dispatch.models.platform_extensions import BillingAccount, DeveloperAccount, OAuthClient

PLATFORM_ADMIN_IDENTITY_ID = uuid5(NAMESPACE_DNS, "platform.admin.example.com")
PLATFORM_DEV_ACCOUNT_ID = uuid5(NAMESPACE_DNS, "platform.developer.example.com")
PLATFORM_BILLING_ID = uuid5(NAMESPACE_DNS, "platform.billing.example.com")

PLATFORM_INTERNAL_APPS = [
    {
        "client_id": "gdx_oauth_self_pat",
        "name": "GDX Self-PAT",
        "description": "Synthetic app for user-minted personal access tokens",
    },
    {
        "client_id": "gdx_oauth_service_account",
        "name": "GDX Service Account",
        "description": "an earlier session service account compatibility",
    },
    {
        "client_id": "gdx_oauth_mcp_internal",
        "name": "GDX MCP Internal",
        "description": "Platform-internal MCP server installation",
    },
    {
        "client_id": "gdx_oauth_platform_internal",
        "name": "GDX Platform Internal Jobs",
        "description": "Cron, drift scanner, nightly aggregation, migration runner",
    },
]

DEFAULT_CAPABILITY_SETS = [
    {"name": "role:owner", "scope_type": "tenant", "description": "Tenant owner"},
    {"name": "role:admin", "scope_type": "tenant", "description": "Tenant admin"},
    {"name": "role:tech", "scope_type": "tenant", "description": "Field technician"},
    {"name": "role:contractor", "scope_type": "tenant", "description": "External contractor"},
    {"name": "role:viewer", "scope_type": "tenant", "description": "Read-only access"},
    {
        "name": "platform:internal",
        "scope_type": "platform",
        "description": "Platform-internal jobs (D-53)",
    },
    {
        "name": "platform:service_account_full",
        "scope_type": "platform",
        "description": "an earlier session service account compatibility set",
    },
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def seed_platform(db: Session, dry_run: bool = False) -> dict:
    created = {
        "platform_admin_identity": 0,
        "developer_account": 0,
        "billing_account": 0,
        "oauth_clients": 0,
        "capability_sets": 0,
    }
    results: dict[str, object] = {}

    platform_admin = db.get(Identity, PLATFORM_ADMIN_IDENTITY_ID)
    if platform_admin is None:
        platform_admin = Identity(
            id=PLATFORM_ADMIN_IDENTITY_ID,
            email="platform-admin@example.com",
            display_name="GDX Platform Admin (synthetic)",
            status="active",
            email_verified_at=_now(),
            created_at=_now(),
        )
        db.add(platform_admin)
        db.flush()
        created["platform_admin_identity"] += 1
    results["platform_admin_identity_id"] = str(platform_admin.id)

    platform_dev = db.get(DeveloperAccount, PLATFORM_DEV_ACCOUNT_ID)
    if platform_dev is None:
        platform_dev = DeveloperAccount(
            id=PLATFORM_DEV_ACCOUNT_ID,
            email="platform-internal@example.com",
            display_name="GDX Platform (synthetic)",
            password_hash=None,
            email_verified_at=_now(),
            status="active",
        )
        db.add(platform_dev)
        db.flush()
        created["developer_account"] += 1
    results["platform_developer_id"] = str(platform_dev.id)

    platform_billing = db.get(BillingAccount, PLATFORM_BILLING_ID)
    if platform_billing is None:
        platform_billing = BillingAccount(
            id=PLATFORM_BILLING_ID,
            owner_type="platform",
            owner_id=PLATFORM_DEV_ACCOUNT_ID,
            stripe_customer_id=None,
            status="active",
            invoice_email="platform-internal@example.com",
        )
        db.add(platform_billing)
        db.flush()
        created["billing_account"] += 1
    results["platform_billing_id"] = str(platform_billing.id)

    oauth_client_ids: dict[str, str] = {}
    for app in PLATFORM_INTERNAL_APPS:
        client = db.execute(
            select(OAuthClient).where(OAuthClient.client_id == app["client_id"])
        ).scalar_one_or_none()
        if client is None:
            client = OAuthClient(
                id=uuid4(),
                client_id=app["client_id"],
                name=app["name"],
                description=app["description"],
                owner_type="platform",
                owner_id=PLATFORM_DEV_ACCOUNT_ID,
                redirect_uris=[],
                scopes_requested=[],
                client_type="confidential",
            )
            db.add(client)
            db.flush()
            created["oauth_clients"] += 1
        oauth_client_ids[app["client_id"]] = str(client.id)
    results["oauth_client_ids"] = oauth_client_ids

    capability_set_ids: dict[str, str] = {}
    for cap_set in DEFAULT_CAPABILITY_SETS:
        existing = db.execute(
            select(CapabilitySet).where(
                CapabilitySet.name == cap_set["name"],
                CapabilitySet.scope_type == cap_set["scope_type"],
            )
        ).scalar_one_or_none()
        if existing is None:
            existing = CapabilitySet(
                id=uuid4(),
                name=cap_set["name"],
                description=cap_set["description"],
                scope_type=cap_set["scope_type"],
            )
            db.add(existing)
            db.flush()
            created["capability_sets"] += 1
        capability_set_ids[cap_set["name"]] = str(existing.id)
    results["capability_set_ids"] = capability_set_ids

    if dry_run:
        db.rollback()
    else:
        db.commit()

    return {"created": created, "ids": results}


def _main() -> int:
    parser = argparse.ArgumentParser(description="Seed synthetic platform platform rows.")
    parser.add_argument("--apply", action="store_true", help="Persist changes (default is dry-run).")
    args = parser.parse_args()

    from gdx_dispatch.core.database import SessionLocal

    with SessionLocal() as db:
        result = seed_platform(db, dry_run=not args.apply)
        mode = "APPLY" if args.apply else "DRY-RUN"
        print(f"{mode} seed_platform_platform result:")
        print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())

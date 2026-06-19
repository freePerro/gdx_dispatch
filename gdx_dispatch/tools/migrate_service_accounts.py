"""Migrate an earlier session service_accounts into SS-3 access_tokens."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from gdx_dispatch.control.models import ServiceAccount, Tenant
from gdx_dispatch.models.platform_extensions import AccessToken, Installation, OAuthClient


def _now() -> datetime:
    return datetime.now(timezone.utc)


def migrate_service_accounts(
    db: Session,
    platform_results: dict,
    dry_run: bool = True,
) -> dict:
    stats = {
        "service_accounts_seen": 0,
        "service_accounts_migrated": 0,
        "installations_created": 0,
        "access_tokens_created": 0,
        "skipped_missing_tenant": 0,
        "errors": [],
    }

    oauth_client = db.execute(
        select(OAuthClient).where(OAuthClient.client_id == "gdx_oauth_service_account")
    ).scalar_one_or_none()
    if oauth_client is None:
        raise RuntimeError("Missing oauth client 'gdx_oauth_service_account'. Run seed_platform_platform.py first.")

    capset_id = UUID(str(platform_results["ids"]["capability_set_ids"]["platform:service_account_full"]))
    billing_id = UUID(str(platform_results["ids"]["platform_billing_id"]))
    installer_identity_id = UUID(str(platform_results["ids"]["platform_admin_identity_id"]))

    tenants_by_uuid = {str(t.id): t for t in db.execute(select(Tenant)).scalars().all()}
    all_tenant_uuids = list(tenants_by_uuid.keys())

    service_accounts = db.execute(
        select(ServiceAccount).where(ServiceAccount.revoked_at.is_(None))
    ).scalars().all()
    stats["service_accounts_seen"] = len(service_accounts)

    for sa in service_accounts:
        allowed_tenants = sa.allowed_tenant_uuids if sa.allowed_tenant_uuids is not None else all_tenant_uuids
        if not allowed_tenants:
            continue
        for tenant_uuid_str in allowed_tenants:
            if tenant_uuid_str not in tenants_by_uuid:
                stats["skipped_missing_tenant"] += 1
                continue
            tenant_uuid = tenants_by_uuid[tenant_uuid_str].id
            try:
                install = db.execute(
                    select(Installation).where(
                        Installation.oauth_client_id == oauth_client.id,
                        Installation.tenant_id == tenant_uuid,
                    )
                ).scalar_one_or_none()
                if install is None:
                    install = Installation(
                        id=uuid4(),
                        oauth_client_id=oauth_client.id,
                        tenant_id=tenant_uuid,
                        installer_identity_id=installer_identity_id,
                        capability_set_id=capset_id,
                        billing_account_id=billing_id,
                        status="active",
                        config={},
                        health_status="healthy",
                    )
                    db.add(install)
                    db.flush()
                    stats["installations_created"] += 1

                token_exists = db.execute(
                    select(AccessToken).where(
                        and_(
                            AccessToken.owner_type == "service_account",
                            AccessToken.owner_id == sa.id,
                            AccessToken.installation_id == install.id,
                            AccessToken.revoked_at.is_(None),
                        )
                    )
                ).scalar_one_or_none()
                if token_exists is None:
                    db.add(
                        AccessToken(
                            id=uuid4(),
                            prefix=sa.key_prefix,
                            secret_hash=sa.key_hash,
                            owner_type="service_account",
                            owner_id=sa.id,
                            installation_id=install.id,
                            capability_set_id=capset_id,
                            name=sa.name,
                            expires_at=None,
                            last_used_at=sa.last_used_at,
                            created_at=sa.created_at or _now(),
                            revoked_at=sa.revoked_at,
                            key_version=1,
                        )
                    )
                    stats["access_tokens_created"] += 1
                stats["service_accounts_migrated"] += 1
            except Exception as exc:  # pragma: no cover - defensive stat capture
                stats["errors"].append(f"service_account={sa.id}, tenant={tenant_slug}: {exc}")

    if dry_run:
        db.rollback()
    else:
        db.commit()
    return stats


def _main() -> int:
    parser = argparse.ArgumentParser(description="Migrate service_accounts to access_tokens.")
    parser.add_argument("--apply", action="store_true", help="Persist changes (default is dry-run).")
    args = parser.parse_args()

    from gdx_dispatch.core.database import SessionLocal
    from gdx_dispatch.tools.seed_platform_platform import seed_platform

    with SessionLocal() as db:
        platform = seed_platform(db, dry_run=True)
        result = migrate_service_accounts(db, platform_results=platform, dry_run=not args.apply)
        mode = "APPLY" if args.apply else "DRY-RUN"
        print(f"{mode} migrate_service_accounts result:")
        print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())

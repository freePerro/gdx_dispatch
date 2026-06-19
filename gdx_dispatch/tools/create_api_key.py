"""CLI to mint a tenant API key.

Usage:
    python -m gdx_dispatch.tools.create_api_key \
        --tenant gdx \
        --name "example.com lead form" \
        --scopes landing_leads:write \
        [--expires-days 365]

Resolves --tenant by slug or UUID against the control DB. Prints the raw key
ONCE — there is no way to recover it later. Hash + prefix are stored.

Run inside the prod container:
    docker exec -it docker-app-1 python -m gdx_dispatch.tools.create_api_key --tenant gdx ...
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import text


def _resolve_tenant_id(db, tenant_arg: str) -> UUID:
    """Look up tenant by UUID or slug. Returns UUID."""
    try:
        return UUID(tenant_arg)
    except ValueError:
        pass

    row = db.execute(
        text("SELECT id FROM tenants WHERE slug = :s AND deleted_at IS NULL"),
        {"s": tenant_arg},
    ).mappings().first()
    if not row:
        raise SystemExit(f"ERROR: tenant slug={tenant_arg!r} not found")
    return UUID(str(row["id"]))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Mint a tenant API key for the public REST API.")
    ap.add_argument("--tenant", required=True, help="Tenant slug or UUID (e.g. 'gdx').")
    ap.add_argument("--name", required=True, help="Human-readable label.")
    ap.add_argument(
        "--scopes",
        required=True,
        help="Comma-separated scopes (e.g. 'landing_leads:write').",
    )
    ap.add_argument(
        "--expires-days",
        type=int,
        default=None,
        help="Optional expiry in days. Default: never expires.",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate inputs + print what would be created without writing.",
    )
    args = ap.parse_args(argv)

    # Validate scopes against the canonical set
    from gdx_dispatch.core.api_keys import VALID_SCOPES, generate_api_key

    requested = {s.strip() for s in args.scopes.split(",") if s.strip()}
    invalid = requested - VALID_SCOPES
    if invalid:
        raise SystemExit(
            f"ERROR: invalid scopes {sorted(invalid)}. "
            f"Valid: {sorted(VALID_SCOPES)}"
        )
    if not requested:
        raise SystemExit("ERROR: at least one scope is required.")

    if args.dry_run:
        print("DRY RUN")
        print(f"  tenant:  {args.tenant}")
        print(f"  name:    {args.name}")
        print(f"  scopes:  {sorted(requested)}")
        print(f"  expires: {args.expires_days or 'never'}")
        return 0

    # Real run — open control DB
    from gdx_dispatch.core.api_keys import APIKey
    from gdx_dispatch.core.database import SessionLocal

    if SessionLocal is None:
        raise SystemExit("ERROR: control DB session factory unavailable. "
                         "Run inside the FastAPI container with DATABASE_URL set.")

    db = SessionLocal()
    try:
        tenant_uuid = _resolve_tenant_id(db, args.tenant)
        raw_key, key_hash, key_prefix = generate_api_key()

        expires_at = None
        if args.expires_days and args.expires_days > 0:
            expires_at = datetime.now(timezone.utc) + timedelta(days=args.expires_days)

        # ORM insert — the JSON column adapter handles the scopes list directly.
        # Audit §3 fix: previous version did `CAST(:sc AS jsonb)` against an
        # `sa.JSON` column, which Postgres tolerated but proved the CLI was
        # never run end-to-end.
        api_key = APIKey(
            id=uuid4(),
            tenant_id=tenant_uuid,
            key_hash=key_hash,
            key_prefix=key_prefix,
            name=args.name,
            scopes=sorted(requested),
            created_at=datetime.now(timezone.utc),
            expires_at=expires_at,
        )
        db.add(api_key)
        db.commit()
    finally:
        db.close()

    print("=" * 70)
    print("API KEY CREATED — store this NOW; it cannot be recovered.")
    print("=" * 70)
    print(f"tenant:  {args.tenant} ({tenant_uuid})")
    print(f"name:    {args.name}")
    print(f"prefix:  {key_prefix}")
    print(f"scopes:  {sorted(requested)}")
    print(f"expires: {expires_at.isoformat() if expires_at else 'never'}")
    print()
    print(f"KEY: {raw_key}")
    print()
    print("Send X-API-Key: <KEY> in every request to /api/v1/landing-leads.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

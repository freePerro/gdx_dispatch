import argparse
import logging
import sys
from typing import Any

from sqlalchemy import select

from gdx_dispatch.control.models import Tenant
from gdx_dispatch.core.database import SessionLocal, _decrypt_db_url
from gdx_dispatch.core.pii import HashColumn
from gdx_dispatch.core.tenant import engine_registry
from gdx_dispatch.models.tenant_models import Customer
from gdx_dispatch.modules.phone_com.customer_resolver import normalize_e164

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def run_backfill_on_engine(
    engine: Any, tenant_slug: str, *, rehash: bool = False,
) -> dict[str, int]:
    """
    Performs the backfill for a single engine.
    Returns a dict with the count of updated rows.
    """
    # Refuse to run against control plane
    if "gdx_control" in str(engine.url):
        raise Exception("Refusing to run backfill against control plane.")

    from sqlalchemy.orm import Session as _Session
    updated_count = 0

    with _Session(engine) as session:
        from sqlalchemy import and_, or_
        if rehash:
            stmt = select(Customer).where(
                or_(
                    Customer.phone.isnot(None),
                    Customer.email.isnot(None),
                    Customer.name.isnot(None),
                )
            )
        else:
            stmt = select(Customer).where(
                or_(
                    and_(Customer.phone.isnot(None), Customer.phone_hash.is_(None)),
                    and_(Customer.email.isnot(None), Customer.email_hash.is_(None)),
                    and_(Customer.name.isnot(None), Customer.name_hash.is_(None)),
                )
            )
        for cust in session.execute(stmt).scalars().all():
            if cust.phone and (rehash or not cust.phone_hash):
                # Normalize to E.164 before hashing so that calls from
                # Phone.com (+1XXXXXXXXXX) hash-match Customer rows stored
                # as "(XXX) XXX-XXXX". The resolver always normalizes —
                # both sides MUST agree on the input form.
                norm = normalize_e164(cust.phone) or cust.phone
                cust.phone_hash = HashColumn.hash_for_search(norm)
                updated_count += 1
            if cust.email and (rehash or not cust.email_hash):
                cust.email_hash = HashColumn.hash_for_search(cust.email)
                updated_count += 1
            if cust.name and (rehash or not cust.name_hash):
                cust.name_hash = HashColumn.hash_for_search(cust.name)
                updated_count += 1
        session.commit()

    return {"updated": updated_count}


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill legacy NULL Customer hash columns.")
    parser.add_argument("--tenant", help="Target a specific tenant by slug.")
    parser.add_argument("--all-tenants", action="store_true", help="Run against all tenants.")
    parser.add_argument(
        "--rehash",
        action="store_true",
        help="Re-hash ALL rows (not just NULL hashes). Use after a hash-formula change.",
    )
    args = parser.parse_args()

    if not args.tenant and not args.all_tenants:
        parser.print_help()
        sys.exit(1)

    with SessionLocal() as control_session:
        stmt = select(Tenant).where(Tenant.deleted_at.is_(None))
        if args.tenant:
            stmt = stmt.where(Tenant.slug == args.tenant)
        tenants = control_session.execute(stmt).scalars().all()

        if args.tenant and not tenants:
            logger.error(f"Tenant slug '{args.tenant}' not found.")
            sys.exit(1)

        total_updated = 0
        for tenant in tenants:
            db_url = _decrypt_db_url(tenant.db_url_enc)
            try:
                engine = engine_registry.get_engine(str(tenant.id), db_url)
                logger.info(f"Processing tenant: {tenant.slug}")
                result = run_backfill_on_engine(engine, tenant_slug=tenant.slug, rehash=args.rehash)
                total_updated += result["updated"]
                logger.info(f"  - Tenant '{tenant.slug}': updated {result['updated']} rows.")
            except Exception as e:
                logger.error(f"  - Tenant '{tenant.slug}' failed: {e}")

        logger.info(f"Total updated across all tenants: {total_updated}")


if __name__ == "__main__":
    main()

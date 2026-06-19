"""Backfill legacy users into platform identities + providers + memberships.

Two entry points:

- ``backfill_users(control_db, legacy_users=None, dry_run=True)`` — writes
  identities/providers/memberships into ``control_db`` from a provided user list
  (or from ``control_db`` itself when ``legacy_users is None``). Back-compat
  seam for Slice A tests that combine control + tenant tables in one SQLite
  session.

- ``backfill_users_from_tenants(control_db, tenant_session_factory=None,
  dry_run=True)`` — Slice C. Enumerates active tenants in the control DB, opens
  each tenant DB READ-ONLY via ``tenant_session_factory``, fetches legacy
  ``users`` rows, and forwards them to ``backfill_users``. Only ``control_db``
  is written; tenant DBs are never mutated.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from gdx_dispatch.control.models import Tenant
from gdx_dispatch.core.database import _decrypt_db_url
from gdx_dispatch.models.platform import CapabilitySet, Identity, IdentityProvider, Membership
from gdx_dispatch.models.tenant_models import User

log = logging.getLogger(__name__)


@dataclass(slots=True)
class LegacyUserRow:
    id: str
    email: str | None
    username: str | None
    role: str | None
    company_id: str
    password_hash: str | None
    created_at: datetime | None
    deleted_at: datetime | None
    email_verified_at: datetime | None


ROLE_TO_CAPSET = {
    "owner": "role:owner",
    "admin": "role:admin",
    "tech": "role:tech",
    "contractor": "role:contractor",
    "viewer": "role:viewer",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _resolve_tenant(company_id: str, tenant_rows: list[Tenant]) -> Tenant | None:
    """D97: returns the Tenant row so callers can use either ``id`` (UUID, FK)
    or ``slug`` (display). Replaces the slug-only resolver."""
    for tenant in tenant_rows:
        if tenant.slug == company_id:
            return tenant
        if str(tenant.id) == company_id:
            return tenant
    return None


def _fetch_legacy_users(db: Session) -> list[LegacyUserRow]:
    present, rows = _fetch_tenant_users(db)
    return rows if present else []


def _fetch_tenant_users(db: Session) -> tuple[bool, list[LegacyUserRow]]:
    """Return (users_table_present, legacy_user_rows).

    Separates the "table missing" signal from "table empty" — the caller
    records them as distinct per-tenant states.
    """
    inspector = sa.inspect(db.bind)
    if "users" not in inspector.get_table_names():
        return False, []
    users = db.execute(select(User).where(User.deleted_at.is_(None))).scalars().all()
    rows: list[LegacyUserRow] = []
    for user in users:
        rows.append(
            LegacyUserRow(
                id=str(user.id),
                email=user.email,
                username=user.username or user.name or user.full_name,
                role=user.role,
                company_id=str(user.company_id),
                password_hash=user.password_hash,
                created_at=user.created_at,
                deleted_at=user.deleted_at,
                email_verified_at=getattr(user, "email_verified_at", None),
            )
        )
    return True, rows


def _default_tenant_session_factory(tenant: Tenant) -> Session:
    """Open a READ-ONLY session against a tenant's database.

    Uses the canonical ``_decrypt_db_url`` resolver (same as
    ``tenant_isolation_audit.py`` and ``get_db``). Falls back to
    plaintext URLs when ``GDX_FERNET_KEY`` is unset (dev mode).

    The returned session has ``_gdx_owned_engine`` attached so
    ``backfill_users_from_tenants`` knows to dispose the engine after close.
    Sessions produced by test-injected factories do NOT carry this marker and
    their engines are left alone (tests own the engine lifecycle).
    """
    db_url = _decrypt_db_url(tenant.db_url_enc)
    engine = create_engine(db_url, future=True)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    session._gdx_owned_engine = engine  # type: ignore[attr-defined]
    return session


def backfill_users(
    db: Session,
    legacy_users: Iterable[LegacyUserRow] | None = None,
    dry_run: bool = True,
) -> dict:
    stats = {
        "legacy_users_table_present": False,
        "users_seen": 0,
        "users_skipped_no_email": 0,
        "users_skipped_no_tenant": 0,
        "identities_created": 0,
        "identities_existing": 0,
        "providers_created": 0,
        "memberships_created": 0,
        "errors": [],
    }

    capset_by_name = {
        cs.name: cs.id for cs in db.execute(select(CapabilitySet)).scalars().all()
    }
    if "role:owner" not in capset_by_name:
        raise RuntimeError("Capability sets missing. Run seed_platform_platform.py first.")

    tenant_rows = db.execute(select(Tenant)).scalars().all()
    if legacy_users is None:
        inspector = sa.inspect(db.bind)
        stats["legacy_users_table_present"] = "users" in inspector.get_table_names()
        legacy_users = _fetch_legacy_users(db)
    else:
        stats["legacy_users_table_present"] = True

    for user in legacy_users:
        stats["users_seen"] += 1
        if not user.email:
            stats["users_skipped_no_email"] += 1
            continue
        tenant_row = _resolve_tenant(user.company_id, tenant_rows)
        if tenant_row is None:
            stats["users_skipped_no_tenant"] += 1
            continue
        tenant_uuid = tenant_row.id
        try:
            email_norm = user.email.strip().lower()
            identity = db.execute(
                select(Identity).where(Identity.email == email_norm)
            ).scalar_one_or_none()
            if identity is None:
                identity = Identity(
                    id=uuid4(),
                    email=email_norm,
                    display_name=user.username or email_norm.split("@")[0],
                    status="deleted" if user.deleted_at else "active",
                    email_verified_at=user.email_verified_at,
                    created_at=user.created_at or _now(),
                )
                db.add(identity)
                db.flush()
                stats["identities_created"] += 1
            else:
                stats["identities_existing"] += 1

            provider = db.execute(
                select(IdentityProvider).where(
                    IdentityProvider.provider_type == "legacy_local",
                    IdentityProvider.provider_subject == str(user.id),
                )
            ).scalar_one_or_none()
            if provider is None:
                db.add(
                    IdentityProvider(
                        id=uuid4(),
                        identity_id=identity.id,
                        provider_type="legacy_local",
                        provider_subject=str(user.id),
                        provider_email=email_norm,
                        email_verified_by_provider=bool(user.email_verified_at),
                        is_authoritative_for_domain=False,
                        linked_at=_now(),
                        provider_metadata={
                            "legacy_user_id": str(user.id),
                            "password_hash": user.password_hash,
                        },
                    )
                )
                stats["providers_created"] += 1

            capset_name = ROLE_TO_CAPSET.get((user.role or "").strip().lower(), "role:viewer")
            membership = db.execute(
                select(Membership).where(
                    Membership.identity_id == identity.id,
                    Membership.tenant_id == tenant_uuid,
                    Membership.role == (user.role or "viewer"),
                    Membership.revoked_at.is_(None),
                )
            ).scalar_one_or_none()
            if membership is None:
                db.add(
                    Membership(
                        id=uuid4(),
                        identity_id=identity.id,
                        tenant_id=tenant_uuid,
                        role=(user.role or "viewer"),
                        capability_set_id=capset_by_name[capset_name],
                        granted_at=user.created_at or _now(),
                    )
                )
                stats["memberships_created"] += 1
        except Exception as exc:  # pragma: no cover - defensive stat capture
            stats["errors"].append(f"user={user.id}: {exc}")

    if dry_run:
        db.rollback()
    else:
        db.commit()

    return stats


def _apply_tenant_read_guards(
    tdb: Session,
    statement_timeout_seconds: int | None,
    enforce_read_only: bool,
) -> None:
    """Apply safety guards to a tenant session.

    SET statement_timeout + default_transaction_read_only at the session
    level. Both are PG-specific — on SQLite (tests) these raise OperationalError
    which we swallow, leaving the session in its natural state. Production
    tenant sessions always get the guards applied.
    """
    if statement_timeout_seconds is not None:
        with contextlib.suppress(Exception):
            tdb.execute(text(f"SET statement_timeout = {int(statement_timeout_seconds) * 1000}"))
    if enforce_read_only:
        with contextlib.suppress(Exception):
            tdb.execute(text("SET default_transaction_read_only = on"))


def backfill_users_from_tenants(
    control_db: Session,
    tenant_session_factory: Callable[[Tenant], Session] | None = None,
    dry_run: bool = True,
    statement_timeout_seconds: int | None = 30,
    enforce_read_only: bool = True,
    error_abort_threshold: float | None = None,
    min_samples_before_abort: int = 5,
) -> dict:
    """Iterate active tenants, source users from each tenant DB, backfill control DB.

    - ``control_db`` is the ONLY session written or rolled back.
    - Each tenant DB is opened via ``tenant_session_factory``. Per-session PG
      guards applied: ``statement_timeout`` (seconds) and
      ``default_transaction_read_only = on``. Both are SET-at-the-session
      commands and fail silently on non-PG backends (SQLite tests).
    - Default factory uses ``_decrypt_db_url`` + ``create_engine`` per tenant.
      Tests inject a factory that returns pre-built SQLite sessions.
    - Tenants without ``db_url_enc`` (unprovisioned) are skipped with a stat.
    - Tenants whose DB fails to connect are recorded per-tenant and do NOT
      abort the run — other tenants still get processed.
    - ``error_abort_threshold`` (0.0 – 1.0) aborts iteration if the observed
      per-tenant error rate exceeds the threshold AFTER at least
      ``min_samples_before_abort`` tenants have been attempted. Disabled by
      default (None) — enable for production-adjacent runs. Aborted tenants
      are recorded with ``skipped = "aborted_error_rate"``.
    """
    factory = tenant_session_factory or _default_tenant_session_factory

    tenants = control_db.execute(
        select(Tenant).where(Tenant.deleted_at.is_(None))
    ).scalars().all()

    per_tenant: dict[str, dict] = {}
    combined_users: list[LegacyUserRow] = []
    attempted = 0
    errored = 0
    aborted = False

    for tenant in tenants:
        entry: dict = {
            "tenant_id": str(tenant.id),
            "users_table_present": False,
            "users_seen": 0,
            "skipped": None,
            "error": None,
        }

        if aborted:
            entry["skipped"] = "aborted_error_rate"
            per_tenant[tenant.slug] = entry
            continue

        if hasattr(tenant, "db_url_enc") and not tenant.db_url_enc:
            entry["skipped"] = "no_db_url_enc"
            per_tenant[tenant.slug] = entry
            continue

        attempted += 1

        try:
            tdb = factory(tenant)
        except Exception as exc:
            entry["error"] = f"connect: {type(exc).__name__}: {exc}"
            errored += 1
            per_tenant[tenant.slug] = entry
            if _should_abort(error_abort_threshold, errored, attempted, min_samples_before_abort):
                aborted = True
            continue

        try:
            _apply_tenant_read_guards(tdb, statement_timeout_seconds, enforce_read_only)
            present, rows = _fetch_tenant_users(tdb)
            entry["users_table_present"] = present
            entry["users_seen"] = len(rows)
            combined_users.extend(rows)
        except Exception as exc:
            entry["error"] = f"fetch: {type(exc).__name__}: {exc}"
            errored += 1
        finally:
            try:
                owned_engine = getattr(tdb, "_gdx_owned_engine", None)
                tdb.close()
                if owned_engine is not None:
                    owned_engine.dispose()
            except Exception:  # noqa: BLE001
                log.exception("backfill_tenant_cleanup_failed")

        per_tenant[tenant.slug] = entry
        if _should_abort(error_abort_threshold, errored, attempted, min_samples_before_abort):
            aborted = True

    stats = backfill_users(control_db, legacy_users=combined_users, dry_run=dry_run)
    stats["per_tenant"] = per_tenant
    stats["tenants_seen"] = len(tenants)
    stats["tenants_with_users_table"] = sum(
        1 for e in per_tenant.values() if e["users_table_present"]
    )
    stats["tenants_skipped"] = sum(1 for e in per_tenant.values() if e["skipped"])
    stats["tenants_errored"] = sum(1 for e in per_tenant.values() if e["error"])
    stats["tenants_attempted"] = attempted
    stats["aborted_on_error_rate"] = aborted
    stats["guards"] = {
        "statement_timeout_seconds": statement_timeout_seconds,
        "enforce_read_only": enforce_read_only,
        "error_abort_threshold": error_abort_threshold,
        "min_samples_before_abort": min_samples_before_abort,
    }
    return stats


def _should_abort(threshold: float | None, errored: int, attempted: int, min_samples: int) -> bool:
    if threshold is None:
        return False
    if attempted < min_samples:
        return False
    return (errored / max(1, attempted)) > threshold


def _main() -> int:
    parser = argparse.ArgumentParser(description="Backfill users into platform identities/providers/memberships.")
    parser.add_argument("--apply", action="store_true", help="Persist changes (default is dry-run).")
    parser.add_argument(
        "--source",
        choices=("tenants", "control-only"),
        default="tenants",
        help=(
            "Where to source legacy users. 'tenants' (default) enumerates active "
            "tenants from the control DB and reads each tenant's users table. "
            "'control-only' reads a users table from the control DB itself — "
            "used only for Slice A smoke tests."
        ),
    )
    args = parser.parse_args()

    from gdx_dispatch.core.database import SessionLocal

    with SessionLocal() as db:
        if args.source == "tenants":
            result = backfill_users_from_tenants(db, dry_run=not args.apply)
        else:
            result = backfill_users(db, dry_run=not args.apply)
        mode = "APPLY" if args.apply else "DRY-RUN"
        print(f"{mode} backfill_users_to_identities ({args.source}) result:")
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())

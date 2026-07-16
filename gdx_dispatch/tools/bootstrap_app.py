#!/usr/bin/env python3
"""First-run application bootstrap (idempotent).

Brings an empty database up to a state where a fresh `docker compose up`
can actually log in. Designed to run on every container start — each step
is a no-op when its work is already done.

Pipeline:
  1. Create the ORM-managed tenant-plane tables (users, companies,
     company_module_grants, …) via TenantBase.metadata.create_all().
     Alembic (run separately, before this) creates the control-plane tables
     (tenants, audit_logs, …); the tenant-plane tables are ORM-only, so
     `alembic upgrade head` alone never creates them.
  2. Seed the single default tenant row (matches what TenantMiddleware pins
     every request to via single_tenant()), so the audit_logs → tenants FK
     resolves and the audit trail works.
  3. Seed the matching company row (company_id == tenant id in single-tenant
     mode).
  4. Seed an initial admin user so someone can log in. The password comes
     from GDX_ADMIN_PASSWORD if set (and is then never written to the logs),
     otherwise a random one is generated and printed to the logs as its one
     handoff channel. The user is flagged must_change_password=True either
     way. Set GDX_ADMIN_PASSWORD to keep any password out of the logs.

Env vars:
  GDX_TENANT_ID / GDX_TENANT_SLUG / GDX_TENANT_NAME — tenant identity
      (defaults supplied by single_tenant(); zero-config works).
  GDX_ADMIN_EMAIL     — admin login (default: admin@example.com)
  GDX_ADMIN_PASSWORD  — admin password (default: randomly generated and
      logged once; when set, the value is never written to the logs)
  GDX_SKIP_BOOTSTRAP=1 — skip entirely (e.g. when managing the DB yourself).

Run: python -m gdx_dispatch.tools.bootstrap_app
"""
from __future__ import annotations

import logging
import os
import secrets
from uuid import uuid4

from sqlalchemy import select

log = logging.getLogger("gdx_dispatch.bootstrap")
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")


def _hash_password(password: str) -> str:
    """Hash a password the same way the rest of the app does (bcrypt, $2b$…)."""
    import bcrypt

    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _resolve_admin_password() -> tuple[str, bool]:
    """(password, generated) from GDX_ADMIN_PASSWORD, single-sourced.

    Set-but-EMPTY must mean "generate" — `.env.template` ships
    ``GDX_ADMIN_PASSWORD=`` and compose ``env_file:`` injects it as an empty
    string, so an ``in os.environ`` test here would claim the password came
    from the operator while a random one was actually seeded (a silent
    lockout: the banner would hide a password nobody holds).
    """
    supplied = os.getenv("GDX_ADMIN_PASSWORD")
    if supplied:
        return supplied, False
    return secrets.token_urlsafe(12), True


def _admin_banner(title: str, email: str, password: str, *, generated: bool) -> str:
    """First-login banner for the seeded owner account.

    Only a GENERATED password is included — the log line is its one handoff
    channel on a zero-config first boot. An operator-supplied
    GDX_ADMIN_PASSWORD is a secret they already hold; echoing it back into
    persisted container logs would only widen its exposure.
    """
    shown = password if generated else "(from GDX_ADMIN_PASSWORD — not shown)"
    return (
        "\n"
        "════════════════════════════════════════════════════════════\n"
        f"  GDX Dispatch — {title}\n"
        f"    email:    {email}\n"
        f"    password: {shown}\n"
        "  You MUST change this password on first login.\n"
        "════════════════════════════════════════════════════════════"
    )


def create_orm_tables() -> None:
    """Create the ORM-managed tenant-plane tables (idempotent; checkfirst).

    #41 — this MUST run BEFORE `alembic upgrade head` on a fresh DB so any
    migration that ALTERs a non-baseline (ORM-managed) table finds it present.
    The squashed baseline only creates the disjoint control-plane tables, so
    create_all-first does not collide with migration 001. Safe to call again
    inside main() (checkfirst makes it a no-op once tables exist).
    """
    import gdx_dispatch.models  # noqa: F401 — register every model on the metadata

    from gdx_dispatch.core.audit import TenantBase
    from gdx_dispatch.core.database import engine

    log.info("Ensuring ORM-managed tenant tables exist (create_all)…")
    TenantBase.metadata.create_all(engine, checkfirst=True)


def main() -> int:
    if os.getenv("GDX_SKIP_BOOTSTRAP", "").strip() == "1":
        log.info("GDX_SKIP_BOOTSTRAP=1 — skipping first-run bootstrap.")
        return 0

    # Importing the models package registers every ORM model on TenantBase's
    # metadata so create_all() sees the full tenant-plane schema.
    import gdx_dispatch.models  # noqa: F401

    from gdx_dispatch.control.models import Tenant
    from gdx_dispatch.core.database import SessionLocal
    from gdx_dispatch.core.tenant import single_tenant
    from gdx_dispatch.models.tenant_models import Company, User

    # ── 1. Tenant-plane tables (ORM-managed; alembic doesn't create these) ──
    # Idempotent — the entrypoint already ran this before alembic (#41), but a
    # bare `python -m bootstrap_app` (e.g. local dev) still needs it here.
    create_orm_tables()

    tenant = single_tenant()
    tenant_id = str(tenant["id"])

    with SessionLocal() as db:
        # ── 2. Default tenant row (control-plane; FK target for audit_logs) ──
        if db.get(Tenant, tenant_id) is None:
            db.add(Tenant(id=tenant_id, slug=str(tenant["slug"]), name=str(tenant["name"])))
            db.flush()
            log.info("Seeded tenant row id=%s slug=%s", tenant_id, tenant["slug"])
        else:
            log.info("Tenant row already present (id=%s).", tenant_id)

        # ── 3. Matching company row (company_id == tenant id) ──
        if db.get(Company, tenant_id) is None:
            db.add(Company(id=tenant_id, name=str(tenant["name"])))
            db.flush()
            log.info("Seeded company row id=%s", tenant_id)
        else:
            log.info("Company row already present (id=%s).", tenant_id)

        # ── 4. Initial admin user ──
        # Only a LIVE (non-tombstoned) row blocks re-seeding. The existence
        # check deliberately filters deleted_at IS NULL so that if a tenant
        # ever loses its last owner out-of-band (direct DB edit, migration
        # mishap), a restart restores a working owner login. The last-owner
        # guard in routers/users.py makes this unreachable through the API,
        # so this is purely a recovery backstop.
        admin_email = os.getenv("GDX_ADMIN_EMAIL", "admin@example.com").strip().lower()
        live = db.execute(
            select(User).where(User.email == admin_email, User.deleted_at.is_(None))
        ).scalars().first()
        if live is not None:
            db.commit()
            log.info("Admin user already present (%s) — leaving it untouched.", admin_email)
        else:
            admin_password, generated = _resolve_admin_password()
            # If a soft-deleted row with this email survives, revive it in
            # place rather than inserting a duplicate-email row.
            tombstoned = db.execute(
                select(User).where(User.email == admin_email)
            ).scalars().first()
            # 'owner' resolves to the WILDCARD permission set
            # (BUILTIN_ROLES["owner"]), so the bootstrap account has full
            # access to everything — including billing and any permission an
            # endpoint requires that isn't in the catalog (e.g. dispatch.read).
            # The first account on a self-hosted single-tenant deploy owns the
            # whole tenant.
            if tombstoned is not None:
                tombstoned.deleted_at = None
                tombstoned.active = True
                tombstoned.role = "owner"
                tombstoned.password_hash = _hash_password(admin_password)
                tombstoned.must_change_password = True
                revived = True
            else:
                db.add(
                    User(
                        id=str(uuid4()),
                        email=admin_email,
                        username="admin",
                        full_name="Administrator",
                        password_hash=_hash_password(admin_password),
                        role="owner",
                        company_id=tenant_id,
                        active=True,
                        must_change_password=True,
                    )
                )
                revived = False
            db.commit()
            title = "initial admin account created" if not revived else "owner account restored"
            log.warning(_admin_banner(title, admin_email, admin_password, generated=generated))

    log.info("Bootstrap complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

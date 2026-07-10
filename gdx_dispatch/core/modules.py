from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timezone
from functools import lru_cache
from uuid import uuid4

from fastapi import Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.roles import normalize_role

MODULES = {
    "jobs": {"name": "Jobs", "tier": "starter", "default": True},
    "customers": {"name": "Customers", "tier": "starter", "default": True},
    "estimates": {"name": "Estimates", "tier": "starter", "default": True},
    "invoices": {"name": "Invoices", "tier": "starter", "default": True},
    "dispatch": {"name": "Dispatch Board", "tier": "starter", "default": True},
    "timeclock": {"name": "Time Clock", "tier": "starter", "default": True},
    "inventory": {"name": "Inventory", "tier": "professional", "default": False},
    "quickbooks": {"name": "QuickBooks Sync", "tier": "professional", "default": False},
    "customer_portal": {"name": "Customer Portal", "tier": "professional", "default": False},
    "equipment_tracking": {"name": "Equipment Tracking", "tier": "professional", "default": False},
    "campaigns": {"name": "Marketing Campaigns", "tier": "professional", "default": False},
    "gps_dispatch": {"name": "GPS Dispatch", "tier": "business", "default": False},
    "ai_estimates": {"name": "AI Smart Estimates", "tier": "business", "default": False},
    "ai_dispatch": {"name": "AI Dispatch Optimization", "tier": "business", "default": False},
    "ai_communication": {"name": "AI Communication Drafts", "tier": "business", "default": False},
    "llm": {"name": "AI Assistant", "tier": "starter", "default": False},
    "stripe_connect": {"name": "Online Payments", "tier": "professional", "default": False},
    "loyalty": {"name": "Loyalty Programs", "tier": "business", "default": False},
    "warranties": {"name": "Warranty Tracking", "tier": "professional", "default": False},
    "automations": {"name": "Workflow Automations", "tier": "business", "default": False},
    "documents": {"name": "Document Management", "tier": "starter", "default": True},
    "communications": {"name": "Communications", "tier": "starter", "default": True},
    "reports_advanced": {"name": "Advanced Reports", "tier": "professional", "default": False},
    "mobile": {"name": "Mobile App", "tier": "starter", "default": True},
    "segments": {"name": "Customer Segments", "tier": "business", "default": False},
    "google_maps": {"name": "Google Maps & Routing", "tier": "professional", "default": False},
    "chrome_extension": {"name": "Supplier Portal Bridge", "tier": "business", "default": False},
    "phone_com": {"name": "Phone.com Voice & SMS", "tier": "professional", "default": False},
    "email": {"name": "Email Integration", "tier": "professional", "default": False},
}

# Legacy keys still referenced by older routers are mapped to canonical module keys.
LEGACY_MODULE_ALIASES = {
    "advanced_reports": "reports_advanced",
    "warranty": "warranties",
    "workflows": "automations",
    "proposals": "estimates",
    "change_orders": "estimates",
    "maintenance_plans": "jobs",
    "purchase_orders": "inventory",
    "fleet": "equipment_tracking",
    "service_areas": "dispatch",
    "contractors": "jobs",
    "service_agreements": "jobs",
}

MODULE_KEYS = list(dict.fromkeys(list(MODULES.keys()) + list(LEGACY_MODULE_ALIASES.keys())))

RBAC_HIERARCHY = {"owner": 5, "admin": 4, "dispatcher": 3, "technician": 2, "viewer": 1}


def invalidate_module_cache(tenant_id: str, module_key: str) -> None:
    _ = (tenant_id, module_key)


def normalize_module_key(module_key: str) -> str:
    key = module_key.strip().lower()
    canonical = LEGACY_MODULE_ALIASES.get(key, key)
    if canonical not in MODULES:
        raise ValueError(f"Unknown module key: {module_key}")
    return canonical



def _ensure_company_module_grants_table(db: Session) -> None:
    db.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS company_module_grants (
                id VARCHAR(36) PRIMARY KEY,
                company_id VARCHAR(36) NOT NULL,
                module_key VARCHAR(100) NOT NULL,
                granted_at TIMESTAMP WITH TIME ZONE,
                created_at TIMESTAMP WITH TIME ZONE,
                expires_at TIMESTAMP WITH TIME ZONE,
                CONSTRAINT uq_company_module_grant UNIQUE (company_id, module_key)
            )
            """
        )
    )
    db.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_company_module_grants_company
            ON company_module_grants (company_id)
            """
        )
    )
    db.commit()


def _seed_default_modules(db: Session, company_id: str) -> None:
    has_any = db.execute(
        text("SELECT 1 FROM company_module_grants WHERE company_id = :company_id LIMIT 1"),
        {"company_id": company_id},
    ).scalar()
    if has_any:
        return
    # Single-tenant: the owner owns the whole install, so seed EVERY module on
    # first boot (the per-module `default` flag is vestigial SaaS tiering). This
    # runs once — guarded by has_any above — so a later admin-disable sticks and
    # is never resurrected.
    for key in MODULES:
        db.execute(
            text(
                """
                INSERT INTO company_module_grants (id, company_id, module_key, granted_at, created_at)
                VALUES (:_id, :company_id, :module_key, :_now, :_now)
                ON CONFLICT (company_id, module_key) DO NOTHING
                """
            ),
            {"company_id": company_id, "module_key": key, "_id": str(uuid4()), "_now": datetime.now(timezone.utc)},
        )
    db.commit()


def _is_granted_in_company_table(db: Session, company_id: str, module_key: str) -> bool:
    granted = db.execute(
        text(
            """
            SELECT 1
            FROM company_module_grants
            WHERE company_id = :company_id AND module_key = :module_key
            LIMIT 1
            """
        ),
        {"company_id": company_id, "module_key": module_key},
    ).scalar()
    return bool(granted)


def enabled_module_keys(db: Session, company_id: str) -> set[str]:
    """All module keys granted to a tenant. Used by the plugin proxy to forward
    the authoritative enabled-modules set to plugin-host (so plugins gate without
    a DB round-trip). Returns an empty set on any DB error — fail closed: a
    transient failure must not silently enable a plugin."""
    if not company_id:
        return set()
    try:
        rows = db.execute(
            text("SELECT module_key FROM company_module_grants WHERE company_id = :cid"),
            {"cid": company_id},
        ).fetchall()
        return {r[0] for r in rows if r[0]}
    except SQLAlchemyError:
        logging.getLogger("gdx_dispatch.modules").exception(
            "enabled_module_keys query failed for tenant=%s", company_id
        )
        return set()


def is_module_enabled(module_key: str, request: Request, db: Session) -> bool:
    canonical_key = normalize_module_key(module_key)
    tenant = getattr(request.state, "tenant", {}) or {}
    tenant_id = str(tenant.get("id", "")).strip()
    if not tenant_id:
        return False

    try:
        _ensure_company_module_grants_table(db)
        _seed_default_modules(db, tenant_id)
        # D101 follow-up (an earlier session, 2026-04-25 evening): the GDX-specific
        # `_grant_all_modules` autohealer used to live here. is_module_enabled
        # runs on every request that hits a require_module(...) decorator, so
        # GDX disables of any module were resurrected within milliseconds —
        # the twin of the settings.py autohealer fixed earlier in this session.
        # Bootstrap of "GDX has all modules" now happens once via
        # gdx_dispatch/tools/bootstrap_modules_for_tenant.py; row absence here means
        # the admin disabled it.
        if _is_granted_in_company_table(db, tenant_id, canonical_key):
            return True
    except SQLAlchemyError:
        import logging
        logging.getLogger("gdx_dispatch.modules").exception("module_check_failed key=%s tenant=%s", canonical_key, tenant_id)
        db.rollback()

    # Backward compatibility: check legacy control-plane grants when table is absent.
    try:
        granted = db.execute(
            text(
                """
                SELECT 1
                FROM tenant_module_grants
                WHERE CAST(tenant_id AS TEXT) = :tenant_id
                  AND module_key = :module_key
                LIMIT 1
                """
            ),
            {"tenant_id": tenant_id, "module_key": canonical_key},
        ).scalar()
        return bool(granted)
    except SQLAlchemyError:  # Return False if module check fails due to database errors.
        import logging
        logging.getLogger("gdx_dispatch.modules").exception("module_check_failed key=%s tenant=%s", canonical_key, tenant_id)
        db.rollback()
        return False


@lru_cache(maxsize=128)
def require_module(module_key: str) -> Callable:
    canonical_key = normalize_module_key(module_key)

    async def _dependency(request: Request, db: Session = Depends(get_db)) -> None:
        tenant = getattr(request.state, "tenant", {}) or {}
        if not tenant.get("id"):
            raise HTTPException(status_code=400, detail="Missing tenant context")
        if not is_module_enabled(canonical_key, request, db):
            raise HTTPException(403, f"Module '{canonical_key}' is not enabled")

    return _dependency


def require_role(*roles: str) -> Callable:
    """Dependency factory that 403s unless the current user has one of `roles`.

    Takes `Depends(get_current_user)` so FastAPI's dependency-override system
    (used by tests + middleware) populates the user. Falls back to
    `request.state.current_user` / `request.state.user` to support tests that
    populate those keys directly without overriding get_current_user.

    Previously this only checked request.state.current_user, which was never
    populated by production middleware, so every require_role gate 403'd in
    production. Now it composes with get_current_user, matching the rest of
    the codebase's auth pattern.

    Both the declared `roles` and the caller's role are run through
    core.roles.normalize_role before comparison, so a gate written with a
    legacy spelling (`"tech"`) still admits the canonical form (`"technician"`)
    and vice-versa. Without this, the #45 role-canonicalization (migration 009,
    which renamed users.role `tech`→`technician`) silently orphaned every gate
    still listing `"tech"` — a migrated technician 403'd on /api/search,
    /api/resources, etc. (prod incident, 2026-07-10). normalize_role only
    collapses known aliases, so this never broadens access to another role.
    """
    allowed = {normalize_role(r) for r in roles}

    def _dependency(request: Request) -> None:
        user: dict = {}

        # 1. request.state.current_user / state.user (production middleware path)
        for key in ("current_user", "user"):
            candidate = getattr(request.state, key, None)
            if candidate:
                user = candidate
                break

        # 2. FastAPI dep-override path. Test fixtures do:
        #      app.dependency_overrides[get_current_user] = lambda: {"role": "admin", ...}
        #    which bypasses request.state entirely. Reach into the app's
        #    override map to find it.
        if not user:
            try:
                app = request.app
                from gdx_dispatch.routers.auth import get_current_user as _gcu
                override = (app.dependency_overrides or {}).get(_gcu)
                if callable(override):
                    result = override()
                    if isinstance(result, dict):
                        user = result
            except Exception:
                logging.getLogger(__name__).exception("_dependency caught exception")
                pass

        role = normalize_role((user or {}).get("role"))
        if role in allowed:
            return

        # JWT from Authorization header as final fallback.
        try:
            auth_header = request.headers.get("authorization", "") or ""
        except AttributeError:
            logging.getLogger(__name__).exception("_dependency caught exception")
            auth_header = ""
        if auth_header.lower().startswith("bearer "):
            token = auth_header.split(" ", 1)[1].strip()
            try:
                import jwt as _jwt

                from gdx_dispatch.routers.auth import ALG as _ALG
                from gdx_dispatch.routers.auth import VERIFY_KEY as _VERIFY
                claims = _jwt.decode(token, _VERIFY, algorithms=[_ALG])
                # SS-7 Slice H parity: this raw-decode fallback must honor the
                # same revocation denylist get_current_user enforces. Without it a
                # revoked token (logout / offboarding / admin revoke) still passes
                # the role gate here. On any denylist error we fall through to 403
                # (fail closed). NOTE: this closes the revoked-token bypass; it does
                # NOT by itself address a *demotion* that leaves an unexpired access
                # token's role claim valid — roles are claim-based, so that needs a
                # revoke-on-role-change (by user/jti). Tracked separately.
                jti = claims.get("jti")
                revoked = False
                if jti:
                    from gdx_dispatch.routers.auth.core import _get_app_denylist
                    revoked = _get_app_denylist(request).contains(str(jti))
                if not revoked and normalize_role(claims.get("role")) in allowed:
                    return
            except Exception:
                logging.getLogger(__name__).exception("_dependency caught exception")
                pass

        raise HTTPException(status_code=403, detail="Insufficient role")

    return _dependency


def _resolve_request_user(request: Request) -> dict:
    """Find the current user dict via the same fallbacks as require_role."""
    for key in ("current_user", "user"):
        candidate = getattr(request.state, key, None)
        if candidate:
            return candidate
    try:
        from gdx_dispatch.routers.auth import get_current_user as _gcu
        override = (request.app.dependency_overrides or {}).get(_gcu)
        if callable(override):
            result = override()
            if isinstance(result, dict):
                return result
    except Exception:
        logging.getLogger(__name__).exception("_resolve_request_user override failed")
    auth_header = request.headers.get("authorization", "") or ""
    if auth_header.lower().startswith("bearer "):
        token = auth_header.split(" ", 1)[1].strip()
        try:
            import jwt as _jwt

            from gdx_dispatch.routers.auth import ALG as _ALG
            from gdx_dispatch.routers.auth import VERIFY_KEY as _VERIFY
            return _jwt.decode(token, _VERIFY, algorithms=[_ALG]) or {}
        except Exception:
            logging.getLogger(__name__).exception("_resolve_request_user jwt decode failed")
    return {}


def _user_id_from(user: dict) -> str:
    return str(user.get("sub") or user.get("user_id") or user.get("id") or "")


def _load_user_permissions(db: Session, request: Request, user: dict) -> set[str]:
    """Resolve the permission set for `user` from their TenantRole assignment.

    Resolution order:
      1. Look up TenantRole snapshot via UserRoleAssignment (source of truth
         for explicit assignments — a tenant can demote an admin by changing
         the assignment to a less-privileged role).
      2. Look up `User.role` from the DB (stale-JWT defense — never let a
         JWT claim outlive the DB-recorded role).
      3. If the assigned-role name equals the DB role AND that role is
         admin/owner: **UNION** the snapshot with BUILTIN_ROLES[role]. This
         closes the S97 D-item (`D-S97-perm-snapshot`): snapshots taken at
         signup miss any BUILTIN keys added later (e.g.,
         `pricing.labor_matrix.read`), silently locking the admin out of
         new features. Admin/owner BUILTIN is the platform contract;
         snapshot is advisory only when admin/owner.
      4. If only a snapshot exists (no admin/owner override): trust it.
      5. If no snapshot: fall back to BUILTIN_ROLES[role-from-DB-or-JWT].
      6. Empty set → caller's require_permission rejects.
    """
    import json as _json

    from gdx_dispatch.core.permissions import BUILTIN_ROLES, WILDCARD
    from gdx_dispatch.models.tenant_models import TenantRole, UserRoleAssignment

    tenant = getattr(request.state, "tenant", {}) or {}
    tenant_id = str(tenant.get("id") or "")
    user_id = _user_id_from(user)

    # 1. DB-truth role lookup FIRST — never trust the JWT `role` claim alone,
    # and we need it to pick the right assignment when a user has more than one.
    db_role: str = ""
    if user_id:
        try:
            from gdx_dispatch.models.tenant_models import User as _User
            from sqlalchemy import select as _select
            db_role = (
                db.execute(_select(_User.role).where(_User.id == user_id, _User.deleted_at.is_(None))).scalar_one_or_none()
                or ""
            )
        except SQLAlchemyError:
            logging.getLogger(__name__).exception("user_role_lookup_failed user=%s", user_id)
            db.rollback()
        except Exception:
            db_role = ""

    legacy_role = (db_role or str(user.get("role") or "")).lower()

    # 2. Load the snapshot AND the assigned role's name. When a user has several
    # assignments, PREFER the one whose role name matches users.role — that makes
    # the resolution deterministic and keeps a stale assignment from shadowing
    # the intended role (the owner-stuck-on-admin drift, 2026-06-26).
    snapshot_perms: set[str] | None = None
    assigned_role_name: str = ""
    if tenant_id and user_id:
        try:
            from sqlalchemy import case as _case
            from sqlalchemy import func as _func
            from sqlalchemy import select as _select
            row = db.execute(
                _select(TenantRole.permissions, TenantRole.name)
                .join(
                    UserRoleAssignment,
                    UserRoleAssignment.role_id == TenantRole.id,
                )
                .where(
                    UserRoleAssignment.company_id == tenant_id,
                    UserRoleAssignment.user_id == user_id,
                    TenantRole.deleted_at.is_(None),
                )
                .order_by(_case((_func.lower(TenantRole.name) == legacy_role, 0), else_=1))
                .limit(1)
            ).first()
            if row:
                raw_perms = row[0]
                assigned_role_name = (row[1] or "").lower()
                try:
                    perms_list = _json.loads(raw_perms) if isinstance(raw_perms, str) else (raw_perms or [])
                except (ValueError, TypeError):
                    perms_list = []
                snapshot_perms = set(perms_list)
        except SQLAlchemyError:
            logging.getLogger(__name__).exception("permission_lookup_failed tenant=%s user=%s", tenant_id, user_id)
            db.rollback()

    # Drift signal: an admin/owner whose assignment doesn't match users.role is
    # silently resolved to the assignment's (lower) perms below. Log it loudly so
    # it's diagnosable instead of a silent downgrade.
    if (
        snapshot_perms is not None
        and legacy_role in ("admin", "owner")
        and assigned_role_name
        and assigned_role_name != legacy_role
    ):
        logging.getLogger(__name__).warning(
            "role_assignment_drift user=%s users.role=%s assignment=%s — resolving to the "
            "assignment's perms; align the assignment (RBAC UI) or use change_role",
            user_id, legacy_role, assigned_role_name,
        )

    # 3. Admin/owner with a matching assignment → BUILTIN only (snapshot
    # ignored). This is the S97 fix: a stale snapshot from signup can't
    # lock the admin out of newly-added BUILTIN keys, AND a tenant who
    # edited the admin snapshot can't escalate beyond the platform contract
    # (e.g., adding billing.write to admin doesn't actually grant it —
    # billing.write is owner-only by design). Override fires only when the
    # assigned role's NAME matches the user's DB role, so demotion (admin
    # → tech assignment) still demotes correctly.
    if (
        snapshot_perms is not None
        and legacy_role in ("admin", "owner")
        and assigned_role_name == legacy_role
    ):
        builtin = BUILTIN_ROLES.get(legacy_role) or []
        return {WILDCARD} if builtin == [WILDCARD] else set(builtin)

    # 4. Snapshot present (and the user wasn't admin/owner with matching
    # assignment): snapshot is authoritative — supports demotion and custom
    # roles.
    if snapshot_perms is not None:
        return snapshot_perms

    # 5. No snapshot: fall back to BUILTIN_ROLES for the DB/JWT role.
    builtin = BUILTIN_ROLES.get(legacy_role)
    if builtin:
        return {WILDCARD} if builtin == [WILDCARD] else set(builtin)
    return set()


def require_permission(*required_keys: str) -> Callable:
    """Dependency factory: 403s unless the current user has every key in `required_keys`.

    Owner/admin always pass (escape hatch — admin must never be locked out).
    Wildcard ``*`` in the user's perm set passes everything.
    Resolved permission set is cached on ``request.state.user_permissions`` so
    composite dependencies on the same request hit the DB once.
    """
    if not required_keys:
        raise ValueError("require_permission needs at least one key")
    required = set(required_keys)

    def _dependency(request: Request, db: Session = Depends(get_db)) -> None:
        from gdx_dispatch.core.permissions import WILDCARD

        user = _resolve_request_user(request)

        # The escape hatch (admin/owner always pass) is enforced at the
        # _load_user_permissions level: builtin admin gets every key except
        # billing.write, builtin owner gets WILDCARD. Reading from User.role
        # in the tenant DB makes role demotions effective immediately —
        # we no longer trust the JWT `role` claim for over-privileged
        # bypass. (Stale-JWT fix, post-phase-4.)
        cached = getattr(request.state, "user_permissions", None)
        if cached is None:
            cached = _load_user_permissions(db, request, user)
            request.state.user_permissions = cached

        if WILDCARD in cached or required.issubset(cached):
            return

        missing = sorted(required - cached)
        raise HTTPException(status_code=403, detail=f"Missing permission: {missing}")

    return _dependency


# @router.get('/api/qb/status', dependencies=[Depends(require_module('quickbooks'))])
# @router.post('/api/jobs', dependencies=[Depends(require_role('admin', 'dispatcher'))])

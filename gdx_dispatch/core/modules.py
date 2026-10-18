from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timezone
from functools import lru_cache
from uuid import UUID, uuid4

from fastapi import Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.requests import HTTPConnection

from gdx_dispatch.core.database import contained_read, get_db
from gdx_dispatch.core.roles import normalize_role

MODULES = {
    "jobs": {"name": "Jobs"},
    "customers": {"name": "Customers"},
    "estimates": {"name": "Estimates"},
    "invoices": {"name": "Invoices"},
    "dispatch": {"name": "Dispatch Board"},
    "timeclock": {"name": "Time Clock"},
    "inventory": {"name": "Inventory"},
    "quickbooks": {"name": "QuickBooks Sync"},
    "customer_portal": {"name": "Customer Portal"},
    "gps_dispatch": {"name": "GPS Dispatch"},
    "ai_estimates": {"name": "AI Smart Estimates"},
    "ai_dispatch": {"name": "AI Dispatch Optimization"},
    "ai_communication": {"name": "AI Communication Drafts"},
    "llm": {"name": "AI Assistant"},
    "loyalty": {"name": "Loyalty Programs"},
    "warranties": {"name": "Warranty Tracking"},
    "automations": {"name": "Workflow Automations"},
    "documents": {"name": "Document Management"},
    # Key kept for compatibility; the NAME says everything it gates, because
    # Settings → Modules renders this name on a toggle. The `/communications`
    # screen this key was minted for was removed (issue #350 — a
    # process-memory messaging shell that reported success without sending),
    # but the key stays: `routers/notifications.py` (the in-app bell),
    # `routers/inbound_comms.py` (inbound SMS/email admin routes) and
    # `routers/ai_communication.py` (AI drafts) all gate on it, and prod holds
    # the grant. Do NOT retire or rename the KEY: a rename would 403 the bell
    # everywhere. Splitting the three gates apart is a separate decision.
    "communications": {"name": "Notifications, inbound messages & AI drafts"},
    "reports_advanced": {"name": "Advanced Reports"},
    "mobile": {"name": "Mobile App"},
    "segments": {"name": "Customer Segments"},
    "google_maps": {"name": "Google Maps & Routing"},
    "phone_com": {"name": "Phone.com Voice & SMS"},
    "email": {"name": "Email Integration"},
    "bank_feeds": {"name": "Bank Feeds"},
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
    "service_areas": "dispatch",
    "service_agreements": "jobs",
}

MODULE_KEYS = list(dict.fromkeys(list(MODULES.keys()) + list(LEGACY_MODULE_ALIASES.keys())))

# Keys (canonical or alias) that once lived in MODULES / LEGACY_MODULE_ALIASES
# and were retired. A grant row for one of these gates nothing, is invisible in
# Settings → Modules, cannot be removed from the UI (normalize_module_key raises),
# and is still forwarded to plugin-host by enabled_module_keys(). So retiring a
# key means: move it here AND ship a migration that deletes its
# company_module_grants rows (088 for chrome_extension, 116 for the rest).
# gdx_dispatch/tests/test_migration_116_retired_module_grants.py checks both,
# for keys in its EVER_REGISTERED list; grant keys that never passed through
# this registry are outside what it can see.
RETIRED_MODULE_KEYS = frozenset({
    "stripe_connect",      # #578, 2026-09-01
    "chrome_extension",    # #626, 2026-09-06
    "equipment_tracking",  # #735, 2026-09-14
    "fleet",               # alias of equipment_tracking, #735
    "campaigns",           # #739, 2026-09-14
    "contractors",         # alias of jobs, dropped with the control plane 2026-08-03
})

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
    # first boot. (The per-module `default` flag that once picked a subset left
    # with the plan tiers, 2026-09-06.) This runs once — guarded by has_any
    # above — so a later admin-disable sticks and is never resurrected.
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
    transient failure must not silently enable a plugin.

    Fail-closed is only half of it: the session belongs to the request, not to
    this function, so on Postgres the swallowed failure also aborted the
    caller's transaction and its next `commit()` died (GDXA-86). `contained_read`
    keeps the empty-set degradation and stops it costing the caller its work."""
    if not company_id:
        return set()
    try:
        with contained_read(db):
            rows = db.execute(
                text("SELECT module_key FROM company_module_grants WHERE company_id = :cid"),
                {"cid": company_id},
            ).fetchall()
        return {r[0] for r in rows if r[0]}
    # Still SQLAlchemyError only. `contained_read` does call `db.connection()`
    # before the read, so something session-SHAPED but not a Session (a bare
    # Connection, a stub carrying only `.execute`) now raises AttributeError
    # here where it used to work — and this handler was briefly widened to
    # (SQLAlchemyError, AttributeError, TypeError) to keep failing closed for it.
    # Reverted after the GDXA-86 audit: there is no such caller (the one
    # production call site, `routers/plugins_proxy.py:107`, is
    # `db: Session = Depends(get_db)`, and the only test monkeypatches this
    # function whole), so the widening bought nothing and cost something real —
    # a genuine TypeError in this function would have returned an empty set,
    # gating every plugin module OFF behind a 200, instead of raising.
    except SQLAlchemyError:
        logging.getLogger("gdx_dispatch.modules").exception(
            "enabled_module_keys query failed for tenant=%s", company_id
        )
        return set()


def _module_grant_state(canonical_key: str, request: Request, db: Session) -> bool:
    """Is `canonical_key` granted to the request's tenant? Raises SQLAlchemyError
    (logged, session rolled back) when the grant could not be read, so a gate
    can tell "off" from "could not check" (GDXA-1197)."""
    tenant = getattr(request.state, "tenant", {}) or {}
    tenant_id = str(tenant.get("id", "")).strip()
    if not tenant_id:
        return False

    try:
        _ensure_company_module_grants_table(db)
        _seed_default_modules(db, tenant_id)
        # _seed_default_modules above grants EVERY module exactly once, when the
        # company has no grant rows at all; after that a missing row means the
        # admin disabled the module and it stays disabled (the old per-request
        # autohealer that resurrected disables within milliseconds is gone).
        # company_module_grants is the only grants table: the control-plane
        # fallback that used to follow this check was deleted 2026-09-03 (its
        # table held 0 rows on prod and only ever served tests).
        return _is_granted_in_company_table(db, tenant_id, canonical_key)
    except SQLAlchemyError:
        logging.getLogger("gdx_dispatch.modules").exception("module_check_failed key=%s tenant=%s", canonical_key, tenant_id)
        try:
            db.rollback()
        except SQLAlchemyError:
            logging.getLogger("gdx_dispatch.modules").exception("module_check_rollback_failed")
        raise


def is_module_enabled(module_key: str, request: Request, db: Session) -> bool:
    """Bool form for callers that branch on a module rather than gate a route:
    a grant that could not be read counts as not enabled (fail closed)."""
    try:
        return _module_grant_state(normalize_module_key(module_key), request, db)
    except SQLAlchemyError:
        logging.getLogger("gdx_dispatch.modules").warning("module_check_fail_closed key=%s", module_key)
        return False


@lru_cache(maxsize=128)
def require_module(module_key: str) -> Callable:
    canonical_key = normalize_module_key(module_key)

    async def _dependency(request: Request, db: Session = Depends(get_db)) -> None:
        tenant = getattr(request.state, "tenant", {}) or {}
        if not tenant.get("id"):
            raise HTTPException(status_code=400, detail="Missing tenant context")
        # A grant that could not be read is not a disabled module: answering
        # 403 "not enabled" made a DB fault read as a definite off state to
        # every client (GDXA-1197). 503 still refuses, and says "retry".
        try:
            enabled = _module_grant_state(canonical_key, request, db)
        except SQLAlchemyError:
            raise HTTPException(503, "Module check unavailable; try again shortly") from None
        if not enabled:
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

        if user:
            _stash_principal(request, user)

        role = normalize_role((user or {}).get("role"))
        if role in allowed:
            return

        # JWT from Authorization header as final fallback.
        claims = _decode_bearer_claims(request)
        if claims and normalize_role(claims.get("role")) in allowed:
            _stash_principal(request, claims)
            return

        if not user and not claims:
            raise _no_principal()
        raise HTTPException(status_code=403, detail="Insufficient role")

    return _dependency


def _no_principal() -> HTTPException:
    """The 401 an auth gate raises when the request carries no live principal
    (no token, or one that is expired, revoked or not an access token) —
    the same answer ``get_current_user`` gives."""
    return HTTPException(
        status_code=401,
        detail="Invalid or expired access token",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _decode_bearer_claims(request: Request) -> dict:
    """Claims of the request's bearer token if it is a live access token, else {}.

    The raw-decode fallback behind ``require_role`` and ``_resolve_request_user``
    (so ``require_permission``). It applies the two token checks
    ``get_current_user`` applies before trusting a JWT as a user:

    - ``typ`` must be absent or ``"access"``. Refresh tokens (30-day TTL) and
      MCP access tokens are signed with the same key and carry a ``sub``;
      without this check a refresh token stood in for an access token on any
      route gated only here, defeating the 15-minute access TTL (GDXA-1147).
    - the ``jti`` must not be on the revocation denylist (SS-7 Slice H parity),
      so a token revoked by logout or ``/auth/admin/revoke`` stops here too.
      Offboarding, demotion and password reset (``revoke_user_sessions``)
      revoke only the refresh family, not this token (GDXA-1215).

    A token PyJWT rejects (bad signature, expired, malformed, unexpected
    ``aud``) returns {}: that is an answer, not an error. An error escaping
    ``Denylist.contains`` raises 503, so an internal failure is neither
    admitted nor reported as "Insufficient role". ``contains`` itself treats a
    Redis outage as "not revoked" (the same as ``get_current_user``), so a
    revocation recorded only in Redis is not seen while Redis is down.

    This does NOT run ``finalize_login_jwt``'s users-row checks, so on routes
    gated only here an unexpired access token (up to 15 minutes) outlives a
    demotion of its role claim, a deactivation or deletion of its user, and the
    must-change-password restriction. Portal customer tokens are ``typ=access``
    with ``role=customer``; no gate admits that role today.
    """
    auth_header = request.headers.get("authorization", "") or ""
    if not auth_header.lower().startswith("bearer "):
        return {}
    token = auth_header.split(" ", 1)[1].strip()
    return decode_live_access_token(token, request)


def decode_live_access_token(token: str, conn: HTTPConnection) -> dict:
    """Claims of ``token`` if it is a live access token, else {}.

    The token checks behind ``_decode_bearer_claims`` (see there), for callers
    that carry the token somewhere other than the Authorization header: the
    dispatch board WebSocket takes it as a query parameter (GDXA-1188).
    ``conn`` is the Request or WebSocket; its app holds the denylist. Raises
    HTTPException(503) only when an error escapes the denylist lookup. A
    Redis outage is not such an error: ``Denylist.contains`` treats it as
    "not revoked" (fail-open), so a revocation recorded only in Redis is not
    seen while Redis is down.
    """
    import jwt as _jwt

    from gdx_dispatch.routers.auth import ALG as _ALG
    from gdx_dispatch.routers.auth import VERIFY_KEY as _VERIFY
    try:
        claims = _jwt.decode(token, _VERIFY, algorithms=[_ALG]) or {}
    except _jwt.InvalidTokenError as exc:  # noqa: E501  # noqa: silent-failure — a rejected token is an answer ({} -> 401/403), not an error
        logging.getLogger(__name__).debug("bearer fallback rejected token: %s", exc)
        return {}
    if claims.get("typ") not in (None, "access"):
        return {}
    jti = claims.get("jti")
    if jti:
        from gdx_dispatch.routers.auth.core import _get_app_denylist
        try:
            revoked = _get_app_denylist(conn).contains(str(jti))
        except Exception as exc:
            logging.getLogger(__name__).exception("decode_live_access_token: denylist check failed")
            raise HTTPException(status_code=503, detail="Token revocation check unavailable") from exc
        if revoked:
            return {}
    return claims


def _stash_principal(request: Request, user: dict) -> None:
    """Publish the resolved principal on request.state for downstream helpers.

    ``routers/auth/core.py`` does this for routes that depend on
    ``get_current_user``, with the comment "so audit helpers see it without
    per-route plumbing". Routes gated only by ``require_role`` never run that
    dependency, so nothing published the principal and every audit row they
    wrote was attributed to "system" (see core/audit.resolve_audit_actor).
    """
    try:
        if not getattr(request.state, "user", None) and not getattr(
            request.state, "current_user", None
        ):
            request.state.current_user = user
    except Exception:  # pragma: no cover - request.state is always writable
        logging.getLogger(__name__).exception("_stash_principal failed")


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
    return _decode_bearer_claims(request)


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
         closes the S97 D-item (`D-S97-perm-snapshot`): snapshots taken
         when the role was created miss any BUILTIN keys added later (e.g.,
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
    # users.id is a Uuid column: bind a UUID, not the str subject, or SQLite
    # raises and the JWT role wins by default (GDXA-723).
    try:
        uid = UUID(str(user_id)) if user_id else None
    except ValueError:
        logging.getLogger(__name__).warning("user_role_lookup_skipped: subject is not a UUID user=%s", user_id)
        uid = None  # not a UUID: no users row to read
    if uid is not None:
        try:
            from sqlalchemy import select as _select

            from gdx_dispatch.models.tenant_models import User as _User
            db_role = (
                db.execute(_select(_User.role).where(_User.id == uid, _User.deleted_at.is_(None))).scalar_one_or_none()
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
    # ignored). This is the S97 fix: a stale snapshot can't
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
        if has_permission(request, db, *required):
            return
        # A route's ``dependencies=[...]`` run before its parameter
        # dependencies, so this gate runs before ``get_current_user`` could
        # 401 an expired token. No principal is "who are you?", not "you may
        # not": answer 401 so the client sends the user to log in again.
        if not _resolve_request_user(request):
            raise _no_principal()
        missing = sorted(required - _request_permissions(request, db))
        raise HTTPException(status_code=403, detail=f"Missing permission: {missing}")

    return _dependency


def _request_permissions(request: Request, db: Session) -> set[str]:
    """The current user's resolved permission set, cached on the request so
    composite checks hit the DB once.

    The escape hatch (admin/owner always pass) is enforced at the
    _load_user_permissions level: builtin admin gets every key except
    billing.write, builtin owner gets WILDCARD. Reading from User.role in the
    tenant DB makes role demotions effective immediately — we no longer trust
    the JWT `role` claim for over-privileged bypass. (Stale-JWT fix,
    post-phase-4.)
    """
    cached = getattr(request.state, "user_permissions", None)
    if cached is None:
        cached = _load_user_permissions(db, request, _resolve_request_user(request))
        request.state.user_permissions = cached
    return cached


def has_permission(request: Request, db: Session, *keys: str) -> bool:
    """True when the current user holds every key — ``require_permission``'s
    check without the 403, for handlers that shape a response by permission
    (e.g. which fields a list includes) rather than refusing it.

    Precondition: call it only from a route that depends on
    ``get_current_user``. With no user on the request yet,
    ``_resolve_request_user`` falls back to decoding the bearer token itself
    (``_decode_bearer_claims``), which checks ``typ`` and the revocation
    denylist but skips the user-row checks ``get_current_user`` does.
    """
    from gdx_dispatch.core.permissions import WILDCARD

    perms = _request_permissions(request, db)
    return WILDCARD in perms or set(keys).issubset(perms)


# @router.get('/api/qb/status', dependencies=[Depends(require_module('quickbooks'))])
# @router.post('/api/jobs', dependencies=[Depends(require_role('admin', 'dispatcher'))])

"""SCIM 2.0 endpoints for enterprise IdP provisioning (SS-22 slices C/D/E).

Implements a RFC 7644-compliant surface for the operations GDX supports:
Users + Groups CRUD (GET/POST/PUT/DELETE) and the three discovery
endpoints (ServiceProviderConfig / ResourceTypes / Schemas). PATCH and
Bulk are explicitly unsupported and return 501 with a SCIM-shaped error
body so IdPs (Okta / Azure AD / OneLogin) can feature-detect before
their first mutation request.

Soft-delete semantics: ``active=False`` (PUT) or DELETE flip
``Identity.status='deleted'`` and set ``deleted_at`` — rows are never
physically removed, preserving the platform audit chain and allowing
re-provision to reactivate an existing identity row (see SS-22 plan §
Soft-delete on deprovision).

INTEGRATION TODO (do not commit from this slice):
  1. Wire this router into ``gdx_dispatch/main.py`` via ``app.include_router(
     scim.router)`` once the tenant-scoped SCIM credential provisioning
     lands (SS-14 / SS-15 PAT issuance).
  2. Replace the env-var-backed ``GDX_SCIM_TOKENS`` table with a
     ``AccessToken`` row lookup keyed by the platform's structured
     ``(action, resource_type)`` capability model.
  3. Decide on Identity.id → SCIM resource id: this router uses the
     native UUID string representation. If a separate ``provider_subject``
     column is added to Identity (see ``platform_ss22_additions.py``),
     switch the ``id`` response field accordingly.
  4. Add tenant scoping on ``request.state.tenant`` once the SCIM host
     (subdomain / path) is decided; current implementation scopes by
     the authenticated SCIM principal's ``tenant_id`` and filters all
     reads/writes on ``Membership.tenant_id``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.unified_principal import principal_tenant_uuid
from gdx_dispatch.core.scim_auth import (
    ScimAuthError,
    ScimPrincipal,
    require_scim_capability,
    scim_error_response,
)
from gdx_dispatch.core.scim_translate import (
    identity_to_scim_user,
    scim_group_to_membership_dicts,
    scim_user_to_identity_dict,
    unsupported_operation_error,
)
from gdx_dispatch.models.platform import Identity, IdentityProvider, Membership

_USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
_GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
_LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
_SPCONFIG_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"
_RESOURCE_TYPE_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:ResourceType"
_SCHEMA_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Schema"

# Capability strings embedded in SCIM tokens (v3 patch P38, flattened).
CAP_READ_SCIM_CONFIG = "read:scim_config"
CAP_READ_IDENTITY = "read:identity"
CAP_WRITE_IDENTITY = "write:identity"
CAP_READ_MEMBERSHIP = "read:membership"
CAP_WRITE_MEMBERSHIP = "write:membership"


router = APIRouter(prefix="/scim/v2", tags=["scim"])


# ─── exception handler ─────────────────────────────────────────────────────
# Registered at app startup by whoever calls ``include_router`` — see
# ``register_scim_exception_handlers`` below. Kept close to the router so
# the integration step is one call.


def register_scim_exception_handlers(app) -> None:
    """Attach the SCIM error-schema handler to a FastAPI app.

    INTEGRATION TODO: call this from ``gdx_dispatch/main.py`` alongside
    ``include_router(scim.router)``.
    """

    @app.exception_handler(ScimAuthError)
    async def _scim_auth_error(_request: Request, exc: ScimAuthError) -> JSONResponse:
        return scim_error_response(exc.detail, exc.http_status)


# ─── helpers ───────────────────────────────────────────────────────────────


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_identity_id(raw: str) -> UUID | None:
    try:
        return UUID(raw)
    except (ValueError, AttributeError):
        return None


def _provider_subject_for(identity: Identity, fallback_email: str | None = None) -> str:
    """Return the provider_subject used as SCIM userName.

    Prefers the first non-revoked IdentityProvider row; otherwise falls
    back to the email so a freshly-provisioned identity still renders
    a userName even before any IdentityProvider row is linked.
    """
    providers = [p for p in (identity.providers or []) if p.revoked_at is None]
    if providers:
        return providers[0].provider_subject
    return fallback_email or identity.email


def _identity_to_scim_dict(identity: Identity) -> dict:
    provider_subject = _provider_subject_for(identity, identity.email)
    return identity_to_scim_user(
        {
            "id": str(identity.id),
            "email": identity.email or "",
            "display_name": identity.display_name or "",
            "status": identity.status,
        },
        provider_subject=provider_subject,
    )


def _identity_not_found(identity_id: str) -> JSONResponse:
    return scim_error_response(f"User not found: {identity_id}", 404)


def _location_header(request: Request, path_suffix: str) -> str:
    # ``request.url_for`` would require named routes — build manually.
    base = str(request.base_url).rstrip("/")
    return f"{base}/scim/v2{path_suffix}"


# ─── ServiceProviderConfig / ResourceTypes / Schemas (slice E) ────────────


_SP_CONFIG_BODY: dict[str, Any] = {
    "schemas": [_SPCONFIG_SCHEMA],
    "documentationUri": "https://example.com/docs/scim",
    "patch": {"supported": False},
    "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
    "filter": {"supported": True, "maxResults": 200},
    "changePassword": {"supported": False},
    "sort": {"supported": False},
    "etag": {"supported": False},
    "authenticationSchemes": [
        {
            "type": "oauthbearertoken",
            "name": "OAuth Bearer Token",
            "description": "Tenant-scoped enterprise IdP bearer token",
            "primary": True,
        }
    ],
    # Advertise feature-detection hints for unsupported ops so clients
    # don't even attempt PATCH/Bulk before seeing 501. (v3 patch P39.)
    "_unsupported": {
        "patch": "Use PUT /Users/{id} for full-resource replacement.",
        "bulk": "Bulk operations are not implemented in GDX SCIM v1.",
    },
    "meta": {"resourceType": "ServiceProviderConfig"},
}


_RESOURCE_TYPES_BODY: dict[str, Any] = {
    "schemas": [_LIST_SCHEMA],
    "totalResults": 2,
    "Resources": [
        {
            "schemas": [_RESOURCE_TYPE_SCHEMA],
            "id": "User",
            "name": "User",
            "endpoint": "/Users",
            "description": "SCIM 2.0 User (RFC 7643)",
            "schema": _USER_SCHEMA,
            "meta": {"resourceType": "ResourceType"},
        },
        {
            "schemas": [_RESOURCE_TYPE_SCHEMA],
            "id": "Group",
            "name": "Group",
            "endpoint": "/Groups",
            "description": "SCIM 2.0 Group (RFC 7643)",
            "schema": _GROUP_SCHEMA,
            "meta": {"resourceType": "ResourceType"},
        },
    ],
}


_SCHEMAS_BODY: dict[str, Any] = {
    "schemas": [_LIST_SCHEMA],
    "totalResults": 2,
    "Resources": [
        {
            "schemas": [_SCHEMA_SCHEMA],
            "id": _USER_SCHEMA,
            "name": "User",
            "description": "SCIM 2.0 User",
            "attributes": [
                {"name": "userName", "type": "string", "required": True, "uniqueness": "server"},
                {"name": "active", "type": "boolean", "required": False},
                {
                    "name": "emails",
                    "type": "complex",
                    "multiValued": True,
                    "subAttributes": [
                        {"name": "value", "type": "string"},
                        {"name": "primary", "type": "boolean"},
                    ],
                },
                {
                    "name": "name",
                    "type": "complex",
                    "subAttributes": [
                        {"name": "givenName", "type": "string"},
                        {"name": "familyName", "type": "string"},
                    ],
                },
                {"name": "externalId", "type": "string", "required": False},
            ],
            "meta": {"resourceType": "Schema"},
        },
        {
            "schemas": [_SCHEMA_SCHEMA],
            "id": _GROUP_SCHEMA,
            "name": "Group",
            "description": "SCIM 2.0 Group",
            "attributes": [
                {"name": "displayName", "type": "string", "required": True},
                {
                    "name": "members",
                    "type": "complex",
                    "multiValued": True,
                    "subAttributes": [
                        {"name": "value", "type": "string"},
                        {"name": "type", "type": "string"},
                    ],
                },
            ],
            "meta": {"resourceType": "Schema"},
        },
    ],
}


@router.get("/ServiceProviderConfig")
def service_provider_config(
    _principal: ScimPrincipal = Depends(require_scim_capability(CAP_READ_SCIM_CONFIG)),
) -> dict:
    return _SP_CONFIG_BODY


@router.get("/ResourceTypes")
def resource_types(
    _principal: ScimPrincipal = Depends(require_scim_capability(CAP_READ_SCIM_CONFIG)),
) -> dict:
    return _RESOURCE_TYPES_BODY


@router.get("/Schemas")
def schemas(
    _principal: ScimPrincipal = Depends(require_scim_capability(CAP_READ_SCIM_CONFIG)),
) -> dict:
    return _SCHEMAS_BODY


# ─── Users CRUD (slice C) ──────────────────────────────────────────────────


@router.get("/Users")
def list_users(
    request: Request,
    startIndex: int = Query(default=1, ge=1),
    count: int = Query(default=50, ge=0, le=200),
    filter: str | None = Query(default=None),
    principal: ScimPrincipal = Depends(require_scim_capability(CAP_READ_IDENTITY)),
    db: Session = Depends(get_db),
) -> JSONResponse:
    stmt = select(Identity)

    # Scope to identities that have at least one membership in the caller's
    # tenant. This is the SCIM-provisioned surface; global identities outside
    # the tenant are invisible to IdPs.
    stmt = stmt.join(Membership, Membership.identity_id == Identity.id).where(
        Membership.tenant_id == principal_tenant_uuid(principal),
        Membership.revoked_at.is_(None),
    )

    if filter:
        flt = _parse_simple_filter(filter)
        if flt is None:
            return scim_error_response(
                f"Unsupported filter expression: {filter}", 400
            )
        attr, value = flt
        if attr == "userName":
            stmt = stmt.join(IdentityProvider, IdentityProvider.identity_id == Identity.id).where(
                IdentityProvider.provider_subject == value
            )
        elif attr == "email":
            stmt = stmt.where(func.lower(Identity.email) == value.lower())
        else:
            return scim_error_response(
                f"Filter attribute not supported: {attr}", 400
            )

    stmt = stmt.distinct().order_by(Identity.created_at.asc())

    total = db.scalar(
        select(func.count()).select_from(stmt.subquery())
    ) or 0

    # RFC 7644: startIndex is 1-based.
    offset = max(0, startIndex - 1)
    stmt = stmt.offset(offset).limit(count)
    rows = list(db.scalars(stmt).all())

    return JSONResponse(
        status_code=200,
        content={
            "schemas": [_LIST_SCHEMA],
            "totalResults": int(total),
            "startIndex": startIndex,
            "itemsPerPage": len(rows),
            "Resources": [_identity_to_scim_dict(r) for r in rows],
        },
    )


def _parse_simple_filter(expr: str) -> tuple[str, str] | None:
    """Minimal RFC 7644 filter parser: ``attr eq "value"``.

    Anything richer (co, sw, and/or) is out of scope for v1 — IdPs that
    need it will hit 400 and fall back to client-side filtering.
    """
    expr = expr.strip()
    # Split on ' eq ' (case-insensitive).
    lowered = expr.lower()
    idx = lowered.find(" eq ")
    if idx == -1:
        return None
    attr = expr[:idx].strip()
    rhs = expr[idx + 4:].strip()
    if len(rhs) >= 2 and rhs[0] == '"' and rhs[-1] == '"':
        return attr, rhs[1:-1]
    return None


@router.get("/Users/{user_id}")
def get_user(
    user_id: str,
    principal: ScimPrincipal = Depends(require_scim_capability(CAP_READ_IDENTITY)),
    db: Session = Depends(get_db),
) -> JSONResponse:
    uid = _parse_identity_id(user_id)
    if uid is None:
        return _identity_not_found(user_id)

    identity = db.get(Identity, uid)
    if identity is None or not _identity_in_tenant(db, identity, principal.tenant_id):
        return _identity_not_found(user_id)

    return JSONResponse(status_code=200, content=_identity_to_scim_dict(identity))


def _identity_in_tenant(db: Session, identity: Identity, tenant_id: str) -> bool:
    try:
        tenant_uuid = UUID(tenant_id)
    except (ValueError, TypeError):
        return False
    exists = db.scalar(
        select(func.count())
        .select_from(Membership)
        .where(
            Membership.identity_id == identity.id,
            Membership.tenant_id == tenant_uuid,
        )
    )
    return bool(exists and exists > 0)


@router.post("/Users")
def create_user(
    request: Request,
    payload: dict,
    principal: ScimPrincipal = Depends(require_scim_capability(CAP_WRITE_IDENTITY)),
    db: Session = Depends(get_db),
) -> JSONResponse:
    if not isinstance(payload, dict) or not payload.get("userName"):
        return scim_error_response("userName is required for POST /Users", 400)

    translated = scim_user_to_identity_dict(payload)
    provider_subject = translated["provider_subject"]
    email = translated.get("email") or payload.get("userName")

    # Re-provision path: if an identity already exists for this
    # provider_subject, reactivate it (idempotent per SS-22 test plan).
    existing = db.scalar(
        select(Identity)
        .join(IdentityProvider, IdentityProvider.identity_id == Identity.id)
        .where(IdentityProvider.provider_subject == provider_subject)
    )

    if existing is not None:
        existing.status = "active"
        existing.deleted_at = None
        if translated.get("display_name"):
            existing.display_name = translated["display_name"]
        if email:
            existing.email = email
        _ensure_membership(db, existing, principal.tenant_id)
        db.commit()
        db.refresh(existing)
        return JSONResponse(
            status_code=200,
            content=_identity_to_scim_dict(existing),
            headers={"Location": _location_header(request, f"/Users/{existing.id}")},
        )

    identity = Identity(
        id=uuid4(),
        email=email or "",
        display_name=translated.get("display_name"),
        status=translated["status"],
    )
    db.add(identity)
    db.flush()

    db.add(
        IdentityProvider(
            id=uuid4(),
            identity_id=identity.id,
            provider_type="scim",
            provider_subject=provider_subject,
            provider_email=email,
            email_verified_by_provider=False,
            is_authoritative_for_domain=False,
        )
    )
    _ensure_membership(db, identity, principal.tenant_id)
    db.commit()
    db.refresh(identity)

    return JSONResponse(
        status_code=201,
        content=_identity_to_scim_dict(identity),
        headers={"Location": _location_header(request, f"/Users/{identity.id}")},
    )


def _ensure_membership(db: Session, identity: Identity, tenant_id: str) -> None:
    """Ensure an active membership exists for this identity in the tenant.

    SCIM provisioning must surface the user to the tenant; without a
    membership row ``list_users`` cannot see it. Uses the first available
    capability_set; NOTE for INTEGRATION: real capability_set selection
    should use a role-name → capability_set lookup. Captured as TODO.
    """
    from gdx_dispatch.models.platform import CapabilitySet

    try:
        tenant_uuid = UUID(tenant_id)
    except (ValueError, TypeError):
        return  # Unresolved/non-UUID principal — nothing to provision against.

    existing = db.scalar(
        select(Membership).where(
            Membership.identity_id == identity.id,
            Membership.tenant_id == tenant_uuid,
        )
    )
    if existing is not None:
        existing.revoked_at = None
        return

    default_capset = db.scalar(select(CapabilitySet).limit(1))
    if default_capset is None:
        # Create a minimal placeholder capability set. INTEGRATION TODO:
        # replace with the real "scim_member" role once SS-14 lands.
        default_capset = CapabilitySet(
            id=uuid4(),
            name="scim_default_member",
            description="Auto-created by SCIM provisioning; replace via SS-14.",
            scope_type="tenant",
        )
        db.add(default_capset)
        db.flush()

    db.add(
        Membership(
            id=uuid4(),
            identity_id=identity.id,
            tenant_id=tenant_uuid,
            role="member",
            capability_set_id=default_capset.id,
        )
    )


@router.put("/Users/{user_id}")
def update_user(
    user_id: str,
    payload: dict,
    principal: ScimPrincipal = Depends(require_scim_capability(CAP_WRITE_IDENTITY)),
    db: Session = Depends(get_db),
) -> JSONResponse:
    uid = _parse_identity_id(user_id)
    if uid is None:
        return _identity_not_found(user_id)

    identity = db.get(Identity, uid)
    if identity is None or not _identity_in_tenant(db, identity, principal.tenant_id):
        return _identity_not_found(user_id)

    translated = scim_user_to_identity_dict(payload or {})
    # Full replacement semantics of PUT (RFC 7644 § 3.5.1).
    new_status = translated["status"]
    if "email" in translated and translated["email"]:
        identity.email = translated["email"]
    if translated.get("display_name"):
        identity.display_name = translated["display_name"]

    if new_status == "deleted":
        identity.status = "deleted"
        if identity.deleted_at is None:
            identity.deleted_at = _utcnow()
    else:
        identity.status = "active"
        identity.deleted_at = None

    db.commit()
    db.refresh(identity)

    return JSONResponse(status_code=200, content=_identity_to_scim_dict(identity))


@router.delete("/Users/{user_id}")
def delete_user(
    user_id: str,
    principal: ScimPrincipal = Depends(require_scim_capability(CAP_WRITE_IDENTITY)),
    db: Session = Depends(get_db),
) -> JSONResponse:
    uid = _parse_identity_id(user_id)
    if uid is None:
        return _identity_not_found(user_id)

    identity = db.get(Identity, uid)
    if identity is None or not _identity_in_tenant(db, identity, principal.tenant_id):
        return _identity_not_found(user_id)

    # Soft delete (SS-22 § Soft-delete on deprovision). Never hard-delete.
    identity.status = "deleted"
    identity.deleted_at = _utcnow()
    db.commit()

    return JSONResponse(status_code=204, content=None)


@router.api_route("/Users/{user_id}", methods=["PATCH"])
@router.api_route("/Users", methods=["PATCH"])
def patch_users_not_supported(user_id: str | None = None) -> JSONResponse:
    body = unsupported_operation_error("PATCH not supported; use PUT")
    return JSONResponse(status_code=501, content=body)


@router.post("/Bulk")
def bulk_not_supported() -> JSONResponse:
    body = unsupported_operation_error(
        "Bulk operations not supported in GDX SCIM v1"
    )
    return JSONResponse(status_code=501, content=body)


# ─── Groups CRUD (slice D) ─────────────────────────────────────────────────


def _membership_row_to_member(m: Membership) -> dict:
    return {"value": str(m.identity_id), "type": "User"}


def _group_response_for_role(
    db: Session, tenant_id: str, role: str
) -> dict:
    try:
        tenant_uuid = UUID(tenant_id)
    except (ValueError, TypeError):
        tenant_uuid = None
    members = list(
        db.scalars(
            select(Membership).where(
                Membership.tenant_id == tenant_uuid,
                Membership.role == role,
                Membership.revoked_at.is_(None),
            )
        ).all()
    )
    return {
        "schemas": [_GROUP_SCHEMA],
        "id": f"{tenant_id}:{role}",
        "displayName": role,
        "members": [_membership_row_to_member(m) for m in members],
        "meta": {"resourceType": "Group"},
    }


@router.get("/Groups")
def list_groups(
    principal: ScimPrincipal = Depends(require_scim_capability(CAP_READ_MEMBERSHIP)),
    db: Session = Depends(get_db),
) -> JSONResponse:
    rows = list(
        db.scalars(
            select(Membership.role)
            .where(
                Membership.tenant_id == principal_tenant_uuid(principal),
                Membership.revoked_at.is_(None),
            )
            .distinct()
        ).all()
    )
    resources = [_group_response_for_role(db, principal.tenant_id, r) for r in rows]
    return JSONResponse(
        status_code=200,
        content={
            "schemas": [_LIST_SCHEMA],
            "totalResults": len(resources),
            "startIndex": 1,
            "itemsPerPage": len(resources),
            "Resources": resources,
        },
    )


def _parse_group_id(group_id: str) -> tuple[str, str] | None:
    if ":" not in group_id:
        return None
    tenant, role = group_id.split(":", 1)
    if not tenant or not role:
        return None
    return tenant, role


@router.get("/Groups/{group_id}")
def get_group(
    group_id: str,
    principal: ScimPrincipal = Depends(require_scim_capability(CAP_READ_MEMBERSHIP)),
    db: Session = Depends(get_db),
) -> JSONResponse:
    parsed = _parse_group_id(group_id)
    if parsed is None or parsed[0] != principal.tenant_id:
        return scim_error_response(f"Group not found: {group_id}", 404)
    return JSONResponse(
        status_code=200,
        content=_group_response_for_role(db, parsed[0], parsed[1]),
    )


@router.post("/Groups")
def create_group(
    request: Request,
    payload: dict,
    principal: ScimPrincipal = Depends(require_scim_capability(CAP_WRITE_MEMBERSHIP)),
    db: Session = Depends(get_db),
) -> JSONResponse:
    if not isinstance(payload, dict) or not payload.get("displayName"):
        return scim_error_response("displayName is required for POST /Groups", 400)

    role = str(payload["displayName"]).strip()
    membership_dicts = scim_group_to_membership_dicts(payload, principal.tenant_id)

    from gdx_dispatch.models.platform import CapabilitySet

    default_capset = db.scalar(select(CapabilitySet).limit(1))
    if default_capset is None:
        default_capset = CapabilitySet(
            id=uuid4(),
            name="scim_default_member",
            description="Auto-created by SCIM provisioning; replace via SS-14.",
            scope_type="tenant",
        )
        db.add(default_capset)
        db.flush()

    for md in membership_dicts:
        identity_uuid = _parse_identity_id(md["identity_id"])
        if identity_uuid is None:
            continue
        identity = db.get(Identity, identity_uuid)
        if identity is None:
            continue
        existing = db.scalar(
            select(Membership).where(
                Membership.identity_id == identity_uuid,
                Membership.tenant_id == principal_tenant_uuid(principal),
                Membership.role == role,
            )
        )
        if existing is not None:
            existing.revoked_at = None
            continue
        db.add(
            Membership(
                id=uuid4(),
                identity_id=identity_uuid,
                tenant_id=principal_tenant_uuid(principal),
                role=role,
                capability_set_id=default_capset.id,
            )
        )

    db.commit()
    group_id = f"{principal.tenant_id}:{role}"
    return JSONResponse(
        status_code=201,
        content=_group_response_for_role(db, principal.tenant_id, role),
        headers={"Location": _location_header(request, f"/Groups/{group_id}")},
    )


@router.put("/Groups/{group_id}")
def update_group(
    group_id: str,
    payload: dict,
    principal: ScimPrincipal = Depends(require_scim_capability(CAP_WRITE_MEMBERSHIP)),
    db: Session = Depends(get_db),
) -> JSONResponse:
    parsed = _parse_group_id(group_id)
    if parsed is None or parsed[0] != principal.tenant_id:
        return scim_error_response(f"Group not found: {group_id}", 404)
    role = parsed[1]

    desired = {
        str(m.get("value"))
        for m in (payload or {}).get("members") or []
        if m.get("value")
    }

    existing_rows = list(
        db.scalars(
            select(Membership).where(
                Membership.tenant_id == principal_tenant_uuid(principal),
                Membership.role == role,
            )
        ).all()
    )
    existing_map = {str(m.identity_id): m for m in existing_rows}

    # Revoke memberships no longer present in the PUT body.
    for identity_id_str, membership in existing_map.items():
        if identity_id_str not in desired and membership.revoked_at is None:
            membership.revoked_at = _utcnow()

    # Add or reactivate desired memberships.
    from gdx_dispatch.models.platform import CapabilitySet

    default_capset = db.scalar(select(CapabilitySet).limit(1))
    if default_capset is None:
        return scim_error_response(
            "No capability_set available; tenant not bootstrapped.", 500
        )

    for identity_id_str in desired:
        identity_uuid = _parse_identity_id(identity_id_str)
        if identity_uuid is None:
            continue
        if identity_id_str in existing_map:
            existing_map[identity_id_str].revoked_at = None
            continue
        db.add(
            Membership(
                id=uuid4(),
                identity_id=identity_uuid,
                tenant_id=principal_tenant_uuid(principal),
                role=role,
                capability_set_id=default_capset.id,
            )
        )

    db.commit()
    return JSONResponse(
        status_code=200,
        content=_group_response_for_role(db, principal.tenant_id, role),
    )


@router.delete("/Groups/{group_id}")
def delete_group(
    group_id: str,
    principal: ScimPrincipal = Depends(require_scim_capability(CAP_WRITE_MEMBERSHIP)),
    db: Session = Depends(get_db),
) -> JSONResponse:
    parsed = _parse_group_id(group_id)
    if parsed is None or parsed[0] != principal.tenant_id:
        return scim_error_response(f"Group not found: {group_id}", 404)
    role = parsed[1]

    now = _utcnow()
    for membership in db.scalars(
        select(Membership).where(
            Membership.tenant_id == principal_tenant_uuid(principal),
            Membership.role == role,
            Membership.revoked_at.is_(None),
        )
    ).all():
        membership.revoked_at = now

    db.commit()
    return JSONResponse(status_code=204, content=None)


@router.api_route("/Groups/{group_id}", methods=["PATCH"])
@router.api_route("/Groups", methods=["PATCH"])
def patch_groups_not_supported(group_id: str | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=501,
        content=unsupported_operation_error("PATCH not supported; use PUT"),
    )

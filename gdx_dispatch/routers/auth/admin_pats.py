"""SS-15 slice B — Tenant-admin PAT issuance-on-behalf.

Supplements ``gdx_dispatch/routers/pats.py`` (SS-14 self-mint). A tenant admin (one
whose capability list contains an ``admin`` action on ``tenant``) may mint a
PAT on behalf of another identity that belongs to the admin's tenant.

Capability-subset rule (mirrors pats.py / D-13 / D-14): the admin may only
issue capabilities they themselves hold. A ``("*", "*")`` wildcard in the
admin's capabilities authorises any requested subset.

Write-scope approval gate (D-17): PATs that include a ``write`` action are
minted in ``pending_approval`` status; a separate POST
/api/admin/pats/{id}/approve endpoint activates them. Read-only PATs are
activated immediately.

Status / approval metadata live on ``AccessToken.status`` (String(32)) and
``AccessToken.metadata_json`` (JSON) — real columns on the canonical
``access_tokens`` table as of Sprint 0.9-a. The pending-approval plaintext
secret is held in ``metadata_json["_pending_secret"]`` until
``/approve`` releases it; on approval the key is popped from the JSON
payload in the same transaction that flips ``status`` → ``active``.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import bcrypt
from fastapi import APIRouter, Body, Depends, HTTPException, status
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from gdx_dispatch.core.auth_dispatcher import get_current_principal
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.events import emit_event
from gdx_dispatch.core.pagination import PageParams, envelope, paginate
from gdx_dispatch.core.unified_principal import Principal, principal_tenant_uuid
from gdx_dispatch.models.platform import Capability, CapabilitySet, Membership
from gdx_dispatch.models.platform_extensions import AccessToken
from gdx_dispatch.routers.auth.pats import _cap_set

router = APIRouter(prefix="/api/admin/pats", tags=["admin-pats"])

_MAX_EXPIRES_DAYS = 366
_DEFAULT_EXPIRES_DAYS = 90

# Sprint 0.9-f: state persistence promoted from in-memory shim to real ORM
# columns on ``AccessToken`` (status + metadata_json). Multi-worker safe,
# survives restart, encrypted at rest via PG.
_PRODUCTION_READY = True


def _is_tenant_admin(principal: Principal) -> bool:
    caps = _cap_set(principal)
    if ("admin", "tenant") in caps or ("*", "*") in caps:
        return True
    # Unified ``Principal`` also exposes role — accept ``owner``/``admin``
    # as tenant-admin equivalents so session-auth callers work without
    # an explicit ``admin:tenant`` capability row.
    return getattr(principal, "principal_role", None) in ("owner", "admin")


def _requires_approval(caps: list[Capability]) -> bool:
    """Any capability with a write action triggers the approval gate."""
    for cap in caps:
        act = (cap.action or "").lower()
        if act.startswith("write") or act in {"create", "update", "delete", "admin"}:
            return True
    return False


def _set_status(pat: AccessToken, status_value: str, metadata: dict[str, Any] | None = None) -> None:
    """Write status + metadata onto the ORM row.

    JSON columns do not auto-mutate; always reassign the merged dict so
    SQLAlchemy flags the attribute dirty.
    """
    pat.status = status_value
    if metadata:
        existing = dict(pat.metadata_json or {})
        existing.update(metadata)
        pat.metadata_json = existing


def _read_status(pat: AccessToken) -> str:
    return str(pat.status or "active")


@router.post("", status_code=status.HTTP_201_CREATED)
def admin_mint_pat(
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> dict[str, Any]:
    """Tenant admin mints a PAT on behalf of an identity in their tenant.

    Payload: ``{target_identity_id, name, capability_ids, expires_in_days}``

    - 403 if caller is not a tenant admin.
    - 403 if target identity is not a member of the admin's tenant.
    - 403 if any requested capability is not held by the admin.
    - If any write-scope capability is requested, mints with
      ``status='pending_approval'``; otherwise ``status='active'``.
    """
    if not _is_tenant_admin(principal):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "tenant admin role required")

    target_raw = payload.get("target_identity_id")
    if not target_raw:
        raise HTTPException(400, "target_identity_id is required")
    try:
        target_identity_id = UUID(str(target_raw))
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, f"target_identity_id invalid: {exc}") from exc

    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name is required")
    # 0.9-s A6: enforce name length. AccessToken.name is String(128) —
    # beyond that the DB rejects, but not before metadata_json / audit
    # event rows bloat. Cap in-request so callers get a clean 400.
    if len(name) > 128:
        raise HTTPException(400, "name must be 128 characters or fewer")

    capability_ids_raw = payload.get("capability_ids") or []
    if not isinstance(capability_ids_raw, list):
        raise HTTPException(400, "capability_ids must be a list")
    # 0.9-s A5: cap the list size. A 1M-UUID payload would otherwise
    # do O(n) parse + O(n) DB filter + O(n) link rows per PAT.
    if len(capability_ids_raw) > 1000:
        raise HTTPException(
            400, "capability_ids cannot exceed 1000 entries per PAT"
        )
    try:
        capability_ids = [UUID(str(cid)) for cid in capability_ids_raw]
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, f"capability_ids contain invalid UUID: {exc}") from exc

    try:
        requested_days = int(payload.get("expires_in_days", _DEFAULT_EXPIRES_DAYS))
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "expires_in_days must be an integer") from exc
    if requested_days <= 0:
        raise HTTPException(400, "expires_in_days must be positive")
    expires_in_days = min(requested_days, _MAX_EXPIRES_DAYS)

    # Verify target is in admin's tenant — direct membership lookup.
    target_in_tenant = db.execute(
        select(Membership).where(
            and_(
                Membership.identity_id == target_identity_id,
                Membership.tenant_id == principal_tenant_uuid(principal),
                Membership.revoked_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if target_in_tenant is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "target user not in your tenant")

    requested_caps: list[Capability] = []
    if capability_ids:
        requested_caps = list(
            db.execute(
                select(Capability).where(
                    and_(
                        Capability.id.in_(capability_ids),
                        Capability.revoked_at.is_(None),
                    )
                )
            ).scalars()
        )
    found_ids = {c.id for c in requested_caps}
    missing = [str(cid) for cid in capability_ids if cid not in found_ids]
    if missing:
        raise HTTPException(400, f"unknown capability_ids: {missing}")

    # Subset check against the admin's held capabilities.
    principal_actions = _cap_set(principal)
    has_wildcard = ("*", "*") in principal_actions
    for cap in requested_caps:
        if not has_wildcard and (cap.action, cap.resource_type) not in principal_actions:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"cannot grant capability you don't hold: {cap.action}:{cap.resource_type}",
            )

    now = datetime.now(timezone.utc)

    capset = CapabilitySet(
        id=uuid4(),
        name=f"admin-pat:{name}:{target_identity_id}:{uuid4().hex[:8]}",
        description=f"Admin-issued PAT capset for '{name}' → {target_identity_id}",
        scope_type="user",
        created_at=now,
    )
    db.add(capset)
    db.flush()

    for cap in requested_caps:
        db.add(
            Capability(
                id=uuid4(),
                capability_set_id=capset.id,
                action=cap.action,
                resource_type=cap.resource_type,
                instance_pattern=cap.instance_pattern,
                conditions=cap.conditions,
                parent_capability_id=cap.id,
                created_at=now,
            )
        )

    # D97: tenant_id is UUID; sandbox-suffix selection retired. See pats.py:155.
    prefix = "gdx_pat_live_"
    full_token = f"{prefix}{secrets.token_urlsafe(32)}"
    secret_hash = bcrypt.hashpw(full_token.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    pat = AccessToken(
        id=uuid4(),
        prefix=prefix,
        secret_hash=secret_hash,
        owner_type="user",
        owner_id=target_identity_id,  # owned by the target, not the admin
        installation_id=None,
        capability_set_id=capset.id,
        name=name,
        expires_at=now + timedelta(days=expires_in_days),
        created_at=now,
    )
    db.add(pat)
    db.flush()

    needs_approval = _requires_approval(requested_caps)
    status_value = "pending_approval" if needs_approval else "active"
    mint_metadata: dict[str, Any] = {
        "issued_by_admin_identity_id": str(principal.identity_id),
        "target_identity_id": str(target_identity_id),
    }
    if needs_approval:
        # Stash the plaintext so /approve can release it atomically. The
        # key is popped when status flips to active (see approve_pat).
        mint_metadata["_pending_secret"] = full_token
    _set_status(pat, status_value, mint_metadata)

    emit_event(
        db,
        "gdx_dispatch.pat.admin_issued.v1",
        {
            "pat_id": str(pat.id),
            "tenant_id": principal.tenant_id,
            "target_identity_id": str(target_identity_id),
            "issued_by_admin_identity_id": str(principal.identity_id),
            "capability_count": len(requested_caps),
            "status": status_value,
            "expires_at": pat.expires_at.isoformat() if pat.expires_at else None,
        },
        tenant_id=principal.tenant_id,
    )

    db.commit()

    body: dict[str, Any] = {
        "id": str(pat.id),
        "name": name,
        "target_identity_id": str(target_identity_id),
        "prefix": prefix,
        "status": status_value,
        "expires_at": pat.expires_at.isoformat() if pat.expires_at else None,
        "created_at": pat.created_at.isoformat(),
    }
    # Plaintext returned ONLY if no approval gate — gated tokens must be
    # approved before the secret is released. Here we return the secret
    # only when active to mirror pats.py; for pending tokens the body
    # omits the secret and the caller must call /approve first.
    if status_value == "active":
        body["secret"] = full_token
    # Pending-approval PATs omit the secret until /approve is called; the
    # plaintext is persisted in metadata_json["_pending_secret"] (committed
    # above) and released on approval.
    return body


@router.post("/{pat_id}/approve")
def approve_pat(
    pat_id: UUID,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> dict[str, Any]:
    """Tenant admin approves a pending write-scope PAT — activates it."""
    if not _is_tenant_admin(principal):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "tenant admin role required")

    pat = db.get(AccessToken, pat_id)
    if pat is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "PAT not found")
    if pat.revoked_at is not None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "PAT is revoked")

    current_status = _read_status(pat)
    if current_status == "active":
        return {"approved": True, "id": str(pat.id), "status": "active"}
    if current_status != "pending_approval":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"PAT is not pending approval (status={current_status})",
        )

    now = datetime.now(timezone.utc)
    # Pop the pending plaintext BEFORE committing, so the secret is only
    # retained in-memory for the response. The same commit flips status →
    # active and strips ``_pending_secret`` from metadata_json atomically.
    current_meta = dict(pat.metadata_json or {})
    released_secret = current_meta.pop("_pending_secret", None)
    pat.metadata_json = current_meta
    _set_status(
        pat,
        "active",
        {
            "approved_by": str(principal.identity_id),
            "approved_at": now.isoformat(),
        },
    )

    emit_event(
        db,
        "gdx_dispatch.pat.approved.v1",
        {
            "pat_id": str(pat.id),
            "tenant_id": principal.tenant_id,
            "approved_by": str(principal.identity_id),
        },
        tenant_id=principal.tenant_id,
    )
    db.commit()

    body: dict[str, Any] = {"approved": True, "id": str(pat.id), "status": "active"}
    if released_secret:
        body["secret"] = released_secret
    return body


@router.get("")
def list_admin_issued_pats(
    page: PageParams = Depends(),
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> dict[str, Any]:
    """List PATs for identities in the admin's tenant (no secrets).

    Paginated: see ``gdx_dispatch.core.pagination.PageParams``. Returns a
    ``{items, meta}`` envelope.
    """
    if not _is_tenant_admin(principal):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "tenant admin role required")

    # Identities in this tenant — membership lookup. Explicit ORDER BY
    # `granted_at` (Membership has no created_at) for deterministic
    # row ordering across backends.
    member_ids = [
        m.identity_id
        for m in db.execute(
            select(Membership)
            .where(
                and_(
                    Membership.tenant_id == principal_tenant_uuid(principal),
                    Membership.revoked_at.is_(None),
                )
            )
            .order_by(Membership.granted_at, Membership.id)
        ).scalars()
    ]
    if not member_ids:
        return envelope([], _empty_meta(page))

    # Ordered by created_at DESC (recency-first) + id tiebreak for stability.
    stmt = (
        select(AccessToken)
        .where(
            and_(
                AccessToken.owner_type == "user",
                AccessToken.owner_id.in_(member_ids),
                AccessToken.revoked_at.is_(None),
            )
        )
        .order_by(AccessToken.created_at.desc(), AccessToken.id)
    )
    paged_stmt, meta = paginate(stmt, page, db=db)
    rows = list(db.execute(paged_stmt).scalars())

    items = [
        {
            "id": str(p.id),
            "name": p.name,
            "prefix": p.prefix,
            "owner_identity_id": str(p.owner_id),
            "status": _read_status(p),
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "expires_at": p.expires_at.isoformat() if p.expires_at else None,
            "last_used_at": p.last_used_at.isoformat() if p.last_used_at else None,
        }
        for p in rows
    ]
    return envelope(items, meta)


def _empty_meta(page: PageParams):
    """Shortcut for the short-circuit path where we know total=0."""
    from gdx_dispatch.core.pagination import PageMeta

    p = page.clamped()
    return PageMeta(offset=p.offset, limit=p.limit, total=0, has_more=False)


@router.delete("/{pat_id}")
def admin_revoke_pat(
    pat_id: UUID,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> dict[str, Any]:
    """Tenant admin revokes a PAT belonging to a member of their tenant."""
    if not _is_tenant_admin(principal):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "tenant admin role required")

    pat = db.get(AccessToken, pat_id)
    if pat is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "PAT not found")

    # Verify owner is in admin's tenant.
    membership = db.execute(
        select(Membership).where(
            and_(
                Membership.identity_id == pat.owner_id,
                Membership.tenant_id == principal_tenant_uuid(principal),
                Membership.revoked_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if membership is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "PAT owner not in your tenant")

    if pat.revoked_at is not None:
        return {"revoked": True, "id": str(pat.id)}

    pat.revoked_at = datetime.now(timezone.utc)
    emit_event(
        db,
        "gdx_dispatch.pat.admin_revoked.v1",
        {
            "pat_id": str(pat.id),
            "tenant_id": principal.tenant_id,
            "revoked_by": str(principal.identity_id),
        },
        tenant_id=principal.tenant_id,
    )
    db.commit()
    return {"revoked": True, "id": str(pat.id)}

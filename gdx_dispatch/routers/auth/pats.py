"""Personal Access Token management endpoints (SS-14 slice D).

#   from gdx_dispatch.routers import pats as pats_router
#   app.include_router(pats_router.router)
#   real project-wide dependency once auth.py is wired (SS-14 integration).
#   Tests use `app.dependency_overrides[get_current_principal] = ...`.

Endpoints:
    POST   /api/pats         — mint a PAT; returns secret exactly once
    GET    /api/pats         — list caller's non-revoked PATs (no secrets)
    DELETE /api/pats/{pat_id} — revoke (soft) a PAT the caller owns

Capability-subset rule (D-13/D-14): a caller may only mint PATs whose
capabilities are a subset of their own. A ``("*", "*")`` capability is
treated as a wildcard grant and authorises any subset request.
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

from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.events import emit_event
from gdx_dispatch.models.platform import Capability, CapabilitySet
from gdx_dispatch.models.platform_extensions import AccessToken

router = APIRouter(prefix="/api/pats", tags=["pats"])

_MAX_EXPIRES_DAYS = 366
_DEFAULT_EXPIRES_DAYS = 90


# ── Principal contract for this router ──────────────────────────────────────


from gdx_dispatch.core.auth_dispatcher import get_current_principal  # noqa: E402
from gdx_dispatch.core.unified_principal import Principal  # noqa: E402


def _cap_set(principal: Principal) -> set[tuple[str, str]]:
    """Normalise principal capabilities to a set of 2-tuples.

    Accepts both the unified tuple-of-tuples shape and the legacy
    dict-of-dicts shape (tests that pre-date 0.9-e still construct
    principals with dict caps via ``SimpleNamespace``).
    """
    out: set[tuple[str, str]] = set()
    for c in principal.capabilities:
        if isinstance(c, dict):
            a = c.get("action")
            r = c.get("resource_type")
            if isinstance(a, str) and isinstance(r, str):
                out.add((a, r))
        elif isinstance(c, (tuple, list)) and len(c) == 2:
            out.add((str(c[0]), str(c[1])))
    return out


# ── Endpoints ───────────────────────────────────────────────────────────────


@router.post("", status_code=status.HTTP_201_CREATED)
def mint_pat(
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> dict[str, Any]:
    """Mint a PAT. The plaintext secret is returned exactly once."""
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name is required")

    capability_ids_raw = payload.get("capability_ids") or []
    if not isinstance(capability_ids_raw, list):
        raise HTTPException(400, "capability_ids must be a list")

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

    # Resolve the requested capability rows — all must exist and be
    # non-revoked. Missing IDs fail loudly.
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

    # Subset check: every requested capability must be held (or *:*).
    principal_actions = _cap_set(principal)
    has_wildcard = ("*", "*") in principal_actions
    for cap in requested_caps:
        if not has_wildcard and (cap.action, cap.resource_type) not in principal_actions:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"cannot grant capability you don't hold: {cap.action}:{cap.resource_type}",
            )

    now = datetime.now(timezone.utc)

    # Per-PAT capability_set, delegated via parent_capability_id.
    capset = CapabilitySet(
        id=uuid4(),
        name=f"pat:{name}:{principal.identity_id}:{uuid4().hex[:8]}",
        description=f"Capability set for PAT '{name}'",
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
                parent_capability_id=cap.id,  # delegation chain (D-32)
                created_at=now,
            )
        )

    # D97: ``principal.tenant_id`` is a UUID; the historical
    # ``-sandbox`` slug suffix can no longer steer prefix selection.
    # Audit an earlier session confirmed 0 prod tenants with sandbox slugs and
    # no callers depend on the test prefix today. Always-live until a
    # future ``Tenant.is_sandbox`` flag replaces this.
    prefix = "gdx_pat_live_"
    full_token = f"{prefix}{secrets.token_urlsafe(32)}"
    secret_hash = bcrypt.hashpw(full_token.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    pat = AccessToken(
        id=uuid4(),
        prefix=prefix,
        secret_hash=secret_hash,
        owner_type="user",
        owner_id=principal.identity_id,
        installation_id=None,
        capability_set_id=capset.id,
        name=name,
        expires_at=now + timedelta(days=expires_in_days),
        created_at=now,
    )
    db.add(pat)
    db.flush()

    emit_event(
        db,
        "gdx_dispatch.pat.created.v1",
        {
            "pat_id": str(pat.id),
            "tenant_id": principal.tenant_id,
            "owner_identity_id": str(principal.identity_id),
            "capability_count": len(requested_caps),
            "expires_at": pat.expires_at.isoformat() if pat.expires_at else None,
        },
        tenant_id=principal.tenant_id,
    )

    db.commit()

    return {
        "id": str(pat.id),
        "name": name,
        "secret": full_token,  # plaintext is returned ONCE, here
        "prefix": prefix,
        "expires_at": pat.expires_at.isoformat() if pat.expires_at else None,
        "created_at": pat.created_at.isoformat(),
    }


@router.get("")
def list_pats(
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> list[dict[str, Any]]:
    """List the caller's non-revoked PATs. Secrets are NEVER returned."""
    rows = list(
        db.execute(
            select(AccessToken).where(
                and_(
                    AccessToken.owner_type == "user",
                    AccessToken.owner_id == principal.identity_id,
                    AccessToken.revoked_at.is_(None),
                )
            )
        ).scalars()
    )
    return [
        {
            "id": str(p.id),
            "name": p.name,
            "prefix": p.prefix,
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "expires_at": p.expires_at.isoformat() if p.expires_at else None,
            "last_used_at": p.last_used_at.isoformat() if p.last_used_at else None,
        }
        for p in rows
    ]


@router.delete("/{pat_id}")
def revoke_pat(
    pat_id: UUID,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> dict[str, Any]:
    """Soft-revoke a PAT the caller owns."""
    pat = db.get(AccessToken, pat_id)
    if pat is None or pat.owner_id != principal.identity_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "PAT not found")
    if pat.revoked_at is not None:
        # Idempotent: already revoked is a 200 with revoked=True.
        return {"revoked": True, "id": str(pat.id)}

    pat.revoked_at = datetime.now(timezone.utc)

    emit_event(
        db,
        "gdx_dispatch.pat.revoked.v1",
        {
            "pat_id": str(pat_id),
            "tenant_id": principal.tenant_id,
        },
        tenant_id=principal.tenant_id,
    )

    db.commit()
    return {"revoked": True, "id": str(pat.id)}

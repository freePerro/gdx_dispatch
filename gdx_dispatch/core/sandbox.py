"""SS-10 Slice D — sandbox lifecycle primitives.

Minimal helpers over ``SandboxEnv`` for the three state transitions a
sandbox row owns: provision, reset, teardown. Mirrors the meter-emit
pattern from Slice C: the helpers stage state on a caller-owned session
and never commit or flush — transaction control stays with the caller.

There is deliberately no app wiring here; routers / the CC control plane
will call these in a later slice.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy.orm import Session

from gdx_dispatch.models.platform_extensions import SandboxEnv


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_tenant_uuid(tenant_id: str | UUID) -> UUID:
    """D97: SandboxEnv.tenant_id is now ``Uuid``; callers may pass UUID or
    a UUID-stringified principal. Raises ValueError on bad input."""
    if isinstance(tenant_id, UUID):
        return tenant_id
    return UUID(str(tenant_id))


def provision_sandbox(
    db: Session,
    tenant_id: str | UUID,
    subdomain: str,
) -> SandboxEnv:
    """Stage a new active ``SandboxEnv`` row and return it.

    The row is added to ``db`` but not persisted — callers own the tx.
    """
    row = SandboxEnv(
        tenant_id=_coerce_tenant_uuid(tenant_id),
        subdomain=subdomain,
        status="active",
    )
    db.add(row)
    return row


def reset_sandbox(
    db: Session,
    tenant_id: str | UUID,
    sandbox_id: UUID | str,
) -> SandboxEnv | None:
    """Mark a sandbox reset; return the row or ``None`` if not found
    or not owned by the given tenant.

    Cross-tenant access returns ``None`` (same shape as "not found") to
    avoid revealing whether a sandbox_id belongs to another tenant.
    """
    try:
        owner_uuid = _coerce_tenant_uuid(tenant_id)
    except (ValueError, TypeError):
        return None
    row = db.get(SandboxEnv, sandbox_id)
    if row is None or row.tenant_id != owner_uuid:
        return None
    row.status = "active"
    row.last_reset_at = _utcnow()
    return row


def teardown_sandbox(
    db: Session,
    tenant_id: str | UUID,
    sandbox_id: UUID | str,
) -> SandboxEnv | None:
    """Mark a sandbox torn down; return the row or ``None`` if not found
    or not owned by the given tenant.

    Cross-tenant access returns ``None`` — same 404 behavior as missing,
    so callers cannot enumerate other tenants' sandbox_ids.
    """
    try:
        owner_uuid = _coerce_tenant_uuid(tenant_id)
    except (ValueError, TypeError):
        return None
    row = db.get(SandboxEnv, sandbox_id)
    if row is None or row.tenant_id != owner_uuid:
        return None
    row.status = "torn_down"
    row.torn_down_at = _utcnow()
    return row

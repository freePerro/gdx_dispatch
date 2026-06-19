"""SS-28 slice A — consumer-audit write helper.

``record_consumer_action`` is the single entry-point every platform
consumer surface uses to write an audit row. It fetches the tenant's
most recent row (for ``prev_hash``), computes ``row_hash`` via
:mod:`gdx_dispatch.core.audit_hash_chain`, and INSERTs the new row inside the
caller-supplied transaction.

Design rules (per SS-28 spec):

* **Fail-closed.** Any exception propagates to the caller. Middleware
  is responsible for turning a write failure into an HTTP 500 with
  ``audit_write_failed`` — see :mod:`gdx_dispatch.core.middleware.consumer_audit_middleware`.
  This module does not swallow exceptions.
* **No implicit commit.** The function flushes (so the row is visible
  to a subsequent chain read inside the same tx) but does NOT commit.
  The request handler — or the middleware wrapper — owns the commit.
* **Deterministic clock.** ``created_at`` is captured once at entry so
  the value that goes into the hash is the value that goes into the
  row. No ``server_default=now()`` second-guessing.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import UUID, uuid4

from gdx_dispatch.core.audit_hash_chain import ZERO_HASH, compute_row_hash


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def record_consumer_action(
    db: Any,
    *,
    tenant_id: str,
    principal_identity_id: str | None,
    action: str,
    resource_type: str,
    resource_id: str | None,
    result: str,
    details: Mapping[str, Any] | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> "Any":
    """Append one row to ``platform_consumer_audit``.

    Returns the ORM row (post-flush, so its generated ``id`` is visible
    to the caller). Does NOT commit.
    """
    from gdx_dispatch.models.platform_ss28_additions import PlatformConsumerAudit

    if not tenant_id:
        raise ValueError("record_consumer_action: tenant_id is required")
    if not action:
        raise ValueError("record_consumer_action: action is required")
    if not resource_type:
        raise ValueError("record_consumer_action: resource_type is required")
    if not result:
        raise ValueError("record_consumer_action: result is required")

    # `tenant_id` is declared `str`, but the column is Uuid(as_uuid=True): on
    # sqlite the bind processor requires a uuid.UUID object (Postgres adapts
    # strings natively), and chain verification reads the column back as a UUID
    # object — so the hash must be computed over the UUID form to stay
    # consistent. Single-tenant pinning forwards the id as a str, so normalize
    # here. A non-UUID id (e.g. an unconfigured "gdx" slug) is left untouched so
    # the backend surfaces a clear error rather than us masking it.
    if isinstance(tenant_id, str):
        try:
            tenant_id = UUID(tenant_id)
        except ValueError:
            pass

    now = _utcnow()
    new_id = uuid4()

    prev_hash = _fetch_latest_row_hash(db, tenant_id)

    row_payload: dict[str, Any] = {
        "id": new_id,
        "tenant_id": tenant_id,
        "principal_identity_id": principal_identity_id,
        "action": action,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "result": result,
        "details": dict(details) if details is not None else None,
        "ip_address": ip_address,
        "user_agent": user_agent,
        "created_at": now,
    }

    row_hash = compute_row_hash(row_payload, prev_hash)

    orm_row = PlatformConsumerAudit(
        id=new_id,
        tenant_id=tenant_id,
        principal_identity_id=principal_identity_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        result=result,
        details=row_payload["details"],
        ip_address=ip_address,
        user_agent=user_agent,
        created_at=now,
        prev_hash=prev_hash,
        row_hash=row_hash,
    )
    db.add(orm_row)
    db.flush()
    return orm_row


def _fetch_latest_row_hash(db: Any, tenant_id: str) -> str:
    """Return the ``row_hash`` of the newest row for this tenant.

    If the tenant has no prior rows, returns :data:`ZERO_HASH`.
    Ordering is ``created_at DESC, id DESC`` — ``id`` is a tiebreaker
    for rows written inside the same microsecond (rare on PG, possible
    on sqlite in tests).
    """
    from gdx_dispatch.models.platform_ss28_additions import PlatformConsumerAudit

    from uuid import UUID as _UUID
    if isinstance(tenant_id, str):
        try:
            tenant_id = _UUID(tenant_id)
        except ValueError:
            pass

    latest = (
        db.query(PlatformConsumerAudit)
        .filter(PlatformConsumerAudit.tenant_id == tenant_id)
        .order_by(
            PlatformConsumerAudit.created_at.desc(),
            PlatformConsumerAudit.id.desc(),
        )
        .first()
    )
    if latest is None:
        return ZERO_HASH
    return latest.row_hash

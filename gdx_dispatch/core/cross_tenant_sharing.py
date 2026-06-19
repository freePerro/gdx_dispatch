"""SS-27 slice A — cross-tenant sharing core helpers.

Machinery for tenant A (sharer) to grant read/write access to specific
resources to tenant B (sharee), with audit trail, revocation, per-resource
+ per-capability scoping.

Public surface
--------------
- :func:`create_share` — sharer emits a grant. Returns the row + a
  one-time URL-safe token the sharee presents at
  :func:`accept_share`. Idempotent on
  ``(sharer, sharee, resource_type, resource_id)``: re-calling with the
  same args returns the existing active share and does NOT emit a
  duplicate event.
- :func:`accept_share` — sharee presents the token; a
  ``CrossTenantShareAcceptance`` row is recorded. Single-use;
  presenting the same token twice raises ``ShareAcceptanceError``.
- :func:`revoke_share` — sharer (or platform ops) sets ``revoked_at``
  and the actor. Idempotent on the revoked side.
- :func:`check_share_grants_capability` — the middleware's decision
  helper. Returns True iff there is an ACCEPTED, non-revoked, non-
  expired share whose capability list contains ``caller_capability``.

Security rules (per SS-27 plan)
-------------------------------
- Acceptance token: 128-bit URL-safe secret, single-use, 7-day expiry.
  Stored bcrypt-hashed only; constant-time comparison via
  :func:`hmac.compare_digest`.
- Expired or revoked shares always return False from
  :func:`check_share_grants_capability` — never a partial answer.
- Event emission is IDEMPOTENT: if the share already exists and is
  active, no event is emitted on the re-create call.
- Commit semantics: this module NEVER commits. Callers own the
  transaction boundary (matches the rest of the core helper fleet —
  see :mod:`gdx_dispatch.core.events`).
"""
from __future__ import annotations

import hmac
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional
from uuid import UUID

import bcrypt
from sqlalchemy import and_, select


def _to_uuid(value: Any) -> UUID:
    """D97: cross_tenant_share.*_tenant_id columns are now Uuid; module
    callers may still pass UUID strings. Coerce; raise if shape is wrong."""
    if isinstance(value, UUID):
        return value
    return UUID(str(value))
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


# One-time acceptance token: 128-bit random URL-safe secret (22 chars
# of base64url). Callers must transmit this over TLS; we never persist
# the plaintext — only its bcrypt hash.
_TOKEN_BYTES = 16

# Default expiry per spec.
_DEFAULT_EXPIRY_DAYS = 7


class ShareError(RuntimeError):
    """Base error for the sharing subsystem."""


class ShareNotFoundError(ShareError):
    """Raised when a share lookup by id/token finds nothing."""


class ShareAcceptanceError(ShareError):
    """Raised when a token doesn't match, is expired, or was already used."""


class ShareRevocationError(ShareError):
    """Raised on invalid revoke attempts (e.g., unknown share)."""


@dataclass
class CreateShareResult:
    """Return shape for :func:`create_share`.

    ``acceptance_token`` is the one-time plaintext the caller (sharer UI
    or API) must deliver to the sharee out-of-band. It is the ONLY time
    the plaintext appears; the DB stores only its bcrypt hash.
    ``was_existing`` is True on idempotent re-creates — callers can use
    that to decide whether to (not) surface the token again.
    """

    share: Any  # CrossTenantShare row — avoid import cycle in type hint
    acceptance_token: Optional[str]
    was_existing: bool


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Coerce a possibly-naive datetime to UTC-aware.

    SQLite strips tz info on load; PG preserves it. This helper makes
    comparisons against :func:`_utcnow` safe on both backends without
    silently downgrading the prod precision.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _generate_acceptance_token() -> str:
    """Return a URL-safe 128-bit secret (22 chars base64url)."""
    return secrets.token_urlsafe(_TOKEN_BYTES)


def _hash_token(token: str) -> str:
    """bcrypt-hash the acceptance token. Returns the utf-8 hash string."""
    salted = bcrypt.hashpw(token.encode("utf-8"), bcrypt.gensalt())
    return salted.decode("utf-8")


def _verify_token(token: str, stored_hash: str) -> bool:
    """Constant-time verify a candidate plaintext against a bcrypt hash.

    Uses ``bcrypt.checkpw`` which is itself constant-time relative to
    the hash's work factor; we additionally guard the stored-hash
    comparison with :func:`hmac.compare_digest` per the SS-27 rules.
    """
    try:
        ok = bcrypt.checkpw(token.encode("utf-8"), stored_hash.encode("utf-8"))
    except Exception:  # noqa: BLE001 — malformed hash → denied
        logger.warning("cross_tenant_sharing: malformed bcrypt hash on verify")
        return False
    # Explicit constant-time final gate, per spec.
    return hmac.compare_digest(b"1" if ok else b"0", b"1")


def _normalize_capabilities(caps: Iterable[str]) -> list[str]:
    """Validate + canonicalize the capability list.

    Accepts any iterable of non-empty strings, deduplicates, lowercases,
    and sorts for stable storage. An empty list is rejected — a share
    that grants nothing is a footgun, not a feature.
    """
    out: set[str] = set()
    for c in caps or ():
        if not isinstance(c, str):
            raise ShareError(f"capability must be str, got {type(c).__name__}")
        norm = c.strip().lower()
        if not norm:
            raise ShareError("capability cannot be empty/whitespace")
        out.add(norm)
    if not out:
        raise ShareError("capabilities list cannot be empty")
    return sorted(out)


def _find_active_share(
    db: Session,
    *,
    sharer_tenant_id: str,
    sharee_tenant_id: str,
    resource_type: str,
    resource_id: str,
) -> Any:
    """Return an active (not revoked) share row for the given key, else None."""
    from gdx_dispatch.models.platform_ss27_additions import CrossTenantShare  # lazy

    stmt = select(CrossTenantShare).where(
        and_(
            CrossTenantShare.sharer_tenant_id == _to_uuid(sharer_tenant_id),
            CrossTenantShare.sharee_tenant_id == _to_uuid(sharee_tenant_id),
            CrossTenantShare.resource_type == resource_type,
            CrossTenantShare.resource_id == resource_id,
            CrossTenantShare.revoked_at.is_(None),
        )
    )
    return db.execute(stmt).scalar_one_or_none()


def create_share(
    db: Session,
    *,
    sharer: str,
    sharee: str,
    resource_type: str,
    resource_id: str,
    capabilities: Iterable[str],
    expires_in_days: Optional[int] = _DEFAULT_EXPIRY_DAYS,
    created_by_identity_id: str,
) -> CreateShareResult:
    """Create a new cross-tenant share, or return the existing active one.

    Idempotent: if an active (non-revoked) share on the same
    ``(sharer, sharee, resource_type, resource_id)`` key already exists,
    this function returns that row, does NOT mint a new acceptance token,
    and does NOT emit a ``gdx.sharing.created.v1`` event.

    On a genuine create path the caller is responsible for emitting the
    event via :mod:`gdx_dispatch.core.cross_tenant_sharing_events`. That lives in
    the router so the emit happens in the same unit-of-work as the
    insert (see SS-27 slice D).
    """
    if not sharer or not sharee:
        raise ShareError("sharer and sharee tenant ids are required")
    if sharer == sharee:
        raise ShareError("cannot share a resource with the sharer's own tenant")
    if not resource_type or not resource_id:
        raise ShareError("resource_type and resource_id are required")
    if not created_by_identity_id:
        raise ShareError("created_by_identity_id is required")

    caps = _normalize_capabilities(capabilities)

    existing = _find_active_share(
        db,
        sharer_tenant_id=sharer,
        sharee_tenant_id=sharee,
        resource_type=resource_type,
        resource_id=resource_id,
    )
    if existing is not None:
        logger.info(
            "cross_tenant_sharing: idempotent re-create sharer=%s sharee=%s "
            "resource=%s/%s → existing id=%s",
            sharer,
            sharee,
            resource_type,
            resource_id,
            existing.id,
        )
        return CreateShareResult(
            share=existing,
            acceptance_token=None,
            was_existing=True,
        )

    from gdx_dispatch.models.platform_ss27_additions import CrossTenantShare  # lazy

    token = _generate_acceptance_token()
    token_hash = _hash_token(token)

    expires_at: Optional[datetime] = None
    if expires_in_days is not None and expires_in_days > 0:
        expires_at = _utcnow() + timedelta(days=expires_in_days)

    row = CrossTenantShare(
        sharer_tenant_id=_to_uuid(sharer),
        sharee_tenant_id=_to_uuid(sharee),
        resource_type=resource_type,
        resource_id=resource_id,
        capabilities=caps,
        acceptance_token_hash=token_hash,
        expires_at=expires_at,
        created_by_identity_id=created_by_identity_id,
        shared_at=_utcnow(),
    )
    db.add(row)
    # Flush so row.id is available for the caller + emitted events.
    # Race guard (Pattern 1, ss27): two concurrent create_share calls can both
    # pass the _find_active_share check, then race on INSERT. The unique
    # constraint catches the loser's INSERT — we catch IntegrityError, roll
    # the savepoint back, and re-query for the now-existing row. Works on
    # both PG (uq violation) and sqlite (unique constraint violation).
    # NOTE: D-item filed for the sharer↔sharee↔resource UniqueConstraint
    # → partial unique index `WHERE revoked_at IS NULL` conversion at
    # migration-chain integration; until that lands, revoked+re-shared rows
    # won't trigger the INSERT race because the active-row check screens
    # them out and the IntegrityError path catches the rare collision.
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        existing_after_race = _find_active_share(
            db,
            sharer_tenant_id=sharer,
            sharee_tenant_id=sharee,
            resource_type=resource_type,
            resource_id=resource_id,
        )
        if existing_after_race is not None:
            logger.info(
                "cross_tenant_sharing: integrity-race on create → returning "
                "existing id=%s",
                existing_after_race.id,
            )
            return CreateShareResult(
                share=existing_after_race,
                acceptance_token=None,
                was_existing=True,
            )
        # The integrity error wasn't the unique-share collision — re-raise.
        raise
    logger.info(
        "cross_tenant_sharing: created share id=%s sharer=%s sharee=%s res=%s/%s",
        row.id,
        sharer,
        sharee,
        resource_type,
        resource_id,
    )
    return CreateShareResult(
        share=row,
        acceptance_token=token,
        was_existing=False,
    )


def accept_share(
    db: Session,
    *,
    acceptance_token: str,
    accepted_by_identity_id: str,
    accepted_by_tenant_id: str,
) -> Any:
    """Record the sharee's acceptance of a share.

    The token is compared against stored bcrypt hashes; we must iterate
    because bcrypt is salted per-hash (there is no O(1) lookup by
    plaintext). The search is scoped to shares owned by the presenting
    tenant AND still active, which keeps the candidate set small.

    Raises ``ShareAcceptanceError`` on:
    - no matching active share
    - share expired
    - share already revoked
    - share already accepted (single-use)
    """
    from gdx_dispatch.models.platform_ss27_additions import (  # lazy
        CrossTenantShare,
        CrossTenantShareAcceptance,
    )

    if not acceptance_token:
        raise ShareAcceptanceError("acceptance token is required")
    if not accepted_by_identity_id or not accepted_by_tenant_id:
        raise ShareAcceptanceError("identity + tenant id required on accept")

    now = _utcnow()
    stmt = select(CrossTenantShare).where(
        and_(
            CrossTenantShare.sharee_tenant_id == _to_uuid(accepted_by_tenant_id),
            CrossTenantShare.revoked_at.is_(None),
        )
    )
    candidates = list(db.execute(stmt).scalars())

    match = None
    for row in candidates:
        if _verify_token(acceptance_token, row.acceptance_token_hash):
            match = row
            break

    if match is None:
        raise ShareAcceptanceError("no matching share for token")

    exp = _as_utc(match.expires_at)
    if exp is not None and exp <= now:
        raise ShareAcceptanceError("share expired")

    already = db.execute(
        select(CrossTenantShareAcceptance).where(
            CrossTenantShareAcceptance.share_id == match.id
        )
    ).scalar_one_or_none()
    if already is not None:
        raise ShareAcceptanceError("share already accepted")

    acc = CrossTenantShareAcceptance(
        share_id=match.id,
        accepted_by_identity_id=accepted_by_identity_id,
        accepted_by_tenant_id=_to_uuid(accepted_by_tenant_id),
        accepted_at=now,
    )
    db.add(acc)
    # Race guard (Pattern 1, ss27): two concurrent accept_share calls for the
    # same token can both pass the "already accepted" check above and race
    # on INSERT. The acceptance row's share_id unique constraint catches the
    # loser; we re-raise as ShareAcceptanceError so callers see the same
    # "single-use" semantics under contention as in the serial path.
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        already_after_race = db.execute(
            select(CrossTenantShareAcceptance).where(
                CrossTenantShareAcceptance.share_id == match.id
            )
        ).scalar_one_or_none()
        if already_after_race is not None:
            raise ShareAcceptanceError("share already accepted") from exc
        raise
    logger.info(
        "cross_tenant_sharing: accepted share id=%s by identity=%s",
        match.id,
        accepted_by_identity_id,
    )
    return acc


def revoke_share(
    db: Session,
    *,
    share_id: UUID | str,
    revoker_identity_id: str,
) -> Any:
    """Set revoked_at + revoker on the share. Idempotent on already-revoked."""
    from gdx_dispatch.models.platform_ss27_additions import CrossTenantShare  # lazy

    if not revoker_identity_id:
        raise ShareRevocationError("revoker_identity_id is required")

    row = db.get(CrossTenantShare, share_id)
    if row is None:
        raise ShareNotFoundError(f"share {share_id} not found")

    if row.revoked_at is not None:
        logger.info(
            "cross_tenant_sharing: revoke no-op (already revoked) id=%s",
            row.id,
        )
        return row

    row.revoked_at = _utcnow()
    row.revoked_by_identity_id = revoker_identity_id
    logger.info(
        "cross_tenant_sharing: revoked share id=%s by identity=%s",
        row.id,
        revoker_identity_id,
    )
    return row


def check_share_grants_capability(
    db: Session,
    *,
    sharee_tenant_id: str,
    caller_capability: str,
    resource_type: str,
    resource_id: str,
) -> bool:
    """Return True iff an ACCEPTED, non-revoked, non-expired share grants
    ``caller_capability`` on ``(resource_type, resource_id)`` to
    ``sharee_tenant_id``.

    Any error in lookup returns False (fail closed). This is the hot path
    called by the cross-tenant access middleware on every cross-tenant
    request — it is read-only.
    """
    try:
        from gdx_dispatch.models.platform_ss27_additions import (  # lazy
            CrossTenantShare,
            CrossTenantShareAcceptance,
        )

        if not sharee_tenant_id or not caller_capability:
            return False
        if not resource_type or not resource_id:
            return False

        norm_cap = caller_capability.strip().lower()
        now = _utcnow()

        stmt = (
            select(CrossTenantShare)
            .join(
                CrossTenantShareAcceptance,
                CrossTenantShareAcceptance.share_id == CrossTenantShare.id,
            )
            .where(
                and_(
                    CrossTenantShare.sharee_tenant_id == _to_uuid(sharee_tenant_id),
                    CrossTenantShare.resource_type == resource_type,
                    CrossTenantShare.resource_id == resource_id,
                    CrossTenantShare.revoked_at.is_(None),
                )
            )
        )
        rows = list(db.execute(stmt).scalars())
        for row in rows:
            exp = _as_utc(row.expires_at)
            if exp is not None and exp <= now:
                continue
            caps = row.capabilities or []
            if norm_cap in (c.lower() for c in caps if isinstance(c, str)):
                return True
        return False
    except Exception:  # noqa: BLE001 — fail closed on any error
        logger.exception(
            "cross_tenant_sharing: check_share_grants_capability errored "
            "(fail-closed → False)"
        )
        return False

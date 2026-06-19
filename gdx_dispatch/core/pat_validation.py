"""PAT bearer-token validation (SS-14 slice C).

#   at the "is this a bearer PAT?" branch. Do NOT modify auth.py from this slice.

Prefix taxonomy (D-13 / D-14, Stripe-style):

    gdx_pat_live_*  — live personal access token (production tenant)
    gdx_pat_test_*  — test PAT (sandbox tenant, ends in "-sandbox")
    gdx_sk_live_*   — reserved; accepted by the validator for forward compat
    gdx_sk_test_*   — reserved

Validation sequence:

    1. Cheap structural check on the token shape. Reject unknown prefixes
       without hitting the DB.
    2. Narrow the candidate set with ``AccessToken.prefix == <prefix>``
       and ``revoked_at IS NULL`` (indexed lookup).
    3. bcrypt.checkpw against each candidate's secret_hash. Bounded by
       the number of PATs sharing a prefix within the revoked_at-null
       set — in practice a handful per user.
    4. Enforce expires_at.
    5. Sample last_used_at updates (only write if prior update > 5 min
       ago) to avoid hot-rowing the AccessToken row on every request.
    6. Build a Principal-like view object with the capability rows
       resolved from the token's capability_set.

This module intentionally returns a lightweight ``PatPrincipal``
dataclass rather than the frozen SS-7 ``Principal`` — those two types
serve different auth paths and the PAT path needs ``identity_id`` +
``capabilities`` fields that SS-7 ``Principal`` does not carry. The
integration layer in auth.py is responsible for mapping PatPrincipal
onto whatever request.state shape the rest of the stack expects.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import bcrypt
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from gdx_dispatch.models.platform_extensions import AccessToken

log = logging.getLogger(__name__)


PAT_PREFIXES: tuple[str, ...] = (
    "gdx_pat_live_",
    "gdx_pat_test_",
    "gdx_sk_live_",
    "gdx_sk_test_",
)

_LAST_USED_SAMPLE_SECONDS = 300  # only update last_used_at every 5 minutes

# 0.9-s R1: bound the bcrypt work a single validate_pat call will do.
# `AccessToken.prefix` stores the BUCKET (e.g. `gdx_pat_live_`), not a
# per-token fragment, so a prefix lookup returns every live PAT in that
# bucket. Without a cap, one request runs N bcrypt.checkpw's where N =
# (live PATs for this bucket) — O(10s of seconds) at prod scale. Attacker
# with bucket knowledge can CPU-exhaust the worker by sending many
# random-suffix tokens. Bcrypt defeats brute-force *matching* (32-byte
# secret), but the per-call CPU cost is the DoS vector. Cap defends it.
# D75 tracks the proper fix: add an indexable per-token fragment on
# AccessToken.prefix so validate narrows to ~1 candidate.
_MAX_CANDIDATES_PER_VALIDATE = 100


@dataclass(frozen=True)
class PatPrincipal:
    """Principal-shaped view of a validated PAT.

    Fields mirror what downstream authorization code expects from a
    successful PAT validation: who (identity_id), what tenant, what
    capabilities, and which AccessToken row was matched (for audit /
    revocation lookups).
    """

    identity_id: str
    tenant_id: str
    role: str
    auth_method: str
    pat_id: str
    owner_type: str
    capabilities: list[dict[str, Any]] = field(default_factory=list)


def has_pat_prefix(token: str) -> bool:
    """Return True iff ``token`` starts with a recognised PAT prefix."""
    if not token:
        return False
    return token.startswith(PAT_PREFIXES)


def _extract_prefix(token: str) -> str | None:
    """Return the longest recognised prefix matching ``token``, else None.

    We do NOT use ``rsplit('_', 1)`` — that breaks when the secret body
    itself contains underscores (urlsafe base64 does). Matching against
    the fixed prefix table is both safer and O(1) with a handful of
    known prefixes.
    """
    for prefix in PAT_PREFIXES:
        if token.startswith(prefix):
            return prefix
    return None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_aware(dt: datetime | None) -> datetime | None:
    """SQLite round-trips strip tz; coerce naive datetimes to UTC-aware.

    Production Postgres preserves tz so this is a no-op; tests on SQLite
    hit the assume-UTC branch.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def validate_pat(
    token: str,
    db: Session,
    redis_client: Any | None = None,
) -> PatPrincipal | None:
    """Validate a ``gdx_pat_*`` / ``gdx_sk_*`` bearer token.

    Returns a :class:`PatPrincipal` on success or ``None`` on any
    failure (unknown prefix, no match, expired, revoked, no membership).
    Never raises for control-flow reasons — a ``None`` return is the
    caller's signal to emit a 401. Unexpected errors (DB outage, etc.)
    are logged and re-raised; the caller's error-handling middleware
    will turn those into 500s loudly.
    """
    if not has_pat_prefix(token):
        return None

    prefix = _extract_prefix(token)
    if prefix is None:
        return None

    # Bounded candidate fetch. A prefix bucket can grow unboundedly; cap
    # both the fetch and the bcrypt loop to guarantee a per-request CPU
    # ceiling. Over-the-cap lookups log loudly so ops sees the signal.
    stmt = (
        select(AccessToken)
        .where(
            and_(
                AccessToken.prefix == prefix,
                AccessToken.revoked_at.is_(None),
            )
        )
        .limit(_MAX_CANDIDATES_PER_VALIDATE + 1)
    )
    candidates = list(db.execute(stmt).scalars())
    if not candidates:
        return None
    if len(candidates) > _MAX_CANDIDATES_PER_VALIDATE:
        log.warning(
            "pat_validate_candidate_cap_hit prefix=%s fetched=%d cap=%d — "
            "add indexable per-token fragment to AccessToken.prefix (D75) "
            "to narrow validation lookups.",
            prefix,
            len(candidates),
            _MAX_CANDIDATES_PER_VALIDATE,
        )
        candidates = candidates[:_MAX_CANDIDATES_PER_VALIDATE]

    token_bytes = token.encode("utf-8")
    matched: AccessToken | None = None
    for pat in candidates:
        try:
            if bcrypt.checkpw(token_bytes, pat.secret_hash.encode("utf-8")):
                matched = pat
                break
        except (ValueError, TypeError) as exc:
            # Malformed hash in the DB — log loudly so it's fixable, but
            # don't let a single bad row lock every other user out.
            log.warning(
                "pat_bcrypt_malformed_hash",
                extra={"pat_id": str(pat.id), "err": str(exc)},
            )
            continue

    if matched is None:
        return None

    now = _now()
    expires_at = _ensure_aware(matched.expires_at)
    if expires_at is not None and expires_at < now:
        return None

    last_used_at = _ensure_aware(matched.last_used_at)
    # Sampled last_used_at update — avoids hot-rowing under burst traffic.
    if (
        last_used_at is None
        or (now - last_used_at).total_seconds() > _LAST_USED_SAMPLE_SECONDS
    ):
        matched.last_used_at = now
        try:
            db.commit()
        except Exception:
            # Don't let a metadata-only write failure break auth; roll
            # back and log. Auth still succeeds from the validated row.
            log.warning("pat_last_used_update_failed", exc_info=True)
            db.rollback()

    capabilities = _resolve_capabilities(db, redis_client, matched.capability_set_id)
    identity_id, tenant_id = _resolve_identity_and_tenant(db, redis_client, matched)
    if identity_id is None or tenant_id is None:
        return None

    return PatPrincipal(
        identity_id=str(identity_id),
        tenant_id=str(tenant_id),
        role=("pat" if matched.owner_type == "user" else "service_account"),
        auth_method="pat",
        pat_id=str(matched.id),
        owner_type=matched.owner_type,
        capabilities=capabilities,
    )


def _resolve_capabilities(
    db: Session,
    redis_client: Any | None,
    capability_set_id: UUID,
) -> list[dict[str, Any]]:
    """Resolve capability rows via IdentityRepo; degrade to raw query on import errors."""
    try:
        from gdx_dispatch.core.identity_repo import IdentityRepo

        repo = IdentityRepo(db, redis_client)
        rows = repo.get_capabilities_for_capability_set(capability_set_id)
    except Exception:
        log.warning("pat_capability_resolve_failed; falling back to raw query", exc_info=True)
        from gdx_dispatch.models.platform import Capability

        rows = list(
            db.execute(
                select(Capability).where(
                    and_(
                        Capability.capability_set_id == capability_set_id,
                        Capability.revoked_at.is_(None),
                    )
                )
            ).scalars()
        )

    return [
        {
            "action": c.action,
            "resource_type": c.resource_type,
            "instance_pattern": c.instance_pattern,
            "conditions": c.conditions or {},
        }
        for c in rows
    ]


def _resolve_identity_and_tenant(
    db: Session,
    redis_client: Any | None,
    pat: AccessToken,
) -> tuple[UUID | None, str | None]:
    """Return (identity_id, tenant_id) for the principal owning this PAT."""
    if pat.installation_id is not None:
        from gdx_dispatch.models.platform_extensions import Installation

        install = db.get(Installation, pat.installation_id)
        if install is None:
            return None, None
        return install.installer_identity_id, install.tenant_id

    # User-owned PAT — find the first active membership.
    try:
        from gdx_dispatch.core.identity_repo import IdentityRepo

        repo = IdentityRepo(db, redis_client)
        memberships = repo.get_memberships(pat.owner_id)
    except Exception:
        log.warning("pat_membership_resolve_failed", exc_info=True)
        return None, None

    if not memberships:
        return None, None
    return pat.owner_id, memberships[0].tenant_id

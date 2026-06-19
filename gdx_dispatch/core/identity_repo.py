"""Cached lookup repository for platform identity primitives."""
from __future__ import annotations

import contextlib
import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, select, text, update
from sqlalchemy.orm import Session

from gdx_dispatch.control.models import Tenant
from gdx_dispatch.models.platform import Capability, Identity, IdentityProvider, Membership

CACHE_TTL_SECONDS = 60
_LOG = logging.getLogger(__name__)


class IdentityRepo:
    def __init__(self, db: Session, cache: Any):
        self.db = db
        self.cache = cache

    def _cache_get_json(self, key: str) -> Any | None:
        try:
            cached = self.cache.get(key)
        except Exception:  # silent failure on cache access is acceptable for this utility
            logging.getLogger(__name__).exception("_cache_get_json caught exception")
            return None
        if cached is None:
            return None
        if isinstance(cached, bytes):
            cached = cached.decode("utf-8")
        try:
            return json.loads(cached)
        except Exception:  # silent failure on cache parsing is acceptable for this utility
            logging.getLogger(__name__).exception("_cache_get_json caught exception")
            return None

    def _cache_set_json(self, key: str, payload: Any) -> None:
        with contextlib.suppress(Exception):
            self.cache.setex(key, CACHE_TTL_SECONDS, json.dumps(payload))

    def _cache_delete(self, *keys: str) -> None:
        if not keys:
            return
        with contextlib.suppress(Exception):
            self.cache.delete(*keys)

    def get_identity_by_provider(self, provider_type: str, provider_subject: str) -> Identity | None:
        cache_key = f"identity:by_provider:{provider_type}:{provider_subject}"
        cached = self._cache_get_json(cache_key)
        if isinstance(cached, dict) and cached.get("id"):
            identity = self.db.get(Identity, UUID(cached["id"]))
            if identity is not None:
                return identity

        idp = self.db.execute(
            select(IdentityProvider).where(
                and_(
                    IdentityProvider.provider_type == provider_type,
                    IdentityProvider.provider_subject == provider_subject,
                    IdentityProvider.revoked_at.is_(None),
                )
            )
        ).scalar_one_or_none()

        if idp is None:
            return None

        identity = self.db.get(Identity, idp.identity_id)
        if identity is not None:
            self._cache_set_json(cache_key, {"id": str(identity.id)})
        return identity

    def get_memberships(self, identity_id: UUID) -> list[Membership]:
        cache_key = f"identity:{identity_id}:memberships"
        cached = self._cache_get_json(cache_key)
        if isinstance(cached, list):
            if not cached:
                return []
            hydrated: list[Membership] = []
            for membership_id in cached:
                try:
                    row = self.db.get(Membership, UUID(str(membership_id)))
                except Exception:
                    logging.getLogger(__name__).exception("get_memberships caught exception")
                    row = None
                if row is not None and row.revoked_at is None:
                    hydrated.append(row)
            if hydrated:
                return hydrated

        memberships = list(
            self.db.execute(
                select(Membership).where(
                    and_(
                        Membership.identity_id == identity_id,
                        Membership.revoked_at.is_(None),
                    )
                )
            ).scalars()
        )
        self._cache_set_json(cache_key, [str(m.id) for m in memberships])
        return memberships

    def get_capabilities_for_capability_set(self, capability_set_id: UUID) -> list[Capability]:
        cache_key = f"capset:{capability_set_id}:capabilities"
        cached = self._cache_get_json(cache_key)
        if isinstance(cached, list):
            if not cached:
                return []
            hydrated: list[Capability] = []
            for capability_id in cached:
                try:
                    row = self.db.get(Capability, UUID(str(capability_id)))
                except Exception:
                    logging.getLogger(__name__).exception("get_capabilities_for_capability_set caught exception")
                    row = None
                if row is not None and row.revoked_at is None:
                    hydrated.append(row)
            if hydrated:
                return hydrated

        capabilities = list(
            self.db.execute(
                select(Capability).where(
                    and_(
                        Capability.capability_set_id == capability_set_id,
                        Capability.revoked_at.is_(None),
                    )
                )
            ).scalars()
        )
        self._cache_set_json(cache_key, [str(c.id) for c in capabilities])
        return capabilities

    def link_provider(
        self,
        identity_id: UUID,
        provider_type: str,
        provider_subject: str,
        provider_email: str | None = None,
        email_verified_by_provider: bool = False,
        is_authoritative_for_domain: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> IdentityProvider:
        provider = IdentityProvider(
            identity_id=identity_id,
            provider_type=provider_type,
            provider_subject=provider_subject,
            provider_email=provider_email,
            email_verified_by_provider=email_verified_by_provider,
            is_authoritative_for_domain=is_authoritative_for_domain,
            linked_at=datetime.now(timezone.utc),
            provider_metadata=metadata or {},
        )
        self.db.add(provider)
        self.db.flush()
        self._cache_delete(f"identity:by_provider:{provider_type}:{provider_subject}")
        return provider

    def match_for_login(
        self,
        provider_type: str,
        provider_subject: str,
        provider_email: str | None,
        is_authoritative_for_domain: bool,
    ) -> tuple[Identity | None, str]:
        identity = self.get_identity_by_provider(provider_type, provider_subject)
        if identity is not None:
            return identity, "found"

        if not provider_email:
            return None, "new"

        email_norm = provider_email.strip().lower()

        if is_authoritative_for_domain:
            verified_identity = self.db.execute(
                select(Identity).where(
                    and_(
                        func.lower(Identity.email) == email_norm,
                        Identity.email_verified_at.is_not(None),
                    )
                )
            ).scalar_one_or_none()
            if verified_identity is not None:
                self.db.execute(
                    update(IdentityProvider)
                    .where(
                        and_(
                            IdentityProvider.identity_id == verified_identity.id,
                            IdentityProvider.provider_type == "local",
                            IdentityProvider.email_verified_by_provider.is_(False),
                            IdentityProvider.revoked_at.is_(None),
                        )
                    )
                    .values(revoked_at=datetime.now(timezone.utc))
                )
                self.db.flush()
                self.invalidate_identity(verified_identity.id)
                return verified_identity, "strip_unverified"

        unverified_match = self.db.execute(
            select(Identity).where(func.lower(Identity.email) == email_norm)
        ).scalar_one_or_none()
        if unverified_match is not None:
            return None, "collision"

        return None, "new"

    def invalidate_identity(self, identity_id: UUID) -> None:
        membership_key = f"identity:{identity_id}:memberships"
        aggressive_key = f"identity:{identity_id}:aggressive_until"
        self._cache_delete(membership_key)
        try:
            self.cache.setex(aggressive_key, 60, "1")
            self.cache.publish("identity_invalidations", str(identity_id))
            return
        except Exception as exc:
            _LOG.warning("redis invalidation failed; queuing pending invalidation", extra={"identity_id": str(identity_id)})
            self.db.execute(
                text(
                    """
                    INSERT INTO pending_invalidations (identity_id, reason, enqueued_at)
                    VALUES (:identity_id, :reason, CURRENT_TIMESTAMP)
                    ON CONFLICT (identity_id) DO UPDATE SET
                        reason = EXCLUDED.reason,
                        enqueued_at = CURRENT_TIMESTAMP,
                        replayed_at = NULL
                    """
                ),
                {"identity_id": identity_id, "reason": f"redis_unreachable:{exc}"},
            )
            self.db.flush()

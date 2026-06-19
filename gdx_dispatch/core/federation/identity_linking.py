"""SS-31 slice D — reconcile a federated identity against the local
platform.

Input: a federation provider record (``provider_id``, ``kind``), the
external subject handle the IdP sent, and a normalised profile dict
produced by ``oidc_provider.claims_to_profile`` or
``saml_provider.assertion_to_profile``.

Cases (spec'd in SS-31 prompt):

1. **new external user** — no IdentityProvider row matches
   (provider_type=``fed:{provider_id}``, provider_subject=subject),
   AND no Identity has that email. Create Identity + link. Return
   ``Outcome.CREATED``.
2. **existing linked** — the IdentityProvider row exists. Refresh the
   profile + bump ``last_used_at`` and the federation_link row's
   ``last_login_at``. Return ``Outcome.UPDATED``.
3. **collision on email** — no existing link, but an Identity already
   exists for this email (via any provider, including local). DO NOT
   auto-merge. Emit ``gdx.federation.identity_collision.v1`` and
   raise ``IdentityCollisionError`` so the router returns 409.
4. **orphan link** — the IdentityProvider row exists but the IdP
   response says the subject was removed (caller passes
   ``orphan=True``). Mark revoked_at; do NOT delete the Identity
   itself (an admin must decide). Return ``Outcome.ORPHANED``.

The function is idempotent for case 2 — repeated logins in a row
produce a single UPDATED outcome with a refreshed ``last_used_at``.

Event emission is a pluggable callable (the router wires a real emitter;
unit tests pass a list-appender). Events NEVER swallowed: if the emitter
raises, the reconcile fails — no silent drift.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Optional
from uuid import uuid4

from sqlalchemy.orm import Session

from gdx_dispatch.models.platform import Identity, IdentityProvider

logger = logging.getLogger(__name__)


class Outcome(str, Enum):
    CREATED = "created"
    UPDATED = "updated"
    ORPHANED = "orphaned"


class IdentityCollisionError(Exception):
    """Raised when the federated email matches an existing Identity
    that is NOT linked to this provider+subject. Auto-merge is
    explicitly refused — an admin must run the merge flow."""

    def __init__(
        self,
        *,
        existing_identity_id: str,
        email: str,
        provider_id: str,
        external_subject: str,
    ) -> None:
        super().__init__("identity_collision")
        self.existing_identity_id = existing_identity_id
        self.email = email
        self.provider_id = provider_id
        self.external_subject = external_subject


@dataclass
class ReconcileResult:
    outcome: Outcome
    identity_id: str
    provider_row_id: str
    emitted_events: list[dict[str, Any]] = field(default_factory=list)


EventEmitter = Callable[[str, dict[str, Any]], None]


def _noop_emitter(_event: str, _payload: dict[str, Any]) -> None:
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _provider_type(provider_id: str) -> str:
    """Namespaced provider_type so the existing (provider_type,
    provider_subject) unique constraint on IdentityProvider partitions
    federated subjects by federation_provider.id."""
    return f"fed:{provider_id}"


def reconcile_federated_identity(
    db: Session,
    *,
    provider_id: str,
    external_subject: str,
    profile: dict[str, Any],
    orphan: bool = False,
    emit_event: EventEmitter = _noop_emitter,
) -> ReconcileResult:
    """See module docstring for the case breakdown."""
    if not provider_id or not external_subject:
        raise ValueError("provider_id and external_subject are required")

    ptype = _provider_type(provider_id)
    events: list[dict[str, Any]] = []

    existing_link = (
        db.query(IdentityProvider)
        .filter(
            IdentityProvider.provider_type == ptype,
            IdentityProvider.provider_subject == external_subject,
        )
        .one_or_none()
    )

    # -- Case 4: orphan ----------------------------------------------------
    if orphan:
        if existing_link is None:
            # Already gone — nothing to do. Still emit event for audit.
            payload = {
                "provider_id": provider_id,
                "external_subject": external_subject,
                "at": _utcnow().isoformat(),
                "note": "no_link_existed",
            }
            _emit(emit_event, "gdx_dispatch.federation.identity_linked.v1", payload, events)
            return ReconcileResult(
                outcome=Outcome.ORPHANED,
                identity_id="",
                provider_row_id="",
                emitted_events=events,
            )
        existing_link.revoked_at = _utcnow()
        db.flush()
        payload = {
            "provider_id": provider_id,
            "identity_id": str(existing_link.identity_id),
            "external_subject": external_subject,
            "at": _utcnow().isoformat(),
        }
        _emit(emit_event, "gdx_dispatch.federation.identity_linked.v1", payload, events)
        return ReconcileResult(
            outcome=Outcome.ORPHANED,
            identity_id=str(existing_link.identity_id),
            provider_row_id=str(existing_link.id),
            emitted_events=events,
        )

    # -- Case 2: existing linked ------------------------------------------
    if existing_link is not None:
        if existing_link.revoked_at is not None:
            # Re-linking a previously orphaned subject — clear revoke.
            existing_link.revoked_at = None
        existing_link.last_used_at = _utcnow()
        if profile.get("email"):
            existing_link.provider_email = profile["email"]
        existing_link.email_verified_by_provider = bool(
            profile.get("email_verified", False)
        )
        # stash last login on metadata so it's queryable without a new column
        meta = dict(existing_link.provider_metadata or {})
        meta["last_login_at"] = _utcnow().isoformat()
        meta["last_profile"] = {
            k: v for k, v in profile.items() if k != "external_subject"
        }
        existing_link.provider_metadata = meta
        db.flush()
        payload = {
            "provider_id": provider_id,
            "identity_id": str(existing_link.identity_id),
            "external_subject": external_subject,
            "outcome": "updated",
            "at": _utcnow().isoformat(),
        }
        _emit(emit_event, "gdx_dispatch.federation.identity_linked.v1", payload, events)
        return ReconcileResult(
            outcome=Outcome.UPDATED,
            identity_id=str(existing_link.identity_id),
            provider_row_id=str(existing_link.id),
            emitted_events=events,
        )

    # -- Case 3: collision on email ---------------------------------------
    email = (profile.get("email") or "").strip().lower()
    if email:
        collider = (
            db.query(Identity)
            .filter(Identity.email == email, Identity.deleted_at.is_(None))
            .one_or_none()
        )
        if collider is not None:
            payload = {
                "provider_id": provider_id,
                "external_subject": external_subject,
                "email": email,
                "existing_identity_id": str(collider.id),
                "at": _utcnow().isoformat(),
                "remediation": (
                    "admin must explicitly link existing identity to "
                    "federation provider via the admin-merge flow"
                ),
            }
            _emit(
                emit_event,
                "gdx_dispatch.federation.identity_collision.v1",
                payload,
                events,
            )
            raise IdentityCollisionError(
                existing_identity_id=str(collider.id),
                email=email,
                provider_id=provider_id,
                external_subject=external_subject,
            )

    # -- Case 1: new external user ----------------------------------------
    new_identity = Identity(
        id=uuid4(),
        email=email or f"{external_subject}@federated.local",
        display_name=profile.get("name") or profile.get("preferred_username"),
        status="active",
        email_verified_at=_utcnow() if profile.get("email_verified") else None,
    )
    db.add(new_identity)
    db.flush()

    link = IdentityProvider(
        id=uuid4(),
        identity_id=new_identity.id,
        provider_type=ptype,
        provider_subject=external_subject,
        provider_email=profile.get("email"),
        email_verified_by_provider=bool(profile.get("email_verified", False)),
        is_authoritative_for_domain=False,
        last_used_at=_utcnow(),
        provider_metadata={
            "federation_provider_id": provider_id,
            "last_profile": {
                k: v for k, v in profile.items() if k != "external_subject"
            },
            "last_login_at": _utcnow().isoformat(),
        },
    )
    db.add(link)
    db.flush()

    payload = {
        "provider_id": provider_id,
        "identity_id": str(new_identity.id),
        "external_subject": external_subject,
        "outcome": "created",
        "at": _utcnow().isoformat(),
    }
    _emit(emit_event, "gdx_dispatch.federation.identity_linked.v1", payload, events)
    return ReconcileResult(
        outcome=Outcome.CREATED,
        identity_id=str(new_identity.id),
        provider_row_id=str(link.id),
        emitted_events=events,
    )


def _emit(
    emit: EventEmitter,
    name: str,
    payload: dict[str, Any],
    events: list[dict[str, Any]],
) -> None:
    emit(name, payload)
    events.append({"event": name, "payload": payload})

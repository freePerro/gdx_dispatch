"""
gdx_dispatch/models/platform_ss21_additions.py — SS-21 platform column/table stubs.

ISOLATION RULE (same as platform_ss22_additions.py): nothing in this file
registers against SQLAlchemy's metadata via a real mapper. The declarative
Base here is DEDICATED and separate from the primary platform Base — the
integration step is a single conscious merge, not a surprise diff.

Tables declared here:
  * authorization_codes       — short-lived auth codes (60s TTL), PKCE state
  * oauth_tokens              — access + refresh pairs
  * admin_consent_grants      — Microsoft-Entra-style tenant-wide grants
  * webhook_subscriptions     — dev-app→customer-tenant webhook endpoints
  * webhook_deliveries        — per-attempt delivery log
  * webhook_signing_keys      — dual-active signing keys (envelope-encrypted)

TODO (when SS-21 integrates with main platform):
  * Re-parent these onto `gdx_dispatch.models.platform.Base` (same as SS-20 plan).
  * Move the column declarations to their real home files (or add a central
    "third_party_oauth.py" model module).
  * Delete this file; replace with a re-export shim for one release cycle.

Until integration: the SS-21 routers use the in-memory stores defined in
gdx_dispatch.core.oauth2_grants / gdx_dispatch.routers.auth.oauth2 / gdx_dispatch.routers.admin_consent /
gdx_dispatch.core.webhook_delivery_ss21. Those stores are Redis-/DB-shaped so the
swap is a single-file change per store.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import declarative_base, relationship

SS21Base = declarative_base()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# authorization_codes
# ---------------------------------------------------------------------------


class AuthorizationCode(SS21Base):
    """Short-lived OAuth2 authorization code (RFC 6749 §4.1, TTL=60s).

    Persisted (rather than pure-memory) so a multi-worker deployment can
    share state and so forensic audit can review recently-used codes.
    """

    __tablename__ = "ss21_authorization_codes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(128), nullable=False, unique=True, index=True)
    client_id = Column(String(64), nullable=False, index=True)
    redirect_uri = Column(String(1024), nullable=False)
    scope = Column(Text, nullable=False, default="")
    tenant_id = Column(String(64), nullable=True, index=True)
    subject_id = Column(String(64), nullable=True, index=True)
    # PKCE — RFC 7636
    code_challenge = Column(String(255), nullable=True)
    code_challenge_method = Column(String(16), nullable=True)  # S256 only
    # Lifecycle
    expires_at = Column(DateTime(timezone=True), nullable=False)
    consumed_at = Column(DateTime(timezone=True), nullable=True)
    admin_consent = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


# ---------------------------------------------------------------------------
# oauth_tokens
# ---------------------------------------------------------------------------


class OAuthToken(SS21Base):
    __tablename__ = "ss21_oauth_tokens"

    id = Column(Integer, primary_key=True, autoincrement=True)
    access_token = Column(String(128), nullable=False, unique=True, index=True)
    refresh_token = Column(String(128), nullable=False, unique=True, index=True)
    client_id = Column(String(64), nullable=False, index=True)
    scope = Column(Text, nullable=False, default="")
    tenant_id = Column(String(64), nullable=True, index=True)
    subject_id = Column(String(64), nullable=True, index=True)
    issued_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    revoked_at = Column(DateTime(timezone=True), nullable=True)


# ---------------------------------------------------------------------------
# admin_consent_grants  (Microsoft-Entra-style, tenant-wide)
# ---------------------------------------------------------------------------


class AdminConsentGrant(SS21Base):
    __tablename__ = "ss21_admin_consent_grants"
    __table_args__ = (
        UniqueConstraint("tenant_id", "client_id", name="uq_ss21_admin_grant_pair"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(String(64), nullable=False, index=True)
    client_id = Column(String(64), nullable=False, index=True)
    scope = Column(Text, nullable=False, default="")
    granted_by = Column(String(64), nullable=False)
    granted_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    status = Column(String(16), nullable=False, default="active")  # active|revoked
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    revoked_by = Column(String(64), nullable=True)


# ---------------------------------------------------------------------------
# webhook_subscriptions + webhook_deliveries + webhook_signing_keys
# ---------------------------------------------------------------------------


class WebhookSubscription(SS21Base):
    __tablename__ = "ss21_webhook_subscriptions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    client_id = Column(String(64), nullable=False, index=True)
    tenant_id = Column(String(64), nullable=False, index=True)
    url = Column(String(1024), nullable=False)
    events = Column(Text, nullable=False, default="")  # space-separated
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    disabled_at = Column(DateTime(timezone=True), nullable=True)

    signing_keys = relationship("WebhookSigningKey", back_populates="subscription")
    deliveries = relationship("WebhookDelivery", back_populates="subscription")


class WebhookSigningKey(SS21Base):
    """Envelope-encrypted webhook signing secret (v3 patch P33).

    Raw secret shown ONCE at creation; stored as ciphertext keyed by a KEK
    held in .env (GDX_WEBHOOK_KEK). Dual-active: during a 7-day rotation
    window, both old and new keys sign outgoing webhooks.
    """

    __tablename__ = "ss21_webhook_signing_keys"

    id = Column(Integer, primary_key=True, autoincrement=True)
    subscription_id = Column(
        Integer,
        ForeignKey("ss21_webhook_subscriptions.id", ondelete="CASCADE"),
        nullable=False,
    )
    kid = Column(String(64), nullable=False, index=True)
    ciphertext = Column(Text, nullable=False)  # envelope-encrypted raw bytes
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    rotated_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)

    subscription = relationship("WebhookSubscription", back_populates="signing_keys")


class WebhookDelivery(SS21Base):
    __tablename__ = "ss21_webhook_deliveries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    subscription_id = Column(
        Integer,
        ForeignKey("ss21_webhook_subscriptions.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_id = Column(String(64), nullable=False, index=True)
    attempt_number = Column(Integer, nullable=False, default=1)
    status_code = Column(Integer, nullable=True)
    error_type = Column(String(128), nullable=True)
    error_msg = Column(Text, nullable=True)
    attempted_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    succeeded = Column(Boolean, nullable=False, default=False)

    subscription = relationship("WebhookSubscription", back_populates="deliveries")


# Column catalog — useful for the integration merge; this list mirrors the
# columns declared above so reviewers can diff without scrolling.
SS21_COLUMN_CATALOG: list[tuple[str, list[str]]] = [
    (
        "ss21_authorization_codes",
        [
            "id", "code", "client_id", "redirect_uri", "scope",
            "tenant_id", "subject_id",
            "code_challenge", "code_challenge_method",
            "expires_at", "consumed_at", "admin_consent", "created_at",
        ],
    ),
    (
        "ss21_oauth_tokens",
        [
            "id", "access_token", "refresh_token", "client_id", "scope",
            "tenant_id", "subject_id",
            "issued_at", "expires_at", "revoked_at",
        ],
    ),
    (
        "ss21_admin_consent_grants",
        [
            "id", "tenant_id", "client_id", "scope",
            "granted_by", "granted_at", "status", "revoked_at", "revoked_by",
        ],
    ),
    (
        "ss21_webhook_subscriptions",
        ["id", "client_id", "tenant_id", "url", "events", "created_at", "disabled_at"],
    ),
    (
        "ss21_webhook_signing_keys",
        ["id", "subscription_id", "kid", "ciphertext", "created_at", "rotated_at", "revoked_at"],
    ),
    (
        "ss21_webhook_deliveries",
        [
            "id", "subscription_id", "event_id", "attempt_number",
            "status_code", "error_type", "error_msg",
            "attempted_at", "succeeded",
        ],
    ),
]


__all__ = [
    "AdminConsentGrant",
    "AuthorizationCode",
    "OAuthToken",
    "SS21Base",
    "SS21_COLUMN_CATALOG",
    "WebhookDelivery",
    "WebhookSigningKey",
    "WebhookSubscription",
]

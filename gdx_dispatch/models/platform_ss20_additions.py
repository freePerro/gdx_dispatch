"""
gdx_dispatch/models/platform_ss20_additions.py — SS-20 developer portal model stubs.

NOTE: INTEGRATION_TODO — these definitions live in their own declarative
Base (`DevPortalBase`) and are *not yet* mounted on the main platform
Base. When SS-20 integrates with SS-13's dev portal foundation, merge or
re-parent these onto the canonical Base in `gdx_dispatch/models/platform.py`.

Until then, the dev portal router creates these tables in its own engine
(sqlite for tests; dedicated schema in prod) so it can ship independently
without mutating the primary schema graph.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import declarative_base, relationship

DevPortalBase = declarative_base()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _verify_expiry() -> datetime:
    return _utcnow() + timedelta(hours=24)


class DeveloperAccount(DevPortalBase):
    """A self-serve developer account (not a tenant user)."""

    __tablename__ = "developer_portal_accounts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String(255), nullable=False, unique=True, index=True)
    password_hash = Column(String(255), nullable=False)
    tier = Column(String(32), nullable=False, default="sandbox")
    email_verified_at = Column(DateTime(timezone=True), nullable=True)
    tos_accepted_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    # 0.9-s A2: primaryjoin filter hides soft-deleted apps from the
    # relationship. Direct queries that explicitly want deleted rows must
    # bypass the relationship (session.query(DeveloperApp).filter(...)).
    apps = relationship(
        "DeveloperApp",
        back_populates="account",
        cascade="all, delete-orphan",
        primaryjoin=(
            "and_(DeveloperApp.account_id == DeveloperAccount.id, "
            "DeveloperApp.deleted_at.is_(None))"
        ),
    )
    verifications = relationship(
        "EmailVerification", back_populates="account", cascade="all, delete-orphan"
    )


class EmailVerification(DevPortalBase):
    """Single-use email verification token; expires after 24h."""

    __tablename__ = "developer_portal_email_verifications"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(
        Integer, ForeignKey("developer_portal_accounts.id", ondelete="CASCADE"), nullable=False
    )
    token = Column(String(128), nullable=False, unique=True, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, default=_verify_expiry)
    consumed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    account = relationship("DeveloperAccount", back_populates="verifications")


class DeveloperApp(DevPortalBase):
    """A third-party OAuth app registered by a developer."""

    __tablename__ = "developer_portal_apps"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(
        Integer, ForeignKey("developer_portal_accounts.id", ondelete="CASCADE"), nullable=False
    )
    name = Column(String(255), nullable=False)
    client_id = Column(String(64), nullable=False, unique=True, index=True)
    redirect_uri = Column(String(1024), nullable=False)
    scopes = Column(Text, nullable=False, default="")  # space-delimited
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    account = relationship("DeveloperAccount", back_populates="apps")
    # 0.9-s A3: primaryjoin filter hides revoked secrets. Secret-rotation
    # code paths that need to see revoked rows (for audit) must query
    # DeveloperAppSecret directly.
    secrets = relationship(
        "DeveloperAppSecret",
        back_populates="app",
        cascade="all, delete-orphan",
        primaryjoin=(
            "and_(DeveloperAppSecret.app_id == DeveloperApp.id, "
            "DeveloperAppSecret.revoked_at.is_(None))"
        ),
    )


class DeveloperAppSecret(DevPortalBase):
    """Hashed client secret. Plaintext returned once at creation/rotation."""

    __tablename__ = "developer_portal_app_secrets"
    __table_args__ = (UniqueConstraint("app_id", "secret_prefix", name="uq_app_secret_prefix"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    app_id = Column(Integer, ForeignKey("developer_portal_apps.id", ondelete="CASCADE"), nullable=False)
    secret_prefix = Column(String(16), nullable=False)  # first 8 chars for identification
    secret_hash = Column(String(255), nullable=False)
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    revoked_at = Column(DateTime(timezone=True), nullable=True)

    app = relationship("DeveloperApp", back_populates="secrets")


class OAuthDynamicClient(DevPortalBase):
    """Sprint mcp-streamable-http S5 — RFC 7591 Dynamic Client Registration.

    claude.ai's MCP connector signup POSTs to ``/oauth/register`` without
    any prior developer-portal account; the server mints a fresh
    ``(client_id, client_secret)`` pair and stores the metadata here.

    Tenant-scoped: each tenant host's ``/oauth/register`` writes into a
    row carrying the resolved ``tenant_id`` (UUID stringification to keep
    the column SQLite-portable for tests; production runs on Postgres).
    The same lookup at ``/oauth/authorize`` enforces that a client_id
    minted under tenant A cannot be reused under tenant B's host.

    Stored separately from ``DeveloperApp`` (which is account-bound) so:
      * DCR clients have no developer-portal account FK.
      * The two tables can evolve independently (RFC 7591 client metadata
        vs. SS-20 portal-curated metadata).
    """

    __tablename__ = "oauth_dcr_clients"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(String(36), nullable=False, index=True)
    client_id = Column(String(64), nullable=False, unique=True, index=True)
    # Public clients (PKCE-only) carry no secret — both columns nullable.
    client_secret_hash = Column(String(255), nullable=True)
    secret_prefix = Column(String(16), nullable=True)
    client_name = Column(String(255), nullable=True)
    # JSON-encoded list of strings; sa.JSON adapts cleanly to sqlite + pg.
    redirect_uris = Column(JSON, nullable=False, default=list)
    grant_types = Column(JSON, nullable=False, default=list)
    response_types = Column(JSON, nullable=False, default=list)
    token_endpoint_auth_method = Column(
        String(64), nullable=False, default="client_secret_basic",
    )
    scope = Column(Text, nullable=False, default="")
    # RFC 7591 epoch-second timestamps.
    client_id_issued_at = Column(Integer, nullable=False)
    client_secret_expires_at = Column(Integer, nullable=False, default=0)  # 0 = never
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    deleted_at = Column(DateTime(timezone=True), nullable=True)


__all__ = [
    "DevPortalBase",
    "DeveloperAccount",
    "DeveloperApp",
    "DeveloperAppSecret",
    "EmailVerification",
    "OAuthDynamicClient",
]

"""SS-31 federation tables (provider + link + trust_bundle_cache).

Revision ID: ss31_federation
Revises: TODO
Create Date: 2026-04-19

TODO:
  * Set ``down_revision`` to the actual live alembic head once the SS-21
    + SS-22 + SS-23 + ... chain is integrated. Proposed order once the
    upstream SS migrations are re-chained:
        ... → ss30_cutover → ss31_federation
  * Rename this file to the next sequential number (e.g.
    ``NNN_ss31_federation.py``) at that time.
  * Promote ``ss31_federation_provider.client_secret_encrypted`` to a
    real EncryptedString column at the integration-merge step (see
    gdx_dispatch/models/platform_ss31_additions.py and gdx_dispatch/routers/federation.py
    set_secret_encoder()).
  * Add FK constraints back to the live ``identities.id`` once that
    table's name is finalised post-SS-30 cutover.
  * Remove this TODO block.

Creates:
  * ss31_federation_provider
  * ss31_federation_link
  * ss31_federation_trust_bundle_cache

All tables prefixed ``ss31_`` so the integration merge (which may
rename them to ``federation_*``) is unambiguous in the diff.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "ss31_federation"
down_revision = "ss30_cutover"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # -----------------------------------------------------------------
    # ss31_federation_provider
    # -----------------------------------------------------------------
    op.create_table(
        "ss31_federation_provider",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("metadata_url", sa.String(length=1024), nullable=False),
        sa.Column("client_id", sa.String(length=255), nullable=True),
        sa.Column("client_secret_encrypted", sa.Text(), nullable=True),
        sa.Column("trust_bundle_ref", sa.String(length=255), nullable=True),
        sa.Column("redirect_uri", sa.String(length=1024), nullable=True),
        sa.Column("sp_entity_id", sa.String(length=255), nullable=True),
        sa.Column("acs_url", sa.String(length=1024), nullable=True),
        sa.Column(
            "scope",
            sa.String(length=255),
            nullable=True,
            server_default=sa.text("'openid email profile'"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind in ('oidc','saml')", name="ck_ss31_fed_provider_kind"
        ),
    )
    op.create_index(
        "ix_ss31_fed_provider_tenant",
        "ss31_federation_provider",
        ["tenant_id"],
    )

    # -----------------------------------------------------------------
    # ss31_federation_link
    # -----------------------------------------------------------------
    op.create_table(
        "ss31_federation_link",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("identity_id", sa.String(length=36), nullable=False),
        sa.Column("provider_id", sa.String(length=36), nullable=False),
        sa.Column("external_subject", sa.String(length=255), nullable=False),
        sa.Column(
            "linked_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "provider_id", "external_subject", name="uq_ss31_fed_link_subject"
        ),
    )
    op.create_index(
        "ix_ss31_fed_link_identity", "ss31_federation_link", ["identity_id"]
    )
    op.create_index(
        "ix_ss31_fed_link_provider", "ss31_federation_link", ["provider_id"]
    )
    op.create_index(
        "ix_ss31_fed_link_subject",
        "ss31_federation_link",
        ["external_subject"],
    )

    # -----------------------------------------------------------------
    # ss31_federation_trust_bundle_cache
    # -----------------------------------------------------------------
    op.create_table(
        "ss31_federation_trust_bundle_cache",
        sa.Column("provider_id", sa.String(length=36), primary_key=True),
        sa.Column("bundle_json", sa.Text(), nullable=False),
        sa.Column(
            "fetched_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "ttl_seconds",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("3600"),
        ),
        sa.Column("last_refresh_error", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("ss31_federation_trust_bundle_cache")
    op.drop_index(
        "ix_ss31_fed_link_subject", table_name="ss31_federation_link"
    )
    op.drop_index(
        "ix_ss31_fed_link_provider", table_name="ss31_federation_link"
    )
    op.drop_index(
        "ix_ss31_fed_link_identity", table_name="ss31_federation_link"
    )
    op.drop_table("ss31_federation_link")
    op.drop_index(
        "ix_ss31_fed_provider_tenant", table_name="ss31_federation_provider"
    )
    op.drop_table("ss31_federation_provider")

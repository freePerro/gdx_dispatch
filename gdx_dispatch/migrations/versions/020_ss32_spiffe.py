"""SS-32 — spiffe_workload_registration + spiffe_trust_bundle_cache.

INTEGRATION_TODO: chained on placeholder ``down_revision = "ss31_federation"``.
The supervisor retargets this to the tip of the main chain at
end-of-sprint. Revision id uses the sprint slug so grep-find works.

Creates:
    - spiffe_workload_registration  — super-admin-registered SPIFFE workloads
    - spiffe_trust_bundle_cache     — persisted per-trust-domain bundle

All tables are NEW and strictly additive — SS-32 does not touch any
existing identity/auth table.

Revision ID: ss32_spiffe
Down revision: INTEGRATION_TODO
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "ss32_spiffe"
down_revision = "ss31_federation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "spiffe_workload_registration",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("spiffe_id_glob", sa.String(length=512), nullable=False),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column(
            "tenant_scope",
            sa.String(length=32),
            nullable=False,
            server_default="per-tenant",
        ),
        sa.Column("spiffe_metadata", sa.JSON(), nullable=True),
        sa.Column(
            "registered_by_identity_id", sa.String(length=64), nullable=False
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "enabled", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_spiffe_workload_glob",
        "spiffe_workload_registration",
        ["spiffe_id_glob"],
        unique=True,
    )
    op.create_index(
        "ix_spiffe_workload_scope",
        "spiffe_workload_registration",
        ["tenant_scope"],
    )

    op.create_table(
        "spiffe_trust_bundle_cache",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("trust_domain", sa.String(length=255), nullable=False),
        sa.Column("bundle_json", sa.JSON(), nullable=False),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "ttl_seconds",
            sa.String(length=16),
            nullable=False,
            server_default="300",
        ),
        sa.Column("source_endpoint", sa.String(length=512), nullable=True),
        sa.Column("last_refresh_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_spiffe_bundle_td",
        "spiffe_trust_bundle_cache",
        ["trust_domain"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_spiffe_bundle_td", table_name="spiffe_trust_bundle_cache"
    )
    op.drop_table("spiffe_trust_bundle_cache")
    op.drop_index(
        "ix_spiffe_workload_scope", table_name="spiffe_workload_registration"
    )
    op.drop_index(
        "ix_spiffe_workload_glob", table_name="spiffe_workload_registration"
    )
    op.drop_table("spiffe_workload_registration")

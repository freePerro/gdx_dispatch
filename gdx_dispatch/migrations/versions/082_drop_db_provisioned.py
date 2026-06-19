"""Drop tenants.db_provisioned (single-tenant collapse).

Follow-on to 081. db_provisioned flagged whether a tenant's per-tenant database
had been paved — a multi-tenant provisioning concept. After the per-tenant
raw-SQL iterators were collapsed (the iterator WHERE clauses, public_router's
SELECT, and tenant.py's request-state dict no longer reference it), the column
has no surviving reader and can be dropped.

KEPT still: db_url_enc (task iterators read it; Phase D removes it later),
subscription_status, stripe_connect_account_id.

Verified up + down against a throwaway Postgres 15 in the devcontainer.

Revision ID: 082_drop_db_provisioned
Revises: 081_drop_saas_tenant_columns
Create Date: 2026-06-09
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "082_drop_db_provisioned"
down_revision = "081_drop_saas_tenant_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("tenants", "db_provisioned")


def downgrade() -> None:
    # Restore the original 001_baseline definition.
    op.add_column(
        "tenants",
        sa.Column("db_provisioned", sa.Boolean(), nullable=False, server_default="false"),
    )

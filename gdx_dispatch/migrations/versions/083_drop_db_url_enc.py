"""Drop tenants.db_url_enc (single-tenant collapse).

Phase D final column drop. db_url_enc stored a Fernet-encrypted (or
plaintext-dev) connection URL to each tenant's per-tenant database.
After the single-tenant collapse (Phase A–D) every "tenant DB" is the
same app database; all callers that read db_url_enc were updated to use
SessionLocal() directly before this migration was written.

No surviving live reader: confirmed by grepping the non-tools/ source
tree for db_url_enc before landing this commit.

Revision ID: 083_drop_db_url_enc
Revises: 082_drop_db_provisioned
Create Date: 2026-06-10
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "083_drop_db_url_enc"
down_revision = "082_drop_db_provisioned"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("tenants", "db_url_enc")


def downgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("db_url_enc", sa.String(), nullable=True),
    )

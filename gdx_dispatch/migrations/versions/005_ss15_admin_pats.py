"""SS-15 admin-PAT issuance columns on access_tokens.

Revision ID: ss15_admin_pats
Revises: INTEGRATION_TODO
Create Date: 2026-04-19

INTEGRATED 2026-04-20 (Sprint 0.9-a merged columns into platform_extensions;
Sprint 0.9-f dropped the in-memory ``_PAT_STATE`` shim + hasattr-guards in
``gdx_dispatch/routers/admin_pats.py``). Note: broader alembic chain reordering is
Sprint 0.9-b's scope — see ``plans/sprint-0.9-integration.md`` §Phase-1.

Adds to ``access_tokens``:
    - ``status`` VARCHAR(32) NOT NULL DEFAULT 'active'
      Lifecycle states: 'active' | 'pending_approval' | 'revoked'
    - ``metadata_json`` JSON NULL
      Audit metadata: admin-issuer, target identity, approver, approved_at
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# Revision identifiers — integrated 2026-04-20.
revision = "ss15_admin_pats"
down_revision = "004d_ss3_supporting"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # One add_column per batch — adding two columns in one batch triggers
    # SQLAlchemy's topological sort on the partial-reordering pass and
    # raises CircularDependencyError when the two new columns are equally
    # unconstrained. Serializing the two batches sidesteps that bug and
    # keeps the migration working on both SQLite (batch_alter_table
    # copy-rebuild) and PostgreSQL (plain ADD COLUMN).
    with op.batch_alter_table("access_tokens") as batch:
        batch.add_column(
            sa.Column(
                "status",
                sa.String(length=32),
                nullable=False,
                server_default=sa.text("'active'"),
            )
        )
    with op.batch_alter_table("access_tokens") as batch:
        batch.add_column(
            sa.Column(
                "metadata_json",
                sa.JSON(),
                nullable=True,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("access_tokens") as batch:
        batch.drop_column("metadata_json")
    with op.batch_alter_table("access_tokens") as batch:
        batch.drop_column("status")

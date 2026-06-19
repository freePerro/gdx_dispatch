"""Add terms_accepted_at + terms_version to tenants for legal compliance.

Captures the timestamp and version of the Terms of Service the customer
agreed to at signup. Required by Stripe T&Cs for accepting recurring
payments and by US consumer-protection law in most states.

terms_version is a semver-like string stored at agreement time so we
can prove WHICH version of the terms a customer agreed to even after
we update the live document. Default is NULL for the existing 13 prod
tenants (signed up before the gate existed) — they grandfather in.
Existing customers will be re-prompted at next major terms update via
in-app banner (separate work).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "074_tenants_terms_accepted"
down_revision = "073_tenants_seat_count"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("terms_accepted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "tenants",
        sa.Column("terms_version", sa.String(length=20), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tenants", "terms_version")
    op.drop_column("tenants", "terms_accepted_at")

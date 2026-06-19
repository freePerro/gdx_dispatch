"""Drop dead SaaS columns from tenants (single-tenant collapse).

Phase C slimmed the Tenant ORM model (commit 5bc62c8); this reconciles the
physical control-plane schema by dropping the 7 columns that have NO surviving
reader after the multi-tenant SaaS strip:

- trial_ends_at, welcome_email_sent_at  — SaaS trial lifecycle
- terms_accepted_at, terms_version       — SaaS signup terms
- seat_count                             — SaaS billing seats
- stripe_customer_id, stripe_subscription_id — SaaS subscription billing
  (tenant-pays-us; the surviving stripe_subscription_id reads are on the
   customer_plan_enrollment table, a different relation)

Deliberately KEPT (still live, mapped on the model):
- stripe_connect_account_id — the merchant's own Connect account for taking
  CUSTOMER card payments (stripe_connect.py / payments.py)
- subscription_status       — read by rate_limiter + task SQL (restored 6cf9ef3)
- db_provisioned            — still read by per-tenant iterator WHERE clauses;
  drops in a later migration once those collapse to SessionLocal()
- db_url_enc                — kept per the collapse plan

Verified up + down against a throwaway Postgres 15 in the devcontainer.

Revision ID: 081_drop_saas_tenant_columns
Revises: 080_api_keys_table
Create Date: 2026-06-09
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "081_drop_saas_tenant_columns"
down_revision = "080_api_keys_table"
branch_labels = None
depends_on = None


_DROP = [
    "trial_ends_at",
    "welcome_email_sent_at",
    "terms_accepted_at",
    "terms_version",
    "seat_count",
    "stripe_customer_id",
    "stripe_subscription_id",
]


def upgrade() -> None:
    for col in _DROP:
        op.drop_column("tenants", col)


def downgrade() -> None:
    # Restore with the exact pre-drop definitions (see migrations 070-074 and
    # the original Tenant model) so the revision is fully reversible.
    op.add_column("tenants", sa.Column("trial_ends_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("tenants", sa.Column("welcome_email_sent_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("tenants", sa.Column("terms_accepted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("tenants", sa.Column("terms_version", sa.String(20), nullable=True))
    op.add_column(
        "tenants",
        sa.Column("seat_count", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column("tenants", sa.Column("stripe_customer_id", sa.String(100), nullable=True))
    op.add_column("tenants", sa.Column("stripe_subscription_id", sa.String(100), nullable=True))

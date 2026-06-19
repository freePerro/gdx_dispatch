"""create subscription_plans, usage_meters, and signup_bypass_codes tables

Revision ID: 051_cc_subscription_plans_meters
Revises: 050_cc_extra_grants_and_audit
Create Date: 2026-05-01 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "051_cc_subscription_plans_meters"
down_revision = "050_cc_extra_grants_and_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Create subscription_plans
    op.create_table(
        "subscription_plans",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("stripe_product_id", sa.Text(), nullable=True),
        sa.Column("stripe_price_id_monthly", sa.Text(), nullable=True),
        sa.Column("stripe_price_id_annual", sa.Text(), nullable=True),
        sa.Column("base_monthly_cents", sa.Integer(), nullable=False),
        sa.Column("included_seats", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("included_ai_tokens", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("included_storage_gb", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("features_json", postgresql.JSONB(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("code", name="uq_subscription_plans_code"),
        sa.CheckConstraint("base_monthly_cents >= 0", name="ck_subscription_plans_base_monthly_cents_positive"),
        sa.CheckConstraint("included_seats >= 0", name="ck_subscription_plans_included_seats_positive"),
        sa.CheckConstraint("included_storage_gb >= 0", name="ck_subscription_plans_included_storage_gb_positive"),
    )

    # Seed subscription_plans
    op.execute(
        """
        INSERT INTO subscription_plans (code, display_name, base_monthly_cents, included_seats, included_storage_gb)
        VALUES
            ('starter', 'Starter', 5000, 1, 5),
            ('pro', 'Pro', 10000, 2, 10),
            ('business', 'Business', 15000, 3, 15),
            ('enterprise', 'Enterprise', 30000, 4, 20)
        """
    )

    # 2. Create usage_meters
    op.create_table(
        "usage_meters",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column(
            "aggregation",
            sa.Text(),
            nullable=False,
        ),
        sa.Column("stripe_meter_id", sa.Text(), nullable=True),
        sa.Column("default_overage_cents_per_unit", sa.Integer(), nullable=False),
        sa.Column(
            "included_in_plans_json",
            postgresql.JSONB(),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("code", name="uq_usage_meters_code"),
        sa.CheckConstraint(
            "aggregation IN ('sum', 'last_during_period', 'max')",
            name="ck_usage_meters_aggregation_valid"
        ),
        sa.CheckConstraint(
            "default_overage_cents_per_unit >= 0",
            name="ck_usage_meters_overage_positive"
        ),
    )

    # Seed usage_meters
    op.execute(
        """
        INSERT INTO usage_meters (code, display_name, aggregation, default_overage_cents_per_unit)
        VALUES
            ('storage_gb', 'Storage (GB-month)', 'last_during_period', 3),
            ('active_seats', 'Active seats', 'last_during_period', 5000)
        """
    )

    # 3. Create signup_bypass_codes
    op.create_table(
        "signup_bypass_codes",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("max_uses", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("used_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_by_staff_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("cc_staff_users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("code", name="uq_signup_bypass_codes_code"),
        sa.CheckConstraint("max_uses >= 1", name="ck_signup_bypass_codes_max_uses_min"),
        sa.CheckConstraint("used_count >= 0", name="ck_signup_bypass_codes_used_count_min"),
        sa.CheckConstraint(
            "used_count <= max_uses",
            name="ck_signup_bypass_codes_used_count_max"
        ),
    )

    # 4. Partial index for signup_bypass_codes
    op.create_index(
        "ix_signup_bypass_codes_active",
        "signup_bypass_codes",
        ["code"],
        unique=False,
        postgresql_where=sa.text("used_count < max_uses")
    )


def downgrade() -> None:
    # Drop in reverse order of creation
    op.drop_index("ix_signup_bypass_codes_active", table_name="signup_bypass_codes")
    op.drop_table("signup_bypass_codes")
    op.drop_table("usage_meters")
    op.drop_table("subscription_plans")


# Verification Manifest
# 1. Table existence: subscription_plans, usage_meters, signup_bypass_codes
# 2. Plan seed: 4 rows (starter, pro, business, enterprise)
# 3. Meter seed: 2 rows (storage_gb, active_seats)
# 4. Constraints: CHECKs for positive cents/seats/storage/overage/uses
# 5. Index: Partial index on signup_bypass_codes(code) where used_count < max_uses

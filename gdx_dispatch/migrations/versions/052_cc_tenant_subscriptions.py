"""Create tenant_subscriptions and tenant_usage_meter_links with RLS and dual-GUC policies.

This migration introduces the core billing tables for the control plane, linking tenants to
subscription plans and Stripe subscription items. It implements Row Level Security (RLS)
to ensure tenant isolation while allowing CC staff access via the cc_staff_id GUC.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "052_cc_tenant_subscriptions"
down_revision = "051_cc_subscription_plans_meters"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create tenant_subscriptions table
    op.create_table(
        "tenant_subscriptions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("plan_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("subscription_plans.id"), nullable=False),
        sa.Column("stripe_customer_id", sa.Text(), nullable=True),
        sa.Column("stripe_subscription_id", sa.Text(), nullable=True),
        sa.Column("custom_price_id", sa.Text(), nullable=True),
        sa.Column(
            "billing_cycle",
            sa.Text(),
            server_default=sa.text("'monthly'"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Text(),
            server_default=sa.text("'trialing'"),
            nullable=False,
        ),
        sa.Column("trial_ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delinquent", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("days_overdue", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("churn_risk_score", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("tenant_id", name="uq_tenant_subscriptions_tenant_id"),
        sa.CheckConstraint("billing_cycle IN ('monthly', 'annual')", name="ck_tenant_subscriptions_billing_cycle"),
        sa.CheckConstraint(
            "status IN ('trialing', 'active', 'past_due', 'canceled', 'unpaid', 'incomplete', 'incomplete_expired')",
            name="ck_tenant_subscriptions_status",
        ),
        sa.CheckConstraint("days_overdue >= 0", name="ck_tenant_subscriptions_days_overdue"),
        sa.CheckConstraint(
            "churn_risk_score >= 0 AND churn_risk_score <= 100",
            name="ck_tenant_subscriptions_churn_risk_score",
        ),
    )

    # Indexes for tenant_subscriptions
    op.create_index("ix_tenant_subscriptions_status", "tenant_subscriptions", ["status"])
    op.create_index(
        "ix_tenant_subscriptions_delinquent",
        "tenant_subscriptions",
        ["delinquent"],
        postgresql_where=sa.text("delinquent = true"),
    )
    op.create_index(
        "ix_tenant_subscriptions_stripe_subscription_id",
        "tenant_subscriptions",
        ["stripe_subscription_id"],
        postgresql_where=sa.text("stripe_subscription_id IS NOT NULL"),
    )

    # RLS for tenant_subscriptions
    op.execute("ALTER TABLE tenant_subscriptions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tenant_subscriptions FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_subscriptions_tenant_isolation ON tenant_subscriptions
        FOR ALL
        USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
        WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid)
        """
    )
    op.execute(
        """
        CREATE POLICY tenant_subscriptions_cc_staff_read ON tenant_subscriptions
        FOR SELECT
        USING (current_setting('app.cc_staff_id', true) IS NOT NULL)
        """
    )
    op.execute(
        """
        CREATE POLICY tenant_subscriptions_cc_staff_write ON tenant_subscriptions
        FOR ALL
        USING (current_setting('app.cc_staff_id', true) IS NOT NULL)
        WITH CHECK (current_setting('app.cc_staff_id', true) IS NOT NULL)
        """
    )

    # Create tenant_usage_meter_links table
    op.create_table(
        "tenant_usage_meter_links",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "meter_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("usage_meters.id"),
            nullable=False,
        ),
        sa.Column("stripe_subscription_item_id", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("tenant_id", "meter_id", name="uq_tenant_usage_meter_links_tenant_meter"),
    )

    # RLS for tenant_usage_meter_links
    op.execute("ALTER TABLE tenant_usage_meter_links ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tenant_usage_meter_links FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_usage_meter_links_tenant_isolation ON tenant_usage_meter_links
        FOR ALL
        USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
        WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid)
        """
    )
    op.execute(
        """
        CREATE POLICY tenant_usage_meter_links_cc_staff_read ON tenant_usage_meter_links
        FOR SELECT
        USING (current_setting('app.cc_staff_id', true) IS NOT NULL)
        """
    )
    op.execute(
        """
        CREATE POLICY tenant_usage_meter_links_cc_staff_write ON tenant_usage_meter_links
        FOR ALL
        USING (current_setting('app.cc_staff_id', true) IS NOT NULL)
        WITH CHECK (current_setting('app.cc_staff_id', true) IS NOT NULL)
        """
    )


def downgrade() -> None:
    # Drop RLS for tenant_usage_meter_links
    op.execute("DROP POLICY IF EXISTS tenant_usage_meter_links_cc_staff_write ON tenant_usage_meter_links")
    op.execute("DROP POLICY IF EXISTS tenant_usage_meter_links_cc_staff_read ON tenant_usage_meter_links")
    op.execute("DROP POLICY IF EXISTS tenant_usage_meter_links_tenant_isolation ON tenant_usage_meter_links")
    op.execute("ALTER TABLE tenant_usage_meter_links DISABLE ROW LEVEL SECURITY")

    # Drop table tenant_usage_meter_links
    op.drop_table("tenant_usage_meter_links")

    # Drop RLS for tenant_subscriptions
    op.execute("DROP POLICY IF EXISTS tenant_subscriptions_cc_staff_write ON tenant_subscriptions")
    op.execute("DROP POLICY IF EXISTS tenant_subscriptions_cc_staff_read ON tenant_subscriptions")
    op.execute("DROP POLICY IF EXISTS tenant_subscriptions_tenant_isolation ON tenant_subscriptions")
    op.execute("ALTER TABLE tenant_subscriptions DISABLE ROW LEVEL SECURITY")

    # Drop indexes for tenant_subscriptions
    op.drop_index("ix_tenant_subscriptions_stripe_subscription_id", "tenant_subscriptions")
    op.drop_index("ix_tenant_subscriptions_delinquent", "tenant_subscriptions")
    op.drop_index("ix_tenant_subscriptions_status", "tenant_subscriptions")

    # Drop table tenant_subscriptions
    op.drop_table("tenant_subscriptions")


# Verification Manifest
# 1. Tables tenant_subscriptions and tenant_usage_meter_links exist with correct UUID PKs and FKs.
# 2. Indexes on status, delinquent (partial), and stripe_subscription_id (partial) exist.
# 3. RLS is enabled and forced on both tables.
# 4. Three policies per table (tenant_isolation, cc_staff_read, cc_staff_write) are active.
# 5. Downgrade successfully removes all policies, indexes, and tables.

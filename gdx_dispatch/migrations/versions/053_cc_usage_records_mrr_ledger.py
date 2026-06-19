"""create usage_records and mrr_ledger tables

This migration adds append-only tables for telemetry (usage_records) and
finance (mrr_ledger) to support MRR dashboarding and Stripe sync.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "053_cc_usage_records_mrr_ledger"
down_revision = "052_cc_tenant_subscriptions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create usage_records table
    op.create_table(
        "usage_records",
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
        sa.Column("period_kind", sa.Text(), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("quantity", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column(
            "emitted_to_stripe_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "stripe_meter_event_id",
            sa.Text(),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "period_kind IN ('hour', 'day', 'month')",
            name="check_usage_records_period_kind"
        ),
        sa.CheckConstraint(
            "quantity >= 0",
            name="check_usage_records_quantity_positive"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "meter_id",
            "period_kind",
            "period_start",
            name="uq_usage_records_idempotency"
        ),
    )

    # Indexes on usage_records
    op.create_index(
        "ix_usage_records_unemitted",
        "usage_records",
        ["meter_id", "period_start"],
        postgresql_where=sa.text("emitted_to_stripe_at IS NULL"),
    )
    op.create_index(
        "ix_usage_records_tenant_period",
        "usage_records",
        ["tenant_id", sa.text("period_start DESC")],
    )

    # RLS on usage_records
    op.execute("ALTER TABLE usage_records ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE usage_records FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON usage_records "
        "USING (tenant_id = current_setting('app.tenant_id')::uuid)"
    )
    op.execute(
        "CREATE POLICY _cc_staff_read ON usage_records "
        "FOR SELECT USING (current_setting('app.cc_staff_id', True) IS NOT NULL)"
    )
    op.execute(
        "CREATE POLICY _cc_staff_write ON usage_records "
        "FOR ALL USING (current_setting('app.cc_staff_id', True) IS NOT NULL)"
    )

    # Create mrr_ledger table
    op.create_table(
        "mrr_ledger",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column(
            "event_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("mrr_delta_cents", sa.BigInteger(), nullable=False),
        sa.Column(
            "plan_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("subscription_plans.id"),
            nullable=True,
        ),
        sa.Column("stripe_event_id", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "event_type IN ('new', 'expansion', 'contraction', 'churn', 'reactivation')",
            name="check_mrr_ledger_event_type"
        ),
        sa.UniqueConstraint("stripe_event_id", name="uq_mrr_ledger_stripe_event_id"),
    )

    # Indexes on mrr_ledger
    op.create_index(
        "ix_mrr_ledger_event_at",
        "mrr_ledger",
        ["event_at"],
    )
    op.create_index(
        "ix_mrr_ledger_tenant_event_at",
        "mrr_ledger",
        ["tenant_id", sa.text("event_at DESC")],
    )

    # RLS on mrr_ledger
    op.execute("ALTER TABLE mrr_ledger ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE mrr_ledger FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON mrr_ledger "
        "USING (tenant_id = current_setting('app.tenant_id')::uuid)"
    )
    op.execute(
        "CREATE POLICY _cc_staff_read ON mrr_ledger "
        "FOR SELECT USING (current_setting('app.cc_staff_id', True) IS NOT NULL)"
    )
    op.execute(
        "CREATE POLICY _cc_staff_write ON mrr_ledger "
        "FOR ALL USING (current_setting('app.cc_staff_id', True) IS NOT NULL)"
    )


def downgrade() -> None:
    # Downgrade mrr_ledger
    op.execute("DROP POLICY IF EXISTS _cc_staff_write ON mrr_ledger")
    op.execute("DROP POLICY IF EXISTS _cc_staff_read ON mrr_ledger")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON mrr_ledger")
    op.execute("ALTER TABLE mrr_ledger DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_mrr_ledger_tenant_event_at", table_name="mrr_ledger")
    op.drop_index("ix_mrr_ledger_event_at", table_name="mrr_ledger")
    op.drop_table("mrr_ledger")

    # Downgrade usage_records
    op.execute("DROP POLICY IF EXISTS _cc_staff_write ON usage_records")
    op.execute("DROP POLICY IF EXISTS _cc_staff_read ON usage_records")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON usage_records")
    op.execute("ALTER TABLE usage_records DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_usage_records_tenant_period", table_name="usage_records")
    op.drop_index("ix_usage_records_unemitted", table_name="usage_records")
    op.drop_table("usage_records")

# Verification Manifest
# Delta: 1 migration, 2 tables, 4 indexes, 6 RLS policies
# Tables: usage_records (telemetry), mrr_ledger (finance)
# Constraints: Check constraints and UniqueConstraints for idempotency
# RLS: Enabled and forced with tenant and staff isolation
# Downgrade: Full reversal of all objects in reverse order

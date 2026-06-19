"""Add invoices, payments, connect_accounts, connect_charges, and dunning_state tables.

This migration introduces the core Stripe billing and Connect activity cache for the control plane.
Includes RLS policies for tenant isolation and CC staff access.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "054_cc_invoices_payments_connect"
down_revision = "053_cc_usage_records_mrr_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Create invoices table
    op.create_table(
        "invoices",
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
        sa.Column("stripe_invoice_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("amount_due_cents", sa.BigInteger(), nullable=False),
        sa.Column("amount_paid_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("amount_remaining_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("currency", sa.Text(), nullable=False, server_default="'usd'"),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("due_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("hosted_invoice_url", sa.Text(), nullable=True),
        sa.Column("raw_json", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'open', 'paid', 'uncollectible', 'void')",
            name="ck_invoices_status",
        ),
        sa.CheckConstraint("amount_due_cents >= 0", name="ck_invoices_amount_due_pos"),
        sa.CheckConstraint(
            "amount_paid_cents >= 0",
            name="ck_invoices_amount_paid_pos",
        ),
        sa.CheckConstraint(
            "amount_remaining_cents >= 0",
            name="ck_invoices_amount_remaining_pos",
        ),
        sa.UniqueConstraint("stripe_invoice_id", name="uq_invoices_stripe_id"),
    )

    # Indexes on invoices
    op.create_index(
        "ix_invoices_tenant_status", "invoices", ["tenant_id", "status"], unique=False
    )
    op.create_index(
        "ix_invoices_open_due",
        "invoices",
        ["due_date"],
        unique=False,
        postgresql_where=sa.text("status = 'open'"),
    )

    # 2. Create payments table
    op.create_table(
        "payments",
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
            "invoice_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("invoices.id"),
            nullable=True,
        ),
        sa.Column("stripe_payment_intent_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False, server_default="'usd'"),
        sa.Column("payment_method_type", sa.Text(), nullable=True),
        sa.Column("failure_code", sa.Text(), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.Column("raw_json", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "status IN ('requires_payment_method', 'requires_confirmation', 'requires_action', "
            "'processing', 'requires_capture', 'canceled', 'succeeded')",
            name="ck_payments_status",
        ),
        sa.CheckConstraint("amount_cents >= 0", name="ck_payments_amount_pos"),
        sa.UniqueConstraint("stripe_payment_intent_id", name="uq_payments_stripe_id"),
    )

    # Index on payments
    op.create_index(
        "ix_payments_tenant_status", "payments", ["tenant_id", "status"], unique=False
    )

    # 3. Create connect_accounts table
    op.create_table(
        "connect_accounts",
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
        sa.Column("stripe_account_id", sa.Text(), nullable=False),
        sa.Column("account_type", sa.Text(), nullable=False, server_default="'standard'"),
        sa.Column("charges_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("payouts_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("requirements_currently_due_json", postgresql.JSONB(), nullable=False, server_default=sa.text("\'[]\'::jsonb")),
        sa.Column(
            "requirements_eventually_due_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("\'[]\'::jsonb"),
        ),
        sa.Column("requirements_disabled_reason", sa.Text(), nullable=True),
        sa.Column("platform_fee_percent", sa.Numeric(precision=5, scale=2), nullable=False, server_default="2.50"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "account_type IN ('standard', 'express', 'custom')",
            name="ck_connect_accounts_type",
        ),
        sa.CheckConstraint(
            "platform_fee_percent >= 0 AND platform_fee_percent <= 100",
            name="ck_connect_accounts_fee_range",
        ),
        sa.UniqueConstraint("tenant_id", name="uq_connect_accounts_tenant"),
        sa.UniqueConstraint("stripe_account_id", name="uq_connect_accounts_stripe"),
    )

    # 4. Create connect_charges table
    op.create_table(
        "connect_charges",
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
            "connect_account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("connect_accounts.id"),
            nullable=False,
        ),
        sa.Column("stripe_charge_id", sa.Text(), nullable=False),
        sa.Column("gross_cents", sa.BigInteger(), nullable=False),
        sa.Column("fee_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("application_fee_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("net_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("currency", sa.Text(), nullable=False, server_default="'usd'"),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("raw_json", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'succeeded', 'failed', 'refunded', 'disputed')",
            name="ck_connect_charges_status",
        ),
        sa.UniqueConstraint("stripe_charge_id", name="uq_connect_charges_stripe"),
    )

    # Index on connect_charges
    op.create_index(
        "ix_connect_charges_tenant_created",
        "connect_charges",
        ["tenant_id", "created_at"],
        unique=False,
    )

    # 5. Create dunning_state table
    op.create_table(
        "dunning_state",
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "state",
            sa.Text(),
            nullable=False,
            server_default="'ok'",
        ),
        sa.Column(
            "entered_state_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("suspend_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("manual_hold", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("manual_hold_reason", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "state IN ('ok', 'reminder_1', 'reminder_2', 'reminder_3', 'grace', 'suspended')",
            name="ck_dunning_state_status",
        ),
    )

    # RLS setup
    for table in ["invoices", "payments", "connect_accounts", "connect_charges", "dunning_state"]:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

        # tenant_isolation (FOR ALL)
        # Note: dunning_state uses tenant_id as PK, others use it as a column.
        # The policy check 'tenant_id = current_setting...' works for both.
        op.execute(
            f"CREATE POLICY {table}_tenant_isolation ON {table} "
            f"FOR ALL USING (tenant_id = current_setting('app.tenant_id')::uuid)"
        )

        # cc_staff_read (FOR SELECT)
        op.execute(
            f"CREATE POLICY {table}_cc_staff_read ON {table} "
            f"FOR SELECT USING (current_setting('app.cc_staff_id', True) IS NOT NULL)"
        )

        # cc_staff_write (FOR ALL)
        op.execute(
            f"CREATE POLICY {table}_cc_staff_write ON {table} "
            f"FOR ALL USING (current_setting('app.cc_staff_id', True) IS NOT NULL)"
        )


def downgrade() -> None:
    # Drop RLS policies and disable RLS (reverse order of creation)
    for table in ["invoices", "payments", "connect_accounts", "connect_charges", "dunning_state"]:
        op.execute(f"DROP POLICY IF EXISTS {table}_cc_staff_write ON {table}")
        op.execute(f"DROP POLICY IF EXISTS {table}_cc_staff_read ON {table}")
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    # Drop tables in reverse dependency order
    op.drop_table("dunning_state")
    op.drop_table("connect_charges")
    op.drop_table("connect_accounts")
    op.drop_table("payments")
    op.drop_table("invoices")


# Verification Manifest
# 1. Table 'invoices' created with constraints and indexes.
# 2. Table 'payments' created with FK to 'invoices'.
# 3. Table 'connect_accounts' created with tenant unique constraint.
# 4. Table 'connect_charges' created with FK to 'connect_accounts'.
# 5. Table 'dunning_state' created with tenant_id as PK.
# 6. RLS enabled and 15 policies applied.
# 7. Downgrade cleans up everything.

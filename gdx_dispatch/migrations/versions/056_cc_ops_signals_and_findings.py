"""create ops_signals and ops_findings tables with seed signals"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "056_cc_ops_signals_and_findings"
down_revision = "055_cc_webhook_billing_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create ops_signals table
    op.create_table(
        "ops_signals",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "query_kind",
            sa.Text(),
            sa.CheckConstraint(
                "query_kind IN ('sql', 'tool')",
                name="ck_ops_signals_query_kind"
            ),
            nullable=False,
        ),
        sa.Column("query_text", sa.Text(), nullable=False),
        sa.Column(
            "threshold_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column(
            "severity",
            sa.Text(),
            sa.CheckConstraint(
                "severity IN ('p0', 'p1', 'p2', 'p3')",
                name="ck_ops_signals_severity"
            ),
            nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "cooldown_minutes",
            sa.Integer(),
            server_default=sa.text("60"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "cooldown_minutes >= 0",
            name="ck_ops_signals_cooldown_non_negative",
        ),
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
        sa.UniqueConstraint("key", name="uq_ops_signals_key"),
        sa.PrimaryKeyConstraint("id"),
    )

    # Create ops_findings table
    op.create_table(
        "ops_findings",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "signal_key",
            sa.Text(),
            sa.ForeignKey("ops_signals.key", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "severity",
            sa.Text(),
            sa.CheckConstraint(
                "severity IN ('p0', 'p1', 'p2', 'p3')",
                name="ck_ops_findings_severity"
            ),
            nullable=False,
        ),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column(
            "evidence_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Text(),
            server_default=sa.text("'open'"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('open', 'acknowledged', 'resolved', 'suppressed')",
            name="ck_ops_findings_status",
        ),
        sa.Column(
            "acknowledged_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("cc_staff_users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id"),
    )

    # Create indexes on ops_signals
    op.create_index(
        "ix_ops_signals_enabled",
        "ops_signals",
        ["enabled", "severity"],
        postgresql_where=sa.text("enabled = true"),
    )

    # Create indexes on ops_findings
    op.create_index(
        "ix_ops_findings_open",
        "ops_findings",
        ["severity", "created_at"],
        postgresql_where=sa.text("status = 'open'"),
    )
    op.create_index(
        "ix_ops_findings_signal_created",
        "ops_findings",
        ["signal_key", "created_at"],
    )
    op.create_index(
        "ix_ops_findings_tenant_created",
        "ops_findings",
        ["tenant_id", "created_at"],
        postgresql_where=sa.text("tenant_id IS NOT NULL"),
    )

    # Enable RLS on ops_findings
    op.execute("ALTER TABLE ops_findings ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE ops_findings FORCE ROW LEVEL SECURITY")

    # Add RLS policies for CC staff
    op.execute(
        "CREATE POLICY ops_findings_cc_staff_read ON ops_findings "
        "FOR SELECT USING (current_setting('app.cc_staff_id', true) IS NOT NULL)"
    )
    op.execute(
        "CREATE POLICY ops_findings_cc_staff_write ON ops_findings "
        "FOR ALL USING (current_setting('app.cc_staff_id', true) IS NOT NULL)"
    )

    # Seed signals
    op.execute(
        """
        INSERT INTO ops_signals (
            key, display_name, description,
            query_kind, query_text, severity, cooldown_minutes
        ) VALUES
        ('mrr.delta_24h', 'MRR delta 24h',
         'Net MRR change in last 24h vs prior 7d avg',
         'tool', 'platform.mrr.summary', 'p2', 240),
        ('signups.cliff', 'Signup cliff',
         'Signups dropped >50% vs trailing 7d',
         'sql', 'signups_24h_vs_7d_avg', 'p1', 120),
        ('churn.spike', 'Churn spike',
         'Churn events >3 sigma above baseline',
         'sql', 'churn_count_24h', 'p1', 240),
        ('errors.p0_in_any_tenant', 'P0 error fired',
         'Any P0-level Sentry/log alert in any tenant',
         'tool', 'platform.errors.recent', 'p0', 30),
        ('errors.spike', 'Error spike',
         '24h error rate >3 sigma above 7d baseline',
         'tool', 'platform.errors.recent', 'p1', 60),
        ('rls.violations', 'RLS violations',
         'The D97 lint baseline grew',
         'sql', 'rls_violation_count_vs_baseline', 'p0', 60),
        ('rls.bypass_active', 'RLS bypass active',
         'Any session with bypassrls=t outside maintenance',
         'sql', 'bypass_session_count', 'p0', 15),
        ('tenant.zero_logins_7d', 'Tenant zero logins 7d',
         'Active tenant with no successful logins in 7d',
         'sql', 'tenants_zero_logins_7d', 'p2', 1440)
        """
    )


def downgrade() -> None:
    # Drop RLS policies
    op.execute("DROP POLICY IF EXISTS ops_findings_cc_staff_write ON ops_findings")
    op.execute("DROP POLICY IF EXISTS ops_findings_cc_staff_read ON ops_findings")

    # Disable RLS
    op.execute("ALTER TABLE ops_findings DISABLE ROW LEVEL SECURITY")

    # Drop indexes
    op.drop_index("ix_ops_findings_tenant_created", table_name="ops_findings")
    op.drop_index("ix_ops_findings_signal_created", table_name="ops_findings")
    op.drop_index("ix_ops_findings_open", table_name="ops_findings")
    op.drop_index("ix_ops_signals_enabled", table_name="ops_signals")

    # Drop tables
    op.drop_table("ops_findings")
    op.drop_table("ops_signals")


# Verification Manifest
# 1. Check ops_signals count: SELECT count(*) FROM ops_signals; (Expected: 8)
# 2. Check signal data: SELECT key, severity, cooldown_minutes FROM ops_signals ORDER BY key;
# 3. Check ops_findings RLS: SELECT relname, relrowsecurity FROM pg_class WHERE relname = 'ops_findings';
# 4. Check ops_findings policies: SELECT * FROM pg_policies WHERE tablename = 'ops_findings';
# 5. Test idempotency: downgrade -1 followed by upgrade head.

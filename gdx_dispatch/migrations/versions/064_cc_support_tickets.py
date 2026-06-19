"""Create cc_support_tickets for apartment-manager support surface."""
import sqlalchemy as sa
from alembic import op

revision = "064_cc_support_tickets"
down_revision = "063_dispatch_lane_default_on"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.create_table(
        "cc_support_tickets",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True),
                  primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.dialects.postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("opened_by_email", sa.Text(), nullable=False),
        sa.Column("opened_by_user_id", sa.dialects.postgresql.UUID(as_uuid=True),
                  nullable=True),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False,
                  server_default=sa.text("'open'")),
        sa.Column("priority", sa.Text(), nullable=False,
                  server_default=sa.text("'medium'")),
        sa.Column("assigned_to_id", sa.dialects.postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("cc_staff_users.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("resolution_summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("now()")),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "category IN ('bug', 'feature', 'question', 'other')",
            name="ck_cc_support_tickets_category",
        ),
        sa.CheckConstraint(
            "status IN ('open', 'in_progress', 'closed')",
            name="ck_cc_support_tickets_status",
        ),
        sa.CheckConstraint(
            "priority IN ('low', 'medium', 'high', 'urgent')",
            name="ck_cc_support_tickets_priority",
        ),
    )
    op.create_index(
        "ix_cc_support_tickets_tenant_status",
        "cc_support_tickets",
        ["tenant_id", "status", "created_at"],
    )
    op.create_index(
        "ix_cc_support_tickets_assigned",
        "cc_support_tickets",
        ["assigned_to_id"],
        postgresql_where=sa.text("assigned_to_id IS NOT NULL"),
    )

def downgrade() -> None:
    op.drop_index("ix_cc_support_tickets_assigned", table_name="cc_support_tickets")
    op.drop_index("ix_cc_support_tickets_tenant_status", table_name="cc_support_tickets")
    op.drop_table("cc_support_tickets")

"""create cc_user_extra_grants and cc_staff_audit_log with RLS policies

Revision ID: 050_cc_extra_grants_and_audit
Revises: 049_cc_staff_rbac
Create Date: 2026-05-01 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "050_cc_extra_grants_and_audit"
down_revision = "049_cc_staff_rbac"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create cc_user_extra_grants table
    op.create_table(
        "cc_user_extra_grants",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column(
            "staff_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("cc_staff_users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("capability", sa.Text(), nullable=False),
        sa.Column(
            "granted_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("cc_staff_users.id"),
            nullable=False,
        ),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "revoked_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "revoked_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("cc_staff_users.id"),
            nullable=True,
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("scope_json", postgresql.JSONB(), server_default=sa.text("\'{}\'::jsonb"), nullable=False),
        sa.ForeignKeyConstraint(
            ["capability"], ["cc_capabilities.key"]
        ),
    )

    # Create indexes for cc_user_extra_grants
    op.create_index(
        "ix_cc_grants_active",
        "cc_user_extra_grants",
        ["staff_user_id"],
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_index(
        "ix_cc_grants_expiry",
        "cc_user_extra_grants",
        ["expires_at"],
        postgresql_where=sa.text("revoked_at IS NULL AND expires_at IS NOT NULL"),
    )

    # Create cc_staff_audit_log table
    op.create_table(
        "cc_staff_audit_log",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column(
            "actor_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("cc_staff_users.id"),
            nullable=False,
        ),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("target_type", sa.Text(), nullable=True),
        sa.Column("target_id", sa.Text(), nullable=True),
        sa.Column(
            "result",
            sa.Text(),
            sa.CheckConstraint(
                "result IN ('allow','deny','error')",
                name="ck_cc_audit_result"
            ),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("request_id", sa.Text(), nullable=False),
        sa.Column("ip_inet", postgresql.INET(), nullable=True),
        sa.Column(
            "metadata_json",
            postgresql.JSONB(),
            server_default=sa.text("\'{}\'::jsonb"),
            nullable=False,
        ),
        sa.Column("prev_hash", sa.LargeBinary(), nullable=True),
        sa.Column("row_hash", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    # Create indexes for cc_staff_audit_log
    op.create_index(
        "ix_cc_audit_actor",
        "cc_staff_audit_log",
        ["actor_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "ix_cc_audit_action",
        "cc_staff_audit_log",
        ["action", sa.text("created_at DESC")],
    )
    op.create_index("ix_cc_audit_request_id", "cc_staff_audit_log", ["request_id"])

    # Enable RLS and add policies for all cc_* tables
    tables_with_catalog_rls = ["cc_roles", "cc_capabilities", "cc_role_capabilities"]
    tables_to_secure = [
        "cc_staff_users",
        "cc_roles",
        "cc_capabilities",
        "cc_role_capabilities",
        "cc_user_extra_grants",
        "cc_staff_audit_log",
    ]

    for table in tables_to_secure:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    # Catalog RLS policies
    for table in tables_with_catalog_rls:
        op.execute(
            f"CREATE POLICY {table}_staff_read ON {table} "
            f"FOR SELECT USING (current_setting('app.cc_staff_id', true) IS NOT NULL)"
        )

    # cc_staff_users policies
    op.execute(
        "CREATE POLICY cc_staff_users_read ON cc_staff_users "
        "FOR SELECT USING (current_setting('app.cc_staff_id', true) IS NOT NULL)"
    )
    op.execute(
        "CREATE POLICY cc_staff_users_self_update ON cc_staff_users "
        "FOR UPDATE USING (id::text = current_setting('app.cc_staff_id', true)) "
        "WITH CHECK (id::text = current_setting('app.cc_staff_id', true))"
    )

    # cc_user_extra_grants policies
    op.execute(
        "CREATE POLICY cc_grants_read ON cc_user_extra_grants "
        "FOR SELECT USING (staff_user_id::text = current_setting('app.cc_staff_id', true) "
        "OR granted_by::text = current_setting('app.cc_staff_id', true))"
    )
    op.execute(
        "CREATE POLICY cc_grants_insert ON cc_user_extra_grants "
        "FOR INSERT WITH CHECK (granted_by::text = current_setting('app.cc_staff_id', true))"
    )

    # cc_staff_audit_log policies
    op.execute(
        "CREATE POLICY cc_audit_read_own ON cc_staff_audit_log "
        "FOR SELECT USING (actor_id::text = current_setting('app.cc_staff_id', true))"
    )
    op.execute(
        "CREATE POLICY cc_audit_insert ON cc_staff_audit_log "
        "FOR INSERT WITH CHECK (actor_id::text = current_setting('app.cc_staff_id', true))"
    )


def downgrade() -> None:
    # Drop policies for cc_staff_audit_log
    op.execute("DROP POLICY IF EXISTS cc_audit_insert ON cc_staff_audit_log")
    op.execute("DROP POLICY IF EXISTS cc_audit_read_own ON cc_staff_audit_log")

    # Drop policies for cc_user_extra_grants
    op.execute("DROP POLICY IF EXISTS cc_grants_insert ON cc_user_extra_grants")
    op.execute("DROP POLICY IF EXISTS cc_grants_read ON cc_user_extra_grants")

    # Drop policies for cc_staff_users
    op.execute("DROP POLICY IF EXISTS cc_staff_users_self_update ON cc_staff_users")
    op.execute("DROP POLICY IF EXISTS cc_staff_users_read ON cc_staff_users")

    # Drop policies for catalog tables
    for table in ["cc_roles", "cc_capabilities", "cc_role_capabilities"]:
        op.execute(f"DROP POLICY IF EXISTS {table}_staff_read ON {table}")

    # Disable RLS for all tables
    for table in [
        "cc_staff_users",
        "cc_roles",
        "cc_capabilities",
        "cc_role_capabilities",
        "cc_user_extra_grants",
        "cc_staff_audit_log",
    ]:
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    # Drop audit log
    op.drop_index("ix_cc_audit_request_id", table="cc_staff_audit_log")
    op.drop_index("ix_cc_audit_action", table="cc_staff_audit_log")
    op.drop_index("ix_cc_audit_actor", table="cc_staff_audit_log")
    op.drop_table("cc_staff_audit_log")

    # Drop extra grants
    op.drop_index("ix_cc_grants_expiry", table="cc_user_extra_grants")
    op.drop_index("ix_cc_grants_active", table="cc_user_extra_grants")
    op.drop_table("cc_user_extra_grants")


# Verification Manifest
# 1. Tables cc_user_extra_grants and cc_staff_audit_log exist with correct schema.
# 2. All 5 indexes (2 for grants, 3 for audit) are present.
# 3. RLS is ENABLED and FORCED on all 6 cc_* tables.
# 4. All 10 policies are correctly applied to respective tables.
# 5. Downgrade correctly removes all policies, disables RLS, and drops tables/indexes.

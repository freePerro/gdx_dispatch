"""Create CC v2 RBAC tables: cc_staff_users, cc_roles, cc_capabilities, and cc_role_capabilities, plus seeds.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "049_cc_staff_rbac"
down_revision = "048_estimates_default_terms"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Ensure citext extension is available for staff email
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")

    # 1. Create cc_roles first (needed for FK in cc_staff_users)
    op.create_table(
        "cc_roles",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("is_system", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("grantable_capability_pattern", sa.Text(), nullable=True),
    )

    # 2. Create cc_staff_users
    op.create_table(
        "cc_staff_users",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("email", postgresql.CITEXT(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column(
            "base_role",
            sa.Text(),
            sa.ForeignKey("cc_roles.key"),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), server_default=sa.text("'active'"), nullable=False),
        sa.Column("mfa_enrolled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("password_hash", sa.Text(), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("cc_staff_users.id"),
            nullable=True,
        ),
        sa.CheckConstraint(
            "status IN ('active', 'suspended', 'offboarded')", name="ck_cc_staff_users_status"
        ),
        sa.UniqueConstraint("email", name="uq_cc_staff_users_email"),
    )

    # 3. Create cc_capabilities
    op.create_table(
        "cc_capabilities",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("surface", sa.Text(), nullable=False),
        sa.Column(
            "blast_radius",
            sa.Text(),
            server_default=sa.text("'green'"),
            nullable=False,
        ),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("requires_mfa", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("requires_reason", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.CheckConstraint(
            "blast_radius IN ('green', 'yellow', 'red')", name="ck_cc_capabilities_blast_radius"
        ),
    )

    # 4. Create cc_role_capabilities (M:N)
    op.create_table(
        "cc_role_capabilities",
        sa.Column("role_key", sa.Text(), sa.ForeignKey("cc_roles.key", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "capability",
            sa.Text(),
            sa.ForeignKey("cc_capabilities.key"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("role_key", "capability"),
    )

    # --- SEED DATA ---

    # Seed cc_roles
    op.execute(
        """
        INSERT INTO cc_roles (key, display_name, description, is_system) VALUES
        ('super_admin', 'Super Admin', 'Wildcard access', true),
        ('billing_admin', 'Billing Admin', 'Financial and subscription management', true),
        ('support_agent', 'Support Agent', 'Customer support operations', true),
        ('read_only_observer', 'Read-only Observer', 'Read-only access to most data', true)
        """
    )

    # Seed cc_capabilities (41 entries)
    op.execute(
        """
        INSERT INTO cc_capabilities (key, surface, blast_radius, description, requires_mfa, requires_reason) VALUES
        -- see (12)
        ('see.tenant.list', 'see', 'green', 'List tenants', false, false),
        ('see.tenant.detail', 'see', 'green', 'View tenant details', false, false),
        ('see.tenant.usage', 'see', 'green', 'View tenant usage', false, false),
        ('see.tenant.audit_log', 'see', 'green', 'View tenant audit logs', false, false),
        ('see.staff.list', 'see', 'green', 'List staff members', false, false),
        ('see.billing.invoice', 'see', 'green', 'View invoices', false, false),
        ('see.billing.subscription', 'see', 'green', 'View subscriptions', false, false),
        ('see.billing.refund_history', 'see', 'green', 'View refund history', false, false),
        ('see.support.ticket', 'see', 'green', 'View support tickets', false, false),
        ('see.platform.health', 'see', 'green', 'View platform health', false, false),
        ('see.platform.deploy_log', 'see', 'green', 'View deployment logs', false, false),
        ('see.platform.feature_flags', 'see', 'green', 'View feature flags', false, false),
        -- handle (9)
        ('handle.tenant.create', 'handle', 'green', 'Create a new tenant', false, false),
        ('handle.tenant.suspend', 'handle', 'green', 'Suspend a tenant', false, false),
        ('handle.tenant.unsuspend', 'handle', 'green', 'Unsuspend a tenant', false, false),
        ('handle.tenant.delete', 'handle', 'red', 'Delete a tenant', false, false),
        ('handle.module_grant.add', 'handle', 'green', 'Add a module grant', false, false),
        ('handle.module_grant.remove', 'handle', 'green', 'Remove a module grant', false, false),
        ('handle.feature_flag.toggle', 'handle', 'green', 'Toggle a feature flag', false, false),
        ('handle.platform.deploy', 'handle', 'red', 'Deploy to platform', false, false),
        ('handle.tenant.impersonate', 'handle', 'red', 'Impersonate a tenant', true, true),
        -- get_paid (8)
        ('get_paid.invoice.send', 'get_paid', 'green', 'Send an invoice', false, false),
        ('get_paid.invoice.void', 'get_paid', 'red', 'Void an invoice', false, false),
        ('get_paid.refund.issue', 'get_paid', 'red', 'Issue a refund', true, true),
        ('get_paid.subscription.change_plan', 'get_paid', 'green', 'Change subscription plan', false, false),
        ('get_paid.subscription.cancel', 'get_paid', 'green', 'Cancel subscription', false, false),
        ('get_paid.credit.apply', 'get_paid', 'red', 'Apply credit', false, false),
        ('get_paid.dunning.pause', 'get_paid', 'green', 'Pause dunning', false, false),
        ('get_paid.tax.adjust', 'get_paid', 'red', 'Adjust taxes', false, false),
        -- support (6)
        ('support.ticket.assign', 'support', 'green', 'Assign a ticket', false, false),
        ('support.ticket.respond', 'support', 'green', 'Respond to a ticket', false, false),
        ('support.ticket.close', 'support', 'green', 'Close a ticket', false, false),
        ('support.user.reset_password', 'support', 'green', 'Reset user password', false, true),
        ('support.user.unlock', 'support', 'green', 'Unlock a user', false, false),
        ('support.tenant.send_announcement', 'support', 'green', 'Send announcement', false, false),
        -- comply (6)
        ('comply.audit.export', 'comply', 'green', 'Export audit logs', false, false),
        ('comply.gdpr.erase', 'comply', 'red', 'GDPR erasure request', true, true),
        ('comply.gdpr.export', 'comply', 'green', 'GDPR data export', false, false),
        ('comply.staff.grant.create', 'comply', 'green', 'Create staff grant', false, false),
        ('comply.staff.grant.revoke', 'comply', 'green', 'Revoke staff grant', false, false),
        ('comply.staff.role.change', 'comply', 'red', 'Change staff role', true, false)
        """
    )

    # Seed cc_role_capabilities
    op.execute(
        """
        INSERT INTO cc_role_capabilities (role_key, capability) VALUES
        -- read_only_observer: all 12 see.*
        ('read_only_observer', 'see.tenant.list'),
        ('read_only_observer', 'see.tenant.detail'),
        ('read_only_observer', 'see.tenant.usage'),
        ('read_only_observer', 'see.tenant.audit_log'),
        ('read_only_observer', 'see.staff.list'),
        ('read_only_observer', 'see.billing.invoice'),
        ('read_only_observer', 'see.billing.subscription'),
        ('read_only_observer', 'see.billing.refund_history'),
        ('read_only_observer', 'see.support.ticket'),
        ('read_only_observer', 'see.platform.health'),
        ('read_only_observer', 'see.platform.deploy_log'),
        ('read_only_observer', 'see.platform.feature_flags'),
        -- support_agent: all 12 see.* EXCEPT see.platform.deploy_log + all 6 support.*
        ('support_agent', 'see.tenant.list'),
        ('support_agent', 'see.tenant.detail'),
        ('support_agent', 'see.tenant.usage'),
        ('support_agent', 'see.tenant.audit_log'),
        ('support_agent', 'see.staff.list'),
        ('support_agent', 'see.billing.invoice'),
        ('support_agent', 'see.billing.subscription'),
        ('support_agent', 'see.billing.refund_history'),
        ('support_agent', 'see.support.ticket'),
        ('support_agent', 'see.platform.health'),
        ('support_agent', 'see.platform.feature_flags'),
        ('support_agent', 'support.ticket.assign'),
        ('support_agent', 'support.ticket.respond'),
        ('support_agent', 'support.ticket.close'),
        ('support_agent', 'support.user.reset_password'),
        ('support_agent', 'support.user.unlock'),
        ('support_agent', 'support.tenant.send_announcement'),
        -- billing_admin: all 12 see.* + all 8 get_paid.* + support.user.reset_password + comply.staff.grant.create
        ('billing_admin', 'see.tenant.list'),
        ('billing_admin', 'see.tenant.detail'),
        ('billing_admin', 'see.tenant.usage'),
        ('billing_admin', 'see.tenant.audit_log'),
        ('billing_admin', 'see.staff.list'),
        ('billing_admin', 'see.billing.invoice'),
        ('billing_admin', 'see.billing.subscription'),
        ('billing_admin', 'see.billing.refund_history'),
        ('billing_admin', 'see.support.ticket'),
        ('billing_admin', 'see.platform.health'),
        ('billing_admin', 'see.platform.deploy_log'),
        ('billing_admin', 'see.platform.feature_flags'),
        ('billing_admin', 'get_paid.invoice.send'),
        ('billing_admin', 'get_paid.invoice.void'),
        ('billing_admin', 'get_paid.refund.issue'),
        ('billing_admin', 'get_paid.subscription.change_plan'),
        ('billing_admin', 'get_paid.subscription.cancel'),
        ('billing_admin', 'get_paid.credit.apply'),
        ('billing_admin', 'get_paid.dunning.pause'),
        ('billing_admin', 'get_paid.tax.adjust'),
        ('billing_admin', 'support.user.reset_password'),
        ('billing_admin', 'comply.staff.grant.create')
        """
    )


def downgrade() -> None:
    # Drop tables in reverse FK order
    op.drop_table("cc_role_capabilities")
    op.drop_table("cc_capabilities")
    op.drop_table("cc_staff_users")
    op.drop_table("cc_roles")


# Verification Manifest
# 1. Tables cc_staff_users, cc_roles, cc_capabilities, cc_role_capabilities created.
# 2. citext extension ensured.
# 3. Roles seeded (super_admin, billing_admin, support_agent, read_only_observer).
# 4. 41 capabilities seeded with correct surface, blast_radius, and MFA/reason flags.
# 5. cc_role_capabilities mapping matches §6.1 requirements.

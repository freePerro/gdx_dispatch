"""add mfa_secret_enc column and granter privilege trigger"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# down_revision = "058_cc_data_backfill"
revision = "059_cc_mfa_granter_trigger"
down_revision = "058_cc_data_backfill"
branch = None
depends_on = None


def upgrade() -> None:
    # 1. Add mfa_secret_enc column to cc_staff_users
    op.add_column(
        "cc_staff_users",
        sa.Column("mfa_secret_enc", sa.LargeBinary(), nullable=True),
    )

    # 2. Create cc_check_granter_privilege() PL/pgSQL trigger function
    op.execute(
        """
        CREATE OR REPLACE FUNCTION cc_check_granter_privilege()
        RETURNS TRIGGER AS $$
        DECLARE
            granter_role TEXT;
            capability_blast_radius TEXT;
            pattern TEXT;
        BEGIN
            -- Look up base_role for the granter
            SELECT base_role INTO granter_role
            FROM cc_staff_users
            WHERE id = NEW.granted_by;

            -- Super admin bypass
            IF granter_role = 'super_admin' THEN
                RETURN NEW;
            END IF;

            -- Check blast radius for the target capability
            SELECT blast_radius INTO capability_blast_radius
            FROM cc_capabilities
            WHERE key = NEW.capability;

            IF capability_blast_radius = 'red' THEN
                RAISE EXCEPTION 'only super_admin may grant red-tier capability %', NEW.capability;
            END IF;

            -- Check pattern for the granter's role
            SELECT grantable_capability_pattern INTO pattern
            FROM cc_roles
            WHERE key = granter_role;

            IF pattern IS NULL THEN
                RAISE EXCEPTION 'role % may not grant any capabilities (pattern not configured)', granter_role;
            END IF;

            IF NEW.capability !~ pattern THEN
                RAISE EXCEPTION 'role % may not grant capability % (pattern: %)', granter_role, NEW.capability, pattern;
            END IF;

            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql SECURITY DEFINER;
        """
    )

    # 3. Create the trigger on cc_user_extra_grants
    op.execute(
        """
        CREATE TRIGGER trg_check_granter_privilege
        BEFORE INSERT ON cc_user_extra_grants
        FOR EACH ROW
        EXECUTE FUNCTION cc_check_granter_privilege();
        """
    )

    # 4. Set grantable_capability_pattern for seed roles
    op.execute("UPDATE cc_roles SET grantable_capability_pattern = '.*' WHERE key = 'super_admin'")
    op.execute("UPDATE cc_roles SET grantable_capability_pattern = '^get_paid\\.' WHERE key = 'billing_admin'")
    # support_agent and read_only_observer are left as NULL per requirement


def downgrade() -> None:
    # 5. Downgrade in reverse
    op.execute("DROP TRIGGER IF EXISTS trg_check_granter_privilege ON cc_user_extra_grants")
    op.execute("DROP FUNCTION IF EXISTS cc_check_granter_privilege()")

    # Reset grantable_capability_pattern
    op.execute("UPDATE cc_roles SET grantable_capability_pattern = NULL WHERE key IN ('super_admin', 'billing_admin')")

    op.drop_column("cc_staff_users", "mfa_secret_enc")


# Verification Manifest
# 1. Check column existence: \d+ cc_staff_users
# 2. Check function existence: \df cc_check_granter_privilege
# 3. Check trigger existence: SELECT tgname FROM pg_trigger WHERE tgname = 'trg_check_granter_privilege'
# 4. Check pattern seed: SELECT key, grantable_capability_pattern FROM cc_roles
# 5. Check downgrade: alembic downgrade -1 && alembic upgrade head

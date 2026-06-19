"""Create cc_bootstrap_super_admin SECURITY DEFINER function.

Phase G.1 — companion to cc_login_lookup (mig 067). The shipped
bootstrap_super_admin.py uses SessionLocal (gdx_app, RLS-enforced)
and cannot INSERT into cc_staff_users because no super_admin exists yet
to satisfy the cc_staff_users RLS policies. This function bypasses RLS
internally and gates idempotently on count(super_admin) = 0.
"""
from __future__ import annotations

from alembic import op

revision = "068_cc_bootstrap_super_admin_fn"
down_revision = "067_cc_login_lookup_fn"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION cc_bootstrap_super_admin(
            email_in text,
            display_name_in text,
            password_hash_in text
        )
        RETURNS uuid
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            existing_count int;
            new_id uuid;
            audit_id uuid;
            req_id text;
        BEGIN
            SELECT count(*) INTO existing_count
            FROM cc_staff_users
            WHERE base_role = 'super_admin' AND status = 'active';

            IF existing_count > 0 THEN
                RAISE EXCEPTION 'bootstrap refused: % active super_admin(s) already exist', existing_count
                    USING ERRCODE = 'P0001';
            END IF;

            new_id := gen_random_uuid();
            audit_id := gen_random_uuid();
            req_id := 'bootstrap-' || substr(replace(new_id::text, '-', ''), 1, 8);

            INSERT INTO cc_staff_users (
                id, email, display_name, base_role, status, password_hash
            ) VALUES (
                new_id, email_in, display_name_in, 'super_admin', 'active', password_hash_in
            );

            INSERT INTO cc_staff_audit_log (
                id, actor_id, action, target_type, target_id,
                result, request_id, prev_hash, row_hash
            ) VALUES (
                audit_id, new_id, 'bootstrap.super_admin.create',
                'cc_staff_users', new_id::text, 'allow',
                req_id, NULL, encode(gen_random_bytes(32), 'hex')::bytea
            );

            RETURN new_id;
        END;
        $$;
        """
    )
    op.execute("REVOKE ALL ON FUNCTION cc_bootstrap_super_admin(text, text, text) FROM PUBLIC;")
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gdx_app') THEN
                EXECUTE 'GRANT EXECUTE ON FUNCTION cc_bootstrap_super_admin(text, text, text) TO gdx_app';
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gdx_cc_app') THEN
                EXECUTE 'GRANT EXECUTE ON FUNCTION cc_bootstrap_super_admin(text, text, text) TO gdx_cc_app';
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS cc_bootstrap_super_admin(text, text, text);")

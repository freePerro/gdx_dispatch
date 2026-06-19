"""Create cc_login_lookup SECURITY DEFINER function for the auth boundary.

Phase G.1 — fixes the chicken/egg between RLS on cc_staff_users and the login
route. The route runs as gdx_app (NOBYPASSRLS) and cannot SELECT from
cc_staff_users without app.cc_staff_id being set first — but login is the
flow that sets it. The textbook fix (per PostgREST docs + PostgreSQL Row
Security Policies §5.9) is a SECURITY DEFINER function owned by a privileged
role; the function bypasses RLS internally and exposes only the columns
auth.py needs.
"""
from __future__ import annotations

from alembic import op

revision = "067_cc_login_lookup_fn"
down_revision = "066_cc_async_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION cc_login_lookup(email_in text)
        RETURNS TABLE(id uuid, password_hash text, status text)
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT u.id, u.password_hash, u.status
            FROM cc_staff_users u
            WHERE u.email = email_in
            LIMIT 1;
        $$;
        """
    )
    op.execute("REVOKE ALL ON FUNCTION cc_login_lookup(text) FROM PUBLIC;")
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gdx_app') THEN
                EXECUTE 'GRANT EXECUTE ON FUNCTION cc_login_lookup(text) TO gdx_app';
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gdx_cc_app') THEN
                EXECUTE 'GRANT EXECUTE ON FUNCTION cc_login_lookup(text) TO gdx_cc_app';
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS cc_login_lookup(text);")

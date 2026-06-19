"""SECURITY DEFINER function for /auth/platform-login email→memberships lookup.

The platform-host login flow (app.example.com) needs to resolve
an unauthenticated email to its memberships *before* a tenant context
exists — but `memberships` is RLS-protected and `gdx_app` (NOBYPASSRLS)
sees zero rows when `app.tenant_id` isn't set. Same chicken/egg the
067 migration solved for the CC staff login: a SECURITY DEFINER function
owned by a privileged role exposes only the columns the auth handler
needs.

The function returns one row per active (identity, membership, tenant)
triple. Callers in gdx_dispatch/routers/auth/core.py iterate the rows for the
single-tenant / picker / explicit-choice branches.
"""
from __future__ import annotations

from alembic import op

revision = "072_platform_login_lookup_fn"
down_revision = "071_tenants_profile_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION tenant_login_lookup(email_in text)
        RETURNS TABLE(
            identity_id uuid,
            tenant_id uuid,
            slug text,
            name text,
            role text,
            db_url_enc text
        )
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT
                i.id AS identity_id,
                t.id AS tenant_id,
                t.slug,
                t.name,
                m.role,
                t.db_url_enc
            FROM identities i
            JOIN memberships m ON m.identity_id = i.id
            JOIN tenants t ON t.id = m.tenant_id
            WHERE lower(i.email) = lower(email_in)
              AND i.deleted_at IS NULL
              AND m.revoked_at IS NULL
              AND t.deleted_at IS NULL
            ORDER BY t.slug;
        $$;
        """
    )
    op.execute("REVOKE ALL ON FUNCTION tenant_login_lookup(text) FROM PUBLIC;")
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gdx_app') THEN
                EXECUTE 'GRANT EXECUTE ON FUNCTION tenant_login_lookup(text) TO gdx_app';
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS tenant_login_lookup(text);")

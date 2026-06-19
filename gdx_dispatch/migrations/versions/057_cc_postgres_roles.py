"""Create CC v2 postgres roles: gdx_cc_app, gdx_cc_readonly, and gdx_ops_watcher_ro with scoped grants.

Note: This migration implements a dev-only fallback for role passwords if environment variables are missing.
This is a security risk in production and must be mitigated by CI/CD checks in a subsequent slice.
"""

import os
from alembic import op

# revision identifiers, used by Alembic.
revision = "057_cc_postgres_roles"
down_revision = "056_cc_ops_signals_and_findings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Define roles and their attributes
    # gdx_cc_app: Full RW access to all CC v2 tables
    # gdx_cc_readonly: SELECT-only access to all CC v2 tables
    # gdx_ops_watcher_ro: SELECT-only access to a subset of tables (excluding audit logs)

    # Fetch passwords from env with dummy fallbacks for dev machines
    app_pw = os.environ.get("GDX_CC_APP_PASSWORD")
    if not app_pw:
        app_pw = "gdx_cc_app_dev_dummy_password_change_me_in_prod"
        print("WARNING: GDX_CC_APP_PASSWORD not set. Using dummy dev password.")

    readonly_pw = os.environ.get("GDX_CC_READONLY_PASSWORD")
    if not readonly_pw:
        readonly_pw = "gdx_cc_readonly_dev_dummy_password_change_me_in_prod"
        print("WARNING: GDX_CC_READONLY_PASSWORD not set. Using dummy dev password.")

    watcher_pw = os.environ.get("GDX_OPS_WATCHER_RO_PASSWORD")
    if not watcher_pw:
        watcher_pw = "gdx_ops_watcher_ro_dev_dummy_password_change_me_in_prod"
        print("WARNING: GDX_OPS_WATCHER_RO_PASSWORD not set. Using dummy dev password.")

    # 1. Create roles idempotently
    # Attributes: NOSUPERUSER, NOBYPASSRLS, NOCREATEDB, NOCREATEROLE, LOGIN
    op.execute(f"""
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'gdx_cc_app') THEN
            CREATE ROLE gdx_cc_app WITH
                NOSUPERUSER
                NOBYPASSRLS
                NOCREATEDB
                NOCREATEROLE
                LOGIN
                PASSWORD '{app_pw.replace(chr(39), chr(39)+chr(39))}';
        END IF;
    END
    $$;
    """)

    op.execute(f"""
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'gdx_cc_readonly') THEN
            CREATE ROLE gdx_cc_readonly WITH
                NOSUPERUSER
                NOBYPASSRLS
                NOCREATEDB
                NOCREATEROLE
                LOGIN
                PASSWORD '{readonly_pw.replace(chr(39), chr(39)+chr(39))}';
        END IF;
    END
    $$;
    """)

    op.execute(f"""
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'gdx_ops_watcher_ro') THEN
            CREATE ROLE gdx_ops_watcher_ro WITH
                NOSUPERUSER
                NOBYPASSRLS
                NOCREATEDB
                NOCREATEROLE
                LOGIN
                PASSWORD '{watcher_pw.replace(chr(39), chr(39)+chr(39))}';
        END IF;
    END
    $$;
    """)

    # 2. Grants for gdx_cc_app
    op.execute("GRANT USAGE ON SCHEMA public TO gdx_cc_app;")

    cc_v2_tables = [
        "cc_staff_users", "cc_roles", "cc_capabilities", "cc_role_capabilities",
        "cc_user_extra_grants", "cc_staff_audit_log", "subscription_plans",
        "usage_meters", "signup_bypass_codes", "tenant_subscriptions",
        "tenant_usage_meter_links", "usage_records", "mrr_ledger", "invoices",
        "payments", "connect_accounts", "connect_charges", "dunning_state",
        "webhook_events", "billing_audit_log", "ops_signals", "ops_findings"
    ]

    for table in cc_v2_tables:
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO gdx_cc_app;")

    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO gdx_cc_app;"
    )

    # 3. Grants for gdx_cc_readonly
    op.execute("GRANT USAGE ON SCHEMA public TO gdx_cc_readonly;")

    for table in cc_v2_tables:
        op.execute(f"GRANT SELECT ON {table} TO gdx_cc_readonly;")

    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO gdx_cc_readonly;")

    # 4. Grants for gdx_ops_watcher_ro (subset)
    # Subset: tenants + CC v2 tables EXCEPT audit logs and capability links
    watcher_subset = [
        "tenants", "cc_staff_users", "cc_roles", "cc_capabilities",
        "subscription_plans", "usage_meters", "tenant_subscriptions",
        "usage_records", "mrr_ledger", "invoices", "payments",
        "connect_accounts", "connect_charges", "dunning_state",
        "webhook_events", "ops_signals", "ops_findings"
    ]

    op.execute("GRANT USAGE ON SCHEMA public TO gdx_ops_watcher_ro;")

    for table in watcher_subset:
        op.execute(f"GRANT SELECT ON {table} TO gdx_ops_watcher_ro;")


def downgrade() -> None:
    # 5. Downgrade: Revoke and Drop
    # Note: Revoke in reverse order of grants (though order doesn't strictly matter for REVOKE)

    # Revoke for gdx_ops_watcher_ro (no default privileges to revoke)
    op.execute("REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM gdx_ops_watcher_ro;")
    op.execute("REVOKE USAGE ON SCHEMA public FROM gdx_ops_watcher_ro;")
    op.execute("DROP ROLE IF EXISTS gdx_ops_watcher_ro;")

    # Revoke for gdx_cc_readonly
    op.execute("REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM gdx_cc_readonly;")
    op.execute("REVOKE USAGE ON SCHEMA public FROM gdx_cc_readonly;")
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM gdx_cc_readonly;")
    op.execute("DROP ROLE IF EXISTS gdx_cc_readonly;")

    # Revoke for gdx_cc_app
    op.execute("REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM gdx_cc_app;")
    op.execute("REVOKE USAGE ON SCHEMA public FROM gdx_cc_app;")
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM gdx_cc_app;")
    op.execute("DROP ROLE IF EXISTS gdx_cc_app;")

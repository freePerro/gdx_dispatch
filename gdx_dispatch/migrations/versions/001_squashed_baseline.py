"""Squashed single-tenant baseline.

Replaces the original 87-migration chain. This release is single-tenant and
self-hosted, so the entire multi-tenant "Command Center" / SaaS-platform schema
(billing/Stripe-Connect/dunning/MRR/usage, MCP registry, developer portal,
OAuth platform, federation, SPIFFE, DR, metering, cross-tenant sharing,
shadow-migration, cc_staff RBAC, …) has been dropped.

This baseline creates ONLY the control-plane tables the single-tenant app still
depends on. Every tenant-plane application table (jobs, customers, users,
companies, invoices, audit_logs, role_permissions, …) is created at runtime by
``TenantBase.metadata.create_all()`` in gdx_dispatch.tools.bootstrap_app, so it
is intentionally NOT recreated here.

The DDL lives in the sibling ``baseline_squashed.sql`` (generated from the
fully-migrated schema, RLS stripped — a single tenant needs no row isolation),
so it stays a faithful snapshot of the real schema.

Schema evolution from here
--------------------------
Squashing resets the migration history, so two things are now true and MUST be
respected by anyone changing the schema later:

1. This baseline + ``create_all()`` produces a correct schema on a FRESH
   install only. ``create_all(checkfirst=True)`` is add-only — it never ALTERs
   an existing table. An install that predates this squash is NOT upgraded by
   running head; it must be re-paved or hand-migrated.
2. Going forward, ship every schema change as a NEW alembic migration stacked
   on this baseline (``down_revision = "001_squashed_baseline"``). Do NOT rely
   on ``create_all()`` to add a column to already-deployed databases — it
   won't, and silent ``UndefinedColumn`` errors are the result.
"""
from pathlib import Path

from alembic import op

# revision identifiers, used by Alembic.
revision = "001_squashed_baseline"
down_revision = None
branch_labels = None
depends_on = None

_SQL_PATH = Path(__file__).resolve().parent.parent / "baseline_squashed.sql"

# Control-plane tables this baseline creates (kept for the single-tenant app).
_BASELINE_TABLES = [
    "server_errors",
    "game_events",
    "game_state",
    "game_definitions",
    "service_accounts",
    "platform_feature_flags",
    "tenant_module_grants",
    "tenant_settings",
    "tenants",
]


def upgrade() -> None:
    sql = _SQL_PATH.read_text()
    # psycopg2 executes the whole multi-statement script in order
    # (CREATE TABLE → PK/index → FK → function).
    op.get_bind().exec_driver_sql(sql)


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DROP FUNCTION IF EXISTS public.tenant_login_lookup(text);")
    for table in _BASELINE_TABLES:
        bind.exec_driver_sql(f"DROP TABLE IF EXISTS public.{table} CASCADE;")

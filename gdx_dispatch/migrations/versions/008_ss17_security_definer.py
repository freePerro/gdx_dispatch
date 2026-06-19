"""SS-17 slice D — CREATE SECURITY DEFINER helper functions.

Chains off the remaining-tables RLS migration
(``down_revision = "ss17_rls_remaining"``) so DEFINER functions are created
only after every remaining tenant table has RLS enabled — otherwise the
functions would bypass RLS on tables that aren't yet protected, which is a
worse state than no DEFINER at all.

Per SS-17 plan P31 (audit finding C-2), every function created here
follows the 6-rule hardening checklist:

1. owned by a dedicated role (``reporting_owner``), NOT ``postgres`` or
   ``gdx_app``;
2. explicit ``SET search_path = pg_catalog, reporting, public``;
3. caller authorization validated inside the body
   (``current_setting('app.principal_identity_id', true)``
   + a membership lookup);
4. no dynamic SQL — all values bound via function parameters;
5. ``REVOKE ALL ... FROM PUBLIC; GRANT EXECUTE ... TO gdx_app``;
6. corresponding tests (see ``test_security_definer_helpers.py`` for the
   allow-list drift guard; PG-level security tests live in
   ``test_rls_remaining_integration.py``).

The set of functions created here MUST equal
:data:`gdx_dispatch.core.security_definer.KNOWN_DEFINER_FUNCTIONS`. This migration
asserts that at upgrade start; drift there = fail loud, not silent.

Revision ID: ss17_security_definer
Down revision: ss17_rls_remaining (chains after slice C)
"""
from __future__ import annotations

import logging

from alembic import op
from sqlalchemy import inspect

from gdx_dispatch.core.security_definer import (
    KNOWN_DEFINER_FUNCTIONS,
    assert_known_functions_match,
)

logger = logging.getLogger(__name__)

revision = "ss17_security_definer"
down_revision = "ss17_rls_remaining"
branch_labels = None
depends_on = None


# 1:1 with KNOWN_DEFINER_FUNCTIONS — cross-checked below.
_FUNCTION_NAMES = [
    "reporting.tenant_aggregate_revenue",
    "reporting.tenant_invoice_count",
    "reporting.tenant_warranty_rollup",
]


# ──────────────────────────────────────────────────────────────────────
# SQL — each function applies all 6 P31 hardening rules.
# ──────────────────────────────────────────────────────────────────────

# schema + owner role live in a bootstrap block so re-running the
# migration against an environment that already has them is safe.
_BOOTSTRAP_SQL = """
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'reporting_owner') THEN
        CREATE ROLE reporting_owner NOLOGIN;
    END IF;
END
$$;

CREATE SCHEMA IF NOT EXISTS reporting AUTHORIZATION reporting_owner;
"""

_FN_TENANT_AGGREGATE_REVENUE = """
CREATE OR REPLACE FUNCTION reporting.tenant_aggregate_revenue(tenant UUID)
RETURNS TABLE (month TEXT, revenue NUMERIC)
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, reporting, public
AS $fn$
    -- rule 3: validate caller authorization. A non-empty
    -- app.principal_identity_id + a matching membership on the requested
    -- tenant with role owner|admin is required; otherwise the WHERE
    -- clause yields zero rows (no leak).
    WITH caller_check AS (
        SELECT 1
        FROM memberships
        WHERE identity_id = NULLIF(current_setting('app.principal_identity_id', true), '')::uuid
          AND tenant_id = tenant::text
          AND role IN ('owner', 'admin')
    )
    SELECT to_char(date_trunc('month', created_at), 'YYYY-MM') AS month,
           SUM(total_amount) AS revenue
    FROM invoices
    WHERE company_id = tenant
      AND EXISTS (SELECT 1 FROM caller_check)
    GROUP BY 1
    ORDER BY 1;
$fn$;

ALTER FUNCTION reporting.tenant_aggregate_revenue(UUID) OWNER TO reporting_owner;
REVOKE ALL ON FUNCTION reporting.tenant_aggregate_revenue(UUID) FROM PUBLIC;
"""

_FN_TENANT_INVOICE_COUNT = """
CREATE OR REPLACE FUNCTION reporting.tenant_invoice_count(tenant UUID)
RETURNS BIGINT
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, reporting, public
AS $fn$
    WITH caller_check AS (
        SELECT 1
        FROM memberships
        WHERE identity_id = NULLIF(current_setting('app.principal_identity_id', true), '')::uuid
          AND tenant_id = tenant::text
          AND role IN ('owner', 'admin')
    )
    SELECT COUNT(*)::bigint
    FROM invoices
    WHERE company_id = tenant
      AND EXISTS (SELECT 1 FROM caller_check);
$fn$;

ALTER FUNCTION reporting.tenant_invoice_count(UUID) OWNER TO reporting_owner;
REVOKE ALL ON FUNCTION reporting.tenant_invoice_count(UUID) FROM PUBLIC;
"""

_FN_TENANT_WARRANTY_ROLLUP = """
CREATE OR REPLACE FUNCTION reporting.tenant_warranty_rollup(tenant UUID)
RETURNS TABLE (status TEXT, cnt BIGINT)
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, reporting, public
AS $fn$
    WITH caller_check AS (
        SELECT 1
        FROM memberships
        WHERE identity_id = NULLIF(current_setting('app.principal_identity_id', true), '')::uuid
          AND tenant_id = tenant::text
          AND role IN ('owner', 'admin')
    )
    SELECT COALESCE(status, 'unknown') AS status, COUNT(*)::bigint AS cnt
    FROM warranties
    WHERE company_id = tenant
      AND EXISTS (SELECT 1 FROM caller_check)
    GROUP BY status
    ORDER BY status;
$fn$;

ALTER FUNCTION reporting.tenant_warranty_rollup(UUID) OWNER TO reporting_owner;
REVOKE ALL ON FUNCTION reporting.tenant_warranty_rollup(UUID) FROM PUBLIC;
"""

# Grants are issued in a separate block so they are robust to the
# ``gdx_app`` role not existing in dev (skip cleanly instead of ERROR).
_GRANT_SQL = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gdx_app') THEN
        GRANT EXECUTE ON FUNCTION reporting.tenant_aggregate_revenue(UUID) TO gdx_app;
        GRANT EXECUTE ON FUNCTION reporting.tenant_invoice_count(UUID) TO gdx_app;
        GRANT EXECUTE ON FUNCTION reporting.tenant_warranty_rollup(UUID) TO gdx_app;
    END IF;
END
$$;
"""

_DROP_SQL = """
DROP FUNCTION IF EXISTS reporting.tenant_warranty_rollup(UUID);
DROP FUNCTION IF EXISTS reporting.tenant_invoice_count(UUID);
DROP FUNCTION IF EXISTS reporting.tenant_aggregate_revenue(UUID);
"""


def _is_postgres() -> bool:
    bind = op.get_bind()
    return bind.dialect.name == "postgresql"


def upgrade() -> None:
    # Fail loudly if the Python allow-list and the migration's function
    # set have drifted. Runs on every dialect — it's a pure Python check.
    try:
        assert_known_functions_match(_FUNCTION_NAMES)
    except AssertionError:
        logger.exception(
            "ss17_security_definer: KNOWN_DEFINER_FUNCTIONS ↔ migration drift"
        )
        raise

    if not _is_postgres():
        logger.info("ss17_security_definer: non-PG dialect — skipping (no-op)")
        return

    # The reporting.* DEFINER functions query tenant-scoped tables
    # (invoices, warranties, memberships). When this migration runs against
    # the CONTROL plane DB those tables don't exist — PG validates FROM
    # at CREATE FUNCTION time so the statements would fail. Skip the
    # function creation block; tenant-DB alembic runs will apply it.
    inspector = inspect(op.get_bind())
    existing_tables = set(inspector.get_table_names())
    required_tables = {"invoices", "warranties", "memberships"}
    if not required_tables.issubset(existing_tables):
        logger.info(
            "ss17_security_definer: tenant tables absent on this DB — "
            "skipping DEFINER function creation (tenant DB will apply)"
        )
        return

    try:
        op.execute(_BOOTSTRAP_SQL)
        op.execute(_FN_TENANT_AGGREGATE_REVENUE)
        op.execute(_FN_TENANT_INVOICE_COUNT)
        op.execute(_FN_TENANT_WARRANTY_ROLLUP)
        op.execute(_GRANT_SQL)
    except Exception:
        logger.exception("ss17_security_definer: upgrade failed")
        raise

    # Sanity: ensure the migration created exactly KNOWN_DEFINER_FUNCTIONS.
    assert set(_FUNCTION_NAMES) == set(KNOWN_DEFINER_FUNCTIONS), (
        "internal: _FUNCTION_NAMES drifted from KNOWN_DEFINER_FUNCTIONS "
        "between top-of-file and end-of-upgrade"
    )


def downgrade() -> None:
    if not _is_postgres():
        return
    try:
        op.execute(_DROP_SQL)
    except Exception:
        logger.exception("ss17_security_definer: downgrade failed")
        raise

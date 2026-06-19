"""SS-27 — cross_tenant_share + cross_tenant_share_acceptance tables,
plus the gdx_cross_tenant_access SECURITY DEFINER helper function.

TODO: chained on placeholder ``down_revision = "ss24_metering"``.
The supervisor will retarget this to the tip of the main chain at
end-of-sprint. Revision id uses the sprint slug so grep-find works.

Creates:
    - cross_tenant_share              — one sharer→sharee grant per resource
    - cross_tenant_share_acceptance   — sharee's single-use accept record
    - FUNCTION gdx_cross_tenant_access(sharee_tenant TEXT,
                                       resource_type TEXT,
                                       resource_id TEXT)
        RETURNS TEXT[] — capabilities granted (empty array if none)

The SECURITY DEFINER function is the PG-side twin of
:func:`gdx_dispatch.core.cross_tenant_sharing.check_share_grants_capability`; RLS-
aware handlers can call it when they need the grant list inside a
single query rather than via the Python helper. Hardened per the SS-17
P31 checklist:

    1. owned by ``reporting_owner`` (reuse of SS-17 role)
    2. explicit ``SET search_path = pg_catalog, public``
    3. caller args are validated (non-NULL) inside the body
    4. no dynamic SQL — only parameterized SELECT
    5. ``REVOKE ALL FROM PUBLIC; GRANT EXECUTE TO reporting_owner``
       (plus ``gdx_app`` when present, so the app runtime can call it)
    6. matching tests live in gdx_dispatch/tests/test_cross_tenant_sharing.py
       (Python-side parity; a PG smoke test lands at integration time)

Revision ID: ss27_cross_tenant_sharing
Down revision: TODO
"""
from __future__ import annotations

import logging

import sqlalchemy as sa
from alembic import op

logger = logging.getLogger(__name__)

revision = "ss27_cross_tenant_sharing"
down_revision = "ss24_metering"
branch_labels = None
depends_on = None


# ────────────────────────── SECURITY DEFINER SQL ──────────────────────────

_BOOTSTRAP_SQL = """
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'reporting_owner') THEN
        -- reporting_owner is provisioned by the SS-17 definer migration;
        -- re-create defensively so SS-27 can apply to a partial env.
        CREATE ROLE reporting_owner NOLOGIN;
    END IF;
END
$$;
"""

_FN_CROSS_TENANT_ACCESS = """
CREATE OR REPLACE FUNCTION gdx_cross_tenant_access(
    sharee_tenant TEXT,
    resource_type TEXT,
    resource_id TEXT
)
RETURNS TEXT[]
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $fn$
    -- Rule 3: reject NULL/empty args by yielding empty result.
    -- Rule 4: no dynamic SQL; all args parameterized.
    WITH active AS (
        SELECT s.capabilities
        FROM cross_tenant_share s
        JOIN cross_tenant_share_acceptance a ON a.share_id = s.id
        WHERE sharee_tenant IS NOT NULL
          AND sharee_tenant <> ''
          AND resource_type IS NOT NULL
          AND resource_type <> ''
          AND resource_id IS NOT NULL
          AND resource_id <> ''
          AND s.sharee_tenant_id = sharee_tenant
          AND s.resource_type = resource_type
          AND s.resource_id = resource_id
          AND s.revoked_at IS NULL
          AND (s.expires_at IS NULL OR s.expires_at > NOW())
    )
    SELECT COALESCE(
        (
            SELECT ARRAY(
                SELECT DISTINCT jsonb_array_elements_text(capabilities::jsonb)
                FROM active
            )
        ),
        ARRAY[]::TEXT[]
    );
$fn$;

ALTER FUNCTION gdx_cross_tenant_access(TEXT, TEXT, TEXT) OWNER TO reporting_owner;
REVOKE ALL ON FUNCTION gdx_cross_tenant_access(TEXT, TEXT, TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION gdx_cross_tenant_access(TEXT, TEXT, TEXT) TO reporting_owner;
"""

# gdx_app grant in a guarded block — dev envs lack the role.
_GRANT_SQL = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gdx_app') THEN
        GRANT EXECUTE ON FUNCTION gdx_cross_tenant_access(TEXT, TEXT, TEXT) TO gdx_app;
    END IF;
END
$$;
"""

_DROP_FN_SQL = """
DROP FUNCTION IF EXISTS gdx_cross_tenant_access(TEXT, TEXT, TEXT);
"""


def _is_postgres() -> bool:
    bind = op.get_bind()
    return bind.dialect.name == "postgresql"


# ────────────────────────── upgrade / downgrade ──────────────────────────


def upgrade() -> None:
    op.create_table(
        "cross_tenant_share",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("sharer_tenant_id", sa.String(length=64), nullable=False),
        sa.Column("sharee_tenant_id", sa.String(length=64), nullable=False),
        sa.Column("resource_type", sa.String(length=64), nullable=False),
        sa.Column("resource_id", sa.String(length=128), nullable=False),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("acceptance_token_hash", sa.String(length=128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by_identity_id", sa.String(length=64), nullable=True),
        sa.Column("created_by_identity_id", sa.String(length=64), nullable=False),
        sa.Column("shared_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "sharer_tenant_id",
            "sharee_tenant_id",
            "resource_type",
            "resource_id",
            name="uq_cts_active_share",
        ),
    )
    op.create_index("ix_cts_sharer", "cross_tenant_share", ["sharer_tenant_id"])
    op.create_index("ix_cts_sharee", "cross_tenant_share", ["sharee_tenant_id"])
    op.create_index(
        "ix_cts_resource",
        "cross_tenant_share",
        ["resource_type", "resource_id"],
    )

    op.create_table(
        "cross_tenant_share_acceptance",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("share_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("accepted_by_identity_id", sa.String(length=64), nullable=False),
        sa.Column("accepted_by_tenant_id", sa.String(length=64), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("share_id", name="uq_ctsa_one_accept"),
    )
    op.create_index(
        "ix_ctsa_share",
        "cross_tenant_share_acceptance",
        ["share_id"],
    )

    if not _is_postgres():
        logger.info(
            "ss27_cross_tenant_sharing: non-PG dialect — skipping DEFINER function"
        )
        return

    try:
        op.execute(_BOOTSTRAP_SQL)
        op.execute(_FN_CROSS_TENANT_ACCESS)
        op.execute(_GRANT_SQL)
    except Exception:
        logger.exception("ss27_cross_tenant_sharing: upgrade SECURITY DEFINER failed")
        raise


def downgrade() -> None:
    if _is_postgres():
        try:
            op.execute(_DROP_FN_SQL)
        except Exception:
            logger.exception("ss27_cross_tenant_sharing: downgrade FN drop failed")
            raise

    op.drop_index("ix_ctsa_share", table_name="cross_tenant_share_acceptance")
    op.drop_table("cross_tenant_share_acceptance")
    op.drop_index("ix_cts_resource", table_name="cross_tenant_share")
    op.drop_index("ix_cts_sharee", table_name="cross_tenant_share")
    op.drop_index("ix_cts_sharer", table_name="cross_tenant_share")
    op.drop_table("cross_tenant_share")

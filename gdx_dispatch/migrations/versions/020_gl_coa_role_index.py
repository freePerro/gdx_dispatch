"""GL Phase 1 S2 — unique active-system-role index on gl_accounts

``uq_gl_accounts_active_system_role`` makes "exactly one ACTIVE system
account per role" unrepresentable at the DB level and arbitrates the
concurrent-first-seed race (two racing ``seed_coa()`` transactions both
insert system rows; the loser's whole seed rolls back). Fresh DBs get the
index from ``create_all`` (it is declared on the model); this migration adds
it to existing DBs, where ``create_orm_tables()`` skips already-present
tables.

The other S2 additions need no migration: ``gl_settings`` is a NEW ORM table,
built by ``create_orm_tables()`` before Alembic runs (#41 ordering), and the
CoA seed itself is runtime data (``ensure_gl_seed()``), not schema.

Idempotent (IF NOT EXISTS / IF EXISTS). ``gl_accounts`` is empty until the
S4.5 settings page first calls the seed, so the unique index cannot meet
pre-existing duplicates; if it ever somehow did, CREATE UNIQUE INDEX fails
loudly — correct, that state must not be certified.

Revision ID: 020_gl_coa_role_index
Revises: 019_estimate_hide_line_prices
"""
from alembic import op

revision = "020_gl_coa_role_index"
down_revision = "019_estimate_hide_line_prices"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    # Loud precondition, same as migration 012: the ORM tables must exist.
    bind.exec_driver_sql(
        """
        DO $guard$
        BEGIN
          IF to_regclass('public.gl_accounts') IS NULL THEN
            RAISE EXCEPTION
              'gl_accounts missing: create_orm_tables() must run before migration 020 (#41 ordering)';
          END IF;
        END
        $guard$;
        """
    )

    bind.exec_driver_sql(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_gl_accounts_active_system_role
        ON gl_accounts (company_id, role)
        WHERE is_system AND active;
        """
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql(
        "DROP INDEX IF EXISTS uq_gl_accounts_active_system_role;"
    )

"""retire the standalone proposals table

The app carried two unrelated implementations of good/better/best:

  1. `proposals` — a flat table with six tier columns baked in
     (good_price/better_price/best_price + matching descriptions), its own
     /api/proposals CRUD router and its own Proposals nav page. No line
     items, no estimate number, no tax, no expiry, no public token, and its
     line-items endpoint was a ui_compat stub that returned an empty list
     and persisted nothing.
  2. `estimates.proposal_mode` + `proposal_tiers` — tiers that hang off a
     real Estimate, so they inherit numbering, tax, lines, the customer
     token, signature capture and job conversion.

(2) wins; (1) goes away. Nothing is migrated because there is nothing to
migrate: `proposals` held 0 rows on both production and the public demo
when this was written (checked 2026-08-10). The guard below refuses to drop
a non-empty table rather than destroying rows on some other install — an
operator hitting that error should export the rows and hand-carry them onto
estimates + proposal_tiers before re-running.

Revision ID: 061_retire_standalone_proposals
Revises: 060_service_labor_description
"""
from alembic import op

revision = "061_retire_standalone_proposals"
down_revision = "060_service_labor_description"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    # to_regclass returns NULL when the table was never created (fresh installs
    # build the schema from the models, which no longer declare Proposal).
    exists = bind.exec_driver_sql("SELECT to_regclass('public.proposals')").scalar()
    if not exists:
        return
    # ACCESS EXCLUSIVE before counting: without it the count and the DROP are a
    # TOCTOU pair, and a row inserted between them would be destroyed by the
    # very guard meant to prevent that. Alembic runs this inside a transaction,
    # so the lock is held until commit — i.e. through the DROP below.
    bind.exec_driver_sql("LOCK TABLE proposals IN ACCESS EXCLUSIVE MODE")
    rows = bind.exec_driver_sql("SELECT count(*) FROM proposals").scalar()
    if rows:
        raise RuntimeError(
            f"REFUSING TO DROP: `proposals` holds {rows} row(s). This migration only "
            "retires an empty legacy table. Export the rows and re-create them as "
            "estimates with proposal_tiers, then re-run."
        )
    bind.exec_driver_sql("DROP TABLE proposals")


def downgrade() -> None:
    # Recreated empty — the rows are gone by construction (upgrade only runs
    # against an empty table), so there is nothing to restore. This exists so a
    # downgrade past this point leaves the schema shape intact.
    bind = op.get_bind()
    bind.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS proposals (
            id UUID PRIMARY KEY,
            company_id VARCHAR(64) NOT NULL,
            customer_id UUID NULL,
            customer_name VARCHAR(200) NULL,
            title VARCHAR(300) NOT NULL,
            description TEXT NULL,
            good_price NUMERIC(12, 2) NOT NULL DEFAULT 0,
            better_price NUMERIC(12, 2) NOT NULL DEFAULT 0,
            best_price NUMERIC(12, 2) NOT NULL DEFAULT 0,
            good_description TEXT NULL,
            better_description TEXT NULL,
            best_description TEXT NULL,
            status VARCHAR(30) NOT NULL DEFAULT 'draft',
            chosen_tier VARCHAR(10) NULL,
            sent_at TIMESTAMPTZ NULL,
            accepted_at TIMESTAMPTZ NULL,
            created_by VARCHAR(200) NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            deleted_at TIMESTAMPTZ NULL
        )
        """
    )
    bind.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_proposals_company_id ON proposals (company_id)")
    bind.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_proposals_customer_id ON proposals (customer_id)")

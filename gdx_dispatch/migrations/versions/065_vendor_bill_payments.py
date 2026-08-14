"""Vendor bill payments — payment records, match provenance, paid-status backfill

Books-convergence Track 1 (docs/design/books-convergence-plan.md). Vendor
bills gain real payment records; the header ``status`` becomes derived
(write-through) from them, and the bank-match confirm can auto-record one.

Three pieces, all idempotent:

1. ``vendor_bill_payments`` — CREATE TABLE IF NOT EXISTS (050/052 pattern:
   the model is ORM-registered, so create_orm_tables() may or may not have
   built it first depending on import order; either order is a no-op here).
   Void-only lifecycle; ``match_id`` FK is the bank-match provenance seam.
   The partial unique index enforces at most one LIVE auto-recorded payment
   per bank match (idempotency backstop for double-confirm races).

2. ``bank_matches.created_expense_id`` — the unconfirm seam for
   create-expense-from-bank-line: unconfirm must find (and soft-delete or
   detach) exactly the expense the confirm created, nothing else.

3. Backfill (plan-audit BLOCKER 2): every bill historically Mark-paid via the
   status PATCH has zero payment children — deriving status without this
   would revert them all to open and flood /payables with phantom cash-out.
   Mint one synthetic payment per currently-paid bill: amount = bill total,
   paid_date NULL (the true date is unknown — NULL means exactly that),
   source 'manual', reference marks the provenance. NOT EXISTS guard makes
   re-runs no-ops.

Revision ID: 065_vendor_bill_payments
Revises: 064_simplefin_provider
"""
from alembic import op

revision = "065_vendor_bill_payments"
down_revision = "064_simplefin_provider"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    # vendor_invoices is ORM-created (never in migrations — the 024/025
    # docstrings are explicit), so its FK lands via a guarded DO-block: on
    # every deployed environment create_orm_tables runs before alembic (#41)
    # and the constraint applies; an alembic-only baseline DB (the CI check)
    # skips it instead of failing. bank_matches IS migration-created (052 <
    # 065), so its FK can be inline.
    bind.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS vendor_bill_payments (
            id                 uuid PRIMARY KEY,
            vendor_invoice_id  uuid NOT NULL,
            amount             numeric(12,2) NOT NULL,
            paid_date          date,
            source             varchar(20) NOT NULL DEFAULT 'manual',
            reference          varchar(200),
            match_id           uuid REFERENCES bank_matches(id),
            statement_id       uuid,
            created_by         varchar(100),
            created_at         timestamptz NOT NULL DEFAULT now(),
            voided_at          timestamptz,
            voided_by          varchar(100)
        );
        """
    )
    bind.exec_driver_sql(
        """
        DO $$ BEGIN
          -- Guard on ANY existing FK over this column, not a constraint
          -- name: when create_orm_tables built the table first (#41 boot
          -- order), the ORM's inline FK already exists under PG's auto
          -- name, and a name-scoped check would stack a duplicate.
          IF to_regclass('public.vendor_invoices') IS NOT NULL
             AND NOT EXISTS (
               SELECT 1 FROM pg_constraint c
               JOIN pg_attribute a
                 ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
               WHERE c.conrelid = 'vendor_bill_payments'::regclass
                 AND c.contype = 'f'
                 AND a.attname = 'vendor_invoice_id'
             ) THEN
            ALTER TABLE vendor_bill_payments
              ADD CONSTRAINT fk_vendor_bill_payments_invoice
              FOREIGN KEY (vendor_invoice_id) REFERENCES vendor_invoices(id)
              ON DELETE CASCADE;
          END IF;
        END $$;
        """
    )
    for stmt in (
        "CREATE INDEX IF NOT EXISTS ix_vendor_bill_payments_vendor_invoice_id "
        "ON vendor_bill_payments (vendor_invoice_id);",
        "CREATE INDEX IF NOT EXISTS ix_vendor_bill_payments_match_id "
        "ON vendor_bill_payments (match_id);",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_vendor_bill_payment_match "
        "ON vendor_bill_payments (match_id) "
        "WHERE match_id IS NOT NULL AND voided_at IS NULL;",
    ):
        bind.exec_driver_sql(stmt)

    bind.exec_driver_sql(
        "ALTER TABLE IF EXISTS bank_matches "
        "ADD COLUMN IF NOT EXISTS created_expense_id uuid"
    )

    # QB scheduled-pull health (plan-audit finding 10): success age drives the
    # loud-stale signal; reads come from QBClient.read_count per run.
    bind.exec_driver_sql(
        "ALTER TABLE IF EXISTS qb_sync_schedule "
        "ADD COLUMN IF NOT EXISTS last_success_at timestamptz"
    )
    bind.exec_driver_sql(
        "ALTER TABLE IF EXISTS qb_sync_schedule "
        "ADD COLUMN IF NOT EXISTS last_run_reads integer"
    )

    # Backfill: one synthetic full-total payment per historically-paid bill
    # with no live payment record. Python-generated UUIDs would need a row
    # loop; gen_random_uuid() is built-in from PG13 (prod is newer) — but
    # md5-derived UUIDs keep the statement portable AND deterministic, so a
    # re-run against a partially-applied state cannot mint duplicates even
    # without the NOT EXISTS guard.
    bind.exec_driver_sql(
        """
        DO $$ BEGIN
          IF to_regclass('public.vendor_invoices') IS NOT NULL THEN
            INSERT INTO vendor_bill_payments
                (id, vendor_invoice_id, amount, paid_date, source, reference, created_at)
            SELECT
                md5('vbp-backfill-' || vi.id::text)::uuid,
                vi.id,
                vi.total,
                NULL,
                'manual',
                'migrated: pre-existing paid status (date unknown)',
                now()
            FROM vendor_invoices vi
            WHERE vi.status = 'paid'
              AND vi.deleted_at IS NULL
              AND vi.total > 0
              AND NOT EXISTS (
                  SELECT 1 FROM vendor_bill_payments p
                  WHERE p.vendor_invoice_id = vi.id AND p.voided_at IS NULL
              );
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("ALTER TABLE IF EXISTS bank_matches DROP COLUMN IF EXISTS created_expense_id")
    bind.exec_driver_sql("DROP TABLE IF EXISTS vendor_bill_payments")

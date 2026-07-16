"""Vendor invoice dedup backstop — vendor_key + partial unique indexes

vendor-invoice-intake follow-up A (2026-07-16). Upload dedup was app-level only;
two concurrent uploads of the same new bill could both pass the app check and
both insert. This adds the DB backstop:

1. ``vendor_invoices.vendor_key`` — normalized vendor identity (str(vendor_id)
   when resolved, else the normalized raw name). Set by the service on insert.
2. partial UNIQUE index ``uq_vendor_invoice_key`` on (vendor_key, invoice_number)
   WHERE deleted_at IS NULL — the concurrent-insert backstop; partial so a
   voided/deleted bill can be re-imported.
3. partial UNIQUE index ``uq_vendor_invoice_document`` on document_id
   WHERE document_id IS NOT NULL AND deleted_at IS NULL — one invoice per doc.

``vendor_invoices`` is ORM-created (no baseline row); on a fresh DB
create_orm_tables() already builds the column + indexes from the model, so this
migration is a guarded no-op there (IF NOT EXISTS). It only does real work if
Phase 1 was deployed BEFORE this — then it adds the column, backfills it (SQL
approximation of the service's normalize: casefold + strip punctuation), and
creates the indexes.

Revision ID: 025_vendor_invoice_dedup_index
Revises: 024_vendor_invoice_intake
"""
from alembic import op

revision = "025_vendor_invoice_dedup_index"
down_revision = "024_vendor_invoice_intake"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        DO $$ BEGIN
          IF to_regclass('public.vendor_invoices') IS NOT NULL THEN
            ALTER TABLE vendor_invoices
              ADD COLUMN IF NOT EXISTS vendor_key VARCHAR(200) NOT NULL DEFAULT '';
            -- Backfill existing rows: resolved vendor -> its id, else normalized name.
            UPDATE vendor_invoices
               SET vendor_key = CASE
                     WHEN vendor_id IS NOT NULL THEN vendor_id::text
                     ELSE btrim(regexp_replace(lower(coalesce(vendor_name_raw, '')),
                                               '[^a-z0-9]+', ' ', 'g'))
                   END
             WHERE vendor_key = '' OR vendor_key IS NULL;
            -- Self-heal before the UNIQUE index: if Phase 1 ran on prod and the
            -- app-only check let a concurrent duplicate through, a surviving
            -- (vendor_key, invoice_number) pair would make CREATE UNIQUE INDEX
            -- abort the deploy. Soft-delete the later dupes (keep the earliest
            -- by created_at, id as tiebreak) so the index can be created.
            UPDATE vendor_invoices v
               SET deleted_at = now()
             WHERE v.deleted_at IS NULL
               AND EXISTS (
                 SELECT 1 FROM vendor_invoices w
                  WHERE w.deleted_at IS NULL
                    AND w.vendor_key = v.vendor_key
                    AND w.invoice_number = v.invoice_number
                    AND (w.created_at < v.created_at
                         OR (w.created_at = v.created_at AND w.id < v.id)));
            -- Same self-heal for the document_id index.
            UPDATE vendor_invoices v
               SET deleted_at = now()
             WHERE v.deleted_at IS NULL
               AND v.document_id IS NOT NULL
               AND EXISTS (
                 SELECT 1 FROM vendor_invoices w
                  WHERE w.deleted_at IS NULL
                    AND w.document_id = v.document_id
                    AND (w.created_at < v.created_at
                         OR (w.created_at = v.created_at AND w.id < v.id)));
            CREATE UNIQUE INDEX IF NOT EXISTS uq_vendor_invoice_key
              ON vendor_invoices (vendor_key, invoice_number)
              WHERE deleted_at IS NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS uq_vendor_invoice_document
              ON vendor_invoices (document_id)
              WHERE document_id IS NOT NULL AND deleted_at IS NULL;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        DO $$ BEGIN
          IF to_regclass('public.vendor_invoices') IS NOT NULL THEN
            DROP INDEX IF EXISTS uq_vendor_invoice_document;
            DROP INDEX IF EXISTS uq_vendor_invoice_key;
            ALTER TABLE vendor_invoices DROP COLUMN IF EXISTS vendor_key;
          END IF;
        END $$;
        """
    )

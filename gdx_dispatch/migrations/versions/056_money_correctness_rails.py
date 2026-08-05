"""money correctness rails — totals_locked + payment reference uniqueness

Money audit 2026-08-04 (docs/design/money-audit-2026-08-04.md). Two rails that
make whole bug classes impossible rather than fixing instances of them:

1. ``invoices.totals_locked`` (M1) — QuickBooks-imported invoices carry a
   correct header total and a lossy line set (the import's create path wrote
   every QBO Line, including the SubTotalLine that QB had already folded into
   TotalAmt). ``_recalculate_invoice`` derives total from the lines, so
   recording the settling payment REWROTE a $1,471.84 invoice to $2,943.68 and
   re-opened it — proven in test_zz_money_correctness_probe.py. Locked invoices
   recalc balance/status only; their imported header total is the truth.

   Backfilled true for rows the importer stamped ``notes = 'Imported from
   QuickBooks'``, which is the only durable marker on those rows.

2. Partial unique index on ``payments(invoice_id, reference)`` (M2) — payment
   idempotency was a SELECT-then-INSERT that two concurrent transactions both
   pass, and ``record_payment`` had no dedupe at all, so an ordinary
   double-click recorded the charge twice. The app-level guards land in the
   same change; this index is the backstop that cannot be raced.

   Partial (``WHERE reference IS NOT NULL AND voided_at IS NULL``): reference
   is optional, so cash payments legitimately share a NULL, and uniqueness
   applies to LIVE payments only — a voided row keeps its reference as history
   and must not block re-recording the real charge.

Pre-existing duplicates would make the index creation fail. They are collapsed
first: for each (invoice_id, reference) group, keep the earliest row and void
the rest (voided payments stay as history and stop counting toward the
balance), so no cash silently disappears from the audit trail.

Revision ID: 056_money_correctness_rails
Revises: 055_job_not_billable
"""
from alembic import op

revision = "056_money_correctness_rails"
down_revision = "055_job_not_billable"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    # -- 0. tax rates need 6 fraction digits, not 4 -----------------------
    # Numeric(6,4) CANNOT represent Minnesota's 7.375%: 0.07375 lands as
    # 0.0737 on the DB round-trip. The creating request computes tax from the
    # in-memory Decimal and gets it right ($73.75 on $1,000); every recalc
    # afterwards reads the truncated rate and gets it wrong ($110.55 instead of
    # $110.63 on $1,500). So an invoice's tax silently CHANGED between
    # creation and the first line edit. Found by the money-audit probes
    # 2026-08-04 — reading the code never would have shown it.
    #
    # Widening is lossless for stored values and safe to run before the
    # backfill in step 3, which depends on this precision to derive rates.
    for table, column in (
        ("invoices", "tax_rate"),
        ("estimates", "tax_rate"),
        ("tax_jurisdictions", "rate"),
        ("tax_config", "default_rate"),
    ):
        bind.exec_driver_sql(
            f"ALTER TABLE IF EXISTS {table} "
            f"ALTER COLUMN {column} TYPE numeric(9,6)"
        )

    # -- 1. totals_locked -------------------------------------------------
    bind.exec_driver_sql(
        "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS "
        "totals_locked boolean NOT NULL DEFAULT false"
    )
    # QB-imported rows: header total is authoritative, local lines are lossy.
    bind.exec_driver_sql(
        """
        UPDATE invoices
           SET totals_locked = true
         WHERE totals_locked = false
           AND notes = 'Imported from QuickBooks'
        """
    )

    # -- 2. payment reference uniqueness ----------------------------------
    # Collapse any existing duplicates first (keep earliest, void the rest).
    # Voiding rather than deleting: _recalculate_invoice already excludes
    # voided payments, so the balance corrects itself while history survives.
    bind.exec_driver_sql(
        """
        WITH ranked AS (
            SELECT id,
                   row_number() OVER (
                       PARTITION BY invoice_id, reference
                       ORDER BY created_at, id
                   ) AS rn
              FROM payments
             WHERE reference IS NOT NULL
               AND voided_at IS NULL
        )
        UPDATE payments p
           SET voided_at = now()
          FROM ranked r
         WHERE p.id = r.id
           AND r.rn > 1
        """
    )
    # `voided_at IS NULL` is part of the predicate, not just `reference IS NOT
    # NULL`. Two reasons, both learned by running this against Postgres:
    #   1. the collapse above VOIDS duplicates rather than deleting them, and a
    #      voided row still carries the same (invoice_id, reference) — so
    #      without this the index creation fails on exactly the data the
    #      collapse was meant to clear;
    #   2. a wrongly-reversed payment (late charge.failed on a retried intent)
    #      must be re-recordable when the real `succeeded` is redelivered.
    #      Uniqueness applies to LIVE payments; voided rows are history.
    bind.exec_driver_sql(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_payments_invoice_reference "
        "ON payments (invoice_id, reference) "
        "WHERE reference IS NOT NULL AND voided_at IS NULL"
    )

    # -- 3. heal the frozen-tax shape (M9) --------------------------------
    # An invoice with tax_amount > 0 but tax_rate NULL sits on
    # _recalculate_invoice's legacy branch: the flat tax is preserved verbatim
    # while the subtotal moves, so editing a line silently under-collects
    # (subtotal 1000 -> 1500 with tax stuck at 73.75). The mobile and one-click
    # creation paths wrote that shape; both now store the rate.
    #
    # Derive the rate for existing rows HERE rather than at recalc time: right
    # now tax_amount and the taxable subtotal are still consistent, so the
    # quotient is the real rate. At recalc time the lines may already have
    # changed, and the same arithmetic would bake in a wrong rate.
    #
    # Only rows where the quotient is a sane rate (0 < r <= 0.25) are touched;
    # anything else is a hand-entered tax we must not reinterpret.
    bind.exec_driver_sql(
        """
        WITH taxable AS (
            SELECT i.id,
                   i.tax_amount,
                   COALESCE(SUM(l.line_total) FILTER (
                       WHERE COALESCE(l.taxable, true) AND l.deleted_at IS NULL
                   ), 0) AS taxable_subtotal
              FROM invoices i
              LEFT JOIN invoice_lines l ON l.invoice_id = i.id
             WHERE i.tax_rate IS NULL
               AND i.tax_amount > 0
               AND i.totals_locked = false
             GROUP BY i.id, i.tax_amount
        )
        UPDATE invoices i
           SET tax_rate = ROUND(t.tax_amount / t.taxable_subtotal, 6)
          FROM taxable t
         WHERE i.id = t.id
           AND t.taxable_subtotal > 0
           AND (t.tax_amount / t.taxable_subtotal) > 0
           AND (t.tax_amount / t.taxable_subtotal) <= 0.25
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DROP INDEX IF EXISTS uq_payments_invoice_reference")
    bind.exec_driver_sql("ALTER TABLE invoices DROP COLUMN IF EXISTS totals_locked")
    # The duplicate-collapsing void is deliberately NOT reversed — re-creating
    # known-duplicate payment rows would put phantom money back on the books.

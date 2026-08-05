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

Step 3 heals rows carrying tax with no rate, but ONLY where the derived rate
agrees with the tenant's configured default — a production dry run showed the
naive version would have written a nonsense rate onto 71 real invoices.

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
    # while the subtotal moves, so editing a line silently under-collects. The
    # mobile and one-click creation paths wrote that shape; both now store the
    # rate. This heals the rows already in that state.
    #
    # ANCHORED to the tenant's configured rate, and this matters. A dry run
    # against production (326 invoices, 200 in this shape) showed the naive
    # rule -- accept any quotient in (0, 0.25] -- would have healed 116 rows
    # correctly and written a NONSENSE rate onto 66 others.
    #
    # Those 66 are provably unrecoverable, not merely suspicious. Minnesota's
    # STATE rate alone is 6.875% (MN DOR; destination-based, local options add
    # on top), so no taxable MN sale can imply a rate BELOW it -- yet these
    # rows imply 0.24% to 6.42%. Spot-checking shows why: the tax was computed
    # at the real rate against a SUBSET of the invoice (11 of the 66 match a
    # single line's total exactly at 7.375%), but the per-line `taxable` flags
    # no longer record which subset. The base is lost, so the rate cannot be
    # recovered by division -- and a wrong rate is WORSE than the frozen tax
    # it replaces: it silently re-prices every future edit.
    #
    # The anchor deliberately UNDER-heals: it also skips ~5 rows at the bare
    # 6.875% state rate (a valid MN rate for a location with no local tax,
    # just not this tenant's configured default). Those keep today's behavior,
    # which is not a regression. Under-healing is safe; over-healing writes a
    # wrong rate onto a real invoice.
    #
    # So: only heal a row whose derived rate agrees with the tenant's
    # configured default. Everything else keeps exactly today's behavior --
    # frozen flat tax, no regression, and an operator can fix it deliberately.
    #
    # The value written is the DERIVED quotient, not the configured rate, so
    # the invoice's existing tax_amount is reproduced to the cent on the next
    # recalc. Healing must not itself move any number.
    bind.exec_driver_sql(
        """
        WITH cfg AS (
            SELECT default_rate FROM tax_config
             WHERE default_rate IS NOT NULL AND default_rate > 0
             LIMIT 1
        ), taxable AS (
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
               AND i.deleted_at IS NULL
             GROUP BY i.id, i.tax_amount
        )
        UPDATE invoices i
           SET tax_rate = ROUND(t.tax_amount / t.taxable_subtotal, 6)
          FROM taxable t, cfg
         WHERE i.id = t.id
           AND t.taxable_subtotal > 0
           -- within 5 basis points of the configured rate, or leave it alone
           AND abs((t.tax_amount / t.taxable_subtotal) - cfg.default_rate) < 0.0005
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DROP INDEX IF EXISTS uq_payments_invoice_reference")
    bind.exec_driver_sql("ALTER TABLE invoices DROP COLUMN IF EXISTS totals_locked")
    # The duplicate-collapsing void is deliberately NOT reversed — re-creating
    # known-duplicate payment rows would put phantom money back on the books.

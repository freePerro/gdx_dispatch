"""One-shot tenant-plane migration: monthly_budgets table.

Sprint monthly-budget (2026-05-24). Creates:
- monthly_budgets table (id, year, month, qb_account_id, account_name,
  amount, line_type, pct_of_revenue, source, is_locked, notes,
  created_at, updated_at)
- UNIQUE(year, month, qb_account_id)
- Index on (year, month)

Idempotent: every statement is IF NOT EXISTS or DROP-IF-EXISTS + ADD.
Tenant-plane schema is ORM-managed via create_all (no column adds for
existing tenants), so this one-shot walks every tenant and runs the
DDL. Same pattern as migrate_jobs_location_id.

Usage (inside the app container):
    python -m gdx_dispatch.tools.migrate_monthly_budgets
"""
from __future__ import annotations

import logging
import sys

from sqlalchemy import create_engine, text

from gdx_dispatch.core.database import SessionLocal, _decrypt_db_url


DDL = [
    """
    CREATE TABLE IF NOT EXISTS qb_pnl_monthly (
        id VARCHAR(36) PRIMARY KEY,
        year INTEGER NOT NULL,
        month INTEGER NOT NULL,
        qb_account_id VARCHAR(64) NOT NULL,
        account_name VARCHAR(300),
        account_type VARCHAR(100),
        amount NUMERIC(14, 2) NOT NULL DEFAULT 0,
        synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "ALTER TABLE qb_pnl_monthly DROP CONSTRAINT IF EXISTS uq_qb_pnl_monthly_year_month_account",
    (
        "ALTER TABLE qb_pnl_monthly "
        "ADD CONSTRAINT uq_qb_pnl_monthly_year_month_account "
        "UNIQUE (year, month, qb_account_id)"
    ),
    "CREATE INDEX IF NOT EXISTS ix_qb_pnl_monthly_year_month ON qb_pnl_monthly(year, month)",
    """
    CREATE TABLE IF NOT EXISTS monthly_budgets (
        id VARCHAR(36) PRIMARY KEY,
        year INTEGER NOT NULL,
        month INTEGER NOT NULL,
        qb_account_id VARCHAR(64) NOT NULL,
        account_name VARCHAR(300),
        amount NUMERIC(14, 2) NOT NULL DEFAULT 0,
        line_type VARCHAR(20) NOT NULL DEFAULT 'fixed',
        pct_of_revenue NUMERIC(6, 4),
        source VARCHAR(20) NOT NULL DEFAULT 'user',
        is_locked BOOLEAN NOT NULL DEFAULT FALSE,
        notes TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    # UNIQUE constraint — drop-add idempotent. CONSTRAINT IF NOT EXISTS
    # is not supported by Postgres, so drop-then-add under a name we own.
    "ALTER TABLE monthly_budgets DROP CONSTRAINT IF EXISTS uq_monthly_budget_year_month_account",
    (
        "ALTER TABLE monthly_budgets "
        "ADD CONSTRAINT uq_monthly_budget_year_month_account "
        "UNIQUE (year, month, qb_account_id)"
    ),
    # CHECK constraints — same drop-add idempotent pattern.
    "ALTER TABLE monthly_budgets DROP CONSTRAINT IF EXISTS ck_monthly_budget_line_type",
    (
        "ALTER TABLE monthly_budgets "
        "ADD CONSTRAINT ck_monthly_budget_line_type "
        "CHECK (line_type IN ('fixed', 'variable', 'percent_of_revenue'))"
    ),
    "ALTER TABLE monthly_budgets DROP CONSTRAINT IF EXISTS ck_monthly_budget_source",
    (
        "ALTER TABLE monthly_budgets "
        "ADD CONSTRAINT ck_monthly_budget_source "
        "CHECK (source IN ('auto_seed', 'user'))"
    ),
    "ALTER TABLE monthly_budgets DROP CONSTRAINT IF EXISTS ck_monthly_budget_month_range",
    (
        "ALTER TABLE monthly_budgets "
        "ADD CONSTRAINT ck_monthly_budget_month_range "
        "CHECK (month BETWEEN 1 AND 12)"
    ),
    "CREATE INDEX IF NOT EXISTS ix_monthly_budgets_year_month ON monthly_budgets(year, month)",
    # Sprint monthly-budget-history (2026-05-24) — Cash vs Accrual setting
    # for the QBO ProfitAndLoss API call. Default Accrual matches the
    # router's previous hardcoded behavior. Idempotent.
    "ALTER TABLE app_settings ADD COLUMN IF NOT EXISTS qb_accounting_method VARCHAR(20) NOT NULL DEFAULT 'Accrual'",
]


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger(__name__)

    with SessionLocal() as cdb:
        rows = cdb.execute(
            text("SELECT id, slug, db_url_enc FROM tenants WHERE db_url_enc IS NOT NULL")
        ).all()

    failures = 0
    for tid, slug, enc in rows:
        try:
            url = _decrypt_db_url(enc)
        except Exception as exc:  # noqa: BLE001
            log.error("decrypt_failed tenant=%s slug=%s err=%s", tid, slug, exc)
            failures += 1
            continue
        try:
            eng = create_engine(url, pool_pre_ping=True)
            with eng.begin() as conn:
                for stmt in DDL:
                    conn.execute(text(stmt))
            eng.dispose()
            log.info("migrated tenant=%s slug=%s", tid, slug)
        except Exception as exc:  # noqa: BLE001
            log.error("migration_failed tenant=%s slug=%s err=%s", tid, slug, exc)
            failures += 1

    log.info("done — %d ok / %d failed", len(rows) - failures, failures)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

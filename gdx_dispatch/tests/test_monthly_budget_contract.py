"""Regression brake — Sprint monthly-budget (2026-05-24).

Pins the cross-stack contract for the monthly budget feature. Mostly
source-scan (same rationale as test_dispatch_capacity_contract.py:
spinning up the full app + tenant DB to verify "does this column exist"
is overkill, and a source scan catches the class-of-bug we worry about
— a refactor that drops a field or unmounts the router).

Covers:
  • MonthlyBudget + QBPnlMonthly ORM column declarations
  • Migration script declares all required DDL statements (idempotent)
  • Budgets router mounted in gdx_dispatch/app.py
  • All expected endpoints exist with correct permission gates
  • SQLite-portable composite IN (no Postgres-only tuple IN)
  • Classifier behavior (keywords + CV fallback)
  • P&L parser handles the QBO Rows tree correctly
  • Auto-seed math snaps to nearest $10
"""
from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


# ─── ORM column declarations ─────────────────────────────────────────


def test_monthly_budget_model_columns():
    src = _read("gdx_dispatch/models/tenant_models.py")
    assert 'class MonthlyBudget(Base):' in src
    assert '__tablename__ = "monthly_budgets"' in src
    # Required columns with the right shapes
    for col in (
        "year", "month", "qb_account_id", "account_name", "amount",
        "line_type", "pct_of_revenue", "source", "is_locked", "notes",
        "created_at", "updated_at",
    ):
        assert col in src, f"MonthlyBudget missing column {col}"
    # UNIQUE on (year, month, qb_account_id) — keyed naming so we can drop-add idempotently
    assert "uq_monthly_budget_year_month_account" in src


def test_qb_pnl_monthly_model_columns():
    src = _read("gdx_dispatch/models/tenant_models.py")
    assert 'class QBPnlMonthly(Base):' in src
    assert '__tablename__ = "qb_pnl_monthly"' in src
    for col in ("year", "month", "qb_account_id", "account_name",
                "account_type", "amount", "synced_at"):
        assert col in src, f"QBPnlMonthly missing column {col}"
    assert "uq_qb_pnl_monthly_year_month_account" in src


# ─── Migration script ─────────────────────────────────────────────────


def test_migration_script_declares_all_ddl():
    src = _read("gdx_dispatch/tools/migrate_monthly_budgets.py")
    # Idempotent CREATEs
    assert "CREATE TABLE IF NOT EXISTS qb_pnl_monthly" in src
    assert "CREATE TABLE IF NOT EXISTS monthly_budgets" in src
    # Drop-add UNIQUEs (idempotent pattern)
    for cname in (
        "uq_qb_pnl_monthly_year_month_account",
        "uq_monthly_budget_year_month_account",
    ):
        assert f"DROP CONSTRAINT IF EXISTS {cname}" in src
        assert f"ADD CONSTRAINT {cname}" in src
    # CHECK constraints
    for ck in (
        "ck_monthly_budget_line_type",
        "ck_monthly_budget_source",
        "ck_monthly_budget_month_range",
    ):
        assert ck in src
    # Indexes
    assert "ix_monthly_budgets_year_month" in src
    assert "ix_qb_pnl_monthly_year_month" in src


def test_migration_script_walks_every_tenant_db():
    src = _read("gdx_dispatch/tools/migrate_monthly_budgets.py")
    assert "SELECT id, slug, db_url_enc FROM tenants WHERE db_url_enc IS NOT NULL" in src
    assert "_decrypt_db_url(enc)" in src


def test_migration_ddl_executes_against_sqlite_after_pg_to_sqlite_substitution():
    """Auditor 2026-05-24: source-scan grep won't catch a SQL syntax typo.
    Run the DDL against an ephemeral sqlite DB after substituting the
    Postgres-only tokens. Catches: misplaced commas, unknown identifiers,
    duplicate column names, constraint refs to non-existent columns.
    The semantic difference (TZ vs no-TZ on TIMESTAMP) is irrelevant for
    syntax validation."""
    import re as _re
    import sqlalchemy as _sa

    from gdx_dispatch.tools.migrate_monthly_budgets import DDL

    eng = _sa.create_engine("sqlite:///:memory:")
    with eng.begin() as conn:
        # Pre-create app_settings so the ADD COLUMN IF NOT EXISTS statement
        # has a target. In prod this table is always present (ORM-managed
        # via create_all on tenant signup); the migration only adds new
        # columns to it.
        conn.execute(_sa.text(
            "CREATE TABLE app_settings (id VARCHAR(36) PRIMARY KEY, name VARCHAR(200))"
        ))
        for stmt in DDL:
            sqlite_stmt = stmt
            # Postgres TIMESTAMPTZ → sqlite TIMESTAMP
            sqlite_stmt = _re.sub(r"\bTIMESTAMPTZ\b", "TIMESTAMP", sqlite_stmt)
            # Postgres NOW() → sqlite CURRENT_TIMESTAMP
            sqlite_stmt = _re.sub(r"\bNOW\(\)", "CURRENT_TIMESTAMP", sqlite_stmt)
            # sqlite doesn't support `ADD COLUMN IF NOT EXISTS` — strip the
            # IF NOT EXISTS clause (the test only runs once so duplicate
            # adds don't apply).
            sqlite_stmt = _re.sub(
                r"ADD COLUMN IF NOT EXISTS", "ADD COLUMN", sqlite_stmt,
            )
            # sqlite doesn't enforce DROP CONSTRAINT; skip those statements
            # since they're no-ops on a fresh CREATE anyway.
            if "DROP CONSTRAINT" in sqlite_stmt or "ADD CONSTRAINT" in sqlite_stmt:
                # ADD CONSTRAINT ALTER TABLE is also unsupported by sqlite;
                # but the CHECK + UNIQUE constraints we care about are
                # captured in the inline CREATE TABLE clauses below in prod.
                # For this syntax-check pass, we just want to verify no
                # statement uses an unknown column or has a paren mismatch.
                continue
            conn.execute(_sa.text(sqlite_stmt))

    # Confirm both tables actually got created with the columns we expect.
    insp = _sa.inspect(eng)
    assert "monthly_budgets" in insp.get_table_names()
    assert "qb_pnl_monthly" in insp.get_table_names()
    cols = {c["name"] for c in insp.get_columns("monthly_budgets")}
    assert {"id", "year", "month", "qb_account_id", "amount",
            "line_type", "pct_of_revenue", "source", "is_locked"}.issubset(cols)


# ─── Router mount + endpoints ─────────────────────────────────────────


def test_budgets_router_mounted_in_app():
    src = _read("gdx_dispatch/app.py")
    assert "from gdx_dispatch.routers import budgets as budgets_router" in src
    assert "app.include_router(budgets_router.router" in src


def test_budgets_router_endpoints():
    src = _read("gdx_dispatch/routers/budgets.py")
    # Verb + path pairs we promise to the UI
    expected = [
        ('@router.get("", ',                         'list_budget_for_month'),
        ('@router.get("/grid"',                      'budget_grid'),
        ('@router.get("/trends"',                    'spending_trends'),
        ('@router.post("", ',                        'create_budget_line'),
        ('@router.patch("/{line_id}"',               'update_budget_line'),
        ('@router.delete("/{line_id}"',              'delete_budget_line'),
        ('@router.post("/seed"',                     'seed_budget'),
        ('@router.post("/{line_id}/lock"',           'lock_line'),
        ('@router.post("/{line_id}/unlock"',         'unlock_line'),
        ('@router.post("/classify"',                 'classify_accounts'),
        ('@router.post("/refresh-actuals"',          'refresh_actuals'),
    ]
    for decorator_prefix, fn_name in expected:
        assert decorator_prefix in src, f"missing route: {decorator_prefix}"
        assert f"def {fn_name}(" in src, f"missing handler: {fn_name}"


def test_budgets_router_role_gates():
    src = _read("gdx_dispatch/routers/budgets.py")
    # Reads gated on accounting.read; writes on accounting.write
    assert src.count('require_permission("accounting.read")') >= 3
    assert src.count('require_permission("accounting.write")') >= 6


def test_budgets_router_uses_expanding_bindparam():
    """The 2026-05-19 customers.py incident: ANY(:ids) was Postgres-only
    and 500'd sqlite tenant fixtures. We pin the portable pattern."""
    src = _read("gdx_dispatch/routers/budgets.py")
    assert "bindparam(\"keys\", expanding=True)" in src
    # And no Postgres-only ANY() in this router
    assert " ANY(:" not in src


def test_budgets_router_does_not_filter_by_tenant_id():
    """Tenant-plane isolation is the CONNECTION (get_db). Adding
    `WHERE tenant_id = :tid` in a tenant-plane router is the 2026-04-22
    bug class. Pin it gone in budgets."""
    src = _read("gdx_dispatch/routers/budgets.py")
    assert " tenant_id = :" not in src
    assert " WHERE tenant_id" not in src


# ─── Classifier ───────────────────────────────────────────────────────


def test_classifier_keyword_fixed():
    from gdx_dispatch.routers.budgets import _classify_one
    r = _classify_one("Rent expense", [Decimal("2500")] * 6)
    assert r["proposed"] == "fixed"
    assert "rent" in r["reason"]


def test_classifier_keyword_variable():
    from gdx_dispatch.routers.budgets import _classify_one
    r = _classify_one("Job Materials", [Decimal("1000")] * 6)
    assert r["proposed"] == "variable"
    assert "material" in r["reason"]


def test_classifier_cv_high_is_variable():
    from gdx_dispatch.routers.budgets import _classify_one
    r = _classify_one("Misc account no kw match", [Decimal(x) for x in (100, 500, 50, 900, 200, 800)])
    assert r["proposed"] == "variable"
    assert "CV=" in r["reason"]


def test_classifier_cv_low_is_fixed():
    from gdx_dispatch.routers.budgets import _classify_one
    r = _classify_one("Misc account no kw match", [Decimal(x) for x in (100, 110, 95, 105, 102, 98)])
    assert r["proposed"] == "fixed"
    assert "CV=" in r["reason"]


def test_revenue_basis_helper_signature_takes_year_month():
    """Auditor 2026-05-24 caught the prior helper always used 'next 30 days
    from today' regardless of which month was viewed. The corrected helper
    MUST accept year+month so it can route past months to actuals and
    current+future months to projection."""
    import inspect
    from gdx_dispatch.routers.budgets import _revenue_basis_for_month
    sig = inspect.signature(_revenue_basis_for_month)
    assert "year" in sig.parameters
    assert "month" in sig.parameters


def test_revenue_basis_uses_pnl_income_sql_for_past_months():
    """Pin the SQL shape that distinguishes past vs current/future months.
    Past months must SUM qb_pnl_monthly Income rows, not call revenue_projection."""
    src = _read("gdx_dispatch/routers/budgets.py")
    assert "SUM(amount)" in src
    assert "account_type IN ('Income', 'Other Income')" in src


def test_seed_records_no_overwrites_because_overwrite_was_removed():
    """Sprint monthly-budget-history (2026-05-24) removed overwrite entirely
    rather than carry forward the audit-restore complexity. Pin the new
    behavior: seed creates new lines and skips existing ones — period."""
    src = _read("gdx_dispatch/routers/budgets.py")
    # The "overwrites" / "prior_amount" recovery machinery should be gone
    # since there is no overwrite to recover from.
    assert '"prior_amount"' not in src
    assert '"overwrites"' not in src
    # And the new behavior is reflected in the audit log details:
    assert '"skipped_existing"' in src


def test_refresh_actuals_does_not_leak_exception_string():
    """Auditor 2026-05-24: str(exc) in HTTPException leaked internal details.
    Prod path must NOT pipe the raw exception text into the response."""
    src = _read("gdx_dispatch/routers/budgets.py")
    # The bad pattern is `f"QuickBooks ProfitAndLoss fetch failed: {exc}"`
    # — verify it's gone.
    assert "f\"QuickBooks ProfitAndLoss fetch failed: {exc}\"" not in src
    # Generic message present
    assert "See server logs for details" in src


def test_pnl_upsert_commits_and_persists():
    """Walking prod 2026-05-24 caught this: a prior version used
    ``with db.begin_nested():`` and the savepoint release did NOT commit
    the outer FastAPI session, so the endpoint returned 200 with the
    right counts but ZERO rows were actually persisted (rolled back when
    the request session closed). This test calls upsert + queries the
    table on a SEPARATE connection to prove the rows survived commit."""
    import sqlalchemy as _sa
    from gdx_dispatch.modules.quickbooks.pnl import upsert_pnl_rows

    eng = _sa.create_engine("sqlite:///:memory:")
    # Create the table (use portable DDL).
    with eng.begin() as conn:
        conn.execute(_sa.text(
            "CREATE TABLE qb_pnl_monthly ("
            "id VARCHAR(36) PRIMARY KEY, "
            "year INTEGER NOT NULL, month INTEGER NOT NULL, "
            "qb_account_id VARCHAR(64) NOT NULL, "
            "account_name VARCHAR(300), account_type VARCHAR(100), "
            "amount NUMERIC(14,2) NOT NULL DEFAULT 0, "
            "synced_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        ))

    SessionLocal = _sa.orm.sessionmaker(bind=eng)

    # Insert via the function (simulates the FastAPI dependency-managed session).
    db = SessionLocal()
    try:
        parsed = {
            (1, "60"): {"amount": Decimal("2500"), "account_name": "Rent", "account_type": "Expense"},
            (2, "60"): {"amount": Decimal("2500"), "account_name": "Rent", "account_type": "Expense"},
            (1, "70"): {"amount": Decimal("800"),  "account_name": "Parts", "account_type": "Cost of Goods Sold"},
        }
        result = upsert_pnl_rows(db, year=2026, parsed=parsed)
        assert result["inserted"] == 3
    finally:
        db.close()  # Simulates the request lifecycle closing the session.

    # Open a FRESH connection — if the rows were rolled back, this returns 0.
    with eng.connect() as conn:
        n = conn.execute(_sa.text("SELECT COUNT(*) FROM qb_pnl_monthly")).scalar()
    assert n == 3, f"upsert returned inserted=3 but only {n} rows persisted after session close"


def test_pnl_upsert_rollback_on_mid_loop_failure():
    """If an INSERT fails mid-loop, the function must rollback so the
    DELETE doesn't leak — prior year's data survives the failed refresh."""
    import sqlalchemy as _sa
    import pytest as _pytest
    from gdx_dispatch.modules.quickbooks.pnl import upsert_pnl_rows

    eng = _sa.create_engine("sqlite:///:memory:")
    with eng.begin() as conn:
        conn.execute(_sa.text(
            "CREATE TABLE qb_pnl_monthly ("
            "id VARCHAR(36) PRIMARY KEY, "
            "year INTEGER NOT NULL, month INTEGER NOT NULL, "
            "qb_account_id VARCHAR(64) NOT NULL, "
            "account_name VARCHAR(300), account_type VARCHAR(100), "
            "amount NUMERIC(14,2) NOT NULL DEFAULT 0, "
            "synced_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "UNIQUE(year, month, qb_account_id))"
        ))
        # Seed last year's data — must survive the failed 2026 refresh.
        conn.execute(_sa.text(
            "INSERT INTO qb_pnl_monthly (id, year, month, qb_account_id, account_name, amount) "
            "VALUES ('seed', 2025, 1, '99', 'Existing', 100)"
        ))
        # Seed 2026 data that we'd wipe in a successful refresh.
        conn.execute(_sa.text(
            "INSERT INTO qb_pnl_monthly (id, year, month, qb_account_id, account_name, amount) "
            "VALUES ('seed2', 2026, 1, '88', 'Old 2026 row', 50)"
        ))

    SessionLocal = _sa.orm.sessionmaker(bind=eng)
    db = SessionLocal()
    # Wrap execute() to raise on the Nth call so the loop fails mid-way
    # AFTER the DELETE has run. This simulates a real-world INSERT
    # failure (e.g. constraint violation, connection drop).
    orig_execute = db.execute
    call_counter = {"n": 0}
    def failing_execute(*args, **kwargs):
        call_counter["n"] += 1
        if call_counter["n"] == 3:  # DELETE was call 1, first INSERT call 2
            raise RuntimeError("simulated mid-loop failure")
        return orig_execute(*args, **kwargs)
    db.execute = failing_execute  # type: ignore[method-assign]
    try:
        parsed = {
            (1, "60"): {"amount": Decimal("2500"), "account_name": "Rent", "account_type": "Expense"},
            (2, "60"): {"amount": Decimal("2500"), "account_name": "Rent", "account_type": "Expense"},
        }
        with _pytest.raises(RuntimeError, match="simulated mid-loop failure"):
            upsert_pnl_rows(db, year=2026, parsed=parsed)
    finally:
        db.close()

    # 2025 seed row + 2026 seed row both survive the failed refresh.
    with eng.connect() as conn:
        rows = conn.execute(_sa.text(
            "SELECT year, qb_account_id FROM qb_pnl_monthly ORDER BY year, qb_account_id"
        )).fetchall()
    assert (2025, "99") in [(r[0], r[1]) for r in rows], "2025 seed lost on failed 2026 refresh"
    assert (2026, "88") in [(r[0], r[1]) for r in rows], "2026 prior data lost on failed refresh — DELETE leaked"


def test_classifier_insufficient_history_defaults_fixed():
    from gdx_dispatch.routers.budgets import _classify_one
    r = _classify_one("Anything", [Decimal("100")])
    assert r["proposed"] == "fixed"
    assert "insufficient history" in r["reason"]


# ─── Auto-seed snap math ──────────────────────────────────────────────


def test_snap_to_ten_rounds_half_up():
    from gdx_dispatch.routers.budgets import _snap_to_ten
    assert _snap_to_ten(Decimal("123.45")) == Decimal("120")
    assert _snap_to_ten(Decimal("125")) == Decimal("130")  # half up
    assert _snap_to_ten(Decimal("999.99")) == Decimal("1000")
    assert _snap_to_ten(Decimal("0")) == Decimal("0")
    assert _snap_to_ten(Decimal("-5")) == Decimal("0")


# ─── P&L parser ───────────────────────────────────────────────────────


def test_parse_pnl_extracts_data_leaves_with_account_id():
    from gdx_dispatch.modules.quickbooks.pnl import parse_profit_and_loss
    sample = {
        "Columns": {"Column": [
            {"ColTitle": "", "ColType": "Account"},
            {"ColTitle": "Jan 2026", "ColType": "Money",
             "MetaData": [{"Name": "StartDate", "Value": "2026-01-01"}]},
            {"ColTitle": "Feb 2026", "ColType": "Money",
             "MetaData": [{"Name": "StartDate", "Value": "2026-02-01"}]},
        ]},
        "Rows": {"Row": [
            {"type": "Section", "group": "Expenses",
             "Rows": {"Row": [
                 {"type": "Data", "ColData": [
                     {"value": "Rent", "id": "60"},
                     {"value": "2500.00"},
                     {"value": "2500.00"},
                 ]},
             ]}},
        ]},
    }
    out = parse_profit_and_loss(sample)
    assert (1, "60") in out
    assert (2, "60") in out
    assert out[(1, "60")]["amount"] == Decimal("2500.00")
    assert out[(1, "60")]["account_type"] == "Expense"


def test_parse_pnl_skips_calculated_subtotal_rows():
    """Calculated rows (no `id` on ColData[0]) must not pollute the cache."""
    from gdx_dispatch.modules.quickbooks.pnl import parse_profit_and_loss
    sample = {
        "Columns": {"Column": [
            {"ColTitle": ""},
            {"ColTitle": "Jan", "MetaData": [{"Name": "StartDate", "Value": "2026-01-01"}]},
        ]},
        "Rows": {"Row": [
            {"type": "Section", "group": "Expenses",
             "Rows": {"Row": [
                 {"type": "Data", "ColData": [
                     {"value": "Total Expenses"},   # NO id field
                     {"value": "5000.00"},
                 ]},
             ]}},
        ]},
    }
    out = parse_profit_and_loss(sample)
    assert out == {}


def test_parse_pnl_propagates_section_group_to_account_type():
    """COGS / Income / OtherExpenses must map to our normalized types."""
    from gdx_dispatch.modules.quickbooks.pnl import parse_profit_and_loss
    sample = {
        "Columns": {"Column": [
            {"ColTitle": ""},
            {"ColTitle": "Jan", "MetaData": [{"Name": "StartDate", "Value": "2026-01-01"}]},
        ]},
        "Rows": {"Row": [
            {"type": "Section", "group": "Income",
             "Rows": {"Row": [
                 {"type": "Data", "ColData": [{"value": "Sales", "id": "1"}, {"value": "1.00"}]},
             ]}},
            {"type": "Section", "group": "COGS",
             "Rows": {"Row": [
                 {"type": "Data", "ColData": [{"value": "Parts", "id": "70"}, {"value": "1.00"}]},
             ]}},
            {"type": "Section", "group": "OtherExpenses",
             "Rows": {"Row": [
                 {"type": "Data", "ColData": [{"value": "Bank fees", "id": "80"}, {"value": "1.00"}]},
             ]}},
        ]},
    }
    out = parse_profit_and_loss(sample)
    assert out[(1, "1")]["account_type"] == "Income"
    assert out[(1, "70")]["account_type"] == "Cost of Goods Sold"
    assert out[(1, "80")]["account_type"] == "Other Expense"


def test_parse_pnl_raises_without_month_columns():
    from gdx_dispatch.modules.quickbooks.pnl import parse_profit_and_loss
    import pytest as _pytest
    sample = {
        "Columns": {"Column": [{"ColTitle": ""}, {"ColTitle": "Total"}]},  # no StartDate
        "Rows": {"Row": []},
    }
    with _pytest.raises(ValueError, match="summarize_column_by=Month"):
        parse_profit_and_loss(sample)


# ─── Frontend wiring ─────────────────────────────────────────────────


def test_history_averages_use_months_with_data_not_fixed_denominator():
    """Auditor 2026-05-24: qb_pnl_monthly is SPARSE — rows only exist
    for months an account posted activity. A fixed /3 or /6 denominator
    under-reports a new tenant's avg by 3x or 6x, and the quick-fill
    button writes that lie straight into the budget. Avg MUST be
    sum / months_with_data."""
    import sqlalchemy as _sa
    from sqlalchemy.orm import sessionmaker
    from gdx_dispatch.routers.budgets import _history_for_accounts

    eng = _sa.create_engine("sqlite:///:memory:")
    with eng.begin() as conn:
        conn.execute(_sa.text(
            "CREATE TABLE qb_pnl_monthly ("
            "id VARCHAR(36) PRIMARY KEY, "
            "year INTEGER NOT NULL, month INTEGER NOT NULL, "
            "qb_account_id VARCHAR(64) NOT NULL, "
            "account_name VARCHAR(300), account_type VARCHAR(100), "
            "amount NUMERIC(14,2) NOT NULL DEFAULT 0, "
            "synced_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        ))
        # New tenant: only 2 months of data (April + May 2026) for one account.
        for m, amt in [(4, "100"), (5, "200")]:
            conn.execute(_sa.text(
                "INSERT INTO qb_pnl_monthly (id, year, month, qb_account_id, account_name, amount) "
                "VALUES (:id, 2026, :m, '60', 'Rent', :a)"
            ), {"id": f"r{m}", "m": m, "a": amt})

    db = sessionmaker(bind=eng)()
    try:
        # Asking about June 2026 → window goes back into April + May (in 3mo)
        # and into April + May (in 6mo too).
        out = _history_for_accounts(db, year=2026, month=6, account_ids=["60"])
    finally:
        db.close()

    h = out["60"]
    # Both windows see the same 2 months of data → both averages are $150.
    # If the denominator were fixed at 3 or 6, we'd see $100 or $50 — both wrong.
    assert h["trailing_3mo_avg"] == Decimal("150.00"), \
        f"3mo avg should be 300/2=$150, got {h['trailing_3mo_avg']} (fixed-denominator bug?)"
    assert h["trailing_6mo_avg"] == Decimal("150.00"), \
        f"6mo avg should be 300/2=$150, got {h['trailing_6mo_avg']}"
    # Diagnostic counts so the UI can show "based on 2 months of data".
    assert h["months_with_data_3mo"] == 2
    assert h["months_with_data_6mo"] == 2


def test_history_averages_zero_when_no_data():
    """Account with zero history must return 0 averages, never NaN or KeyError."""
    import sqlalchemy as _sa
    from sqlalchemy.orm import sessionmaker
    from gdx_dispatch.routers.budgets import _history_for_accounts

    eng = _sa.create_engine("sqlite:///:memory:")
    with eng.begin() as conn:
        conn.execute(_sa.text(
            "CREATE TABLE qb_pnl_monthly ("
            "id VARCHAR(36) PRIMARY KEY, "
            "year INTEGER NOT NULL, month INTEGER NOT NULL, "
            "qb_account_id VARCHAR(64) NOT NULL, "
            "account_name VARCHAR(300), account_type VARCHAR(100), "
            "amount NUMERIC(14,2) NOT NULL DEFAULT 0, "
            "synced_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        ))

    db = sessionmaker(bind=eng)()
    try:
        out = _history_for_accounts(db, year=2026, month=6, account_ids=["99"])
    finally:
        db.close()

    h = out["99"]
    assert h["trailing_3mo_avg"] == Decimal("0")
    assert h["trailing_6mo_avg"] == Decimal("0")
    assert h["same_month_last_year"] is None


def test_quickfill_does_not_silently_mutate_line_type():
    """Auditor 2026-05-24: the prior frontend `onQuickFill` rewrote
    `line_type=percent_of_revenue → fixed` via a buried ternary on click.
    That's a semantic mutation hidden in a UI action. The new code MUST
    refuse to quick-fill percent_of_revenue lines."""
    src = _read("gdx_dispatch/frontend/src/views/MonthlyBudgetView.vue")
    # The bad pattern is gone:
    assert "row.line_type === 'percent_of_revenue' ? 'fixed' : row.line_type" not in src
    # Replaced by an explicit refusal:
    assert "percent-of-revenue line" in src.lower() or "percent_of_revenue" in src


def test_accounting_method_change_shows_stale_banner():
    """Cash↔Accrual toggle does NOT auto-invalidate qb_pnl_monthly. The
    cached numbers are still on the prior basis until the user re-pulls.
    The UI MUST surface this — auditor caught the silent-stale path."""
    src = _read("gdx_dispatch/frontend/src/views/MonthlyBudgetView.vue")
    assert "accountingMethodStaleSinceChange" in src
    assert "stale-banner" in src
    assert "Refresh now" in src


def test_trends_view_uses_dynamic_palette_for_account_colors():
    """Auditor 2026-05-24: prior 12-color fixed palette collided at
    account #13. GDX has >20 expense accounts. New palette must scale."""
    src = _read("gdx_dispatch/frontend/src/views/SpendingTrendsView.vue")
    # New function signature:
    assert "function paletteFor(n)" in src
    # Old fixed palette gone:
    assert "const PALETTE = [" not in src
    # Uses HSL hue cycling (golden ratio is the canonical choice):
    assert "137.508" in src


def test_history_columns_on_budget_line_out():
    """Sprint monthly-budget-history (2026-05-24): the line response shape
    surfaces trailing-3mo/6mo/same-month-last-year so the UI can render
    history alongside the budget instead of hiding it behind a button."""
    src = _read("gdx_dispatch/routers/budgets.py")
    assert "trailing_3mo_avg: Decimal" in src
    assert "trailing_6mo_avg: Decimal" in src
    assert "same_month_last_year: Decimal | None" in src
    # Helper exists + batched (no N+1 over accounts)
    assert "def _history_for_accounts(" in src
    # Used inside list_budget_for_month
    assert "history = _history_for_accounts(" in src


def test_freshness_indicator_in_budget_response():
    """The list endpoint MUST include pnl_last_synced_at so the UI can
    show 'actuals last synced N min ago'."""
    src = _read("gdx_dispatch/routers/budgets.py")
    assert "def _pnl_last_synced_at(" in src
    assert "pnl_last_synced_at" in src


def test_seed_no_longer_accepts_overwrite_param():
    """Sprint monthly-budget-history (2026-05-24): the overwrite_user_edits
    footgun was removed. Seed touches ONLY empty rows. Pin the parameter
    signature gone (historical mentions in docstrings are fine)."""
    src = _read("gdx_dispatch/routers/budgets.py")
    # The bad parameter signature must not reappear.
    assert "overwrite_user_edits: bool" not in src
    assert "overwrite_user_edits=" not in src.replace("# overwrite_user_edits=", "")  # allow doc refs
    # Check the specific code patterns are gone:
    assert "if existing.source == \"user\" and not overwrite_user_edits" not in src
    # New return-shape field
    assert '"skipped_existing"' in src


def test_settings_router_exposes_qb_accounting_method():
    """Cash vs Accrual toggle — tenant setting on AppSettings exposed via
    /api/settings GET + PATCH."""
    src_settings = _read("gdx_dispatch/routers/settings.py")
    src_model = _read("gdx_dispatch/models/tenant_models.py")
    assert "qb_accounting_method" in src_settings
    assert 'pattern="^(Cash|Accrual)$"' in src_settings
    # Model column
    assert "qb_accounting_method: Mapped[str]" in src_model


def test_pnl_pull_passes_accounting_method_through():
    """The router must read the tenant setting and pass it to
    fetch_profit_and_loss, which must forward it to QBO."""
    router_src = _read("gdx_dispatch/routers/budgets.py")
    pnl_src = _read("gdx_dispatch/modules/quickbooks/pnl.py")
    assert "qb_accounting_method" in router_src
    assert "accounting_method=accounting_method" in router_src
    assert "accounting_method: str = \"Accrual\"" in pnl_src
    assert "accounting_method=accounting_method" in pnl_src


def test_migration_adds_qb_accounting_method_column():
    src = _read("gdx_dispatch/tools/migrate_monthly_budgets.py")
    assert "ALTER TABLE app_settings ADD COLUMN IF NOT EXISTS qb_accounting_method" in src


def test_trends_endpoint_query_shape():
    """Trends endpoint returns one series per account over N months.
    Pins the response keys the SpendingTrendsView depends on."""
    src = _read("gdx_dispatch/routers/budgets.py")
    assert "def spending_trends(" in src
    assert "months: int = Query(24" in src
    assert '"accounts":' in src
    assert '"series":' in src
    assert '"pnl_last_synced_at"' in src


def test_trends_default_kind_covers_all_spending_types():
    """Doug 2026-05-25: "shouldn't it cover more of the chart of accounts?"
    The prior default `account_type=Expense` hid COGS (Contract labor,
    Supplies & Materials) which is the largest spend bucket for service
    businesses. Default account_kind="spending" must include Expense
    + Cost of Goods Sold + Other Expense."""
    src = _read("gdx_dispatch/routers/budgets.py")
    assert "_KIND_TO_TYPES" in src
    assert '"spending": ("Expense", "Cost of Goods Sold", "Other Expense")' in src
    assert '"income": ("Income", "Other Income")' in src
    # account_kind default = "spending"
    assert 'account_kind: str = Query("spending"' in src


def test_anomaly_drawer_filters_to_suspicious_by_default():
    """Doug 2026-05-25: on the Vehicle gas & fuel anomaly drawer, the bulk
    of rows were legitimate fuel purchases with "no rule matched" + a
    disabled Apply button. Felt like "no way to submit it to QuickBooks."
    Default view must hide the unknown-suggestion rows; a "Show all"
    toggle reveals them for manual recategorize."""
    src = _read("gdx_dispatch/frontend/src/views/SpendingTrendsView.vue")
    # Toggle ref exists, defaults false
    assert "showAllAnomalies = ref(false)" in src
    # Filter computed wraps allAnomalyTransactions with the toggle
    assert "if (showAllAnomalies.value) return all" in src
    # Diagnostic counts visible to the user
    assert "suspiciousCount" in src
    assert "legitimateCount" in src
    # Toggle label visible in the dialog
    assert "Show legitimate" in src


def test_anomaly_picks_prepopulate_all_txns_for_reactive_v_model():
    """Doug 2026-05-25: even on "pick manually" rows, the Apply stayed
    disabled after picking from the dropdown — Vue reactivity gap on
    a key that didn't exist yet. openAnomalyPanel MUST pre-populate
    picks for every txn (with null for unknown rows) so the v-model
    write hits an existing reactive key and Apply enables."""
    src = _read("gdx_dispatch/frontend/src/views/SpendingTrendsView.vue")
    # Pre-fill loop builds a complete `next` dict and assigns to picks.value
    assert "next[t.txn_id] = (" in src
    assert "picks.value = next" in src
    # Old "only fill recategorize rows" pattern is gone (left no orphan keys)
    assert "picks.value[t.txn_id] = t.suggestion.suggested_account_id;" not in src


def test_trends_frontend_default_to_all_spending():
    """SpendingTrendsView must default to the spending kind (not single
    Expense type) so COGS shows up by default."""
    src = _read("gdx_dispatch/frontend/src/views/SpendingTrendsView.vue")
    assert "const accountKind = ref('spending')" in src
    # Old dropdown that filtered to single QBO account_type is gone.
    assert "const accountType = ref('Expense')" not in src
    # The query string uses account_kind not account_type
    assert "account_kind=" in src
    # Three meaningful kind options exposed
    assert "All spending" in src
    assert "Income" in src


def test_spending_trends_view_route_registered():
    src = _read("gdx_dispatch/frontend/src/router/index.js")
    assert "import('../views/SpendingTrendsView.vue')" in src
    assert "path: '/spending-trends'" in src


def test_spending_trends_in_financials_nav():
    src = _read("gdx_dispatch/frontend/src/constants/modules.js")
    assert "key: 'spending_trends'" in src
    assert "to: '/spending-trends'" in src


# ─── Sprint fix-in-quickbooks (2026-05-25) ─────────────────────


def test_anomalies_endpoint_registered():
    src = _read("gdx_dispatch/routers/budgets.py")
    assert '@router.get("/anomalies"' in src
    assert "def list_anomalies(" in src
    assert "fetch_profit_and_loss_detail" in src
    assert "suggest_target_account" in src


def test_recategorize_endpoint_registered():
    src = _read("gdx_dispatch/routers/budgets.py")
    assert '@router.post("/recategorize"' in src
    assert "async def recategorize_one(" in src
    # Role gated as write
    assert "require_permission(\"accounting.write\")" in src
    # Maps RecategorizeError to a 400 (user-visible reason)
    assert "except RecategorizeError" in src


def test_recategorize_writes_audit_log_with_before_after():
    """Yellow-tier discipline: every QB write must record a recoverable
    before/after state in the audit log."""
    src = _read("gdx_dispatch/routers/budgets.py")
    assert 'action="qb.recategorize"' in src
    rec_src = _read("gdx_dispatch/modules/quickbooks/recategorize.py")
    # New shape (per audit 2026-05-25): per-line before-state list
    assert '"before_lines"' in rec_src
    assert '"after_account_id"' in rec_src
    assert '"synctoken_before"' in rec_src


def test_pnl_detail_parser_extracts_txn_id_from_cd1_not_cd0():
    """Verified against GDX live QBO response 2026-05-25: transaction id
    + txn_type live on ColData[1] (Transaction Type column), NOT on
    ColData[0] (Date). The prior unit-test fixture guessed wrong, the
    prod walk caught it. This fixture mirrors the REAL shape so a future
    regression is impossible."""
    from gdx_dispatch.modules.quickbooks.pnl import parse_profit_and_loss_detail
    sample = {
        "Rows": {"Row": [
            {"type": "Section",
             "Rows": {"Row": [
                # Real-shape Data row (id on cd[1], NOT cd[0]).
                {"type": "Data", "ColData": [
                    {"value": "2026-01-06"},                                # Date
                    {"value": "Expense", "id": "2636"},                     # Type + id
                    {"value": ""},                                          # Num
                    {"value": "North Star Gas", "id": "195"},               # Vendor + id
                    {"value": "DBT CRD NORTH STAR GAS"},                    # Memo
                    {"value": "Garage Door inc Main(2204)", "id": "178"},   # Split
                    {"value": "77.11"},                                     # Amount
                    {"value": "150.08"},                                    # Balance
                ]},
                # Subtotal row — no id on cd[1], must be skipped.
                {"type": "Data", "ColData": [
                    {"value": ""}, {"value": ""}, {"value": ""}, {"value": ""},
                    {"value": ""}, {"value": ""}, {"value": "-17287.49"}, {"value": ""},
                ]},
            ]}},
        ]},
    }
    out = parse_profit_and_loss_detail(sample)
    assert len(out) == 1, f"expected 1 leaf row, got {len(out)} — subtotal not skipped?"
    assert out[0]["txn_id"] == "2636"  # cd[1].id
    assert out[0]["txn_type"] == "Expense"  # cd[1].value
    assert out[0]["txn_date"] == "2026-01-06"
    assert out[0]["vendor_name"] == "North Star Gas"
    assert out[0]["vendor_id"] == "195"
    assert out[0]["memo"] == "DBT CRD NORTH STAR GAS"
    assert out[0]["amount"] == Decimal("77.11")


def test_suggester_transfer_marked_open_in_qb():
    """Bank-to-bank transfers MUST get action=open_in_qb so the UI sends the
    user to QB rather than attempting an automated delete+create."""
    from gdx_dispatch.modules.quickbooks.recategorize import suggest_target_account
    accts = [{"qb_account_id": "1", "name": "Service Income", "account_type": "Income", "active": True}]
    r = suggest_target_account(
        txn_type="Deposit",
        vendor_name="",
        memo="TRANSFER FROM X2204 TO X1099",
        amount=Decimal("-1000"),
        accounts=accts,
    )
    assert r["action"] == "open_in_qb"


def test_suggester_deposit_always_defers_to_qb():
    """Auditor 2026-05-25: bank Deposits aggregating customer payments
    should NOT be auto-recategorized to Income — that would sever the A/R
    linkage to invoices. ALL Deposits (regardless of memo) must
    action=open_in_qb so the bookkeeper handles them with the proper
    ReceivePayment workflow in QB UI."""
    from gdx_dispatch.modules.quickbooks.recategorize import suggest_target_account
    accts = [
        {"qb_account_id": "1", "name": "Service Income", "account_type": "Income", "active": True},
        {"qb_account_id": "124", "name": "Vehicle gas & fuel", "account_type": "Expense", "active": True},
    ]
    # Even the most obvious "DEPOSIT/CREDIT" memo on a customer payment batch
    # must NOT be auto-recategorized. v1 always defers Deposits to QB UI.
    r = suggest_target_account(
        txn_type="Deposit",
        vendor_name="",
        memo="DEPOSIT/CREDIT",
        amount=Decimal("-17364.60"),
        accounts=accts,
    )
    assert r["action"] == "open_in_qb"
    assert "A/R" in r["reason"] or "ReceivePayment" in r["reason"]


def test_suggester_claude_ai_to_subscriptions():
    """Software vendors (Anthropic / Hostinger / GitHub etc.) → Subscriptions."""
    from gdx_dispatch.modules.quickbooks.recategorize import suggest_target_account
    accts = [
        {"qb_account_id": "50", "name": "Software Subscriptions", "account_type": "Expense", "active": True},
    ]
    r = suggest_target_account(
        txn_type="Purchase",
        vendor_name="",
        memo="DBT CRD CLAUDE.AI SUBSCRIPTION ANTHROPIC.COM",
        amount=Decimal("200"),
        accounts=accts,
    )
    assert r["action"] == "recategorize"
    assert r["suggested_account_name"] == "Software Subscriptions"


def test_suggester_unknown_returns_no_suggestion():
    from gdx_dispatch.modules.quickbooks.recategorize import suggest_target_account
    accts = [{"qb_account_id": "1", "name": "Service Income", "account_type": "Income", "active": True}]
    r = suggest_target_account(
        txn_type="Purchase",
        vendor_name="Unknown Vendor",
        memo="Random charge",
        amount=Decimal("50"),
        accounts=accts,
    )
    assert r["action"] == "unknown"
    assert r["suggested_account_id"] is None


def test_recategorize_rejects_unsupported_txn_type():
    """V1 supports Purchase/Expense only. Deposit / JournalEntry /
    SalesReceipt / Transfer must be rejected."""
    import asyncio
    from unittest.mock import MagicMock
    from gdx_dispatch.modules.quickbooks.recategorize import (
        recategorize_transaction, RecategorizeError, SUPPORTED_TYPES,
    )
    import pytest as _pytest
    # Pin the supported set
    assert SUPPORTED_TYPES == frozenset({"Purchase", "Expense"})

    qb = MagicMock()
    for bad_type in ("Deposit", "JournalEntry", "SalesReceipt", "Transfer"):
        with _pytest.raises(RecategorizeError, match="not supported"):
            asyncio.run(recategorize_transaction(
                qb, txn_type=bad_type, txn_id="X", new_account_id="1",
            ))


def test_recategorize_audit_captures_per_line_before_state():
    """Auditor 2026-05-25: prior implementation captured ONE before_account_id
    via short-circuit on the first line, losing 4-of-5 lines on a multi-line
    Purchase. The result dict MUST carry `before_lines: [{line_index,
    account_id, account_name, amount}, ...]` for full reversibility."""
    src = _read("gdx_dispatch/modules/quickbooks/recategorize.py")
    assert '"before_lines"' in src
    assert '"line_index"' in src
    # Old shape is gone — single before_account_id is no longer in result
    # (only in the loop-local capture if at all). Confirm the result dict
    # uses before_lines exclusively.
    # Find the `return {` block at the end and check it doesn't have a
    # single-value before_account_id.
    assert "\"before_account_id\":" not in src or src.count("\"before_account_id\":") == 0


def test_recategorize_put_body_is_minimal_not_spread_entity():
    """Auditor 2026-05-25: dropping the entire GET response back as the PUT
    payload drags server-computed fields (MetaData, TotalTax, LinkedTxn,
    domain, ExchangeRate) that QBO either rejects or silently recomputes.
    The PUT payload MUST be a minimal dict, NOT `{**entity, ...}`."""
    src = _read("gdx_dispatch/modules/quickbooks/recategorize.py")
    assert "{**entity" not in src, \
        "PUT payload must not spread the GET entity — sends server-computed fields"
    # The minimal payload includes only the necessary keys.
    assert '"Id": txn_id' in src
    assert '"SyncToken": sync_token_before' in src
    assert '"sparse": True' in src
    assert '"Line": lines' in src


def test_recategorize_rejects_mixed_item_and_account_lines():
    """Purchase with BOTH ItemBasedExpenseLineDetail AND
    AccountBasedExpenseLineDetail can't be safely recategorized via API —
    we'd silently leave the item lines untouched and the user wouldn't know.
    Refuse the operation."""
    src = _read("gdx_dispatch/modules/quickbooks/recategorize.py")
    assert "item_based_lines_skipped" in src
    assert "ItemBasedExpenseLineDetail" in src
    assert "inconsistent state" in src.lower() or "fix in qb ui" in src.lower()


def test_recategorize_get_then_put_with_synctoken():
    """The QBO write path MUST read SyncToken from GET and forward it on
    PUT — otherwise we lose optimistic-concurrency protection and could
    clobber edits made in QB UI between our GET and PUT."""
    rec_src = _read("gdx_dispatch/modules/quickbooks/recategorize.py")
    # GET reads SyncToken
    assert 'entity.get("SyncToken")' in rec_src
    # Update payload includes SyncToken + sparse=true
    assert '"SyncToken": sync_token_before' in rec_src
    assert '"sparse": True' in rec_src
    # Idempotency key flowed through
    assert "idempotency_key" in rec_src
    assert "requestid=" in rec_src


def test_anomalies_endpoint_filters_to_net_negative_accounts():
    """The query MUST filter HAVING SUM(amount) < 0 — only inspect accounts
    that actually look anomalous. Pulling ProfitAndLossDetail for every
    expense account would be wasteful + hit QBO rate limits."""
    src = _read("gdx_dispatch/routers/budgets.py")
    assert "HAVING SUM(amount) < 0" in src


def test_anomalies_endpoint_caps_sequential_qbo_calls():
    """Auditor 2026-05-25: P&L Detail is 3-8s per account; without a cap
    the frontend's 30s default fetch timeout would kill a request to a
    tenant with 10+ anomalies before the panel rendered. Bound the
    sequential-call path."""
    src = _read("gdx_dispatch/routers/budgets.py")
    assert "MAX_ANOMALIES_PER_REQUEST" in src


def test_anomaly_panel_wired_into_spending_trends():
    src = _read("gdx_dispatch/frontend/src/views/SpendingTrendsView.vue")
    assert "useBudgetAnomalies" in src
    assert "Fix in QuickBooks" in src
    assert "openAnomalyPanel" in src


def test_anomaly_composable_uses_correct_endpoints():
    src = _read("gdx_dispatch/frontend/src/composables/useBudgetAnomalies.js")
    assert "/api/budgets/anomalies" in src
    assert "/api/budgets/recategorize" in src
    # openInQB builds the QBO deep-link
    assert "app.qbo.intuit.com" in src


def test_budget_view_route_registered():
    src = _read("gdx_dispatch/frontend/src/router/index.js")
    assert "import('../views/MonthlyBudgetView.vue')" in src
    assert "path: '/budget'" in src
    assert "requiresPermission: 'accounting.read'" in src


def test_budget_module_in_financials_nav():
    src = _read("gdx_dispatch/frontend/src/constants/modules.js")
    assert "key: 'budget'" in src
    assert "to: '/budget'" in src
    assert "permission: 'accounting.read'" in src

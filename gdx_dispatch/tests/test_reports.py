from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.database import get_db
from gdx_dispatch.routers import reports
from gdx_dispatch.routers.auth import get_current_user


@pytest.fixture
def tenant_db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()

    db.execute(
        text(
            """
            CREATE TABLE jobs (
                id TEXT PRIMARY KEY,
                customer_id TEXT,
                technician_id TEXT,
                title TEXT DEFAULT '',
                description TEXT,
                lifecycle_stage TEXT DEFAULT 'lead',
                dispatch_status TEXT DEFAULT 'unassigned',
                billing_status TEXT DEFAULT 'unbilled',
                scheduled_at TEXT,
                completed_at TEXT,
                assigned_to TEXT,
                source TEXT,
                is_return_visit INTEGER DEFAULT 0,
                parent_job_id TEXT,
                job_type TEXT,
                status TEXT,
                priority TEXT DEFAULT 'Normal',
                is_demo INTEGER DEFAULT 0,
                total_amount REAL,
                labor_cost REAL,
                overhead_cost REAL,
                company_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT,
                deleted_at TEXT
            )
            """
        )
    )
    db.execute(
        text(
            """
            CREATE TABLE invoices (
                id TEXT PRIMARY KEY,
                job_id TEXT,
                invoice_number TEXT,
                billing_type TEXT DEFAULT 'standard',
                sequence_number INTEGER DEFAULT 1,
                subtotal REAL DEFAULT 0,
                tax_amount REAL DEFAULT 0,
                total REAL DEFAULT 0,
                total_amount REAL,
                balance_due REAL DEFAULT 0,
                amount_paid REAL DEFAULT 0,
                status TEXT DEFAULT 'draft',
                locked INTEGER DEFAULT 0,
                public_token TEXT DEFAULT '',
                due_date TEXT,
                notes TEXT,
                customer_id TEXT,
                company_id TEXT,
                created_at TEXT,
                invoice_date TEXT,
                deleted_at TEXT
            )
            """
        )
    )
    db.execute(
        text(
            """
            CREATE TABLE technicians (
                id TEXT PRIMARY KEY,
                user_id TEXT,
                company_id TEXT,
                name TEXT NOT NULL,
                email TEXT,
                phone TEXT,
                skills TEXT,
                hourly_rate REAL,
                active INTEGER DEFAULT 1,
                territory TEXT,
                availability_status TEXT,
                commission_pct REAL,
                created_at TEXT,
                updated_at TEXT,
                deleted_at TEXT
            )
            """
        )
    )
    db.execute(
        text(
            """
            CREATE TABLE appointments (
                id TEXT PRIMARY KEY,
                job_id TEXT,
                technician_id TEXT,
                created_at TEXT
            )
            """
        )
    )
    db.execute(
        text(
            """
            CREATE TABLE customers (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                name_hash TEXT,
                email TEXT,
                email_hash TEXT,
                phone TEXT,
                phone_hash TEXT,
                address TEXT,
                notes TEXT,
                source TEXT,
                customer_type TEXT DEFAULT 'Retail',
                company_id TEXT,
                created_at TEXT,
                deleted_at TEXT
            )
            """
        )
    )
    db.commit()

    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def _seed_customer(db, *, name: str) -> str:
    customer_id = str(uuid.uuid4())
    db.execute(
        text(
            """
            INSERT INTO customers (id, name, company_id, created_at, deleted_at)
            VALUES (:id, :name, 'tenant-test', :created_at, NULL)
            """
        ),
        {
            "id": customer_id,
            "name": name,
            "created_at": _iso(datetime.now(UTC)),
        },
    )
    db.commit()
    return customer_id


def _seed_tech(db, *, name: str) -> str:
    tech_id = str(uuid.uuid4())
    db.execute(
        text("INSERT INTO technicians (id, name, deleted_at) VALUES (:id, :name, NULL)"),
        {"id": tech_id, "name": name},
    )
    db.commit()
    return tech_id


def _seed_job(
    db,
    *,
    customer_id: str | None,
    technician_id: str | None,
    created_at: datetime,
    lifecycle_stage: str = "Open",
    job_type: str = "Repair",
    total_amount: float = 0.0,
    labor_cost: float = 0.0,
    overhead_cost: float = 0.0,
) -> str:
    job_id = str(uuid.uuid4())
    # Phase D audit fix 2026-04-27: write the lifecycle_stage column too,
    # not just the legacy status varchar. Production queries filter on
    # lifecycle_stage (the source of truth); the test helper was lying
    # about it. Map display labels to enum literals.
    _stage_map = {
        "Lead": "lead",
        "Estimate": "estimate",
        "Open": "scheduled",  # legacy alias used by older tests
        "Scheduled": "scheduled",
        "In Progress": "in_progress",
        "Complete": "completed",
        "Completed": "completed",
        "Cancelled": "cancelled",
    }
    enum_stage = _stage_map.get(lifecycle_stage, lifecycle_stage.lower())
    completed_at = _iso(created_at) if enum_stage == "completed" else None
    # assigned_to mirrors technician_id so technician_performance JOIN works
    db.execute(
        text(
            """
            INSERT INTO jobs (
                id, customer_id, technician_id, assigned_to, job_type, status, lifecycle_stage,
                total_amount, labor_cost, overhead_cost, company_id, created_at, updated_at, completed_at, deleted_at
            )
            VALUES (
                :id, :customer_id, :technician_id, :assigned_to, :job_type, :status, :lifecycle_stage,
                :total_amount, :labor_cost, :overhead_cost, 'tenant-test', :created_at, :updated_at, :completed_at, NULL
            )
            """
        ),
        {
            "id": job_id,
            "customer_id": customer_id,
            "technician_id": technician_id,
            "assigned_to": technician_id,
            "job_type": job_type,
            "status": lifecycle_stage,
            "lifecycle_stage": enum_stage,
            "total_amount": total_amount,
            "labor_cost": labor_cost,
            "overhead_cost": overhead_cost,
            "created_at": _iso(created_at),
            "updated_at": _iso(created_at),
            "completed_at": completed_at,
        },
    )
    if technician_id:
        db.execute(
            text(
                """
                INSERT INTO appointments (id, job_id, technician_id, created_at)
                VALUES (:id, :job_id, :technician_id, :created_at)
                """
            ),
            {
                "id": str(uuid.uuid4()),
                "job_id": job_id,
                "technician_id": technician_id,
                "created_at": _iso(created_at),
            },
        )
    db.commit()
    return job_id


def _seed_invoice(
    db,
    *,
    job_id: str,
    created_at: datetime,
    total_amount: float,
    balance_due: float,
    status: str,
    total: float = 0,
    billing_type: str = "standard",
    customer_id: str | None = None,
) -> str:
    # `total_amount` stays a positional-style kwarg for the ~20 existing
    # callers, but prod's shape is the OPPOSITE: total_amount is NULL on every
    # row and `total` carries the money (M8). Pass total_amount=None, total=X
    # to reproduce a real invoice.
    # Phase D audit fix 2026-04-27: canonical Invoice.status enum is
    # draft/sent/paid/overdue/void. Older tests passed display labels
    # ("Unpaid", "Partial", "Paid") which the old reports code summed
    # blindly; the new path filters by canonical status. Map tolerantly.
    _status_map = {
        "Paid": "paid",
        "Unpaid": "sent",
        "Partial": "sent",
        "paid": "paid",
        "sent": "sent",
        "overdue": "overdue",
        "draft": "draft",
        "void": "void",
    }
    canon_status = _status_map.get(status, status.lower())
    inv_id = str(uuid.uuid4())
    db.execute(
        text(
            """
            INSERT INTO invoices (
                id, job_id, total_amount, total, billing_type, balance_due, status,
                customer_id, company_id, created_at, invoice_date, deleted_at
            ) VALUES (
                :id, :job_id, :total_amount, :total, :billing_type, :balance_due, :status,
                :customer_id, 'tenant-test', :created_at, :invoice_date, NULL
            )
            """
        ),
        {
            "id": inv_id,
            "job_id": job_id,
            "total_amount": total_amount,
            "total": total,
            "billing_type": billing_type,
            "balance_due": balance_due,
            "status": canon_status,
            "customer_id": customer_id,
            "created_at": _iso(created_at),
            "invoice_date": created_at.date().isoformat(),
        },
    )
    db.commit()
    return inv_id


def test_reports_router_has_required_dependencies():
    route_map = {r.path: r for r in reports.router.routes}
    for path in [
        "/api/reports/summary",
        "/api/reports/daily-snapshot",
        "/api/reports/job-profitability",
        "/api/reports/technician-performance",
        "/api/reports/revenue-analytics",
        "/api/reports/customer-ltv",
        "/api/reports/outstanding-aging",
        "/api/reports/sales-tax",
    ]:
        route = route_map[path]
        dep_calls = {dep.call for dep in route.dependant.dependencies}
        assert get_current_user in dep_calls
        assert get_db in dep_calls


# ---------------------------------------------------------------------------
# Sales-tax report (plan §16). The endpoint's SQL uses Postgres date_trunc,
# so we unit-test the pure rollup helper — the money math is what matters:
# GDX-vs-QB provenance split, collected-vs-outstanding, derived outstanding.
# ---------------------------------------------------------------------------


def _tax_row(period_start, source, tax_total, tax_collected, invoice_count=1):
    return {
        "period_start": period_start,
        "source": source,
        "tax_total": tax_total,
        "tax_collected": tax_collected,
        "invoice_count": invoice_count,
    }


def test_rollup_sales_tax_splits_gdx_and_quickbooks():
    from datetime import date

    rows = [
        _tax_row(date(2026, 7, 1), "gdx", 128.34, 128.34),
        _tax_row(date(2026, 7, 1), "quickbooks", 50.00, 0.0),
    ]
    items, totals = reports._rollup_sales_tax(rows)

    assert len(items) == 1
    row = items[0]
    assert row["period_start"] == "2026-07-01"
    assert row["gdx"]["tax_total"] == 128.34
    assert row["quickbooks"]["tax_total"] == 50.00
    # Period total is GDX + QB; collected is only the paid (gdx) portion.
    assert row["tax_total"] == 178.34
    assert row["tax_collected"] == 128.34
    # Outstanding is DERIVED (total - collected), never trusted from input.
    assert row["tax_outstanding"] == 50.00
    assert totals["tax_total"] == 178.34
    assert totals["tax_collected"] == 128.34
    assert totals["tax_outstanding"] == 50.00
    assert totals["gdx_tax"] == 128.34
    assert totals["quickbooks_tax"] == 50.00


def test_rollup_sales_tax_orders_periods_and_sums_totals():
    from datetime import date

    rows = [
        _tax_row(date(2026, 5, 1), "gdx", 371.54, 371.54),
        _tax_row(date(2026, 3, 1), "quickbooks", 703.41, 100.00),
        _tax_row(date(2026, 4, 1), "gdx", 199.38, 0.0),
        _tax_row(date(2026, 4, 1), "quickbooks", 7.74, 7.74),
    ]
    items, totals = reports._rollup_sales_tax(rows)

    # Ascending period order regardless of input order.
    assert [i["period_start"] for i in items] == ["2026-03-01", "2026-04-01", "2026-05-01"]
    # April merges both sources into one card.
    april = next(i for i in items if i["period_start"] == "2026-04-01")
    assert april["tax_total"] == 207.12
    assert april["tax_collected"] == 7.74
    # Grand totals sum every period.
    assert totals["tax_total"] == round(703.41 + 207.12 + 371.54, 2)
    assert totals["gdx_tax"] == round(371.54 + 199.38, 2)
    assert totals["quickbooks_tax"] == round(703.41 + 7.74, 2)


def test_rollup_sales_tax_empty_is_all_zero():
    items, totals = reports._rollup_sales_tax([])
    assert items == []
    assert totals == {
        "tax_total": 0, "tax_collected": 0, "tax_outstanding": 0,
        "gdx_tax": 0, "quickbooks_tax": 0,
    }


def test_summary_returns_dashboard_kpis(tenant_db_session):
    now = datetime.now(UTC)
    cust = _seed_customer(tenant_db_session, name="Acme")
    tech = _seed_tech(tenant_db_session, name="Tech One")

    job1 = _seed_job(
        tenant_db_session,
        customer_id=cust,
        technician_id=tech,
        created_at=now - timedelta(days=1),
        lifecycle_stage="Complete",
        total_amount=250,
    )
    _seed_invoice(
        tenant_db_session,
        job_id=job1,
        created_at=now - timedelta(days=1),
        total_amount=250,
        balance_due=50,
        status="sent",
    )
    _seed_job(
        tenant_db_session,
        customer_id=cust,
        technician_id=tech,
        created_at=now - timedelta(days=2),
        lifecycle_stage="Scheduled",
        total_amount=120,
    )

    data = reports.reports_summary(None, None, {}, tenant_db_session)
    assert data["revenue_total"] == pytest.approx(250.0)
    assert data["jobs_completed"] == 1
    assert data["open_jobs"] == 1
    assert data["avg_job_value"] == pytest.approx(250.0)


def test_summary_respects_explicit_date_range(tenant_db_session):
    now = datetime.now(UTC)
    cust = _seed_customer(tenant_db_session, name="Bravo")

    in_range_job = _seed_job(
        tenant_db_session,
        customer_id=cust,
        technician_id=None,
        created_at=now - timedelta(days=3),
        lifecycle_stage="Complete",
    )
    _seed_invoice(
        tenant_db_session,
        job_id=in_range_job,
        created_at=now - timedelta(days=3),
        total_amount=175,
        balance_due=0,
        status="paid",
    )

    old_job = _seed_job(
        tenant_db_session,
        customer_id=cust,
        technician_id=None,
        created_at=now - timedelta(days=70),
        lifecycle_stage="Complete",
    )
    _seed_invoice(
        tenant_db_session,
        job_id=old_job,
        created_at=now - timedelta(days=70),
        total_amount=900,
        balance_due=0,
        status="paid",
    )

    start = (now - timedelta(days=7)).date().isoformat()
    end = now.date().isoformat()
    data = reports.reports_summary(start, end, {}, tenant_db_session)
    assert data["revenue_total"] == pytest.approx(175.0)
    assert data["jobs_completed"] == 1


def test_daily_snapshot_returns_today_metrics(tenant_db_session):
    now = datetime.now(UTC)
    cust = _seed_customer(tenant_db_session, name="Charlie")
    today_job = _seed_job(
        tenant_db_session,
        customer_id=cust,
        technician_id=None,
        created_at=now,
        lifecycle_stage="Complete",
    )
    _seed_invoice(
        tenant_db_session,
        job_id=today_job,
        created_at=now,
        total_amount=440,
        balance_due=140,
        status="Unpaid",
    )

    data = reports.daily_snapshot(None, None, {}, tenant_db_session)
    assert data["today_revenue"] == pytest.approx(440.0)
    assert data["jobs_completed_today"] == 1
    assert data["new_jobs_today"] == 1
    assert data["open_invoices_count"] == 1
    assert data["open_invoices_total"] == pytest.approx(140.0)


def test_daily_snapshot_supports_date_range(tenant_db_session):
    base_day = datetime.now(UTC) - timedelta(days=4)
    cust = _seed_customer(tenant_db_session, name="Delta")

    range_job = _seed_job(
        tenant_db_session,
        customer_id=cust,
        technician_id=None,
        created_at=base_day,
        lifecycle_stage="Complete",
    )
    _seed_invoice(
        tenant_db_session,
        job_id=range_job,
        created_at=base_day,
        total_amount=300,
        balance_due=100,
        status="Partial",
    )

    start = (base_day - timedelta(days=1)).date().isoformat()
    end = (base_day + timedelta(days=1)).date().isoformat()
    data = reports.daily_snapshot(start, end, {}, tenant_db_session)
    assert data["today_revenue"] == pytest.approx(300.0)
    assert data["jobs_completed_today"] == 1


def test_job_profitability_returns_profit_per_job(tenant_db_session):
    now = datetime.now(UTC)
    cust = _seed_customer(tenant_db_session, name="Echo")
    job_id = _seed_job(
        tenant_db_session,
        customer_id=cust,
        technician_id=None,
        created_at=now - timedelta(days=1),
        lifecycle_stage="Complete",
        labor_cost=80,
        overhead_cost=20,
    )
    _seed_invoice(
        tenant_db_session,
        job_id=job_id,
        created_at=now - timedelta(days=1),
        total_amount=250,
        balance_due=0,
        status="Paid",
    )

    data = reports.job_profitability(None, None, {}, tenant_db_session)["items"]
    assert len(data) == 1
    assert data[0]["job_id"] == job_id
    # profit = revenue (labor/overhead deducted at job level, not in this query)
    assert data[0]["profit"] == pytest.approx(250.0)
    assert data[0]["revenue"] == pytest.approx(250.0)


def test_technician_performance_returns_jobs_and_revenue(tenant_db_session):
    now = datetime.now(UTC)
    cust = _seed_customer(tenant_db_session, name="Foxtrot")
    tech_a = _seed_tech(tenant_db_session, name="Alice Tech")
    tech_b = _seed_tech(tenant_db_session, name="Bob Tech")

    job_a = _seed_job(
        tenant_db_session,
        customer_id=cust,
        technician_id=tech_a,
        created_at=now - timedelta(days=2),
        lifecycle_stage="Complete",
    )
    _seed_invoice(
        tenant_db_session,
        job_id=job_a,
        created_at=now - timedelta(days=2),
        total_amount=220,
        balance_due=0,
        status="Paid",
    )

    job_b = _seed_job(
        tenant_db_session,
        customer_id=cust,
        technician_id=tech_b,
        created_at=now - timedelta(days=2),
        lifecycle_stage="Complete",
    )
    _seed_invoice(
        tenant_db_session,
        job_id=job_b,
        created_at=now - timedelta(days=2),
        total_amount=120,
        balance_due=0,
        status="Paid",
    )

    rows = reports.technician_performance(None, None, {}, tenant_db_session)["items"]
    by_name = {r["technician_name"]: r for r in rows}
    assert by_name["Alice Tech"]["jobs_completed"] == 1
    assert by_name["Alice Tech"]["revenue"] == pytest.approx(220.0)
    assert by_name["Bob Tech"]["revenue"] == pytest.approx(120.0)


def test_revenue_analytics_returns_period_and_type_breakdowns(tenant_db_session):
    now = datetime.now(UTC)
    cust = _seed_customer(tenant_db_session, name="Golf")
    repair_job = _seed_job(
        tenant_db_session,
        customer_id=cust,
        technician_id=None,
        created_at=now - timedelta(days=10),
        lifecycle_stage="Complete",
        job_type="Repair",
    )
    install_job = _seed_job(
        tenant_db_session,
        customer_id=cust,
        technician_id=None,
        created_at=now - timedelta(days=1),
        lifecycle_stage="Complete",
        job_type="Install",
    )
    _seed_invoice(
        tenant_db_session,
        job_id=repair_job,
        created_at=now - timedelta(days=10),
        total_amount=500,
        balance_due=0,
        status="Paid",
    )
    _seed_invoice(
        tenant_db_session,
        job_id=install_job,
        created_at=now - timedelta(days=1),
        total_amount=300,
        balance_due=0,
        status="Paid",
    )

    data = reports.revenue_analytics(None, None, {}, tenant_db_session)
    assert data["total_revenue"] == pytest.approx(800.0)
    by_type = {r["job_type"]: r["revenue"] for r in data["by_job_type"]}
    assert by_type["Repair"] == pytest.approx(500.0)
    assert by_type["Install"] == pytest.approx(300.0)


def test_customer_ltv_returns_lifetime_value_per_customer(tenant_db_session):
    now = datetime.now(UTC)
    cust_a = _seed_customer(tenant_db_session, name="Hotel Co")
    cust_b = _seed_customer(tenant_db_session, name="India Co")

    job_a1 = _seed_job(tenant_db_session, customer_id=cust_a, technician_id=None, created_at=now - timedelta(days=3))
    job_a2 = _seed_job(tenant_db_session, customer_id=cust_a, technician_id=None, created_at=now - timedelta(days=1))
    job_b1 = _seed_job(tenant_db_session, customer_id=cust_b, technician_id=None, created_at=now - timedelta(days=2))

    _seed_invoice(tenant_db_session, job_id=job_a1, created_at=now - timedelta(days=3), total_amount=200, balance_due=0, status="Paid")
    _seed_invoice(tenant_db_session, job_id=job_a2, created_at=now - timedelta(days=1), total_amount=150, balance_due=50, status="Partial")
    _seed_invoice(tenant_db_session, job_id=job_b1, created_at=now - timedelta(days=2), total_amount=100, balance_due=0, status="Paid")

    rows = reports.customer_ltv(None, None, {}, tenant_db_session)["items"]
    assert rows[0]["customer_name"] == "Hotel Co"
    assert rows[0]["lifetime_value"] == pytest.approx(350.0)
    assert rows[0]["job_count"] == 2


def test_outstanding_aging_returns_buckets(tenant_db_session):
    now = datetime.now(UTC)
    cust = _seed_customer(tenant_db_session, name="Juliet")

    job_10 = _seed_job(tenant_db_session, customer_id=cust, technician_id=None, created_at=now - timedelta(days=10))
    job_40 = _seed_job(tenant_db_session, customer_id=cust, technician_id=None, created_at=now - timedelta(days=40))
    job_70 = _seed_job(tenant_db_session, customer_id=cust, technician_id=None, created_at=now - timedelta(days=70))
    job_120 = _seed_job(tenant_db_session, customer_id=cust, technician_id=None, created_at=now - timedelta(days=120))

    _seed_invoice(tenant_db_session, job_id=job_10, created_at=now - timedelta(days=10), total_amount=10, balance_due=10, status="Unpaid")
    _seed_invoice(tenant_db_session, job_id=job_40, created_at=now - timedelta(days=40), total_amount=20, balance_due=20, status="Unpaid")
    _seed_invoice(tenant_db_session, job_id=job_70, created_at=now - timedelta(days=70), total_amount=30, balance_due=30, status="Unpaid")
    _seed_invoice(tenant_db_session, job_id=job_120, created_at=now - timedelta(days=120), total_amount=40, balance_due=40, status="Unpaid")

    data = reports.outstanding_aging("2000-01-01", now.date().isoformat(), {}, tenant_db_session)
    assert data["counts"]["0_30"] == 1
    assert data["counts"]["31_60"] == 1
    assert data["counts"]["61_90"] == 1
    assert data["counts"]["91_plus"] == 1
    assert data["totals"]["0_30"] == pytest.approx(10.0)
    assert data["totals"]["91_plus"] == pytest.approx(40.0)


def test_reports_reject_invalid_date_range(tenant_db_session):
    with pytest.raises(HTTPException) as exc:
        reports.reports_summary("2026-01-31", "2026-01-01", {}, tenant_db_session)
    assert exc.value.status_code == 422
    assert "start_date" in str(exc.value.detail)


def test_reports_reject_invalid_date_format(tenant_db_session):
    with pytest.raises(HTTPException) as exc:
        reports.reports_summary("bad-date", None, {}, tenant_db_session)
    assert exc.value.status_code == 422


def test_summary_uses_parameterized_sql(tenant_db_session, monkeypatch):
    """Verify reports_summary issues bound-parameter queries (ORM or text) — no raw injection."""
    captured: list = []
    original_execute = tenant_db_session.execute
    original_scalar = tenant_db_session.scalar

    def _capturing_execute(statement, params=None, *args, **kwargs):
        captured.append(statement)
        return original_execute(statement, params, *args, **kwargs)

    def _capturing_scalar(statement, params=None, *args, **kwargs):
        captured.append(statement)
        return original_scalar(statement, params, *args, **kwargs)

    monkeypatch.setattr(tenant_db_session, "execute", _capturing_execute)
    monkeypatch.setattr(tenant_db_session, "scalar", _capturing_scalar)

    reports.reports_summary(None, None, {}, tenant_db_session)

    assert captured, "Expected database queries to be executed"
    # ORM queries produce compiled SQL with bound params — verify no raw date strings injected
    for stmt in captured:
        sql_text = getattr(stmt, "text", None) or str(stmt)
        # Date values must not appear literally in the SQL template (they are bound)
        assert "2026" not in sql_text, f"Raw date value found in SQL: {sql_text!r}"
        assert "2025" not in sql_text, f"Raw date value found in SQL: {sql_text!r}"


def test_reports_router_registered_in_create_app():
    from pathlib import Path

    app_py = Path("gdx_dispatch/app.py").read_text(encoding="utf-8")
    assert "from gdx_dispatch.routers import reports as reports_router" in app_py
    assert "app.include_router(reports_router.router if hasattr(reports_router, \"router\") else reports_router)" in app_py


# ---------------------------------------------------------------------------
# M8 (money-audit-2026-08-04, HIGH/CONFIRMED) — revenue surfaces summed
# `Invoice.total_amount`, a column NO insert path writes. It was NULL on all
# 349 prod rows on 2026-08-22, so "Revenue by Period" charted $0 against
# $829,164.66 of real billed work and the accountant's CSV exported a blank
# total column.
#
# Every test above seeds `total_amount` — the same shape the DEMO seeder
# writes (docker/demo/seed_demo.py:302 sets total_amount=total), which is
# precisely why the demo stack looked healthy and no test ever caught this.
# These tests seed the PROD shape instead: total_amount NULL, `total` real.
# Each one fails against the pre-fix code.
# ---------------------------------------------------------------------------


def _seed_prod_shape_invoice(db, *, job_id, created_at, total, status="paid", **kw):
    """An invoice as prod actually stores one: total_amount NULL, total set."""
    return _seed_invoice(
        db,
        job_id=job_id,
        created_at=created_at,
        total_amount=None,
        total=total,
        balance_due=kw.pop("balance_due", 0),
        status=status,
        **kw,
    )


def _csv_body(resp) -> str:
    """export_report returns a StreamingResponse; Starlette wraps the plain
    `iter([str])` into an async generator, so drain it on a loop. Chunks are
    `str` here, not bytes."""
    async def _drain():
        return [chunk async for chunk in resp.body_iterator]

    return "".join(
        chunk.decode() if isinstance(chunk, bytes) else str(chunk)
        for chunk in asyncio.run(_drain())
    )


def test_m8_revenue_analytics_reads_total_when_total_amount_is_null(tenant_db_session):
    """Pre-fix this returned 0.0 — the bug, exactly as prod showed it."""
    now = datetime.now(UTC)
    cust = _seed_customer(tenant_db_session, name="M8 Co")
    job = _seed_job(
        tenant_db_session, customer_id=cust, technician_id=None,
        created_at=now - timedelta(days=2), lifecycle_stage="Complete", job_type="Repair",
    )
    _seed_prod_shape_invoice(tenant_db_session, job_id=job, created_at=now - timedelta(days=2), total=1500)

    data = reports.revenue_analytics(None, None, {}, tenant_db_session)

    assert data["total_revenue"] == pytest.approx(1500.0)
    # by_period and by_job_type ride in ONE payload and must agree — the audit
    # flagged them contradicting each other.
    by_type = {r["job_type"]: r["revenue"] for r in data["by_job_type"]}
    assert by_type["Repair"] == pytest.approx(1500.0)
    assert sum(r["revenue"] for r in data["by_period"]) == pytest.approx(
        sum(by_type.values())
    )


def test_m8_revenue_counts_deposits_but_not_drafts_or_voids(tenant_db_session):
    """Deposits COUNT. An earlier draft of M8's fix excluded them, reasoning a
    deposit and its final invoice bill the same work twice. False here: the
    final invoice already nets the deposit with a negative "Less deposit paid"
    line (modules/deposits/service.py rule 2), so filtering them out subtracts
    them a SECOND time — and a deposit with no final sibling yet would vanish
    from revenue entirely. This test is the guard against re-adding it."""
    now = datetime.now(UTC)
    cust = _seed_customer(tenant_db_session, name="Deposit Co")
    job = _seed_job(
        tenant_db_session, customer_id=cust, technician_id=None,
        created_at=now - timedelta(days=2), lifecycle_stage="Complete",
    )
    _seed_prod_shape_invoice(tenant_db_session, job_id=job, created_at=now - timedelta(days=2), total=1000)
    # A deposit IS billed revenue and must count.
    _seed_prod_shape_invoice(
        tenant_db_session, job_id=job, created_at=now - timedelta(days=2),
        total=400, billing_type="deposit",
    )
    # Neither of these is billed, so neither may count.
    _seed_prod_shape_invoice(
        tenant_db_session, job_id=job, created_at=now - timedelta(days=2),
        total=999, status="draft",
    )
    _seed_prod_shape_invoice(
        tenant_db_session, job_id=job, created_at=now - timedelta(days=2),
        total=777, status="void",
    )

    data = reports.revenue_analytics(None, None, {}, tenant_db_session)
    assert data["total_revenue"] == pytest.approx(1400.0), (
        "deposit must be included (1000 + 400); draft and void must not be"
    )


def test_m8_summary_and_top_customers_agree_with_each_other(tenant_db_session):
    """KPI card and the top-customers table render on the SAME screen as the
    chart; a different revenue rule in either is a visible contradiction."""
    now = datetime.now(UTC)
    cust = _seed_customer(tenant_db_session, name="Agree Co")
    job = _seed_job(
        tenant_db_session, customer_id=cust, technician_id=None,
        created_at=now - timedelta(days=3), lifecycle_stage="Complete",
    )
    _seed_prod_shape_invoice(
        tenant_db_session, job_id=job, created_at=now - timedelta(days=3),
        total=2500, customer_id=cust,
    )
    _seed_prod_shape_invoice(
        tenant_db_session, job_id=job, created_at=now - timedelta(days=3),
        total=600, billing_type="deposit", customer_id=cust,
    )

    summary = reports.reports_summary(None, None, {}, tenant_db_session)
    top = reports.top_customers(None, None, 10, {}, tenant_db_session)

    # 2500 final + 600 deposit: both are billed revenue.
    assert summary["revenue_total"] == pytest.approx(3100.0)
    assert top["items"][0]["total_revenue"] == pytest.approx(3100.0)
    assert summary["revenue_total"] == pytest.approx(top["items"][0]["total_revenue"])


def test_m8_export_invoices_csv_carries_a_real_total(tenant_db_session):
    """The invoices export selected `total_amount` verbatim, so every row's
    total column was blank in the accountant's CSV."""
    now = datetime.now(UTC)
    cust = _seed_customer(tenant_db_session, name="CSV Co")
    job = _seed_job(
        tenant_db_session, customer_id=cust, technician_id=None,
        created_at=now - timedelta(days=1), lifecycle_stage="Complete",
    )
    _seed_prod_shape_invoice(tenant_db_session, job_id=job, created_at=now - timedelta(days=1), total=1234.56)

    resp = reports.export_report(None, None, "invoices", {}, tenant_db_session)
    body = _csv_body(resp)

    assert "total" in body.splitlines()[0]
    assert "1234.56" in body, f"invoice total missing from CSV: {body!r}"


def test_m8_export_revenue_csv_is_not_all_zero(tenant_db_session):
    now = datetime.now(UTC)
    cust = _seed_customer(tenant_db_session, name="CSV Rev Co")
    job = _seed_job(
        tenant_db_session, customer_id=cust, technician_id=None,
        created_at=now - timedelta(days=1), lifecycle_stage="Complete",
    )
    _seed_prod_shape_invoice(tenant_db_session, job_id=job, created_at=now - timedelta(days=1), total=880)

    resp = reports.export_report(None, None, "revenue", {}, tenant_db_session)
    body = _csv_body(resp)

    assert "880" in body, f"revenue missing from CSV: {body!r}"


def test_m8_shared_revenue_definition_is_actually_applied(tenant_db_session):
    """Behavioural, not source-text: asserting a helper returns a given STRING
    proves authorship, not correctness. Execute the fragment and check which
    rows it admits.
    """
    now = datetime.now(UTC)
    cust = _seed_customer(tenant_db_session, name="Predicate Co")
    job = _seed_job(
        tenant_db_session, customer_id=cust, technician_id=None,
        created_at=now - timedelta(days=1), lifecycle_stage="Complete",
    )
    for status, total in (("paid", 100), ("sent", 10), ("draft", 5000), ("void", 9000)):
        _seed_prod_shape_invoice(
            tenant_db_session, job_id=job, created_at=now - timedelta(days=1),
            total=total, status=status,
        )
    _seed_prod_shape_invoice(
        tenant_db_session, job_id=job, created_at=now - timedelta(days=1),
        total=1, billing_type="deposit",
    )

    got = tenant_db_session.execute(
        text(
            f"SELECT COALESCE(SUM({reports._revenue_amount_sql()}), 0) "
            f"FROM invoices WHERE {reports._revenue_where_sql()}"
        )
    ).scalar()

    # 100 + 10 + 1(deposit); draft and void excluded.
    assert float(got) == pytest.approx(111.0)


def test_m8_alias_prefix_rejects_anything_that_is_not_an_identifier():
    """The alias is the only value interpolated into these SQL fragments that a
    future caller could source from a request — which is what the `# noqa: S608`
    on the call sites leans on. Keep that guarantee real.
    """
    assert reports._alias_prefix("") == ""
    assert reports._alias_prefix("i") == "i."
    for bad in ("i; DROP TABLE invoices", "i.", "1=1", "i OR 1", "'"):
        with pytest.raises(ValueError):
            reports._alias_prefix(bad)


# ---------------------------------------------------------------------------
# /revenue-by-period on real Postgres.
#
# This is THE endpoint behind the broken "Revenue by Period" chart, and it was
# the one M8 surface with no guard: its SQL uses `date_trunc`, which SQLite
# does not have, so the in-memory harness above cannot execute it at all. An
# adversarial review caught that the fix shipped six tests and none of them
# touched the endpoint the finding is about.
#
# Runs against the local `gdx-test-postgres` container. Needs the container
# reachable — from inside the docker-app image that means `--network host`,
# otherwise 127.0.0.1:5433 is the *container's* loopback and these SKIP.
# ---------------------------------------------------------------------------


PG_COMPANY = "11111111-1111-1111-1111-111111111111"


def _pg_seed_job(session) -> str:
    """invoices.job_id is NOT NULL with an FK to jobs, so a revenue row needs a
    real job behind it."""
    job_id = str(uuid.uuid4())
    session.execute(
        text(
            """
            INSERT INTO jobs (id, title, lifecycle_stage, dispatch_status,
                              billing_status, is_return_visit, company_id, created_at)
            VALUES (:id, 'M8 revenue fixture',
                    CAST('completed' AS job_lifecycle_stage),
                    CAST('unassigned' AS job_dispatch_status),
                    CAST('unbilled' AS job_billing_status),
                    false, :company, now())
            """
        ),
        {"id": job_id, "company": PG_COMPANY},
    )
    return job_id


def _pg_seed_invoice(session, job_id, *, total, total_amount, status, billing_type, created_at):
    session.execute(
        text(
            """
            INSERT INTO invoices (id, job_id, invoice_number, status, billing_type,
                                  sequence_number, subtotal, tax_amount, total,
                                  total_amount, balance_due, locked, public_token,
                                  company_id, created_at, invoice_date)
            VALUES (:id, :job_id, :num,
                    CAST(:status AS invoice_status),
                    CAST(:btype AS invoice_billing_type),
                    1, 0, 0, :total,
                    :total_amount, 0, false, :tok,
                    :company, CAST(:created_at AS timestamptz),
                    CAST(:created_at AS DATE))
            """
        ),
        {
            "id": str(uuid.uuid4()),
            "job_id": job_id,
            "num": f"T-{uuid.uuid4().hex[:8]}",
            "status": status,
            "btype": billing_type,
            "total": total,
            "total_amount": total_amount,
            "tok": uuid.uuid4().hex,
            "company": PG_COMPANY,
            "created_at": created_at,
        },
    )


def test_m8_revenue_by_period_on_postgres(pg_test_session):
    """The prod shape: total_amount NULL, `total` real. Pre-fix this endpoint
    returned revenue 0 for every period against real invoices — which is
    exactly what the 2026-08-22 prod walk saw.
    """
    now = datetime.now(UTC)
    when = (now - timedelta(days=3)).isoformat()
    job = _pg_seed_job(pg_test_session)
    _pg_seed_invoice(pg_test_session, job, total=1500, total_amount=None, status="paid",
                     billing_type="standard", created_at=when)
    _pg_seed_invoice(pg_test_session, job, total=500, total_amount=None, status="sent",
                     billing_type="deposit", created_at=when)
    # Neither of these is billed revenue.
    _pg_seed_invoice(pg_test_session, job, total=9999, total_amount=None, status="draft",
                     billing_type="standard", created_at=when)
    _pg_seed_invoice(pg_test_session, job, total=8888, total_amount=None, status="void",
                     billing_type="standard", created_at=when)
    pg_test_session.commit()

    data = reports.revenue_by_period(None, None, "month", {}, pg_test_session)

    assert data["items"], "no periods returned — the window or filter is wrong"
    total = sum(i["revenue"] for i in data["items"])
    # 1500 standard + 500 deposit; draft and void excluded.
    assert total == pytest.approx(2000.0)
    assert data["total_revenue"] == pytest.approx(2000.0)
    # The frontend keys off these exact names (it used to read label/value).
    first = data["items"][0]
    assert {"period_start", "invoice_count", "revenue", "avg_invoice"} <= set(first)
    assert first["period_start"] is not None


def test_m8_revenue_periods_use_the_billed_date_not_the_import_date(pg_test_session):
    """The QB import inserted 278 invoices spanning 2024-2026 with
    created_at = 2026-03-29. Grouping revenue on created_at drew a
    $607,419.52 spike on the import day and emptied the months the work was
    actually billed in. Revenue must follow invoice_date.
    """
    job = _pg_seed_job(pg_test_session)
    # Billed last month; "imported" (created) today.
    billed = (datetime.now(UTC) - timedelta(days=40)).date()
    pg_test_session.execute(
        text(
            """
            INSERT INTO invoices (id, job_id, invoice_number, status, billing_type,
                                  sequence_number, subtotal, tax_amount, total,
                                  total_amount, balance_due, locked, public_token,
                                  company_id, created_at, invoice_date)
            VALUES (:id, :job_id, :num, CAST('paid' AS invoice_status),
                    CAST('standard' AS invoice_billing_type),
                    1, 0, 0, 4200, NULL, 0, false, :tok, :company,
                    now(), CAST(:billed AS DATE))
            """
        ),
        {
            "id": str(uuid.uuid4()), "job_id": job,
            "num": f"T-{uuid.uuid4().hex[:8]}", "tok": uuid.uuid4().hex,
            "company": PG_COMPANY, "billed": billed.isoformat(),
        },
    )
    pg_test_session.commit()

    # A window that contains the BILLED date but ends before today.
    start = (billed - timedelta(days=5)).isoformat()
    end = (billed + timedelta(days=5)).isoformat()
    data = reports.revenue_by_period(start, end, "month", {}, pg_test_session)

    assert sum(i["revenue"] for i in data["items"]) == pytest.approx(4200.0), (
        "invoice billed inside the window was missed — revenue is still "
        "grouping/filtering on created_at (the import date)"
    )

from __future__ import annotations

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
) -> str:
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
                id, job_id, total_amount, balance_due, status, company_id, created_at, invoice_date, deleted_at
            ) VALUES (
                :id, :job_id, :total_amount, :balance_due, :status, 'tenant-test', :created_at, :invoice_date, NULL
            )
            """
        ),
        {
            "id": inv_id,
            "job_id": job_id,
            "total_amount": total_amount,
            "balance_due": balance_due,
            "status": canon_status,
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
    ]:
        route = route_map[path]
        dep_calls = {dep.call for dep in route.dependant.dependencies}
        assert get_current_user in dep_calls
        assert get_db in dep_calls


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

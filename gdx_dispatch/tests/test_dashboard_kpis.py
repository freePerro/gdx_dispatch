"""Tests for sales-funnel / operations / cash-risk dashboard KPI endpoints.

Sprint S107 — adds bookings clock (Estimate.accepted_at), close rate,
estimates outstanding aging, first-time fix, response speed, AR aging,
gross margin, warranty callbacks. Endpoints in gdx_dispatch/routers/reports.py.

Pattern follows test_reports.py — sqlite in-memory tenant DB with the
columns each endpoint reads, seeded via raw SQL helpers.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.database import get_db
from gdx_dispatch.routers import reports
from gdx_dispatch.routers.auth import get_current_user


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


@pytest.fixture
def kpi_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()

    db.execute(text("""
        CREATE TABLE jobs (
            id TEXT PRIMARY KEY,
            customer_id TEXT, title TEXT, lifecycle_stage TEXT, status TEXT,
            scheduled_at TEXT, completed_at TEXT, created_at TEXT NOT NULL,
            assigned_to TEXT, parent_job_id TEXT, is_return_visit INTEGER DEFAULT 0,
            total_amount REAL, deleted_at TEXT, company_id TEXT
        )
    """))
    db.execute(text("""
        CREATE TABLE invoices (
            id TEXT PRIMARY KEY, job_id TEXT, invoice_number TEXT,
            total REAL DEFAULT 0, total_amount REAL, balance_due REAL DEFAULT 0,
            amount_paid REAL DEFAULT 0, status TEXT, due_date TEXT,
            invoice_date TEXT, created_at TEXT NOT NULL, deleted_at TEXT,
            customer_id TEXT, company_id TEXT
        )
    """))
    db.execute(text("""
        CREATE TABLE customers (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, company_id TEXT,
            created_at TEXT, deleted_at TEXT
        )
    """))
    db.execute(text("""
        CREATE TABLE estimates (
            id TEXT PRIMARY KEY, job_id TEXT, customer_id TEXT,
            estimate_number TEXT NOT NULL, label TEXT, total REAL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'draft', sent_at TEXT, accepted_at TEXT,
            declined_at TEXT, public_token TEXT NOT NULL, company_id TEXT NOT NULL,
            created_at TEXT NOT NULL, deleted_at TEXT, proposal_mode INTEGER DEFAULT 0
        )
    """))
    db.execute(text("""
        CREATE TABLE estimate_lines (
            id TEXT PRIMARY KEY, estimate_id TEXT NOT NULL,
            description TEXT NOT NULL, category TEXT, quantity INTEGER DEFAULT 1,
            unit_price REAL DEFAULT 0, line_total REAL DEFAULT 0,
            cost_snapshot REAL, margin_pct_snapshot REAL, margin_pct_override REAL,
            company_id TEXT NOT NULL, created_at TEXT NOT NULL, sort_order INTEGER DEFAULT 1
        )
    """))
    db.execute(text("""
        CREATE TABLE invoice_lines (
            id TEXT PRIMARY KEY, invoice_id TEXT NOT NULL,
            description TEXT, category TEXT,
            unit_price REAL DEFAULT 0, quantity INTEGER DEFAULT 1,
            line_total REAL DEFAULT 0, cost_snapshot REAL,
            margin_pct_snapshot REAL, company_id TEXT,
            created_at TEXT, deleted_at TEXT
        )
    """))
    db.execute(text("""
        CREATE TABLE warranty_claims (
            id TEXT PRIMARY KEY, company_id TEXT NOT NULL, customer_id TEXT NOT NULL,
            job_id TEXT, status TEXT NOT NULL DEFAULT 'filed',
            filed_at TEXT, resolved_at TEXT, created_by TEXT NOT NULL,
            created_at TEXT, deleted_at TEXT
        )
    """))
    db.commit()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def _seed_estimate(db, *, status: str, total: float, accepted_at=None, sent_at=None, lines=None):
    eid = uuid.uuid4().hex
    db.execute(text("""
        INSERT INTO estimates (id, estimate_number, total, status, sent_at, accepted_at,
                               public_token, company_id, created_at)
        VALUES (:id, :num, :tot, :st, :sent, :acc, :tok, 'tenant-test', :created)
    """), {
        "id": eid, "num": eid[:8], "tot": total, "st": status,
        "sent": _iso(sent_at) if sent_at else None,
        "acc": _iso(accepted_at) if accepted_at else None,
        "tok": uuid.uuid4().hex,
        "created": _iso(accepted_at or sent_at or datetime.now(UTC)),
    })
    for ln in lines or []:
        db.execute(text("""
            INSERT INTO estimate_lines (id, estimate_id, description, category, quantity,
                                        unit_price, line_total, cost_snapshot, margin_pct_snapshot,
                                        company_id, created_at)
            VALUES (:id, :eid, :desc, :cat, :qty, :up, :lt, :cs, :mps, 'tenant-test', :created)
        """), {
            "id": uuid.uuid4().hex, "eid": eid, "desc": ln.get("description", "x"),
            "cat": ln.get("category"), "qty": ln.get("quantity", 1),
            "up": ln.get("unit_price", 0), "lt": ln.get("line_total", 0),
            "cs": ln.get("cost_snapshot"), "mps": ln.get("margin_pct_snapshot"),
            "created": _iso(datetime.now(UTC)),
        })
    db.commit()
    return eid


def _seed_job(db, *, lifecycle_stage="completed", created_at=None, completed_at=None,
              scheduled_at=None, is_return_visit=False):
    jid = str(uuid.uuid4())
    cr = created_at or datetime.now(UTC)
    db.execute(text("""
        INSERT INTO jobs (id, lifecycle_stage, created_at, completed_at, scheduled_at,
                          is_return_visit, company_id)
        VALUES (:id, :ls, :cr, :co, :sc, :rv, 'tenant-test')
    """), {
        "id": jid, "ls": lifecycle_stage, "cr": _iso(cr),
        "co": _iso(completed_at) if completed_at else None,
        "sc": _iso(scheduled_at) if scheduled_at else None,
        "rv": 1 if is_return_visit else 0,
    })
    db.commit()
    return jid


def _seed_invoice(db, *, balance_due, due_date, status="sent", total=None, amount_paid=0):
    iid = str(uuid.uuid4())
    db.execute(text("""
        INSERT INTO invoices (id, total, total_amount, balance_due, amount_paid, status,
                              due_date, invoice_date, created_at, company_id)
        VALUES (:id, :t, :ta, :bd, :ap, :st, :dd, :idate, :created, 'tenant-test')
    """), {
        "id": iid, "t": total or balance_due, "ta": total or balance_due,
        "bd": balance_due, "ap": amount_paid, "st": status,
        "dd": due_date.isoformat() if due_date else None,
        "idate": (due_date or datetime.now(UTC).date()).isoformat(),
        "created": _iso(datetime.now(UTC)),
    })
    db.commit()
    return iid


def _seed_warranty(db, *, filed_at):
    wid = str(uuid.uuid4())
    db.execute(text("""
        INSERT INTO warranty_claims (id, company_id, customer_id, status, filed_at, created_by)
        VALUES (:id, 'tenant-test', :cid, 'filed', :fa, 'user-1')
    """), {"id": wid, "cid": str(uuid.uuid4()), "fa": _iso(filed_at)})
    db.commit()
    return wid


def _override_deps(app, db):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {"id": "u-test", "email": "t@t.com"}


def test_endpoints_registered():
    paths = {r.path for r in reports.router.routes}
    assert "/api/reports/sales-funnel" in paths
    assert "/api/reports/operations" in paths
    assert "/api/reports/cash-risk" in paths


def test_sales_funnel_empty(kpi_db):
    from fastapi.testclient import TestClient
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(reports.router)
    _override_deps(app, kpi_db)
    # Bypass module gate for test
    from gdx_dispatch.core.modules import require_module
    app.dependency_overrides[require_module("reports_advanced")] = lambda: None

    r = TestClient(app).get("/api/reports/sales-funnel")
    assert r.status_code == 200
    j = r.json()
    assert j["sold"]["today"]["count"] == 0
    assert j["sold"]["last_30_days"]["dollar_amount"] == 0.0
    assert j["close_rate"]["rate"] is None
    assert j["estimates_outstanding"]["count"] == 0


def test_sales_funnel_with_accepted_estimate(kpi_db):
    from fastapi.testclient import TestClient
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(reports.router)
    _override_deps(app, kpi_db)
    from gdx_dispatch.core.modules import require_module
    app.dependency_overrides[require_module("reports_advanced")] = lambda: None

    now = datetime.now(UTC)
    _seed_estimate(kpi_db, status="accepted", total=4500, accepted_at=now, sent_at=now - timedelta(days=2),
                   lines=[
                       {"category": "doors", "quantity": 2, "unit_price": 1500, "line_total": 3000,
                        "cost_snapshot": 800, "margin_pct_snapshot": 0.46},
                       {"category": "labor", "quantity": 4, "unit_price": 100, "line_total": 400},
                   ])
    _seed_estimate(kpi_db, status="declined", total=2000, sent_at=now - timedelta(days=10))
    _seed_estimate(kpi_db, status="sent", total=1200, sent_at=now - timedelta(days=5))

    r = TestClient(app).get("/api/reports/sales-funnel")
    j = r.json()
    assert j["sold"]["today"]["count"] == 1
    assert j["sold"]["today"]["door_count"] == 2
    assert j["sold"]["today"]["dollar_amount"] == 4500.0
    assert j["sold"]["today"]["avg_ticket"] == 4500.0
    # close_rate = 1 accepted / 2 decisions (accepted + declined) = 0.5
    assert j["close_rate"]["rate"] == 0.5
    assert j["estimates_outstanding"]["count"] == 1
    assert j["estimates_outstanding"]["dollar_amount"] == 1200.0


def test_operations_first_time_fix(kpi_db):
    from fastapi.testclient import TestClient
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(reports.router)
    _override_deps(app, kpi_db)
    from gdx_dispatch.core.modules import require_module
    app.dependency_overrides[require_module("reports_advanced")] = lambda: None

    now = datetime.now(UTC)
    # 3 completed jobs, 1 callback → first-time fix = 2/3
    for _ in range(2):
        _seed_job(kpi_db, lifecycle_stage="completed", completed_at=now - timedelta(days=1))
    _seed_job(kpi_db, lifecycle_stage="completed", completed_at=now - timedelta(days=1), is_return_visit=True)

    # response speed: 1 same-day, 1 next-day, 1 in 5 days
    _seed_job(kpi_db, lifecycle_stage="scheduled", created_at=now - timedelta(days=2),
              scheduled_at=now - timedelta(days=2))
    _seed_job(kpi_db, lifecycle_stage="scheduled", created_at=now - timedelta(days=4),
              scheduled_at=now - timedelta(days=3))
    _seed_job(kpi_db, lifecycle_stage="scheduled", created_at=now - timedelta(days=10),
              scheduled_at=now - timedelta(days=5))

    r = TestClient(app).get("/api/reports/operations")
    j = r.json()
    assert j["first_time_fix"]["completed"] == 3
    assert j["first_time_fix"]["callbacks"] == 1
    assert abs(j["first_time_fix"]["rate"] - (2/3)) < 1e-6
    # response_speed counts only jobs with scheduled_at set; the 3 completed jobs above have no schedule.
    assert j["response_speed"]["total_booked"] == 3
    assert j["avg_job_duration"]["value"] is None
    assert j["tech_utilization"]["value"] is None


def test_cash_risk_aging_buckets(kpi_db):
    from fastapi.testclient import TestClient
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(reports.router)
    _override_deps(app, kpi_db)
    from gdx_dispatch.core.modules import require_module
    app.dependency_overrides[require_module("reports_advanced")] = lambda: None

    today = datetime.now(UTC).date()
    # 1 in current bucket (15d overdue), 1 in 31-60, 1 in 90+
    _seed_invoice(kpi_db, balance_due=500, due_date=today - timedelta(days=15), status="sent")
    _seed_invoice(kpi_db, balance_due=1000, due_date=today - timedelta(days=45), status="sent")
    _seed_invoice(kpi_db, balance_due=300, due_date=today - timedelta(days=120), status="overdue")
    # paid invoice should be excluded
    _seed_invoice(kpi_db, balance_due=0, due_date=today - timedelta(days=10), status="paid")

    r = TestClient(app).get("/api/reports/cash-risk")
    j = r.json()
    assert j["ar_aging"]["buckets"]["current"]["count"] == 1
    assert j["ar_aging"]["buckets"]["current"]["total"] == 500.0
    assert j["ar_aging"]["buckets"]["d31_60"]["count"] == 1
    assert j["ar_aging"]["buckets"]["d90_plus"]["count"] == 1
    assert j["ar_aging"]["total_outstanding"] == 1800.0


def test_cash_risk_gross_margin_and_warranty(kpi_db):
    from fastapi.testclient import TestClient
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(reports.router)
    _override_deps(app, kpi_db)
    from gdx_dispatch.core.modules import require_module
    app.dependency_overrides[require_module("reports_advanced")] = lambda: None

    now = datetime.now(UTC)
    _seed_estimate(kpi_db, status="accepted", total=2000, accepted_at=now,
                   lines=[{"category": "doors", "quantity": 1, "unit_price": 2000, "line_total": 2000,
                            "cost_snapshot": 1200, "margin_pct_snapshot": 0.4}])
    # 5 completed jobs, 1 warranty callback → 20%
    for _ in range(5):
        _seed_job(kpi_db, lifecycle_stage="completed", completed_at=now - timedelta(days=1))
    _seed_warranty(kpi_db, filed_at=now - timedelta(days=2))

    r = TestClient(app).get("/api/reports/cash-risk")
    j = r.json()
    # margin: sell=2000, cost=1200, profit=800 → 0.4
    assert j["gross_margin"]["total_sell"] == 2000.0
    assert j["gross_margin"]["total_cost"] == 1200.0
    assert j["gross_margin"]["margin_pct"] == 0.4
    assert j["warranty_callbacks"]["filed"] == 1
    assert j["warranty_callbacks"]["completed_jobs"] == 5
    assert j["warranty_callbacks"]["rate"] == 0.2

"""Tests for the payroll router (commission rates, summary, CSV export)."""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.payroll import (
    calculate_commission,
    calculate_gross_pay,
    calculate_weekly_overtime,
    router,
)


def _make_client(tenant_id: str = "tenant-test") -> TestClient:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)

    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    setup = Session()
    setup.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS tenant_module_grants (
                id TEXT PRIMARY KEY, tenant_id TEXT, module_key TEXT,
                granted_at TEXT, created_at TEXT, expires_at TEXT
            )
            """
        )
    )
    setup.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS company_module_grants (
                id TEXT PRIMARY KEY, company_id TEXT, module_key TEXT,
                granted_at TEXT, created_at TEXT, expires_at TEXT,
                UNIQUE(company_id, module_key)
            )
            """
        )
    )
    setup.execute(
        text(
            """
            INSERT OR IGNORE INTO tenant_module_grants (id, tenant_id, module_key, granted_at, created_at)
            VALUES (:id, :tid, 'timeclock', datetime('now'), datetime('now'))
            """
        ),
        {"id": f"g1-{tenant_id}", "tid": tenant_id},
    )
    setup.execute(
        text(
            """
            INSERT OR IGNORE INTO company_module_grants (id, company_id, module_key, granted_at, created_at)
            VALUES (:id, :tid, 'timeclock', datetime('now'), datetime('now'))
            """
        ),
        {"id": f"g2-{tenant_id}", "tid": tenant_id},
    )
    setup.commit()
    setup.close()

    def _override_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()

    @app.middleware("http")
    async def inject_tenant(request, call_next):
        request.state.tenant = {"id": tenant_id}
        return await call_next(request)

    app.include_router(router)
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "user-1",
        "sub": "user-1",
        "role": "admin",
        "tenant_id": tenant_id,
    }

    tc = TestClient(app, raise_server_exceptions=True)
    tc._engine = engine  # type: ignore[attr-defined]
    return tc


@pytest.fixture()
def client():
    tc = _make_client()
    yield tc
    tc.app.dependency_overrides.clear()
    tc._engine.dispose()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Commission rate CRUD
# ---------------------------------------------------------------------------
def test_create_commission_rate(client: TestClient):
    r = client.post(
        "/api/payroll/commission-rates",
        json={"tech_id": "tech-1", "rate_type": "percent", "rate_value": 12.5},
    )
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["tech_id"] == "tech-1"
    assert data["rate_type"] == "percent"
    assert data["rate_value"] == 12.5
    assert data["active"] is True
    assert data["effective_until"] is None
    assert data["company_id"] == "tenant-test"


def test_new_rate_expires_prior(client: TestClient):
    a = client.post(
        "/api/payroll/commission-rates",
        json={"tech_id": "tech-2", "rate_type": "percent", "rate_value": 10},
    ).json()
    b = client.post(
        "/api/payroll/commission-rates",
        json={"tech_id": "tech-2", "rate_type": "percent", "rate_value": 15},
    ).json()
    assert a["id"] != b["id"]

    # Fetch all rates for tech-2 including inactive ones
    all_rates = client.get(
        "/api/payroll/commission-rates",
        params={"tech_id": "tech-2", "active_only": "false"},
    ).json()
    by_id = {r["id"]: r for r in all_rates}
    assert by_id[a["id"]]["effective_until"] is not None
    assert by_id[a["id"]]["active"] is False
    assert by_id[b["id"]]["effective_until"] is None
    assert by_id[b["id"]]["active"] is True


def test_list_active_rates(client: TestClient):
    client.post(
        "/api/payroll/commission-rates",
        json={"tech_id": "tech-3", "rate_type": "flat", "rate_value": 50},
    )
    client.post(
        "/api/payroll/commission-rates",
        json={"tech_id": "tech-4", "rate_type": "hourly", "rate_value": 5},
    )
    active = client.get("/api/payroll/commission-rates").json()
    assert len(active) == 2
    tech_ids = {r["tech_id"] for r in active}
    assert tech_ids == {"tech-3", "tech-4"}


def test_rate_type_validation(client: TestClient):
    r = client.post(
        "/api/payroll/commission-rates",
        json={"tech_id": "tech-x", "rate_type": "bogus", "rate_value": 10},
    )
    assert r.status_code == 422


def test_rate_value_bounds_negative(client: TestClient):
    r = client.post(
        "/api/payroll/commission-rates",
        json={"tech_id": "tech-x", "rate_type": "percent", "rate_value": -1},
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Summary / export (degrade gracefully when time_entries missing)
# ---------------------------------------------------------------------------
def test_summary_refuses_when_the_revenue_basis_is_broken(client: TestClient):
    """Was `test_summary_returns_empty_when_time_entries_missing`, and its
    premise was wrong.

    It asserted 200 + an empty list, and passed — but not for the reason its
    name gave. The revenue query names `j.assigned_tech_id`, a column that
    exists in no schema here, so it raised, the handler swallowed it, and the
    endpoint returned an empty page. The test proved the swallow, not the
    degrade. Now the refusal is the assertion (money audit M27 follow-up).
    """
    r = client.get(
        "/api/payroll/summary",
        params={"start": "2026-01-01", "end": "2026-01-31"},
    )
    assert r.status_code == 503, r.text
    assert "cannot be computed" in r.json()["detail"]


def test_summary_bad_date_range(client: TestClient):
    r = client.get(
        "/api/payroll/summary",
        params={"start": "2026-02-01", "end": "2026-01-01"},
    )
    assert r.status_code == 422


def test_summary_bad_date_format(client: TestClient):
    r = client.get("/api/payroll/summary", params={"start": "not-a-date"})
    assert r.status_code == 422


def test_tech_detail_refuses_rather_than_returning_a_zero_row(client: TestClient):
    """Was `test_tech_detail_returns_zero_row`. A zero row for a named person
    on a pay screen is a claim about what they earned. It was never computed."""
    r = client.get(
        "/api/payroll/tech/tech-99",
        params={"start": "2026-01-01", "end": "2026-01-31"},
    )
    assert r.status_code == 503, r.text


def test_export_refuses_rather_than_writing_a_csv_of_zeroes(client: TestClient):
    """Was `test_export_returns_csv`, and it was the most misleading of the
    three: it asserted a well-formed CSV with a `gross_pay` column and passed,
    while the file's contents came from a query that had raised. A CSV is the
    shape that leaves the building — a spreadsheet of $0.00 gross pay is worse
    than no file."""
    r = client.get(
        "/api/payroll/export",
        params={"start": "2026-01-01", "end": "2026-01-31"},
    )
    assert r.status_code == 503, r.text


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------
def test_tenant_scope():
    c1 = _make_client(tenant_id="tenant-a")
    c2 = _make_client(tenant_id="tenant-b")
    try:
        r1 = c1.post(
            "/api/payroll/commission-rates",
            json={"tech_id": "techA", "rate_type": "percent", "rate_value": 10},
        )
        assert r1.status_code == 201
        r2 = c2.post(
            "/api/payroll/commission-rates",
            json={"tech_id": "techB", "rate_type": "percent", "rate_value": 20},
        )
        assert r2.status_code == 201

        list_a = c1.get("/api/payroll/commission-rates").json()
        list_b = c2.get("/api/payroll/commission-rates").json()
        assert len(list_a) == 1
        assert len(list_b) == 1
        assert list_a[0]["tech_id"] == "techA"
        assert list_b[0]["tech_id"] == "techB"
    finally:
        c1.app.dependency_overrides.clear()
        c2.app.dependency_overrides.clear()
        c1._engine.dispose()  # type: ignore[attr-defined]
        c2._engine.dispose()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Pure math (unit tests for overtime + commission)
# ---------------------------------------------------------------------------
def test_overtime_under_40_hours_all_regular():
    # Mon-Fri 8 hours each = 40 hours, no OT
    days = {date(2026, 1, 5) + timedelta(days=i): 8.0 for i in range(5)}
    reg, ot = calculate_weekly_overtime(days)
    assert reg == 40.0
    assert ot == 0.0


def test_overtime_over_40_hours_split():
    # 50 hours in one week → 40 regular + 10 OT
    days = {date(2026, 1, 5) + timedelta(days=i): 10.0 for i in range(5)}
    reg, ot = calculate_weekly_overtime(days)
    assert reg == 40.0
    assert ot == 10.0


def test_overtime_across_weeks():
    # Week 1 (Mon Jan 5): 45h → 40 reg + 5 OT
    # Week 2 (Mon Jan 12): 30h → 30 reg + 0 OT
    days = {}
    for i in range(5):
        days[date(2026, 1, 5) + timedelta(days=i)] = 9.0  # 45h
    for i in range(5):
        days[date(2026, 1, 12) + timedelta(days=i)] = 6.0  # 30h
    reg, ot = calculate_weekly_overtime(days)
    assert reg == 70.0
    assert ot == 5.0


def test_commission_percent():
    assert calculate_commission(
        rate_type="percent", rate_value=10, revenue=1000,
        jobs_completed=5, hours_worked=40,
    ) == 100.0


def test_commission_flat():
    assert calculate_commission(
        rate_type="flat", rate_value=25, revenue=1000,
        jobs_completed=4, hours_worked=40,
    ) == 100.0


def test_commission_hourly():
    assert calculate_commission(
        rate_type="hourly", rate_value=3, revenue=1000,
        jobs_completed=5, hours_worked=40,
    ) == 120.0


def test_gross_pay_with_overtime():
    # 40 reg + 10 OT at $20/hr base + $50 commission
    # = 40*20 + 10*20*1.5 + 50 = 800 + 300 + 50 = 1150
    assert calculate_gross_pay(
        regular_hours=40, overtime_hours=10, base_rate=20, commission=50
    ) == 1150.0


# ---------------------------------------------------------------------------
# The revenue basis fails loudly (money audit M27 follow-up, 2026-08-23)
# ---------------------------------------------------------------------------


class TestRevenueBasisRefusesRatherThanReportingZero:
    """`_fetch_tech_revenue` used to swallow an OperationalError and return
    `{}`, so every tech reported $0.00 revenue and $0.00 commission as though
    it had been calculated.

    It has been failing that way continuously: the query names
    `j.assigned_tech_id`, a column that exists in no schema here — the jobs
    table has `assigned_to`. An empty dict is indistinguishable from "this
    tech earned nothing", which on a pay surface is the difference between a
    number and a guess.

    The query is deliberately NOT repaired here. Commission is heading for a
    plugin (`docs/design/commission-as-a-plugin-plan.md`), and repairing it in
    place would turn a silent zero into a confident wrong number: the invoice
    join still counts voided and draft invoices (M27), the status literal
    matches neither spelling this tenant stores, and the deposit basis is
    undecided.
    """

    def test_the_summary_endpoint_503s_rather_than_paging_zeroes(self, client: TestClient):
        """A page of $0.00 rows reads as a calculation. A 503 reads as what it
        is — and names which half still works."""
        r = client.get("/api/payroll/summary")
        assert r.status_code == 503, r.text
        detail = r.json()["detail"]
        assert "cannot be computed" in detail
        assert "Hours are unaffected" in detail

    def test_the_tech_detail_endpoint_refuses_too(self, client: TestClient):
        r = client.get("/api/payroll/tech/some-tech-id")
        assert r.status_code == 503, r.text

    def test_the_export_refuses_rather_than_writing_a_csv_of_zeroes(self, client: TestClient):
        """A CSV is the shape that leaves the building. A file of $0.00
        commission rows is worse than no file."""
        r = client.get("/api/payroll/export")
        assert r.status_code == 503, r.text

    def test_the_503_points_at_a_log_key_that_is_actually_emitted(self, client: TestClient, caplog):
        """The 503 tells the reader which log line to look for. A pointer that
        is right in tests and wrong in production is worse than none.

        It was wrong: the live cause — the missing `j.assigned_tech_id` column
        — raises psycopg2 UndefinedColumn on Postgres, which maps to SQLAlchemy
        `ProgrammingError`. That is NOT a subclass of `OperationalError`, so
        production took the second `except` arm and logged a different key,
        while SQLite (which raises `OperationalError` for the same SQL) took
        the first. Measured on prod 2026-08-23 before the fix: **0** hits for
        the key the 503 named, **2** for the one actually written.

        Both arms now emit `REVENUE_BASIS_LOG_KEY`, and this asserts the body
        and the log agree.
        """
        import logging

        from gdx_dispatch.routers.payroll import REVENUE_BASIS_LOG_KEY

        with caplog.at_level(logging.ERROR, logger="gdx_dispatch.routers.payroll"):
            r = client.get("/api/payroll/summary")
        assert r.status_code == 503
        assert REVENUE_BASIS_LOG_KEY in r.json()["detail"], "the 503 must name the key"
        assert any(REVENUE_BASIS_LOG_KEY in rec.getMessage() for rec in caplog.records), (
            "the handler must actually emit the key the 503 points at"
        )

    def test_both_failure_arms_emit_the_same_key(self):
        """Guards the arm SQLite cannot reach. `ProgrammingError` (Postgres'
        missing-column path) and `OperationalError` (SQLite's) must not log
        different names, or the 503 pointer is right on only one engine."""
        import inspect

        from sqlalchemy.exc import OperationalError, ProgrammingError

        from gdx_dispatch.routers import payroll as _p

        # The hierarchy fact the bug turned on — assert it, do not assume it.
        assert not issubclass(ProgrammingError, OperationalError)

        src = inspect.getsource(_p._fetch_tech_revenue)
        # Every log.exception in the helper routes through the constant.
        emits = [ln for ln in src.splitlines() if "log.exception" in ln]
        assert emits, "expected the helper to log its failures"
        assert all("REVENUE_BASIS_LOG_KEY" in ln for ln in emits), (
            f"an arm logs a different key: {emits}"
        )

    def test_the_helper_raises_a_named_error(self, client: TestClient):
        """Not a bare Exception — callers have to be able to tell this apart
        from a genuine empty period."""
        from datetime import date as _date

        from gdx_dispatch.routers.payroll import (
            RevenueBasisUnavailable,
            _fetch_tech_revenue,
        )

        db = sessionmaker(bind=client._engine)()  # type: ignore[attr-defined]
        try:
            with pytest.raises(RevenueBasisUnavailable):
                _fetch_tech_revenue(
                    db, tenant_id="tenant-test",
                    start=_date(2026, 1, 1), end=_date(2026, 12, 31),
                )
        finally:
            db.close()

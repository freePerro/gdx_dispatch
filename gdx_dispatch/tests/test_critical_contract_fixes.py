"""The contract-gap sweep's FIX-IMMEDIATELY singletons (2026-07-24 doc).

1. Mobile invoice email — build_invoice_email_html was called without its
   required subtotal/tax_amount/balance_due kwargs; the TypeError was
   swallowed and every truck email since mobile invoicing shipped silently
   returned False. Every prior test mocked _send_invoice_email, so the
   suite never noticed. The test here runs the REAL builder path with only
   the provider mocked.
2. Forecasting GETs — had no authorization beyond login; any technician
   could pull revenue projections by URL. Now require accounting.read.
3. /api/jobs?date= — was ignored server-side while both dispatch boards
   sent it; with the 50-row default page, a busy tenant's board silently
   missed jobs. Now filters a ±1-day UTC window and lifts the page cap.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models import tenant_models  # noqa: F401 — registers tables
from gdx_dispatch.models.tenant_models import Customer, Invoice, InvoiceLine, Job

TENANT = "tenant-test"


def _engine_and_sessions():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    return engine, sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _make_app(routers: list, role: str = "admin"):
    """Minimal wired app: tenant middleware + auth/db overrides."""
    from gdx_dispatch.core.auth import get_current_user as core_gcu
    from gdx_dispatch.core.database import get_db as core_get_db
    from gdx_dispatch.routers.auth import get_current_user as routers_gcu

    engine, SessionLocal = _engine_and_sessions()
    app = FastAPI()

    @app.middleware("http")
    async def _tenant_shim(request, call_next):
        request.state.tenant = {"id": TENANT, "slug": TENANT}
        return await call_next(request)

    for r in routers:
        app.include_router(r)

    def _override_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    user = {"sub": "u-1", "user_id": "u-1", "role": role, "tenant_id": TENANT}
    app.dependency_overrides[core_get_db] = _override_db
    app.dependency_overrides[routers_gcu] = lambda: user
    app.dependency_overrides[core_gcu] = lambda: user
    return app, SessionLocal, engine


# ── 1. Mobile invoice email actually builds + sends ────────────────────────


def test_mobile_invoice_email_really_sends():
    engine, SessionLocal = _engine_and_sessions()
    db = SessionLocal()
    try:
        cust = Customer(id=uuid4(), company_id=TENANT, name="Acme", email="a@example.com")
        db.add(cust)
        inv = Invoice(
            id=uuid4(),
            company_id=TENANT,
            customer_id=cust.id,
            invoice_number="INV-CRIT-1",
            public_token=uuid4().hex,
            subtotal=100.0,
            tax_amount=8.38,
            total=108.38,
            balance_due=58.38,
            status="sent",
        )
        db.add(inv)
        db.add(
            InvoiceLine(
                id=uuid4(),
                invoice_id=inv.id,
                company_id=TENANT,
                description="Spring replacement",
                quantity=1,
                unit_price=100.0,
                line_total=100.0,
                sort_order=1,
            )
        )
        db.commit()
        db.refresh(inv)

        from gdx_dispatch.routers import mobile_invoicing

        captured: dict = {}

        def _fake_send(**kwargs):
            captured.update(kwargs)
            return True, "test-provider", None

        # ONLY the provider is mocked — the html builder, the kwargs it
        # requires, and the PDF-attach best-effort all run for real. Before
        # the fix this returned False (TypeError swallowed) without ever
        # reaching the provider. (_send_invoice_email imports the provider
        # lazily, so patch at the source module.)
        with patch(
            "gdx_dispatch.core.transactional_email.send_transactional_email", _fake_send
        ):
            ok = mobile_invoicing._send_invoice_email(db, inv, tenant_id=TENANT, user_id="u-1")

        assert ok is True
        assert captured.get("to_email") == "a@example.com"
        html = captured.get("html_body") or ""
        # The specialised builder's fingerprints — proves we did not fall
        # back to the bare-bones f-string body.
        assert "INV-CRIT-1" in captured.get("subject", "")
        assert "58.38" in html  # balance due rendered
        assert "100.00" in html or "100.0" in html  # subtotal rendered
    finally:
        db.close()
        engine.dispose()


def test_mobile_invoice_email_skips_soft_deleted_lines():
    """A mobile re-send after an office edit must not email ghost lines —
    the mobile lines query previously had no deleted_at filter (the
    canonical desktop path filters it)."""
    engine, SessionLocal = _engine_and_sessions()
    db = SessionLocal()
    try:
        cust = Customer(id=uuid4(), company_id=TENANT, name="Acme", email="a@example.com")
        db.add(cust)
        inv = Invoice(
            id=uuid4(),
            company_id=TENANT,
            customer_id=cust.id,
            invoice_number="INV-CRIT-2",
            public_token=uuid4().hex,
            subtotal=50.0,
            tax_amount=0.0,
            total=50.0,
            balance_due=50.0,
            status="sent",
        )
        db.add(inv)
        db.add(
            InvoiceLine(
                id=uuid4(),
                invoice_id=inv.id,
                company_id=TENANT,
                description="Live line",
                quantity=1,
                unit_price=50.0,
                line_total=50.0,
                sort_order=1,
            )
        )
        db.add(
            InvoiceLine(
                id=uuid4(),
                invoice_id=inv.id,
                company_id=TENANT,
                description="Ghost line removed by office",
                quantity=1,
                unit_price=999.0,
                line_total=999.0,
                sort_order=2,
                deleted_at=datetime.now(UTC),
            )
        )
        db.commit()
        db.refresh(inv)

        from gdx_dispatch.routers import mobile_invoicing

        captured: dict = {}

        def _fake_send(**kwargs):
            captured.update(kwargs)
            return True, "test-provider", None

        with patch(
            "gdx_dispatch.core.transactional_email.send_transactional_email", _fake_send
        ):
            ok = mobile_invoicing._send_invoice_email(db, inv, tenant_id=TENANT, user_id="u-1")

        assert ok is True
        html = captured.get("html_body") or ""
        assert "Live line" in html
        assert "Ghost line removed by office" not in html
    finally:
        db.close()
        engine.dispose()


# ── 2. Forecasting GETs require accounting.read ────────────────────────────


@pytest.mark.parametrize(
    ("role", "expected"),
    [("technician", 403), ("accounting", 200), ("admin", 200), ("owner", 200)],
)
def test_forecast_revenue_requires_accounting_read(role: str, expected: int):
    from gdx_dispatch.modules.forecasting import router as forecasting_router

    app, SessionLocal, engine = _make_app([forecasting_router.router], role=role)
    try:
        client = TestClient(app)
        r = client.get("/api/forecast/revenue")
        assert r.status_code == expected, r.text
        if expected == 403:
            assert "accounting.read" in r.text
    finally:
        engine.dispose()


def test_forecast_settings_blocked_for_technician():
    from gdx_dispatch.modules.forecasting import router as forecasting_router

    app, SessionLocal, engine = _make_app([forecasting_router.router], role="technician")
    try:
        client = TestClient(app)
        for path in (
            "/api/forecast/settings",
            "/api/forecast/snapshots",
            "/api/forecast/recurring/streams",
        ):
            assert client.get(path).status_code == 403, path
    finally:
        engine.dispose()


# ── 3. /api/jobs?date= filters server-side and lifts the page cap ──────────


def _seed_jobs(SessionLocal, day: datetime):
    db = SessionLocal()
    try:
        # 60 jobs on the selected day — more than the old 50-row default page.
        for i in range(60):
            db.add(
                Job(
                    id=uuid4(),
                    company_id=TENANT,
                    title=f"day-job-{i}",
                    status="Scheduled",
                    scheduled_at=day + timedelta(hours=8, minutes=i),
                )
            )
        # A job a week out — must NOT be returned for the selected date.
        db.add(
            Job(
                id=uuid4(),
                company_id=TENANT,
                title="far-job",
                status="Scheduled",
                scheduled_at=day + timedelta(days=7),
            )
        )
        # An undated job — stays included (boards surface it on today).
        db.add(Job(id=uuid4(), company_id=TENANT, title="undated-job", status="New"))
        db.commit()
    finally:
        db.close()


def test_jobs_date_param_returns_full_day_not_page_one():
    from gdx_dispatch.routers import jobs as jobs_router

    app, SessionLocal, engine = _make_app([jobs_router.router], role="admin")
    try:
        day = datetime(2026, 7, 24, tzinfo=UTC)
        _seed_jobs(SessionLocal, day)
        client = TestClient(app)

        r = client.get("/api/jobs?date=2026-07-24")
        assert r.status_code == 200, r.text
        items = r.json()["items"]
        titles = {j["title"] for j in items}
        day_jobs = [t for t in titles if t.startswith("day-job-")]
        assert len(day_jobs) == 60  # all of the day's jobs — not page 1's 50
        assert "undated-job" in titles
        assert "far-job" not in titles
    finally:
        engine.dispose()


def test_jobs_date_param_respects_explicit_page_size():
    from gdx_dispatch.routers import jobs as jobs_router

    app, SessionLocal, engine = _make_app([jobs_router.router], role="admin")
    try:
        day = datetime(2026, 7, 24, tzinfo=UTC)
        _seed_jobs(SessionLocal, day)
        client = TestClient(app)

        r = client.get("/api/jobs?date=2026-07-24&page_size=10")
        assert r.status_code == 200, r.text
        assert len(r.json()["items"]) == 10
    finally:
        engine.dispose()


def test_jobs_date_param_rejects_garbage():
    from gdx_dispatch.routers import jobs as jobs_router

    app, SessionLocal, engine = _make_app([jobs_router.router], role="admin")
    try:
        client = TestClient(app)
        r = client.get("/api/jobs?date=not-a-date")
        assert r.status_code == 422
    finally:
        engine.dispose()


def test_jobs_without_date_keeps_default_page_size():
    from gdx_dispatch.routers import jobs as jobs_router

    app, SessionLocal, engine = _make_app([jobs_router.router], role="admin")
    try:
        day = datetime(2026, 7, 24, tzinfo=UTC)
        _seed_jobs(SessionLocal, day)
        client = TestClient(app)

        r = client.get("/api/jobs")
        assert r.status_code == 200, r.text
        assert len(r.json()["items"]) == 50  # unchanged default paging
    finally:
        engine.dispose()


def test_jobs_date_range_covers_week_view():
    """The desktop board's Week view fetches date_from/date_to — the server
    must return the whole week, widened, not a single day's window."""
    from gdx_dispatch.routers import jobs as jobs_router

    app, SessionLocal, engine = _make_app([jobs_router.router], role="admin")
    try:
        db = SessionLocal()
        try:
            sunday = datetime(2026, 7, 19, tzinfo=UTC)
            for i in range(7):
                db.add(
                    Job(
                        id=uuid4(),
                        company_id=TENANT,
                        title=f"week-job-{i}",
                        status="Scheduled",
                        scheduled_at=sunday + timedelta(days=i, hours=9),
                    )
                )
            db.add(
                Job(
                    id=uuid4(),
                    company_id=TENANT,
                    title="next-month-job",
                    status="Scheduled",
                    scheduled_at=sunday + timedelta(days=30),
                )
            )
            db.commit()
        finally:
            db.close()

        client = TestClient(app)
        r = client.get("/api/jobs?date_from=2026-07-19&date_to=2026-07-25")
        assert r.status_code == 200, r.text
        titles = {j["title"] for j in r.json()["items"]}
        for i in range(7):
            assert f"week-job-{i}" in titles
        assert "next-month-job" not in titles
    finally:
        engine.dispose()


def test_jobs_date_range_requires_both_bounds():
    from gdx_dispatch.routers import jobs as jobs_router

    app, SessionLocal, engine = _make_app([jobs_router.router], role="admin")
    try:
        client = TestClient(app)
        assert client.get("/api/jobs?date_from=2026-07-19").status_code == 422
        assert client.get("/api/jobs?date_to=2026-07-25").status_code == 422
        assert (
            client.get("/api/jobs?date_from=2026-07-25&date_to=2026-07-19").status_code == 422
        )
    finally:
        engine.dispose()

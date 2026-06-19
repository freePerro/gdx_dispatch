"""Tests for GET /api/estimates/pipeline-summary.

Mirrors the dashboard widget: count + total_sell + net_profit + blended
margin across all NON-CONVERTED estimates. Math must match
EstimateProfitPanel.vue (engine lines only; manual lines excluded from
both cost and sell).
"""
from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.modules.proposals.models import Estimate, EstimateLine
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.estimates import router


def _make_client(role: str = "admin") -> TestClient:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    setup = Session()
    setup.execute(text(
        "CREATE TABLE IF NOT EXISTS tenant_module_grants ("
        "id TEXT PRIMARY KEY, tenant_id TEXT, module_key TEXT, "
        "granted_at TEXT, created_at TEXT, expires_at TEXT)"
    ))
    setup.execute(text(
        "CREATE TABLE IF NOT EXISTS company_module_grants ("
        "id TEXT PRIMARY KEY, company_id TEXT, module_key TEXT, "
        "granted_at TEXT, created_at TEXT, expires_at TEXT, "
        "UNIQUE(company_id, module_key))"
    ))
    setup.execute(text(
        "INSERT OR IGNORE INTO tenant_module_grants (id, tenant_id, module_key, granted_at, created_at)"
        " VALUES ('g1','tenant-test','estimates',datetime('now'),datetime('now'))"
    ))
    setup.execute(text(
        "INSERT OR IGNORE INTO company_module_grants (id, company_id, module_key, granted_at, created_at)"
        " VALUES ('g2','tenant-test','estimates',datetime('now'),datetime('now'))"
    ))
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
        request.state.tenant = {"id": "tenant-test"}
        request.state.current_user = {
            "user_id": "user-1",
            "role": role,
            "tenant_id": "tenant-test",
        }
        return await call_next(request)

    app.include_router(router)
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "user-1",
        "role": role,
        "tenant_id": "tenant-test",
    }

    tc = TestClient(app, raise_server_exceptions=True)
    tc._engine = engine  # type: ignore[attr-defined]
    tc._Session = Session  # type: ignore[attr-defined]
    return tc


def _seed_estimate(
    client: TestClient,
    *,
    status: str = "sent",
    job_id: UUID | None = None,
    deleted: bool = False,
    lines: list[dict] | None = None,
    estimate_number: str | None = None,
) -> Estimate:
    db = client._Session()  # type: ignore[attr-defined]
    try:
        from uuid import uuid4
        est = Estimate(
            estimate_number=estimate_number or f"E-{uuid4().hex[:8]}",
            status=status,
            job_id=job_id,
            company_id="tenant-test",
            public_token=uuid4().hex,
        )
        if deleted:
            from datetime import UTC, datetime
            est.deleted_at = datetime.now(UTC)
        db.add(est)
        db.flush()
        for ln in lines or []:
            db.add(EstimateLine(
                estimate_id=est.id,
                description=ln.get("description", "line"),
                quantity=ln.get("quantity", 1),
                unit_price=Decimal(str(ln["unit_price"])),
                line_total=Decimal(str(ln["unit_price"])) * ln.get("quantity", 1),
                cost_snapshot=Decimal(str(ln["cost_snapshot"])) if ln.get("cost_snapshot") is not None else None,
                margin_pct_snapshot=Decimal(str(ln["margin_pct_snapshot"])) if ln.get("margin_pct_snapshot") is not None else None,
                company_id="tenant-test",
            ))
        db.commit()
        db.refresh(est)
        return est
    finally:
        db.close()


def test_empty_pipeline_returns_zeros():
    client = _make_client()
    r = client.get("/api/estimates/pipeline-summary")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data == {
        "count": 0,
        "total_sell": 0.0,
        "total_cost": 0.0,
        "net_profit": 0.0,
        "blended_margin": 0.0,
        "estimates_with_manual_lines": 0,
    }


def test_aggregates_engine_lines():
    client = _make_client()
    # Mirrors Doug's example: 3 lines, 2 with cost, 1 manual ($1300 install).
    _seed_estimate(client, status="sent", lines=[
        {"unit_price": "2962.11", "cost_snapshot": "1925.37", "margin_pct_snapshot": "0.35", "quantity": 1},
        {"unit_price": "1337.45", "cost_snapshot": "869.34", "margin_pct_snapshot": "0.35", "quantity": 1},
        {"unit_price": "1300.00", "cost_snapshot": "1300.00", "margin_pct_snapshot": "0.0", "quantity": 1},
    ])
    r = client.get("/api/estimates/pipeline-summary")
    assert r.status_code == 200
    d = r.json()
    assert d["count"] == 1
    assert d["total_sell"] == pytest.approx(5599.56)
    assert d["total_cost"] == pytest.approx(4094.71)
    assert d["net_profit"] == pytest.approx(1504.85)
    assert d["blended_margin"] == pytest.approx(0.2687, abs=1e-3)
    assert d["estimates_with_manual_lines"] == 0


def test_manual_lines_excluded_and_counted():
    client = _make_client()
    # One engine line + one manual line (no cost_snapshot). Manual should
    # NOT contribute to either cost or sell — same as the per-estimate panel.
    _seed_estimate(client, status="draft", lines=[
        {"unit_price": "1000.00", "cost_snapshot": "600.00", "margin_pct_snapshot": "0.4", "quantity": 1},
        {"unit_price": "500.00", "cost_snapshot": None, "margin_pct_snapshot": None, "quantity": 1},
    ])
    r = client.get("/api/estimates/pipeline-summary").json()
    assert r["count"] == 1
    assert r["total_sell"] == pytest.approx(1000.00)
    assert r["total_cost"] == pytest.approx(600.00)
    assert r["net_profit"] == pytest.approx(400.00)
    assert r["estimates_with_manual_lines"] == 1


def test_excludes_converted_deleted_and_terminal_status():
    client = _make_client()
    from uuid import uuid4
    # Should be EXCLUDED:
    fake_job_id = uuid4()
    _seed_estimate(client, status="accepted", job_id=fake_job_id, lines=[  # converted
        {"unit_price": "999", "cost_snapshot": "500", "margin_pct_snapshot": "0.5", "quantity": 1},
    ])
    _seed_estimate(client, status="sent", deleted=True, lines=[  # soft-deleted
        {"unit_price": "999", "cost_snapshot": "500", "margin_pct_snapshot": "0.5", "quantity": 1},
    ])
    for terminal in ("declined", "rejected", "expired"):
        _seed_estimate(client, status=terminal, lines=[
            {"unit_price": "999", "cost_snapshot": "500", "margin_pct_snapshot": "0.5", "quantity": 1},
        ])
    # Should be INCLUDED:
    _seed_estimate(client, status="draft", lines=[
        {"unit_price": "200", "cost_snapshot": "100", "margin_pct_snapshot": "0.5", "quantity": 2},
    ])
    _seed_estimate(client, status="accepted", lines=[  # accepted but not yet converted
        {"unit_price": "300", "cost_snapshot": "150", "margin_pct_snapshot": "0.5", "quantity": 1},
    ])
    d = client.get("/api/estimates/pipeline-summary").json()
    assert d["count"] == 2
    assert d["total_sell"] == pytest.approx(700.00)  # 200*2 + 300
    assert d["total_cost"] == pytest.approx(350.00)  # 100*2 + 150
    assert d["net_profit"] == pytest.approx(350.00)


def test_excludes_zero_line_drafts():
    """S-autosave slice 4 — server-side draft autosave creates draft rows the
    moment a customer is picked. Drafts with no lines yet are not real
    pipeline; they're an in-progress form. Only count estimates with ≥1 line."""
    client = _make_client()
    # Empty draft (would otherwise leak into the count).
    _seed_estimate(client, status="draft", lines=[])
    # Real draft with a line — should count.
    _seed_estimate(client, status="draft", lines=[
        {"unit_price": "100", "cost_snapshot": "60", "margin_pct_snapshot": "0.4", "quantity": 1},
    ])
    d = client.get("/api/estimates/pipeline-summary").json()
    assert d["count"] == 1
    assert d["total_sell"] == pytest.approx(100.00)


def test_technician_role_is_forbidden():
    client = _make_client(role="technician")
    r = client.get("/api/estimates/pipeline-summary")
    assert r.status_code == 403


def test_viewer_role_is_forbidden():
    client = _make_client(role="viewer")
    r = client.get("/api/estimates/pipeline-summary")
    assert r.status_code == 403


def test_dispatcher_role_is_allowed():
    client = _make_client(role="dispatcher")
    r = client.get("/api/estimates/pipeline-summary")
    assert r.status_code == 200


def test_sales_role_is_allowed():
    client = _make_client(role="sales")
    r = client.get("/api/estimates/pipeline-summary")
    assert r.status_code == 200

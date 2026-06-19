"""Tests for AI quote generate/history/feedback endpoints."""
from __future__ import annotations

from collections.abc import Generator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.ai_quote import router as ai_quote_router
from gdx_dispatch.core.database import get_db


def _override_auth():
    return None


@pytest.fixture()
def client(tmp_path) -> Generator[TestClient, None, None]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = SessionLocal()

    # Create QuoteTemplate table for the suggestion function
    db.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS quote_templates (
                id TEXT PRIMARY KEY,
                tenant_id TEXT,
                job_type TEXT,
                typical_parts TEXT,
                typical_labor_hours REAL,
                typical_price_low REAL,
                typical_price_high REAL,
                last_used_at TIMESTAMP,
                use_count INTEGER DEFAULT 0,
                created_at TIMESTAMP,
                deleted_at TIMESTAMP
            )
            """
        )
    )
    db.commit()

    app = FastAPI()
    app.include_router(ai_quote_router)

    # Mock tenant state and user role
    @app.middleware("http")
    async def inject_tenant(request, call_next):
        request.state.tenant = {"id": "test-tenant-123"}
        request.state.current_user = {"role": "admin", "user_id": "test-user"}
        return await call_next(request)

    app.dependency_overrides[get_db] = lambda: db

    yield TestClient(app)

    db.close()
    engine.dispose()


def test_quote_generate_returns_suggestion(client: TestClient) -> None:
    resp = client.post(
        "/api/ai/quote-generate",
        json={
            "job_type": "Spring Replacement",
            "customer_id": "cust-1",
            "notes": "Broken torsion spring on 16x7 door",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "id" in data
    assert data["job_type"] == "Spring Replacement"
    assert "suggested_parts" in data
    assert data["estimated_total_low"] > 0
    assert data["labor_hours"] > 0


def test_quote_history_returns_paginated(client: TestClient) -> None:
    # Generate two quotes first
    client.post(
        "/api/ai/quote-generate",
        json={"job_type": "Spring Replacement", "notes": "spring broke"},
    )
    client.post(
        "/api/ai/quote-generate",
        json={"job_type": "Opener Install", "notes": "new opener"},
    )

    resp = client.get("/api/ai/quote-history?page=1&per_page=10")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 2
    assert len(data["items"]) >= 2


def test_quote_history_filters_by_job_type(client: TestClient) -> None:
    client.post(
        "/api/ai/quote-generate",
        json={"job_type": "Cable Fix", "notes": "cable snapped"},
    )

    resp = client.get("/api/ai/quote-history?job_type=Cable+Fix")
    assert resp.status_code == 200
    data = resp.json()
    for item in data["items"]:
        assert item["job_type"] == "Cable Fix"


def test_quote_feedback_updates_record(client: TestClient) -> None:
    gen_resp = client.post(
        "/api/ai/quote-generate",
        json={"job_type": "Spring Replacement", "notes": "spring fix"},
    )
    quote_id = gen_resp.json()["id"]

    fb_resp = client.post(
        "/api/ai/quote-feedback",
        json={
            "quote_id": quote_id,
            "accepted": True,
            "final_price": 450.00,
            "notes": "Customer accepted",
        },
    )
    assert fb_resp.status_code == 200
    assert fb_resp.json()["status"] == "ok"


def test_quote_feedback_404_for_bad_id(client: TestClient) -> None:
    resp = client.post(
        "/api/ai/quote-feedback",
        json={"quote_id": "nonexistent", "accepted": False},
    )
    assert resp.status_code == 404

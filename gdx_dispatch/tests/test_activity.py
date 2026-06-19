"""Tests for the read-only activity feed router (gdx_dispatch/routers/activity.py)."""
from __future__ import annotations

import pytest
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.routers import activity as activity_router
from gdx_dispatch.tests.conftest import make_fresh_db

_TEST_USER = {"user_id": "user-1", "sub": "user-1", "role": "admin"}


def _request(tenant_id: str = "tenant-a") -> Request:
    req = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    req.state.tenant = {"id": tenant_id}
    return req


@pytest.fixture()
def session_factory():
    engine = make_fresh_db()
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    yield SessionLocal
    engine.dispose()


def _seed_audit(
    db,
    *,
    tenant_id: str,
    action: str,
    entity_type: str,
    entity_id: str,
    user_id: str = "user-1",
) -> None:
    log_audit_event_sync(
        db,
        tenant_id=tenant_id,
        user_id=user_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        details={"k": "v"},
    )
    db.commit()


def test_activity_recent_returns_200(session_factory):
    db = session_factory()
    try:
        _seed_audit(db, tenant_id="tenant-a", action="create_job", entity_type="job", entity_id="job-1")
        _seed_audit(db, tenant_id="tenant-a", action="update_job", entity_type="job", entity_id="job-1")

        result = activity_router.list_recent_activity(
            request=_request("tenant-a"),
            _=_TEST_USER,
            db=db,
            limit=50,
            offset=0,
            entity_type=None,
            user_id=None,
        )
        assert isinstance(result, dict)
        assert "items" in result and "total" in result
        assert result["total"] >= 2
        assert len(result["items"]) >= 2
        first = result["items"][0]
        assert {"id", "user_id", "action", "entity_type", "entity_id", "details", "created_at"}.issubset(first.keys())
    finally:
        db.close()


def test_activity_recent_respects_tenant_scope(session_factory):
    db = session_factory()
    try:
        _seed_audit(db, tenant_id="tenant-a", action="create_job", entity_type="job", entity_id="job-A")
        _seed_audit(db, tenant_id="tenant-b", action="create_job", entity_type="job", entity_id="job-B")

        result_b = activity_router.list_recent_activity(
            request=_request("tenant-b"),
            _=_TEST_USER,
            db=db,
            limit=50,
            offset=0,
            entity_type=None,
            user_id=None,
        )
        entity_ids = {item["entity_id"] for item in result_b["items"]}
        assert "job-B" in entity_ids
        assert "job-A" not in entity_ids
        assert result_b["total"] == 1
    finally:
        db.close()


def test_activity_by_job_filters_correctly(session_factory):
    db = session_factory()
    try:
        _seed_audit(db, tenant_id="tenant-a", action="create_job", entity_type="job", entity_id="job-X")
        _seed_audit(db, tenant_id="tenant-a", action="create_job", entity_type="job", entity_id="job-Y")
        # Ensure a non-job row is not returned
        _seed_audit(db, tenant_id="tenant-a", action="create_customer", entity_type="customer", entity_id="cust-1")

        result = activity_router.list_job_activity(
            job_id="job-X",
            request=_request("tenant-a"),
            _=_TEST_USER,
            db=db,
            limit=50,
            offset=0,
        )
        assert result["total"] == 1
        assert len(result["items"]) == 1
        assert result["items"][0]["entity_id"] == "job-X"
        assert result["items"][0]["entity_type"] == "job"


        # Customer feed returns only customer rows
        cust_result = activity_router.list_customer_activity(
            customer_id="cust-1",
            request=_request("tenant-a"),
            _=_TEST_USER,
            db=db,
            limit=50,
            offset=0,
        )
        assert cust_result["total"] == 1
        assert cust_result["items"][0]["entity_type"] == "customer"
        assert cust_result["items"][0]["entity_id"] == "cust-1"
    finally:
        db.close()

"""Tests for the safety checklist router."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from conftest import make_fresh_db
from sqlalchemy import Text, text
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.models.tenant_models import SafetyChecklist
from gdx_dispatch.routers.safety_checklist import (
    ChecklistCompleteIn,
    complete_checklist,
    get_job_checklist,
    get_template,
)


class DummyRequest:
    def __init__(self, tenant_id: str = "tenant-safety-test") -> None:
        self.state = SimpleNamespace(tenant={"id": tenant_id}, request_id="req-s1")
        self.client = SimpleNamespace(host="127.0.0.1")
        self.headers: dict[str, str] = {}


@pytest.fixture()
def ctx():
    # Temporarily swap SafetyChecklist DateTime columns to Text so the router's
    # ISO-string timestamps are accepted by SQLite.  The router passes
    # datetime(...).isoformat() (a string) which SQLite's DateTime type rejects.
    _orig_signed = SafetyChecklist.signed_at.property.columns[0].type
    _orig_created = SafetyChecklist.created_at.property.columns[0].type
    _orig_deleted = SafetyChecklist.deleted_at.property.columns[0].type
    SafetyChecklist.signed_at.property.columns[0].type = Text()
    SafetyChecklist.created_at.property.columns[0].type = Text()
    SafetyChecklist.deleted_at.property.columns[0].type = Text()

    engine = make_fresh_db()
    # Recreate with TEXT columns to match
    with engine.connect() as conn:
        conn.execute(text("DROP TABLE IF EXISTS safety_checklists"))
        conn.execute(text(
            """CREATE TABLE safety_checklists (
                id VARCHAR(36) PRIMARY KEY,
                company_id VARCHAR(36) NOT NULL,
                job_id VARCHAR(36) NOT NULL,
                technician_id VARCHAR(36) NOT NULL,
                items TEXT NOT NULL,
                completed BOOLEAN,
                photo_url TEXT,
                signed_at TEXT,
                created_at TEXT,
                deleted_at TEXT
            )"""
        ))
        conn.commit()
    SL = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = SL()
    req = DummyRequest()
    user = {"user_id": "tech-1", "sub": "tech-1", "role": "tech"}
    try:
        yield db, req, user, SL
    finally:
        db.close()
        engine.dispose()
        # Restore original column types
        SafetyChecklist.signed_at.property.columns[0].type = _orig_signed
        SafetyChecklist.created_at.property.columns[0].type = _orig_created
        SafetyChecklist.deleted_at.property.columns[0].type = _orig_deleted


def test_template_returns_10_items(ctx):
    _, _, user, _ = ctx
    result = get_template(user=user)
    assert len(result["items"]) == 10
    for item in result["items"]:
        assert item["checked"] is False
        assert isinstance(item["item"], str)
        assert len(item["item"]) > 5


def test_template_items_are_garage_door_specific(ctx):
    _, _, user, _ = ctx
    result = get_template(user=user)
    names = [i["item"] for i in result["items"]]
    assert "Tested auto-reverse sensor" in names
    assert "Verified spring tension" in names
    assert "Photographed completed work" in names


def test_complete_all_checked_sets_completed_true(ctx):
    db, req, user, _ = ctx
    payload = ChecklistCompleteIn(
        job_id="job-all-checked",
        items=[{"item": f"Item {i}", "checked": True} for i in range(10)],
    )
    result = complete_checklist(request=req, payload=payload, user=user, db=db)
    assert result["completed"] is True


def test_complete_partial_checked_sets_completed_false(ctx):
    db, req, user, _ = ctx
    items = [{"item": f"Item {i}", "checked": i < 5} for i in range(10)]
    payload = ChecklistCompleteIn(job_id="job-partial", items=items)
    result = complete_checklist(request=req, payload=payload, user=user, db=db)
    assert result["completed"] is False


def test_complete_signed_sets_signed_at(ctx):
    db, req, user, _ = ctx
    payload = ChecklistCompleteIn(
        job_id="job-signed",
        items=[{"item": "Test", "checked": True}],
        signed=True,
    )
    result = complete_checklist(request=req, payload=payload, user=user, db=db)
    assert result["signed_at"] is not None


def test_complete_unsigned_has_no_signed_at(ctx):
    db, req, user, _ = ctx
    payload = ChecklistCompleteIn(
        job_id="job-unsigned",
        items=[{"item": "Test", "checked": True}],
        signed=False,
    )
    result = complete_checklist(request=req, payload=payload, user=user, db=db)
    assert result["signed_at"] is None


def test_get_missing_job_returns_status_missing(ctx):
    db, req, user, _ = ctx
    result = get_job_checklist(job_id="nonexistent", request=req, user=user, db=db)
    assert result["status"] == "missing"
    assert result["checklist"] is None


def test_complete_then_get_returns_saved(ctx):
    db, req, user, _ = ctx
    payload = ChecklistCompleteIn(
        job_id="job-roundtrip",
        items=[{"item": "Sensor test", "checked": True}, {"item": "Spring check", "checked": False}],
    )
    complete_checklist(request=req, payload=payload, user=user, db=db)
    result = get_job_checklist(job_id="job-roundtrip", request=req, user=user, db=db)
    assert result["job_id"] == "job-roundtrip"
    assert len(result["items"]) == 2
    assert result["items"][0]["checked"] is True
    assert result["items"][1]["checked"] is False


def test_complete_with_photo_url(ctx):
    db, req, user, _ = ctx
    payload = ChecklistCompleteIn(
        job_id="job-photo",
        items=[{"item": "Photo test", "checked": True}],
        photo_url="https://example.com/photo.jpg",
    )
    result = complete_checklist(request=req, payload=payload, user=user, db=db)
    assert result["photo_url"] == "https://example.com/photo.jpg"


def test_complete_creates_audit_log(ctx):
    db, req, user, SL = ctx
    payload = ChecklistCompleteIn(
        job_id="job-audit",
        items=[{"item": "Audit test", "checked": True}],
    )
    complete_checklist(request=req, payload=payload, user=user, db=db)
    count = db.execute(
        text("SELECT COUNT(*) FROM audit_logs WHERE entity_type = 'safety_checklist'")
    ).scalar()
    assert count >= 1


def test_complete_returns_correct_fields(ctx):
    db, req, user, _ = ctx
    payload = ChecklistCompleteIn(
        job_id="job-fields",
        items=[{"item": "Field test", "checked": True}],
    )
    result = complete_checklist(request=req, payload=payload, user=user, db=db)
    assert "id" in result
    assert "company_id" in result
    assert "job_id" in result
    assert "technician_id" in result
    assert "items" in result
    assert "completed" in result
    assert "created_at" in result

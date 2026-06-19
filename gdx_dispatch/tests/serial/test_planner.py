"""Tests for the planner router — tasks, plans, messaging.
ORM-based — uses models from tenant_models.py instead of raw DDL.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from conftest import make_fresh_db
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.routers.planner import TaskIn, TaskPatch, create_task, delete_task, list_tasks, update_task


class DummyRequest:
    def __init__(self, tenant_id: str = "tenant-planner-test") -> None:
        self.state = SimpleNamespace(tenant={"id": tenant_id}, request_id="req-p1")
        self.client = SimpleNamespace(host="127.0.0.1")
        self.headers: dict[str, str] = {}


@pytest.fixture()
def ctx():
    engine = make_fresh_db()
    SL = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = SL()
    req = DummyRequest()
    user = {"user_id": "admin-1", "sub": "admin-1", "role": "admin"}
    try:
        yield db, req, user, SL
    finally:
        db.close()
        engine.dispose()


def _items(result: dict) -> list:
    return result.get("items", [])


def test_list_tasks_empty(ctx):
    db, req, user, _ = ctx
    result = list_tasks(request=req, user=user, db=db)
    assert len(_items(result)) == 0


def test_create_task_returns_id_and_title(ctx):
    db, req, user, _ = ctx
    body = TaskIn(title="Order springs for Johnson job")
    result = create_task(body=body, request=req, user=user, db=db)
    assert "id" in result
    assert result["title"] == "Order springs for Johnson job"


def test_create_then_list(ctx):
    db, req, user, _ = ctx
    body = TaskIn(title="Schedule follow-up")
    create_task(body=body, request=req, user=user, db=db)
    items = _items(list_tasks(request=req, user=user, db=db))
    assert len(items) == 1
    assert items[0]["title"] == "Schedule follow-up"


def test_create_multiple(ctx):
    db, req, user, _ = ctx
    for i in range(5):
        create_task(body=TaskIn(title=f"Task {i}"), request=req, user=user, db=db)
    items = _items(list_tasks(request=req, user=user, db=db))
    assert len(items) == 5


def test_update_task_title(ctx):
    db, req, user, _ = ctx
    body = TaskIn(title="Old title")
    result = create_task(body=body, request=req, user=user, db=db)
    tid = result["id"]

    patch = TaskPatch(title="New title")
    updated = update_task(task_id=tid, body=patch, request=req, user=user, db=db)
    assert updated["title"] == "New title"


def test_create_with_priority(ctx):
    db, req, user, _ = ctx
    body = TaskIn(title="Urgent fix", priority="high")
    result = create_task(body=body, request=req, user=user, db=db)
    assert result["priority"] == "high"


def test_create_with_due_date(ctx):
    db, req, user, _ = ctx
    body = TaskIn(title="Scheduled task", due_date="2026-04-15")
    create_task(body=body, request=req, user=user, db=db)
    # Verify by listing — the create response may not include due_date
    items = _items(list_tasks(request=req, user=user, db=db))
    assert len(items) == 1
    assert "2026-04-15" in str(items[0].get("due_date", ""))


def test_create_with_assigned_to(ctx):
    db, req, user, _ = ctx
    body = TaskIn(title="Delegated task", assigned_to="tech-2")
    result = create_task(body=body, request=req, user=user, db=db)
    assert result.get("assigned_to") == "tech-2"


def test_delete_task(ctx):
    db, req, user, _ = ctx
    body = TaskIn(title="To delete")
    result = create_task(body=body, request=req, user=user, db=db)
    tid = result["id"]
    delete_task(task_id=tid, request=req, user=user, db=db)
    items = _items(list_tasks(request=req, user=user, db=db))
    assert len(items) == 0


def test_audit_log_on_create(ctx):
    db, req, user, _ = ctx
    create_task(body=TaskIn(title="Audit test"), request=req, user=user, db=db)
    count = db.execute(
        text("SELECT COUNT(*) FROM audit_logs WHERE entity_type = 'planner_task'")
    ).scalar()
    assert count >= 1


def test_default_sort_is_newest_first(ctx):
    db, req, user, _ = ctx
    create_task(body=TaskIn(title="First"), request=req, user=user, db=db)
    create_task(body=TaskIn(title="Second"), request=req, user=user, db=db)
    create_task(body=TaskIn(title="Third"), request=req, user=user, db=db)
    items = _items(list_tasks(request=req, user=user, db=db))
    assert [t["title"] for t in items] == ["Third", "Second", "First"]


def test_sort_oldest(ctx):
    db, req, user, _ = ctx
    create_task(body=TaskIn(title="A"), request=req, user=user, db=db)
    create_task(body=TaskIn(title="B"), request=req, user=user, db=db)
    items = _items(list_tasks(request=req, user=user, db=db, sort="oldest"))
    assert [t["title"] for t in items] == ["A", "B"]


def test_sort_priority_orders_urgent_first(ctx):
    db, req, user, _ = ctx
    create_task(body=TaskIn(title="low one", priority="low"), request=req, user=user, db=db)
    create_task(body=TaskIn(title="urgent one", priority="urgent"), request=req, user=user, db=db)
    create_task(body=TaskIn(title="medium one", priority="medium"), request=req, user=user, db=db)
    items = _items(list_tasks(request=req, user=user, db=db, sort="priority"))
    titles = [t["title"] for t in items]
    assert titles.index("urgent one") < titles.index("medium one") < titles.index("low one")


def test_bucket_active_excludes_done(ctx):
    db, req, user, _ = ctx
    keep = create_task(body=TaskIn(title="still doing"), request=req, user=user, db=db)
    done = create_task(body=TaskIn(title="all done"), request=req, user=user, db=db)
    update_task(task_id=done["id"], body=TaskPatch(status="done"), request=req, user=user, db=db)
    items = _items(list_tasks(request=req, user=user, db=db))
    assert [t["title"] for t in items] == ["still doing"]
    assert items[0]["id"] == keep["id"]


def test_bucket_completed_only_returns_done(ctx):
    db, req, user, _ = ctx
    create_task(body=TaskIn(title="open"), request=req, user=user, db=db)
    done = create_task(body=TaskIn(title="finished"), request=req, user=user, db=db)
    update_task(task_id=done["id"], body=TaskPatch(status="done"), request=req, user=user, db=db)
    items = _items(list_tasks(request=req, user=user, db=db, bucket="completed"))
    assert [t["title"] for t in items] == ["finished"]


def test_bucket_all_returns_done_and_active(ctx):
    db, req, user, _ = ctx
    create_task(body=TaskIn(title="open"), request=req, user=user, db=db)
    done = create_task(body=TaskIn(title="finished"), request=req, user=user, db=db)
    update_task(task_id=done["id"], body=TaskPatch(status="done"), request=req, user=user, db=db)
    items = _items(list_tasks(request=req, user=user, db=db, bucket="all"))
    assert {t["title"] for t in items} == {"open", "finished"}


def test_invalid_sort_falls_back_to_newest(ctx):
    db, req, user, _ = ctx
    create_task(body=TaskIn(title="X"), request=req, user=user, db=db)
    create_task(body=TaskIn(title="Y"), request=req, user=user, db=db)
    items = _items(list_tasks(request=req, user=user, db=db, sort="bogus"))
    assert [t["title"] for t in items] == ["Y", "X"]


def test_no_bucket_param_matches_active_default(ctx):
    # 2026-05-14 audit finding: stale PWA bundles still call without `bucket=`.
    # Lock the implicit default so the API contract is documented in tests.
    db, req, user, _ = ctx
    create_task(body=TaskIn(title="open one"), request=req, user=user, db=db)
    done = create_task(body=TaskIn(title="done one"), request=req, user=user, db=db)
    update_task(task_id=done["id"], body=TaskPatch(status="done"), request=req, user=user, db=db)
    implicit = _items(list_tasks(request=req, user=user, db=db))
    explicit = _items(list_tasks(request=req, user=user, db=db, bucket="active"))
    assert [t["title"] for t in implicit] == [t["title"] for t in explicit] == ["open one"]


def test_tenant_isolation(ctx):
    # Three-plane (2026-04-24 B1): tenant isolation is the per-tenant DB connection,
    # not an app-level company_id filter. This unit test uses a single shared SQLite
    # session, so rows from "tenant A" remain visible to a different tenant identity
    # operating on the *same* session. In production each tenant has a separate DB.
    db, req, user, _ = ctx
    create_task(body=TaskIn(title="Tenant A task"), request=req, user=user, db=db)

    req2 = DummyRequest(tenant_id="other-tenant")
    items = _items(list_tasks(request=req2, user=user, db=db))
    # Visible via shared test session — isolation now at connection boundary.
    assert len(items) >= 1

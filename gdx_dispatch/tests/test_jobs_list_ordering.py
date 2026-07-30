"""GET /api/jobs ordering — BOTH contracts, because ordering is eviction.

2026-07-29 (piece 1 of the closeout→billing plan). Two forces pull on the
list's ORDER BY and the fix must satisfy both:

* The /jobs list view: `scheduled_at DESC NULLS LAST` sank a just-created
  undated job below every dated job in the tenant — on GDX prod (227 jobs,
  20/page) it landed on page 12, invisible the moment it was saved, and the
  operator concluded the save failed and saved again.
* Every OTHER consumer: ~15 views use this endpoint as a capped job picker
  (50/200/500 per page). For them NULLS LAST is load-bearing: a cap evicts
  undated backlog, never old scheduled-but-unfinished work. Changing the
  default would silently drop the oldest scheduled jobs from expense/photo/
  change-order pickers — prod is already past the 200-row caps.

So the timeline ordering is OPT-IN: `?order=activity`, requested by JobsView
alone. Both orderings are pinned behaviorally below (the adversarial audit of
the first version of this file caught that a default-changing fix regressed
the pickers; it also caught that the static brake matched comments — the
brake now scopes to list_jobs's body).

Harness mirrors ``test_jobs_endpoints_smoke.py`` (in-memory SQLite, real
TestClient, tenant injected by middleware). Rows are INSERTed directly — the
thing under test is the ORDER BY, not the create path.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from conftest import make_fresh_db
from gdx_dispatch.core.database import get_db
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.jobs import router as jobs_router

TENANT_ID = "00000000-0000-4000-8000-0000000000ab"
REPO_ROOT = Path(__file__).resolve().parents[2]


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


@pytest.fixture
def harness() -> tuple[TestClient, sessionmaker]:
    engine = make_fresh_db()
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    setup = Session()
    setup.execute(
        text(
            "INSERT OR IGNORE INTO company_module_grants "
            "(id, company_id, module_key, granted_at, created_at) "
            "VALUES (:id, :tid, 'jobs', datetime('now'), datetime('now'))"
        ),
        {"id": f"grant-{TENANT_ID}", "tid": TENANT_ID},
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
    async def _inject_tenant(request, call_next):
        request.state.tenant = {"id": TENANT_ID}
        request.state.request_id = "ordering-test"
        return await call_next(request)

    app.include_router(jobs_router)
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "user-ordering",
        "sub": "user-ordering",
        "role": "admin",
        "tenant_id": TENANT_ID,
    }
    return TestClient(app, raise_server_exceptions=True), Session


def _insert_job(
    session,
    *,
    title: str,
    created_at: datetime,
    scheduled_at: datetime | None,
) -> str:
    job_id = uuid4().hex  # Uuid(as_uuid=True) stores 32-hex on SQLite
    session.execute(
        text(
            "INSERT INTO jobs (id, title, company_id, created_at, scheduled_at, "
            " lifecycle_stage, dispatch_status, billing_status, is_return_visit) "
            "VALUES (:id, :title, :tid, :created, :sched, "
            " 'service_call', 'unassigned', 'unbilled', 0)"
        ),
        {
            "id": job_id,
            "title": title,
            "tid": TENANT_ID,
            "created": _iso(created_at),
            "sched": _iso(scheduled_at) if scheduled_at else None,
        },
    )
    return job_id


def _seed_timeline(Session) -> dict[str, str]:
    now = datetime.now(UTC).replace(tzinfo=None)
    s = Session()
    ids = {
        "past": _insert_job(
            s, title="past-scheduled", created_at=now - timedelta(days=40),
            scheduled_at=now - timedelta(days=30),
        ),
        "old_undated": _insert_job(
            s, title="old-undated", created_at=now - timedelta(days=20),
            scheduled_at=None,
        ),
        "future": _insert_job(
            s, title="future-scheduled", created_at=now - timedelta(days=10),
            scheduled_at=now + timedelta(days=2),
        ),
        "fresh_undated": _insert_job(
            s, title="fresh-undated", created_at=now, scheduled_at=None,
        ),
    }
    s.commit()
    s.close()
    return ids


def _fetch_order(client: TestClient, query: str) -> list[str]:
    r = client.get(f"/api/jobs?{query}")
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    items = body if isinstance(body, list) else body.get("items") or body.get("data") or []
    return [str(j["id"]).replace("-", "") for j in items]


def test_default_ordering_keeps_dated_jobs_above_undated(harness) -> None:
    """The picker contract: WITHOUT order=activity, every dated job outranks
    every undated one, so a capped fetch evicts undated backlog — never old
    scheduled work. ~15 views with 50/200/500 caps depend on this."""
    client, Session = harness
    ids = _seed_timeline(Session)

    order = _fetch_order(client, "per_page=50")
    assert set(order) >= set(ids.values()), f"seeded jobs missing: {order}"

    expected = [ids["future"], ids["past"], ids["fresh_undated"], ids["old_undated"]]
    positions = [order.index(j) for j in expected]
    assert positions == sorted(positions), (
        "DEFAULT /api/jobs ordering changed — dated jobs must outrank undated "
        "ones (scheduled DESC NULLS LAST). This is the eviction policy for "
        "every capped picker fetch; if undated jobs now interleave, capped "
        "views (Expenses/Photos/ChangeOrders at 200, dispatch window at 500) "
        f"start silently dropping their OLDEST SCHEDULED jobs. Got {positions} "
        "for [future, past, fresh_undated, old_undated]."
    )


def test_activity_ordering_slots_undated_jobs_by_recency(harness) -> None:
    """The /jobs list contract: WITH order=activity a just-created undated job
    outranks past-scheduled work — it must be visible on page 1, not banished
    below every dated job in the tenant."""
    client, Session = harness
    ids = _seed_timeline(Session)

    order = _fetch_order(client, "per_page=50&order=activity")
    assert set(order) >= set(ids.values()), f"seeded jobs missing: {order}"

    expected = [ids["future"], ids["fresh_undated"], ids["old_undated"], ids["past"]]
    positions = [order.index(j) for j in expected]
    assert positions == sorted(positions), (
        "order=activity regressed — expected the coalesced timeline "
        "future > fresh-undated > old-undated > past, got positions "
        f"{dict(zip(['future', 'fresh_undated', 'old_undated', 'past'], positions, strict=True))}. "
        "A just-created undated job must never sort below the whole tenant."
    )
    assert order.index(ids["fresh_undated"]) < order.index(ids["past"])


def test_jobsview_requests_the_activity_ordering() -> None:
    """The wiring: JobsView must actually ASK for order=activity, or the
    undated-job-invisible bug returns with both behavioral tests still green.
    (Scoped source checks — the audit caught the earlier version of this brake
    matching SQL quoted in comments anywhere in a 3000-line file.)"""
    jobs_src = (REPO_ROOT / "gdx_dispatch/routers/jobs.py").read_text(encoding="utf-8")
    # Scope to list_jobs's body only.
    start = jobs_src.index("def list_jobs(")
    end = jobs_src.index("\ndef ", start + 1)
    body = jobs_src[start:end]
    assert 'order_sql = "ORDER BY COALESCE(j.scheduled_at, j.created_at) DESC' in body, (
        "list_jobs lost the order=activity branch."
    )
    assert 'order_sql = "ORDER BY j.scheduled_at DESC NULLS LAST' in body, (
        "list_jobs lost the default NULLS LAST branch — the picker eviction "
        "contract is gone."
    )

    view_src = (
        REPO_ROOT / "gdx_dispatch/frontend/src/views/JobsView.vue"
    ).read_text(encoding="utf-8")
    assert "order=activity" in view_src, (
        "JobsView no longer requests order=activity — a just-created undated "
        "job sinks to the last page again."
    )

"""Sprint 1.0 Phase E3 — cross-view data-divergence regression tests.

Pattern: for every "same question" the app answers from multiple code
paths, assert they agree on a fixture.

This file establishes the pattern with the canonical customer→jobs
question (the H7 case from earlier sessions/28's click-through audit, fixed
under C8: customer detail showed "no jobs" while reports said the same
customer had $82k LTV — three code paths counting jobs differently).

Future divergence tests should follow this same shape:
  - one fixture seeded with known data
  - call each code path directly (router functions, not HTTP) so the
    test fails on a numeric divergence, not on auth/middleware glue
  - assert numeric agreement; the failure message names which paths
    disagreed and by how much

Other "same question" candidates queued for follow-up sessions:
  - dashboard revenue vs reports top-customers vs invoices list
    (all should respect the same BILLED_STATUSES per an earlier session C1)
  - invoice amount: customer detail aggregate vs invoices list vs
    reports.outstanding-aging
  - dispatch unassigned count vs jobs list filter (an earlier session C9)
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models import tenant_models  # noqa: F401 — registers models
from gdx_dispatch.routers.customers import get_customer
from gdx_dispatch.routers.jobs import list_jobs

pytestmark = pytest.mark.anyio

TENANT_ID = "tenant-divergence-test"


def _mock_request(tenant_id: str = TENANT_ID) -> SimpleNamespace:
    """Mimic the FastAPI Request shape the routers reach into."""
    return SimpleNamespace(
        state=SimpleNamespace(tenant={"id": tenant_id}),
        client=SimpleNamespace(host="127.0.0.1"),
        headers={},
    )


@pytest.fixture
def tenant_db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def _seed_customer(db, *, name: str = "Acme") -> str:
    cust_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc).isoformat()
    db.execute(text("""
        INSERT INTO customers (id, name, company_id, created_at)
        VALUES (:id, :name, :tenant, :now)
    """), {"id": cust_id, "name": name, "tenant": TENANT_ID, "now": now})
    db.commit()
    return cust_id


def _seed_job(db, *, customer_id: str, title: str,
              status: str = "scheduled", deleted: bool = False) -> str:
    job_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc).isoformat()
    deleted_at = now if deleted else None
    # NOT-NULL columns on Job (per gdx_dispatch/models/tenant_models.py): supply
    # explicit defaults so the insert works on a freshly-created SQLite
    # schema without relying on server_default (sqlite ignores some).
    db.execute(text("""
        INSERT INTO jobs (id, title, status, customer_id, company_id,
                          lifecycle_stage, dispatch_status, billing_status,
                          created_at, deleted_at)
        VALUES (:id, :title, :status, :customer_id, :tenant,
                'scheduled', 'unassigned', 'unbilled',
                :now, :deleted_at)
    """), {
        "id": job_id, "title": title, "status": status,
        "customer_id": customer_id, "tenant": TENANT_ID,
        "now": now, "deleted_at": deleted_at,
    })
    db.commit()
    return job_id


async def test_customer_detail_jobs_count_matches_jobs_list(tenant_db_session):
    """H7/C8 regression — customer detail must agree with jobs list filtered
    by customer_id on how many active jobs the customer has.

    Two code paths:
      1. GET /api/customers/{id} → embeds jobs[] (via select() ORM, capped 200)
      2. GET /api/jobs?customer_id={id} → raw SQL with WHERE clause + total

    Pre-C8 these disagreed because the customer detail JOIN returned []
    on records where customer_id was uuid-shaped but the FK column was
    typed differently. The test catches any future class of this bug
    by asserting numeric agreement on a known fixture.
    """
    db = tenant_db_session
    customer_id = _seed_customer(db, name="Divergence Co")

    # 5 active jobs + 2 deleted (deleted should NOT count in either path)
    for i in range(5):
        _seed_job(db, customer_id=customer_id, title=f"Active job {i}")
    for i in range(2):
        _seed_job(db, customer_id=customer_id,
                  title=f"Deleted job {i}", deleted=True)

    # Path 1: customer detail (returns dict with `jobs` key)
    request = _mock_request()
    customer_response = await get_customer(
        customer_id=customer_id,
        request=request,
        _={"sub": "test-user", "id": "test-user"},
        db=db,
    )
    detail_job_count = len(customer_response["jobs"])

    # Path 2: jobs list filtered by customer_id. Returns a JSONResponse;
    # decode the body to compare numerically (the route encodes via
    # jsonable_response → JSONResponse).
    import json as _json
    raw_response = list_jobs(
        request=request,
        current_user={"sub": "test-user", "id": "test-user"},
        db=db,
        page=1,
        page_size=200,
        customer_id=customer_id,
    )
    list_response = _json.loads(raw_response.body.decode())
    list_total = list_response["total"]
    list_returned = len(list_response["items"])  # router returns "items"

    # Both paths must agree
    assert detail_job_count == 5, (
        f"Customer detail returned {detail_job_count} jobs; "
        f"expected 5 active jobs (2 deleted should be filtered)"
    )
    assert list_total == 5, (
        f"Jobs list total is {list_total}; expected 5 active jobs"
    )
    assert detail_job_count == list_total == list_returned, (
        f"DIVERGENCE: customer detail={detail_job_count} jobs, "
        f"jobs list total={list_total}, jobs list returned items={list_returned} — "
        f"all three must agree on the same question"
    )

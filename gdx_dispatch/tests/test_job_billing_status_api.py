"""GET /api/jobs and /api/jobs/{id} serve billing_status derived from invoices.

`Job.billing_status` is a stale cache: nothing has advanced it since July
2026 and nothing ever wrote "paid", so the jobs behind every paid invoice since
August still say "unbilled" (37 of 37, checked 2026-10-07; older jobs mostly say
"invoiced"). The API used to serve the column, which is how a read of the jobs
behind four paid invoices came back "unbilled". The
key now comes from the same invoice derivation as display_state.

Every job here stores "unbilled", so a non-"unbilled" answer can only have
come from the invoices. Harness mirrors test_jobs_list_ordering.py.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from conftest import make_fresh_db
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import Invoice, Job, Payment
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.jobs import router as jobs_router

TENANT_ID = "00000000-0000-4000-8000-0000000000ac"


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
        request.state.request_id = "billing-status-test"
        return await call_next(request)

    app.include_router(jobs_router)
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "user-billing",
        "sub": "user-billing",
        "role": "admin",
        "tenant_id": TENANT_ID,
    }
    return TestClient(app, raise_server_exceptions=True), Session


def _seed_job(Session, *, invoice: dict | None) -> str:
    s = Session()
    now = datetime.now(UTC)
    job = Job(
        id=uuid.uuid4(),
        title="Opener install",
        company_id=TENANT_ID,
        lifecycle_stage="completed",
        dispatch_status="done",
        billing_status="unbilled",
        is_return_visit=False,
        created_at=now,
        updated_at=now,
    )
    s.add(job)
    if invoice is not None:
        n = uuid.uuid4().hex[:10]
        inv = Invoice(
            id=uuid.uuid4(),
            job_id=job.id,
            customer_id=uuid.uuid4(),
            company_id=TENANT_ID,
            invoice_number=f"INV-{n}",
            public_token=f"tok-{n}",
            status=invoice["status"],
            total=invoice["total"],
            balance_due=invoice["balance_due"],
            billing_type="standard",
            created_at=now,
        )
        if invoice.get("paid"):
            inv.payments.append(
                Payment(id=uuid.uuid4(), amount=invoice["paid"], company_id=TENANT_ID, created_at=now)
            )
        s.add(inv)
    s.commit()
    jid = str(job.id)
    s.close()
    return jid


def _list_by_id(client: TestClient) -> dict[str, dict]:
    r = client.get("/api/jobs?per_page=50")
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    items = body if isinstance(body, list) else body.get("items") or body.get("data") or []
    return {str(j["id"]).replace("-", ""): j for j in items}


CASES = [
    ("paid", {"status": "paid", "total": 3995.34, "balance_due": 0, "paid": 3995.34}),
    ("partial_paid", {"status": "sent", "total": 500, "balance_due": 300, "paid": 200}),
    ("invoiced", {"status": "sent", "total": 250, "balance_due": 250}),
    ("unbilled", None),
]


@pytest.mark.parametrize(("expected", "invoice"), CASES, ids=[c[0] for c in CASES])
def test_list_and_detail_serve_derived_billing_status(harness, expected, invoice) -> None:
    client, Session = harness
    jid = _seed_job(Session, invoice=invoice)

    listed = _list_by_id(client)[jid.replace("-", "")]
    assert listed["billing_status"] == expected
    assert listed["display_state"]["billing_status"] == expected

    r = client.get(f"/api/jobs/{jid}")
    assert r.status_code == 200, r.text[:300]
    assert r.json()["billing_status"] == expected


def test_admin_full_export_serves_derived_billing_status(harness) -> None:
    """GET /api/admin/export dumped whole Job rows, stale column included."""
    from gdx_dispatch.routers.admin_ops import full_export

    _client, Session = harness
    paid = _seed_job(Session, invoice=CASES[0][1])
    unbilled = _seed_job(Session, invoice=None)
    db = Session()
    try:
        out = full_export(_={"sub": "user-billing", "role": "admin"}, db=db, request=None)
    finally:
        db.close()
    by_id = {str(j["id"]).replace("-", ""): j["billing_status"] for j in out["jobs"]}
    assert by_id[paid.replace("-", "")] == "paid"
    assert by_id[unbilled.replace("-", "")] == "unbilled"

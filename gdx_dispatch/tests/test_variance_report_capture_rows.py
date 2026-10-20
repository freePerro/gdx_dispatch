"""Variance Report actual parts come from the capture rows (GDXA-1825).

The report read ``van_inventory_log``, which only ``POST /api/van-inventory/use``
wrote, and no screen calls that. Parts a tech really uses are recorded by the
closeout sheet (its live ``job_closeouts`` snapshot) and by mobile parts-used
and van usage (``job_parts_needed`` rows tagged by source). Where the closeout
is involved these tests drive the real writer, not hand-made rows. The last
test runs the endpoints on Postgres, where a failed statement aborts the
transaction and SQLite would hide it.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from gdx_dispatch.models.tenant_models import Customer, Invoice, Job, JobCloseout, JobPartNeeded
from gdx_dispatch.modules.inventory.models import Part
from gdx_dispatch.modules.proposals.models import Estimate, EstimateLine
from gdx_dispatch.routers.jobs import CloseoutPayload, closeout_job
from gdx_dispatch.routers.variance_report import job_variance, variance_summary

TID = "tenant-test"
TECH = {"user_id": "tech-1", "tenant_id": TID, "role": "technician"}
OFFICE = {"id": "office-user", "email": "office@example.com", "role": "admin"}


def _request() -> Request:
    req = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    req.state.tenant = {"id": TID}
    req.state.tenant_id = TID
    return req


def _job(db, title: str, *, company_id: str = TID) -> Job:
    job = Job(title=title, description=title, company_id=company_id,
              lifecycle_stage="in_progress", dispatch_status="on_site",
              billing_status="unbilled", assigned_to="tech-1")
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _part(db, sku: str = "SPR-200") -> Part:
    part = Part(id=uuid4(), sku=sku, name=f"Torsion spring {sku}",
                unit_cost=Decimal("40"), unit_price=Decimal("89.50"),
                qty_on_hand=10, reorder_point=1)
    db.add(part)
    db.commit()
    return part


def _estimate(db, job: Job, qty: int) -> None:
    est = Estimate(job_id=job.id, estimate_number=f"EST-{uuid4().hex[:8]}",
                   total=Decimal("100"), status="draft",
                   public_token=uuid4().hex, company_id=TID)
    db.add(est)
    db.flush()
    db.add(EstimateLine(estimate_id=est.id, description="Spring", quantity=qty,
                        unit_price=Decimal("50"), line_total=Decimal("50") * qty,
                        sort_order=0, company_id=TID))
    db.commit()


def _close_out(db, job: Job, parts: list[dict]) -> None:
    resp = closeout_job(payload=CloseoutPayload(parts=parts, hours=1.0),
                        job_id=str(job.id), request=_request(),
                        current_user=TECH, db=db)
    assert resp.status_code == 201, resp.body


def _summary(db, **kw):
    return variance_summary(_request(), OFFICE, db, start_date=kw.get("start_date"),
                            end_date=kw.get("end_date"), limit=kw.get("limit", 50))


def test_summary_is_non_empty_from_closeout_rows(tenant_db):
    db = tenant_db
    job = _job(db, "spring swap")
    part = _part(db)
    _estimate(db, job, qty=1)
    _close_out(db, job, [
        {"part_id": str(part.id), "sku": part.sku, "name": part.name, "qty": 2, "unit_cost": 40.0},
        {"name": "Custom strut", "qty": 1, "unit_cost": 15.0},
    ])

    out = _summary(db)

    assert [r["job_id"] for r in out] == [str(job.id)]
    row = out[0]
    assert row["actual_qty"] == 3
    # The estimate side is matched on a Uuid column; the old raw-SQL
    # comparison against a dashed string never matched on SQLite.
    assert row["estimated_qty"] == 1
    assert row["variance"] == 2


def test_job_variance_lists_closeout_and_mobile_rows_not_requests(tenant_db):
    db = tenant_db
    job = _job(db, "opener install")
    part = _part(db)
    _estimate(db, job, qty=3)
    _close_out(db, job, [
        {"part_id": str(part.id), "sku": part.sku, "name": part.name, "qty": 1, "unit_cost": 40.0},
    ])
    now = datetime.now(UTC)
    for source, name, qty in (("mobile", "Roller set", 4), ("request", "Asked-for cable", 9),
                              ("vendor_invoice", "Billed door", 1)):
        db.add(JobPartNeeded(id=str(uuid4()), company_id=TID, job_id=str(job.id),
                             part_name=name, quantity=qty, status="used",
                             source=source, created_at=now, updated_at=now))
    db.commit()

    out = job_variance(str(job.id), _request(), OFFICE, db)

    by_source = {i["source"]: i for i in out["actual_items"]}
    assert set(by_source) == {"closeout", "mobile"}
    assert by_source["closeout"]["sku"] == part.sku
    assert out["actual_total_qty"] == 5
    assert [i["quantity"] for i in out["estimated_items"]] == [3]
    assert out["estimated_total_value"] == 150.0
    assert out["quantity_variance"] == 2


def test_billed_recloseout_counts_the_restated_quantity_once(tenant_db):
    """A re-closeout keeps the BILLED closeout row and adds the restated one
    beside it; the report must read the restatement, not the sum."""
    db = tenant_db
    job = _job(db, "restated")
    _close_out(db, job, [{"name": "Strut", "qty": 2, "unit_cost": 10.0}])
    cust = Customer(name="Example Customer", company_id=TID)
    db.add(cust)
    db.flush()
    inv = Invoice(company_id=TID, customer_id=cust.id, invoice_number=f"INV-{uuid4().hex[:8]}",
                  billing_type="standard", sequence_number=1, subtotal=Decimal("20"),
                  tax_amount=Decimal("0"), total=Decimal("20"), balance_due=Decimal("20"),
                  status="draft", public_token=uuid4().hex, locked=False)
    db.add(inv)
    db.commit()
    first = db.query(JobPartNeeded).filter_by(job_id=str(job.id), source="closeout").one()
    first.billed_invoice_id = inv.id
    db.commit()

    _close_out(db, job, [{"name": "Strut", "qty": 3, "unit_cost": 10.0}])
    rows = db.query(JobPartNeeded).filter_by(job_id=str(job.id), source="closeout").all()
    assert sorted(r.quantity for r in rows) == [2, 3]  # billing keeps both, by design

    assert _summary(db)[0]["actual_qty"] == 3
    assert job_variance(str(job.id), _request(), OFFICE, db)["actual_total_qty"] == 3


def test_capture_row_with_an_uppercase_job_id_still_counts(tenant_db):
    """``job_parts_needed.job_id`` is a string stored as given."""
    db = tenant_db
    job = _job(db, "upper")
    now = datetime.now(UTC)
    db.add(JobPartNeeded(id=str(uuid4()), company_id=TID, job_id=str(job.id).upper(),
                         part_name="Hinge", quantity=2, status="used",
                         source="mobile", created_at=now, updated_at=now))
    db.commit()

    assert _summary(db)[0]["actual_qty"] == 2
    assert job_variance(str(job.id), _request(), OFFICE, db)["actual_total_qty"] == 2


def test_summary_does_not_filter_on_company_id(tenant_db):
    """Isolation is the connection; a row whose company_id differs from the
    request's tenant id is still this tenant's job."""
    db = tenant_db
    job = _job(db, "legacy import", company_id="some-other-id")
    now = datetime.now(UTC)
    # Seeded by hand: the closeout writer itself refuses this job.
    db.add(JobPartNeeded(id=str(uuid4()), company_id="some-other-id", job_id=str(job.id),
                         part_name="Weatherseal", quantity=1, status="used",
                         source="mobile", created_at=now, updated_at=now))
    db.commit()

    assert [r["job_id"] for r in _summary(db)] == [str(job.id)]


def test_summary_date_window_is_inclusive_and_validated(tenant_db):
    db = tenant_db
    job = _job(db, "dated")
    _close_out(db, job, [{"name": "Bracket", "qty": 1, "unit_cost": 5.0}])
    today = datetime.now(UTC).date()

    assert _summary(db, start_date=today.isoformat(), end_date=today.isoformat())
    assert _summary(db, start_date=(today + timedelta(days=1)).isoformat()) == []
    with pytest.raises(HTTPException) as exc:
        _summary(db, start_date="10/01/2026")
    assert exc.value.status_code == 422


def test_summary_limit_keeps_the_largest_usage(tenant_db):
    db = tenant_db
    small = _job(db, "small")
    big = _job(db, "big")
    _close_out(db, small, [{"name": "Bolt", "qty": 1, "unit_cost": 1.0}])
    _close_out(db, big, [{"name": "Bolt", "qty": 7, "unit_cost": 1.0}])

    assert [r["job_id"] for r in _summary(db, limit=1)] == [str(big.id)]


def test_pg_endpoints_answer_from_capture_data(pg_test_session):
    """On Postgres: the per-job endpoint's old estimate query selected a
    column that does not exist and aborted the transaction."""
    db = pg_test_session
    job = Job(title="pg job", description="pg job", company_id=TID,
              lifecycle_stage="completed", dispatch_status="done",
              billing_status="unbilled")
    db.add(job)
    db.flush()
    _estimate(db, job, qty=1)
    now = datetime.now(UTC)
    db.add(JobCloseout(job_id=job.id, hours_worked=Decimal("1"), closed_by_user_id="tech-1",
                       closed_at=now,
                       parts_used=[{"name": "Spring", "sku": "SPR-200", "qty": 2}]))
    db.add(JobPartNeeded(id=str(uuid4()), company_id=TID, job_id=str(job.id),
                         part_name="Roller", quantity=1, status="used",
                         source="mobile", created_at=now, updated_at=now))
    db.commit()

    one = job_variance(str(job.id), _request(), OFFICE, db)
    assert (one["estimated_total_qty"], one["actual_total_qty"]) == (1, 3)
    out = _summary(db)
    assert [(r["job_id"], r["estimated_qty"], r["actual_qty"]) for r in out] == [(str(job.id), 1, 3)]

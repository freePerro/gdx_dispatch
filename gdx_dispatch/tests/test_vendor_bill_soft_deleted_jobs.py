"""A soft-deleted job is gone to the books side (GDXA-603, invariant #2).

``routers/jobs.py`` deletes a job by stamping ``deleted_at``; the row stays.
Before this fix the vendor-bill job suggestions still offered such a job,
both through the PO#-to-customer branch and through an open parts-needed row
left behind on it, and the PATCH that attaches a bill to a job, the line
confirm, and the per-job variance report all accepted it.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from fastapi import HTTPException

from gdx_dispatch.models.tenant_models import Customer, Job, JobPartNeeded
from gdx_dispatch.modules.vendor_invoices.confirm import ConfirmError, confirm_line
from gdx_dispatch.modules.vendor_invoices.matching import (
    compute_vendor_key,
    suggest_job_matches,
)
from gdx_dispatch.modules.vendor_invoices.models import VendorInvoice, VendorInvoiceLine

TID = "tenant-test"
VENDOR = "Example Door Supply"
OFFICE = {"id": "office-user", "email": "office@example.com", "role": "admin"}


class _Req:  # the handlers read only .state.tenant
    class state:
        tenant = {"id": TID}


def _bill(db, *, po=None, matched_job_id=None):
    inv = VendorInvoice(
        vendor_key=compute_vendor_key(None, VENDOR),
        vendor_name_raw=VENDOR,
        invoice_number="SD-1",
        invoice_date=date(2026, 10, 1),
        po_reference=po,
        subtotal=Decimal("50.00"), tax=Decimal("0"), shipping=Decimal("0"),
        total=Decimal("50.00"),
        matched_job_id=matched_job_id,
    )
    inv.lines = [
        VendorInvoiceLine(line_no=1, description="Spring pair", quantity=Decimal("1"),
                          unit_cost=Decimal("50.00"), line_total=Decimal("50.00")),
    ]
    db.add(inv)
    db.flush()
    return inv


def _job(db, title, *, customer=None, deleted=False):
    job = Job(title=title, company_id=TID, lifecycle_stage="scheduled",
              customer_id=customer.id if customer else None)
    if deleted:
        job.deleted_at = datetime.now(timezone.utc)
    db.add(job)
    db.flush()
    return job


def _parts_row(db, job, rid):
    db.add(JobPartNeeded(id=rid, company_id=TID, job_id=str(job.id),
                         part_name="Torsion spring", quantity=1,
                         status="ordered", supplier=VENDOR))
    db.flush()


def test_po_text_suggestions_skip_a_soft_deleted_job(tenant_db):
    cust = Customer(name="Example Contractor", company_id=TID)
    tenant_db.add(cust)
    tenant_db.flush()
    live = _job(tenant_db, "live job", customer=cust)
    gone = _job(tenant_db, "deleted job", customer=cust, deleted=True)
    inv = _bill(tenant_db, po="Example Contractor")

    ids = {s.job_id for s in suggest_job_matches(tenant_db, inv)}

    assert str(live.id) in ids
    assert str(gone.id) not in ids


def test_parts_needed_suggestions_skip_a_soft_deleted_job(tenant_db):
    live = _job(tenant_db, "live job")
    gone = _job(tenant_db, "deleted job", deleted=True)
    _parts_row(tenant_db, live, "pn-live")
    _parts_row(tenant_db, gone, "pn-gone")
    inv = _bill(tenant_db)

    ids = {s.job_id for s in suggest_job_matches(tenant_db, inv)}

    assert str(live.id) in ids
    assert str(gone.id) not in ids


def test_confirm_refuses_a_soft_deleted_job(tenant_db):
    gone = _job(tenant_db, "deleted job", deleted=True)
    inv = _bill(tenant_db, matched_job_id=gone.id)
    with pytest.raises(ConfirmError):
        confirm_line(tenant_db, inv, inv.lines[0], disposition="job",
                     company_id=TID, actor_id="u1")


def test_attaching_a_bill_to_a_soft_deleted_job_is_refused(tenant_db):
    from gdx_dispatch.routers.vendor_invoices import InvoicePatch, patch_invoice

    live = _job(tenant_db, "live job")
    gone = _job(tenant_db, "deleted job", deleted=True)
    inv = _bill(tenant_db)
    tenant_db.commit()

    with pytest.raises(HTTPException) as exc:
        asyncio.run(patch_invoice(inv.id, InvoicePatch(matched_job_id=gone.id),
                                  request=_Req(), user=OFFICE, db=tenant_db))
    assert exc.value.status_code == 422
    tenant_db.rollback()
    tenant_db.refresh(inv)
    assert inv.matched_job_id is None

    out = asyncio.run(patch_invoice(inv.id, InvoicePatch(matched_job_id=live.id),
                                    request=_Req(), user=OFFICE, db=tenant_db))
    assert str(out.matched_job_id) == str(live.id)


def test_variance_report_404s_a_soft_deleted_job(tenant_db):
    from gdx_dispatch.routers.variance_report import job_variance

    gone = _job(tenant_db, "deleted job", deleted=True)
    tenant_db.commit()
    with pytest.raises(HTTPException) as exc:
        job_variance(str(gone.id), _Req(), OFFICE, tenant_db)
    assert exc.value.status_code == 404


def test_variance_summary_omits_a_soft_deleted_job(tenant_db):
    """The live job must be present too: an empty result would also pass a
    bare absence check."""
    from gdx_dispatch.routers.variance_report import variance_summary

    live = _job(tenant_db, "live job")
    gone = _job(tenant_db, "deleted job", deleted=True)
    for job in (live, gone):
        tenant_db.add(JobPartNeeded(id=str(uuid.uuid4()), company_id=TID, job_id=str(job.id),
                                    part_name="Spring", quantity=2, status="used",
                                    source="mobile"))
    tenant_db.commit()

    out = variance_summary(_Req(), OFFICE, tenant_db, start_date=None,
                           end_date=None, limit=50)
    ids = {str(r["job_id"]).replace("-", "") for r in out}

    assert live.id.hex in ids
    assert gone.id.hex not in ids


def test_a_live_jobs_parts_row_with_a_non_canonical_id_is_suggested_once(tenant_db):
    """The parts-needed route stores job_id as given; an uppercase id on a
    live job must not be dropped by the soft-delete filter."""
    cust = Customer(name="Example Contractor", company_id=TID)
    tenant_db.add(cust)
    tenant_db.flush()
    live = _job(tenant_db, "live job", customer=cust)
    tenant_db.add(JobPartNeeded(id="pn-upper", company_id=TID,
                                job_id=str(live.id).upper(),
                                part_name="Torsion spring", quantity=1,
                                status="ordered", supplier=VENDOR))
    tenant_db.flush()
    inv = _bill(tenant_db, po="Example Contractor")

    # Found by both branches; offered once, under the canonical id.
    ids = [s.job_id for s in suggest_job_matches(tenant_db, inv)]

    assert ids.count(str(live.id)) == 1
    assert str(live.id).upper() not in ids

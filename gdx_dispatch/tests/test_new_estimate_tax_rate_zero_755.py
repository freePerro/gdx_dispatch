"""GDXA-755: a new estimate's tax_rate follows its job lane.

A NULL Estimate.tax_rate is not "no tax": compute_estimate_totals falls through
to resolve_rate, i.e. TaxConfig.default_rate, for any non-exempt customer.
Installations are a construction contract and are not taxed; parts on repair
and service-call jobs are (CLAUDE.md, Doug 2026-10-12). So an estimate on no
job or on an install-lane job is stored at 0, and one on a service-call job
stays NULL, the tenant default (Doug's ruling on GDXA-772, 2026-10-13). Every
test here seeds a 7.38% tenant default: on $1,000, 73.80 is the default and
0.0 is the construction-contract rate.

Paths covered here: POST /api/estimates, POST /{id}/duplicate, the
estimates_create_draft MCP tool and proposals.service.create_estimate. The lead
start-estimate, mobile quote and portal resale paths are covered in their own
files (test_lead_estimate_link, test_mobile_quoting_phase21, test_portal_resale).
"""
from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import Job
from gdx_dispatch.modules.proposals.models import Estimate, EstimateLine
from gdx_dispatch.modules.proposals.totals import compute_estimate_totals
from gdx_dispatch.modules.tax.models import TaxConfig
from gdx_dispatch.tests.test_estimates import _create_customer, _create_job, client  # noqa: F401

RETAIL_DEFAULT = Decimal("0.0738")


@pytest.fixture()
def tc(client: TestClient) -> TestClient:  # noqa: F811 — test_estimates' app fixture
    return client


def _db(tc: TestClient):
    return next(tc.app.dependency_overrides[get_db]())


def _seed_tax_default(tc: TestClient) -> None:
    db = _db(tc)
    try:
        db.add(TaxConfig(name="Default", default_rate=RETAIL_DEFAULT))
        db.commit()
    finally:
        db.close()


def _tax_on_1000(tc: TestClient, estimate_id) -> tuple[Decimal | None, float]:
    """The stored rate, and the tax compute_estimate_totals charges on a
    $1,000 line on that estimate (the engine the PDF, portal and invoice seed read)."""
    db = _db(tc)
    try:
        est = db.get(Estimate, UUID(str(estimate_id)))
        db.add(EstimateLine(
            estimate_id=est.id, description="Door", quantity=1, unit_price=Decimal("1000"),
            line_total=Decimal("1000"), sort_order=1, company_id="tenant-test",
        ))
        est.total = Decimal("1000.00")
        db.commit()
        db.refresh(est)
        return est.tax_rate, compute_estimate_totals(est, db)["tax"]
    finally:
        db.close()


def _job(tc: TestClient, job_type: str) -> str:
    db = _db(tc)
    try:
        row = Job(
            title=job_type, job_type=job_type, customer_id=UUID(_create_customer(tc)),
            company_id="tenant-test",
        )
        db.add(row)
        db.commit()
        return str(row.id)
    finally:
        db.close()


def _legacy_null_estimate(tc: TestClient, customer_id: str, job_id: str | None = None) -> str:
    db = _db(tc)
    try:
        est = Estimate(
            customer_id=UUID(customer_id), estimate_number="EST-000500", tax_rate=None,
            job_id=UUID(job_id) if job_id else None,
            status="draft", total=Decimal("0"), public_token=uuid.uuid4().hex,
            company_id="tenant-test",
        )
        db.add(est)
        db.commit()
        return str(est.id)
    finally:
        db.close()


def test_the_seeded_default_does_tax_a_null_rate(tc: TestClient):
    """Control: without it every 0 below could be a fixture that taxes nothing.
    On a service call: a NULL rate on no job is untaxed since GDXA-930."""
    _seed_tax_default(tc)
    legacy = _legacy_null_estimate(tc, _create_customer(tc), job_id=_job(tc, "Service Call"))
    rate, tax = _tax_on_1000(tc, legacy)
    assert rate is None
    assert tax == 73.8


def test_create_without_a_rate_stores_zero(tc: TestClient):
    _seed_tax_default(tc)
    r = tc.post("/api/estimates", json={"customer_id": _create_customer(tc)})
    assert r.status_code == 201, r.text
    assert r.json()["tax_rate"] == 0.0
    rate, tax = _tax_on_1000(tc, r.json()["id"])
    assert rate == Decimal("0")
    assert tax == 0.0


def test_create_with_an_explicit_null_rate_stores_zero(tc: TestClient):
    """The editor sends tax_rate: null whenever its form equals the tenant default."""
    _seed_tax_default(tc)
    r = tc.post("/api/estimates", json={"customer_id": _create_customer(tc), "tax_rate": None})
    assert r.status_code == 201, r.text
    assert _tax_on_1000(tc, r.json()["id"]) == (Decimal("0"), 0.0)


def test_create_on_an_install_job_without_a_rate_stores_zero(tc: TestClient):
    _seed_tax_default(tc)
    r = tc.post("/api/estimates", json={"job_id": _job(tc, "Installation")})
    assert r.status_code == 201, r.text
    assert _tax_on_1000(tc, r.json()["id"]) == (Decimal("0"), 0.0)


def test_create_on_a_service_call_job_without_a_rate_stays_on_the_default(tc: TestClient):
    """Parts on a service call are taxed: a 0 here would be billed as a quoted
    0% (GDXA-842). _create_job's Job takes the model default, "Service Call"."""
    _seed_tax_default(tc)
    job_id = _create_job(tc, customer_id=_create_customer(tc))
    r = tc.post("/api/estimates", json={"job_id": job_id})
    assert r.status_code == 201, r.text
    assert r.json()["tax_rate"] is None
    assert _tax_on_1000(tc, r.json()["id"]) == (None, 73.8)


def test_create_keeps_a_rate_the_caller_names(tc: TestClient):
    _seed_tax_default(tc)
    r = tc.post("/api/estimates", json={"customer_id": _create_customer(tc), "tax_rate": 0.05})
    assert r.status_code == 201, r.text
    rate, tax = _tax_on_1000(tc, r.json()["id"])
    assert rate == Decimal("0.05")
    assert tax == 50.0


def test_duplicate_of_a_legacy_null_estimate_stores_zero(tc: TestClient):
    _seed_tax_default(tc)
    legacy = _legacy_null_estimate(tc, _create_customer(tc))
    r = tc.post(f"/api/estimates/{legacy}/duplicate")
    assert r.status_code == 201, r.text
    assert r.json()["tax_rate"] == 0.0
    assert _tax_on_1000(tc, r.json()["id"]) == (Decimal("0"), 0.0)
    # The source is left as it was: the backfill is GDXA-756's, not a copy's.
    db = _db(tc)
    try:
        assert db.get(Estimate, UUID(legacy)).tax_rate is None
    finally:
        db.close()


def test_duplicate_of_a_null_service_call_estimate_stores_zero(tc: TestClient):
    """The copy is on no job, and converting it makes an installation whose
    invoice is untaxed (audit, GDXA-772): the source's service lane would have
    quoted 73.80 and billed 0."""
    _seed_tax_default(tc)
    legacy = _legacy_null_estimate(tc, _create_customer(tc), job_id=_job(tc, "Service Call"))
    r = tc.post(f"/api/estimates/{legacy}/duplicate")
    assert r.status_code == 201, r.text
    assert r.json()["job_id"] is None
    assert _tax_on_1000(tc, r.json()["id"]) == (Decimal("0"), 0.0)


def test_duplicate_keeps_the_sources_named_rate(tc: TestClient):
    _seed_tax_default(tc)
    r = tc.post("/api/estimates", json={"customer_id": _create_customer(tc), "tax_rate": 0.05})
    dup = tc.post(f"/api/estimates/{r.json()['id']}/duplicate")
    assert dup.status_code == 201, dup.text
    assert _tax_on_1000(tc, dup.json()["id"]) == (Decimal("0.05"), 50.0)


def test_patch_null_rate_does_not_put_a_new_estimate_on_the_default(tc: TestClient):
    """A page loaded before GDXA-755 PATCHes tax_rate: null whenever its form
    equals the tenant default. Before the guard that wrote NULL: tax 73.80."""
    _seed_tax_default(tc)
    est_id = tc.post("/api/estimates", json={"customer_id": _create_customer(tc)}).json()["id"]
    r = tc.patch(f"/api/estimates/{est_id}", json={"tax_rate": None})
    assert r.status_code == 200, r.text
    assert r.json()["tax_rate"] == 0.0
    assert _tax_on_1000(tc, est_id) == (Decimal("0"), 0.0)


def test_patch_null_rate_clears_a_named_rate_to_zero(tc: TestClient):
    _seed_tax_default(tc)
    est_id = tc.post("/api/estimates", json={"customer_id": _create_customer(tc), "tax_rate": 0.05}).json()["id"]
    assert tc.patch(f"/api/estimates/{est_id}", json={"tax_rate": None}).status_code == 200
    assert _tax_on_1000(tc, est_id) == (Decimal("0"), 0.0)


def test_patch_null_rate_on_a_rated_service_call_estimate_returns_it_to_the_default(tc: TestClient):
    _seed_tax_default(tc)
    job_id = _job(tc, "Service Call")
    est_id = tc.post("/api/estimates", json={"job_id": job_id, "tax_rate": 0.05}).json()["id"]
    assert tc.patch(f"/api/estimates/{est_id}", json={"tax_rate": None}).status_code == 200
    assert _tax_on_1000(tc, est_id) == (None, 73.8)


def test_patch_null_rate_on_an_install_estimate_stores_zero(tc: TestClient):
    _seed_tax_default(tc)
    job_id = _job(tc, "Installation")
    est_id = tc.post("/api/estimates", json={"job_id": job_id, "tax_rate": 0.05}).json()["id"]
    assert tc.patch(f"/api/estimates/{est_id}", json={"tax_rate": None}).status_code == 200
    assert _tax_on_1000(tc, est_id) == (Decimal("0"), 0.0)


def test_patch_null_rate_leaves_a_legacy_null_row_as_it_was(tc: TestClient):
    """Existing rows are GDXA-756's backfill, not a side effect of an edit."""
    _seed_tax_default(tc)
    legacy = _legacy_null_estimate(tc, _create_customer(tc), job_id=_job(tc, "Service Call"))
    assert tc.patch(f"/api/estimates/{legacy}", json={"tax_rate": None, "notes": "x"}).status_code == 200
    assert _tax_on_1000(tc, legacy) == (None, 73.8)


def _mcp_draft(tc: TestClient, job_id: str | None = None) -> dict:
    """A real handler call on a real session — not the mocked one in test_tool_estimates."""
    from gdx_dispatch.core.mcp_tools.estimates_create_draft import handler

    customer_id = _create_customer(tc)
    if job_id is not None:  # the tool requires the job to be the customer's
        db = _db(tc)
        try:
            db.get(Job, UUID(job_id)).customer_id = UUID(customer_id)
            db.commit()
        finally:
            db.close()
    db = _db(tc)
    try:
        out = asyncio.run(handler(
            SimpleNamespace(tenant_id="tenant-test"), db, customer_id=customer_id,
            job_id=job_id, label="AI draft", approval_ref="appr-1",
        ))
    finally:
        db.close()
    assert out["estimate"]["preview"] is False, out
    return out["estimate"]


def test_mcp_create_draft_without_a_job_stores_zero(tc: TestClient):
    _seed_tax_default(tc)
    assert _tax_on_1000(tc, _mcp_draft(tc)["id"]) == (Decimal("0"), 0.0)


def test_mcp_create_draft_on_a_service_call_job_stays_on_the_default(tc: TestClient):
    _seed_tax_default(tc)
    est = _mcp_draft(tc, job_id=_job(tc, "Service Call"))
    assert _tax_on_1000(tc, est["id"]) == (None, 73.8)


@pytest.mark.parametrize(("job_type", "expected"), [
    ("Installation", (Decimal("0"), 0.0)),
    ("New Construction", (Decimal("0"), 0.0)),  # GDXA-889
    ("Service Call", (None, 73.8)),
])
def test_proposals_service_create_estimate_follows_the_job_lane(tc: TestClient, job_type, expected):
    from gdx_dispatch.modules.proposals.service import create_estimate

    _seed_tax_default(tc)
    job_id = _job(tc, job_type)
    db = _db(tc)
    try:
        est = create_estimate(UUID(job_id), "EST-000600", db)
        est_id = est.id
    finally:
        db.close()
    assert _tax_on_1000(tc, est_id) == expected


# --- GDXA-892 (Doug, 2026-10-13): the 2026-10-13 wholesale ruling does not
# reach a job-less estimate. A contractor or wholesale customer is keyed on
# customer_type (modules/reseller/service.py), and a job-less estimate for one
# stays at 0 like any other: it converts to an Installation, a construction
# contract, untaxed on both the quote and the invoice.


def _customer(tc: TestClient, customer_type: str) -> str:
    from gdx_dispatch.models.tenant_models import Customer

    customer_id = _create_customer(tc, name=f"{customer_type} customer")
    db = _db(tc)
    try:
        db.get(Customer, UUID(customer_id)).customer_type = customer_type
        db.commit()
    finally:
        db.close()
    return customer_id


def _job_for(tc: TestClient, job_type: str, customer_id: str) -> str:
    job_id = _job(tc, job_type)
    db = _db(tc)
    try:
        db.get(Job, UUID(job_id)).customer_id = UUID(customer_id)
        db.commit()
    finally:
        db.close()
    return job_id


@pytest.mark.parametrize(("customer_type", "job_type", "expected"), [
    ("Retail", None, (Decimal("0"), 0.0)),
    ("Contractor", None, (Decimal("0"), 0.0)),
    ("Wholesale", None, (Decimal("0"), 0.0)),
    ("Contractor", "Installation", (Decimal("0"), 0.0)),
    ("Retail", "Installation", (Decimal("0"), 0.0)),
    ("Contractor", "Service Call", (None, 73.8)),
    ("Retail", "Service Call", (None, 73.8)),
])
def test_create_follows_the_lane_whatever_the_customer_type(tc: TestClient, customer_type, job_type, expected):
    _seed_tax_default(tc)
    customer_id = _customer(tc, customer_type)
    body = {"customer_id": customer_id}
    if job_type is not None:
        body["job_id"] = _job_for(tc, job_type, customer_id)
    r = tc.post("/api/estimates", json=body)
    assert r.status_code == 201, r.text
    assert _tax_on_1000(tc, r.json()["id"]) == expected


def test_walk_contractor_create_duplicate_convert_invoice_is_untaxed(tc: TestClient, monkeypatch):
    """GDXA-892 step 2, a non-exempt contractor on $1,000 of parts: create,
    duplicate, convert-to-job (job_type "Installation") and the invoice seeded
    from that estimate all charge 0, so the quote and the bill agree."""
    import gdx_dispatch.routers.estimates as estimates_router
    from gdx_dispatch.core.pay_periods import shop_today_from_settings
    from gdx_dispatch.routers.invoices import InvoiceCreateIn, InvoiceLineCreateIn, create_invoice

    monkeypatch.setattr(estimates_router, "allocate_job_number", lambda *a, **k: "JOB-T-001")
    _seed_tax_default(tc)
    customer_id = _customer(tc, "Contractor")

    created = tc.post("/api/estimates", json={"customer_id": customer_id})
    assert created.status_code == 201, created.text
    assert _tax_on_1000(tc, created.json()["id"]) == (Decimal("0"), 0.0)

    dup = tc.post(f"/api/estimates/{created.json()['id']}/duplicate")
    assert dup.status_code == 201, dup.text
    dup_id = dup.json()["id"]
    assert _tax_on_1000(tc, dup_id) == (Decimal("0"), 0.0)

    db = _db(tc)
    try:
        db.get(Estimate, UUID(dup_id)).status = "accepted"
        db.commit()
    finally:
        db.close()
    conv = tc.post(f"/api/estimates/{dup_id}/convert-to-job")
    assert conv.status_code == 200, conv.text
    job_id = UUID(conv.json()["job_id"])

    db = _db(tc)
    try:
        assert db.get(Job, job_id).job_type == "Installation"
        est = db.get(Estimate, UUID(dup_id))
        assert compute_estimate_totals(est, db)["tax"] == 0.0
        inv = create_invoice(
            payload=InvoiceCreateIn(
                job_id=job_id, customer_id=UUID(customer_id),
                invoice_date=shop_today_from_settings(db), source_estimate_id=est.id,
                line_items=[InvoiceLineCreateIn(
                    description="Door", quantity=1, unit_price=1000.0, taxable=True,
                )],
            ),
            _={"sub": "u-1", "user_id": "u-1", "role": "admin", "tenant_id": "tenant-test"},
            db=db,
        )
    finally:
        db.close()
    assert float(inv["tax_rate"]) == 0.0
    assert round(inv["tax_amount"], 2) == 0.0

"""GDXA-930: an estimate stored with a NULL tax_rate follows its job lane too.

GDXA-755 stamps new estimates by lane (`new_estimate_tax_rate`) but left
existing rows alone, and `compute_estimate_totals` resolved a NULL rate to the
tenant default with no lane check. So a legacy NULL-rate estimate on no job
or on an install-lane job printed "Tax (7.38%)" on the PDF, the public page
and the portal, while the invoice made from it (`resolve_rate_for_job`)
billed 0. Installations are never taxed (CLAUDE.md, Doug 2026-10-12).

Every test seeds a 7.38% tenant default: on $1,000, 73.80 is the default and
0.0 is the construction-contract rate.
"""
from __future__ import annotations

import uuid
from decimal import Decimal
from uuid import UUID

from fastapi.testclient import TestClient

from gdx_dispatch.models.tenant_models import Job
from gdx_dispatch.modules.proposals.models import Estimate
from gdx_dispatch.modules.tax.models import TaxExemption
from gdx_dispatch.tests.test_estimates import _create_customer, client  # noqa: F401
from gdx_dispatch.tests.test_new_estimate_tax_rate_zero_755 import (
    _db,
    _job,
    _seed_tax_default,
    _tax_on_1000,
    tc,  # noqa: F401
)


def _legacy_null_estimate(tc: TestClient, customer_id: str, job_id: str | None = None) -> str:  # noqa: F811
    """A NULL-rate row as it predates GDXA-755, with its own estimate number."""
    db = _db(tc)
    try:
        est = Estimate(
            customer_id=UUID(customer_id), estimate_number=f"EST-{uuid.uuid4().hex[:8]}",
            tax_rate=None, job_id=UUID(job_id) if job_id else None,
            status="draft", total=Decimal("0"), public_token=uuid.uuid4().hex,
            company_id="tenant-test",
        )
        db.add(est)
        db.commit()
        return str(est.id)
    finally:
        db.close()


def test_null_rate_service_call_estimate_is_still_taxed(tc: TestClient):  # noqa: F811
    """Control: without it every 0 below could be a fixture that taxes nothing."""
    _seed_tax_default(tc)
    est = _legacy_null_estimate(tc, _create_customer(tc), job_id=_job(tc, "Service Call"))
    assert _tax_on_1000(tc, est) == (None, 73.8)


def test_null_rate_jobless_estimate_is_not_taxed(tc: TestClient):  # noqa: F811
    _seed_tax_default(tc)
    est = _legacy_null_estimate(tc, _create_customer(tc))
    assert _tax_on_1000(tc, est) == (None, 0.0)


def test_null_rate_install_lane_estimates_are_not_taxed(tc: TestClient):  # noqa: F811
    _seed_tax_default(tc)
    for job_type in ("Installation", "New Construction"):
        est = _legacy_null_estimate(tc, _create_customer(tc), job_id=_job(tc, job_type))
        assert _tax_on_1000(tc, est) == (None, 0.0), job_type


def test_null_rate_estimate_on_a_missing_job_is_not_taxed(tc: TestClient):  # noqa: F811
    """A job id that matches no job is treated as no job, as new_estimate_tax_rate does."""
    _seed_tax_default(tc)
    est = _legacy_null_estimate(tc, _create_customer(tc), job_id=_job(tc, "Service Call"))
    db = _db(tc)
    try:
        db.delete(db.get(Job, db.get(Estimate, UUID(est)).job_id))
        db.commit()
    finally:
        db.close()
    assert _tax_on_1000(tc, est) == (None, 0.0)


def test_null_rate_service_call_estimate_for_an_exempt_customer_is_not_taxed(tc: TestClient):  # noqa: F811
    _seed_tax_default(tc)
    customer_id = _create_customer(tc)
    db = _db(tc)
    try:
        db.add(TaxExemption(customer_id=UUID(customer_id), exempt=True))
        db.commit()
    finally:
        db.close()
    est = _legacy_null_estimate(tc, customer_id, job_id=_job(tc, "Service Call"))
    assert _tax_on_1000(tc, est) == (None, 0.0)


def test_a_named_rate_on_a_jobless_estimate_is_kept(tc: TestClient):  # noqa: F811
    """Only the NULL branch changes: a rate someone typed is the quote."""
    _seed_tax_default(tc)
    est = _legacy_null_estimate(tc, _create_customer(tc))
    db = _db(tc)
    try:
        db.get(Estimate, UUID(est)).tax_rate = Decimal("0.05")
        db.commit()
    finally:
        db.close()
    assert _tax_on_1000(tc, est) == (Decimal("0.05"), 50.0)


def test_get_estimate_names_the_rate_a_null_rate_resolves_to(tc: TestClient):  # noqa: F811
    """The office editor shows that rate for a NULL-rate estimate; reading the
    tenant default there would show 7.38% tax the PDF does not print."""
    _seed_tax_default(tc)
    jobless = _legacy_null_estimate(tc, _create_customer(tc))
    service = _legacy_null_estimate(tc, _create_customer(tc), job_id=_job(tc, "Service Call"))
    assert tc.get(f"/api/estimates/{jobless}").json()["null_tax_rate"] == 0.0
    assert tc.get(f"/api/estimates/{service}").json()["null_tax_rate"] is None

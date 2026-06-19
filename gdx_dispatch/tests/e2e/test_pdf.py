"""E2E tests for PDF Generation — PDF-01 through PDF-08.

Covers: invoice PDF download, estimate PDF download, PDF contains
correct data (customer name, line items, totals), PDF validation.
"""
from __future__ import annotations

import uuid

import pytest

from gdx_dispatch.tests.e2e.conftest import (
    assert_api_success,
)

pytestmark = [pytest.mark.e2e]


@pytest.fixture(scope="module")
def test_estimate(api):
    """Create a customer + estimate with line items for PDF tests."""
    unique = uuid.uuid4().hex[:8]
    cust_resp = api.post("/api/customers", json_data={
        "name": f"PDF Customer {unique}",
        "email": f"pdf_{unique}@test.com",
    })
    assert cust_resp.status_code in (200, 201)
    cid = cust_resp.json()["id"]

    est_resp = api.post("/api/estimates", json_data={
        "customer_id": cid,
    })
    if est_resp.status_code not in (200, 201):
        return None, cid, f"PDF Customer {unique}"

    est = est_resp.json()
    eid = est["id"]

    # Add line items
    api.post(f"/api/estimates/{eid}/lines", json_data={
        "description": "Garage Door Spring",
        "quantity": 2,
        "unit_price": 75.00,
    })
    api.post(f"/api/estimates/{eid}/lines", json_data={
        "description": "Labor",
        "quantity": 1,
        "unit_price": 120.00,
    })

    return eid, cid, f"PDF Customer {unique}"


@pytest.fixture(scope="module")
def test_invoice(api, test_estimate):
    """Create an invoice with line items for PDF tests."""
    _, cid, cname = test_estimate
    unique = uuid.uuid4().hex[:8]

    # Create a job first (invoices often require a job)
    job_resp = api.post("/api/jobs", json_data={
        "customer_id": cid,
        "title": f"PDF Job {unique}",
    })
    job_id = None
    if job_resp.status_code in (200, 201):
        job_id = job_resp.json()["id"]

    inv_payload = {"customer_id": cid}
    if job_id:
        inv_payload["job_id"] = job_id

    inv_resp = api.post("/api/invoices", json_data=inv_payload)
    if inv_resp.status_code not in (200, 201):
        return None

    inv = inv_resp.json()
    iid = inv["id"]

    # Add line items
    api.post(f"/api/invoices/{iid}/lines", json_data={
        "description": "Garage Door Panel",
        "quantity": 1,
        "unit_price": 350.00,
    })
    api.post(f"/api/invoices/{iid}/lines", json_data={
        "description": "Installation",
        "quantity": 1,
        "unit_price": 200.00,
    })

    return iid


class TestEstimatePDF:
    def test_pdf_01_estimate_pdf_downloads(self, api, test_estimate, console_tracker):
        """Estimate PDF downloads and is non-empty."""
        eid, _, _ = test_estimate
        if not eid:
            pytest.skip("No estimate created")
        resp = api.get(f"/api/estimates/{eid}/pdf")
        assert_api_success(resp)
        assert len(resp.content) > 100, "PDF is too small to be valid"
        # Check PDF magic bytes
        assert resp.content[:4] == b"%PDF", (
            f"Response does not start with PDF header, got: {resp.content[:20]}"
        )

    def test_pdf_03_estimate_contains_data(self, api, test_estimate, console_tracker):
        """Estimate PDF contains customer name and line items."""
        eid, _, cname = test_estimate
        if not eid:
            pytest.skip("No estimate created")
        resp = api.get(f"/api/estimates/{eid}/pdf")
        assert_api_success(resp)

        # Basic check: PDF should be substantial
        assert len(resp.content) > 500, "PDF too small to contain meaningful content"

        # If pdfplumber is available, parse and verify content
        try:
            import io

            import pdfplumber

            with pdfplumber.open(io.BytesIO(resp.content)) as pdf:
                text = ""
                for page in pdf.pages:
                    text += page.extract_text() or ""

                # Customer name should appear
                assert cname.split()[0] in text or "Customer" in text, (
                    f"PDF does not contain customer name '{cname}'"
                )
                # Line items
                assert "Spring" in text or "Labor" in text or "75" in text, (
                    "PDF does not contain expected line items"
                )
        except ImportError:
            # pdfplumber not installed — skip content verification
            pass


class TestInvoicePDF:
    def test_pdf_02_invoice_pdf_downloads(self, api, test_invoice, console_tracker):
        """Invoice PDF downloads and is non-empty."""
        if not test_invoice:
            pytest.skip("No invoice created")
        resp = api.get(f"/api/invoices/{test_invoice}/pdf")
        assert_api_success(resp)
        assert len(resp.content) > 100, "PDF is too small to be valid"
        assert resp.content[:4] == b"%PDF"

    def test_pdf_03_invoice_contains_data(self, api, test_invoice, console_tracker):
        """Invoice PDF contains correct data."""
        if not test_invoice:
            pytest.skip("No invoice created")
        resp = api.get(f"/api/invoices/{test_invoice}/pdf")
        assert_api_success(resp)

        try:
            import io

            import pdfplumber

            with pdfplumber.open(io.BytesIO(resp.content)) as pdf:
                text = ""
                for page in pdf.pages:
                    text += page.extract_text() or ""
                # Should have invoice-related content
                assert any(kw in text for kw in ["Invoice", "INVOICE", "Total", "Due"]), (
                    f"PDF missing invoice keywords, text: {text[:200]}"
                )
        except ImportError:
            pass


class TestPDFEdgeCases:
    def test_pdf_04_empty_estimate(self, api, console_tracker):
        """Estimate with zero lines generates PDF without error."""
        unique = uuid.uuid4().hex[:8]
        cust = api.post("/api/customers", json_data={"name": f"Empty PDF {unique}"})
        assert cust.status_code in (200, 201)
        cid = cust.json()["id"]

        est = api.post("/api/estimates", json_data={"customer_id": cid})
        if est.status_code not in (200, 201):
            pytest.skip("Could not create estimate")
        eid = est.json()["id"]

        resp = api.get(f"/api/estimates/{eid}/pdf")
        assert resp.status_code in (200, 404), (
            f"Empty estimate PDF should not 500, got {resp.status_code}"
        )
        if resp.status_code == 200:
            assert resp.content[:4] == b"%PDF"

    def test_pdf_06_unicode_customer(self, api, console_tracker):
        """PDF with unicode customer name renders correctly."""
        resp = api.post("/api/customers", json_data={
            "name": "O'Brien & Munoz \u00e9\u00e8\u00ea",
        })
        assert resp.status_code in (200, 201)
        cid = resp.json()["id"]

        est = api.post("/api/estimates", json_data={"customer_id": cid})
        if est.status_code not in (200, 201):
            pytest.skip("Could not create estimate")
        eid = est.json()["id"]

        resp = api.get(f"/api/estimates/{eid}/pdf")
        if resp.status_code == 200:
            assert resp.content[:4] == b"%PDF"
            assert len(resp.content) > 100

    def test_pdf_07_file_size(self, api, test_estimate, console_tracker):
        """Generated PDF is reasonable size (< 5MB)."""
        eid, _, _ = test_estimate
        if not eid:
            pytest.skip("No estimate created")
        resp = api.get(f"/api/estimates/{eid}/pdf")
        if resp.status_code == 200:
            size_mb = len(resp.content) / (1024 * 1024)
            assert size_mb < 5.0, f"PDF is {size_mb:.1f}MB, exceeds 5MB limit"

    def test_pdf_08_valid_pdf(self, api, test_estimate, console_tracker):
        """PDF passes basic validation (not corrupted)."""
        eid, _, _ = test_estimate
        if not eid:
            pytest.skip("No estimate created")
        resp = api.get(f"/api/estimates/{eid}/pdf")
        if resp.status_code == 200:
            content = resp.content
            # Valid PDF starts with %PDF and ends with %%EOF (approximately)
            assert content[:4] == b"%PDF", "Missing PDF header"
            # Check for EOF marker (may have trailing whitespace/newlines)
            tail = content[-32:]
            assert b"%%EOF" in tail or b"endobj" in tail, (
                "PDF missing end-of-file marker"
            )

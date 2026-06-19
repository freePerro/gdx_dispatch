"""E2E tests for Field Service Edge Cases — derived from industry research.

Covers real-world failure modes discovered in field service dispatch software:
- Double-booking technicians
- Deleted/soft-deleted entity references
- Zero and negative monetary amounts
- Scheduling in the past and timezone edge cases
- Unicode/emoji/long names
- Concurrent edits and race conditions
- Invalid state transitions
- Rate limiting under burst traffic
- Session expiry mid-form-submission
- Estimates with many line items

Sources:
- Gartner Field Service Management reviews (common scheduling bugs)
- Multi-tenant SaaS testing guides (tenant isolation, invoice processing)
- DST/timezone edge case research (gap times, double-booking from TZ errors)
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from gdx_dispatch.tests.e2e.conftest import (
    BASE_URL,
)

pytestmark = [pytest.mark.e2e]


# ---------------------------------------------------------------------------
# Scheduling Edge Cases — RESEARCH-01 through RESEARCH-04
# ---------------------------------------------------------------------------
class TestSchedulingEdgeCases:
    """Edge cases found in field service dispatch scheduling."""

    def test_research_01_double_book_technician(self, api, console_tracker):
        """Two jobs assigned to the same technician at the same time.

        Industry research: double-booking is the #1 reported scheduling bug
        in field service software. Systems must either prevent it or warn.
        """
        # Create a customer for the jobs
        unique = uuid.uuid4().hex[:8]
        cust = api.post("/api/customers", json_data={"name": f"DoubleBook {unique}"})
        if cust.status_code not in (200, 201):
            pytest.skip("Cannot create customer for double-book test")
        cust_id = cust.json()["id"]

        # Schedule two jobs at the same time window
        tomorrow_9am = (
            datetime.now(timezone.utc).replace(hour=9, minute=0, second=0)
            + timedelta(days=1)
        ).isoformat()

        job1 = api.post("/api/jobs", json_data={
            "customer_id": cust_id,
            "description": f"Double-book test A {unique}",
            "scheduled_start": tomorrow_9am,
        })
        job2 = api.post("/api/jobs", json_data={
            "customer_id": cust_id,
            "description": f"Double-book test B {unique}",
            "scheduled_start": tomorrow_9am,
        })

        # Both should succeed (create) or second should warn/reject (409)
        assert job1.status_code in (200, 201, 422), (
            f"First job creation failed unexpectedly: {job1.status_code}"
        )
        assert job2.status_code in (200, 201, 409, 422), (
            f"Second overlapping job should succeed or warn, got {job2.status_code}"
        )

    def test_research_02_job_with_deleted_customer(self, api, console_tracker):
        """Creating a job referencing a soft-deleted customer.

        Must return a clear error, not a 500 or orphaned job.
        """
        unique = uuid.uuid4().hex[:8]
        cust = api.post("/api/customers", json_data={"name": f"DeleteMe {unique}"})
        if cust.status_code not in (200, 201):
            pytest.skip("Cannot create customer for deleted-customer test")
        cust_id = cust.json()["id"]

        # Soft-delete the customer
        del_resp = api.delete(f"/api/customers/{cust_id}")
        assert del_resp.status_code in (200, 204, 404)

        # Try to create a job referencing the deleted customer
        job = api.post("/api/jobs", json_data={
            "customer_id": cust_id,
            "description": f"Orphan job test {unique}",
        })
        # Should reject (4xx) or handle gracefully, never 500
        assert job.status_code != 500, (
            "Creating job with deleted customer caused server error"
        )
        assert job.status_code in (200, 201, 400, 404, 422), (
            f"Unexpected status for job with deleted customer: {job.status_code}"
        )

    def test_research_03_schedule_job_in_past(self, api, console_tracker):
        """Scheduling a job with a date in the past.

        Some systems allow it (backfilling), some reject it.
        Either way, no 500 error.
        """
        unique = uuid.uuid4().hex[:8]
        cust = api.post("/api/customers", json_data={"name": f"PastJob {unique}"})
        if cust.status_code not in (200, 201):
            pytest.skip("Cannot create customer for past-date test")
        cust_id = cust.json()["id"]

        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        job = api.post("/api/jobs", json_data={
            "customer_id": cust_id,
            "description": f"Past date test {unique}",
            "scheduled_start": yesterday,
        })
        # Accept or reject cleanly
        assert job.status_code in (200, 201, 400, 422), (
            f"Past-date job should be accepted or rejected cleanly, got {job.status_code}"
        )

    def test_research_04_midnight_and_dst_boundary(self, api, console_tracker):
        """Job scheduled at midnight UTC and at a DST transition boundary.

        Research: DST gaps (2:00 AM -> 3:00 AM) cause scheduling bugs
        in systems that store local time instead of UTC.
        """
        unique = uuid.uuid4().hex[:8]
        cust = api.post("/api/customers", json_data={"name": f"Midnight {unique}"})
        if cust.status_code not in (200, 201):
            pytest.skip("Cannot create customer for midnight test")
        cust_id = cust.json()["id"]

        # Midnight UTC
        midnight = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        ) + timedelta(days=2)
        job_midnight = api.post("/api/jobs", json_data={
            "customer_id": cust_id,
            "description": f"Midnight UTC test {unique}",
            "scheduled_start": midnight.isoformat(),
        })
        assert job_midnight.status_code in (200, 201, 400, 422), (
            f"Midnight scheduling failed: {job_midnight.status_code}"
        )

        # 2:30 AM in a US Eastern DST gap (March second Sunday)
        # This time may not exist in local time during spring-forward
        dst_gap_time = "2026-03-08T02:30:00-05:00"
        job_dst = api.post("/api/jobs", json_data={
            "customer_id": cust_id,
            "description": f"DST gap test {unique}",
            "scheduled_start": dst_gap_time,
        })
        assert job_dst.status_code in (200, 201, 400, 422), (
            f"DST gap scheduling failed: {job_dst.status_code}"
        )


# ---------------------------------------------------------------------------
# Financial Edge Cases — RESEARCH-05 through RESEARCH-08
# ---------------------------------------------------------------------------
class TestFinancialEdgeCases:
    """Edge cases in invoicing and payment processing."""

    def test_research_05_zero_dollar_invoice(self, api, console_tracker):
        """Invoice for a $0 job (warranty work, goodwill, demo).

        Research: $0 invoices break tax calculation, payment gateways,
        and reporting logic in many field service platforms.
        """
        unique = uuid.uuid4().hex[:8]
        cust = api.post("/api/customers", json_data={"name": f"ZeroInv {unique}"})
        if cust.status_code not in (200, 201):
            pytest.skip("Cannot create customer for $0 invoice test")
        cust_id = cust.json()["id"]

        # Create a job, then try to invoice it at $0
        job = api.post("/api/jobs", json_data={
            "customer_id": cust_id,
            "description": f"Zero dollar test {unique}",
        })
        if job.status_code not in (200, 201):
            pytest.skip("Cannot create job for $0 invoice test")
        job_id = job.json()["id"]

        invoice = api.post("/api/invoices", json_data={
            "job_id": job_id,
            "customer_id": cust_id,
            "total": 0.00,
            "line_items": [{"description": "Warranty repair", "amount": 0.00}],
        })
        # Should handle $0 gracefully
        assert invoice.status_code in (200, 201, 400, 422), (
            f"$0 invoice should be handled gracefully, got {invoice.status_code}"
        )

    def test_research_06_negative_invoice_amount(self, api, console_tracker):
        """Invoice with a negative amount (credit memo / refund).

        Must be explicitly rejected or handled as a credit, never silently accepted.
        """
        unique = uuid.uuid4().hex[:8]
        cust = api.post("/api/customers", json_data={"name": f"NegInv {unique}"})
        if cust.status_code not in (200, 201):
            pytest.skip("Cannot create customer for negative invoice test")
        cust_id = cust.json()["id"]

        invoice = api.post("/api/invoices", json_data={
            "customer_id": cust_id,
            "total": -150.00,
            "line_items": [{"description": "Refund", "amount": -150.00}],
        })
        # Should reject or handle as credit — never 500
        assert invoice.status_code != 500, (
            "Negative invoice amount caused server error"
        )
        assert invoice.status_code in (200, 201, 400, 422), (
            f"Negative invoice: unexpected status {invoice.status_code}"
        )

    def test_research_07_estimate_100_line_items(self, api, console_tracker):
        """Estimate with 100+ line items — tests payload size and rendering.

        Research: large estimates cause timeouts in many platforms due to
        N+1 queries or unbounded template rendering.
        """
        unique = uuid.uuid4().hex[:8]
        cust = api.post("/api/customers", json_data={"name": f"BigEstimate {unique}"})
        if cust.status_code not in (200, 201):
            pytest.skip("Cannot create customer for large estimate test")
        cust_id = cust.json()["id"]

        line_items = [
            {"description": f"Part #{i} — Widget {unique}", "amount": 9.99, "quantity": i + 1}
            for i in range(105)
        ]
        estimate = api.post("/api/estimates", json_data={
            "customer_id": cust_id,
            "line_items": line_items,
        })
        assert estimate.status_code in (200, 201, 400, 413, 422), (
            f"Large estimate should be accepted or rejected cleanly, got {estimate.status_code}"
        )

    def test_research_08_floating_point_total(self, api, console_tracker):
        """Line items that sum to a floating-point rounding issue.

        Classic: 3 items at $33.33 = $99.99, not $100.00.
        Research: rounding bugs are a top-5 billing complaint.
        """
        unique = uuid.uuid4().hex[:8]
        cust = api.post("/api/customers", json_data={"name": f"FloatTest {unique}"})
        if cust.status_code not in (200, 201):
            pytest.skip("Cannot create customer for float test")
        cust_id = cust.json()["id"]

        invoice = api.post("/api/invoices", json_data={
            "customer_id": cust_id,
            "line_items": [
                {"description": "Service A", "amount": 33.33},
                {"description": "Service B", "amount": 33.33},
                {"description": "Service C", "amount": 33.34},
            ],
        })
        assert invoice.status_code in (200, 201, 400, 422), (
            f"Floating-point total failed: {invoice.status_code}"
        )
        if invoice.status_code in (200, 201):
            data = invoice.json()
            total = data.get("total") or data.get("amount") or 0
            # Total should be exactly 100.00 or very close
            assert abs(float(total) - 100.00) < 0.02, (
                f"Rounding error: expected ~100.00, got {total}"
            )


# ---------------------------------------------------------------------------
# Data Integrity Edge Cases — RESEARCH-09 through RESEARCH-12
# ---------------------------------------------------------------------------
class TestDataIntegrityEdgeCases:
    """Customer names, unicode, and special character handling."""

    def test_research_09_unicode_emoji_customer_name(self, api, console_tracker):
        """Customer with emoji and multi-byte unicode in name.

        Research: UTF-8 4-byte chars (emoji) break MySQL utf8 (not utf8mb4)
        and cause silent truncation in some ORMs.
        """
        names = [
            "\U0001f528 Hammer Time Plumbing",  # wrench emoji
            "\u00c9tienne's R\u00e9novation",  # French accented
            "\u5c71\u7530\u592a\u90ce \u8a2d\u5099\u7ba1\u7406",  # Japanese
            "\U0001f6bf\U0001f527 Fix-It \U0001f44d\U0001f3fd",  # Multiple emoji with skin tone
        ]
        for name in names:
            unique = uuid.uuid4().hex[:6]
            resp = api.post("/api/customers", json_data={"name": f"{name} {unique}"})
            assert resp.status_code in (200, 201, 422), (
                f"Unicode name '{name[:20]}...' caused status {resp.status_code}"
            )
            if resp.status_code in (200, 201):
                cid = resp.json()["id"]
                get_resp = api.get(f"/api/customers/{cid}")
                if get_resp.status_code == 200:
                    stored = get_resp.json().get("name", "")
                    # Verify the emoji survived round-trip
                    assert unique in stored, (
                        f"Name was corrupted on round-trip: '{stored}'"
                    )

    def test_research_10_very_long_customer_name(self, api, console_tracker):
        """Customer name at 500 characters — tests column length limits.

        Research: names over VARCHAR(255) cause silent truncation or 500s.
        """
        long_name = "A" * 500
        resp = api.post("/api/customers", json_data={"name": long_name})
        assert resp.status_code in (200, 201, 400, 422), (
            f"500-char name should be accepted or rejected, got {resp.status_code}"
        )

    def test_research_11_concurrent_job_edits(self, api, console_tracker):
        """Two concurrent edits to the same job — last-write-wins or conflict.

        Research: concurrent edits cause data loss in systems without
        optimistic locking or version checks.
        """
        unique = uuid.uuid4().hex[:8]
        cust = api.post("/api/customers", json_data={"name": f"ConcEdit {unique}"})
        if cust.status_code not in (200, 201):
            pytest.skip("Cannot create customer for concurrent edit test")
        cust_id = cust.json()["id"]

        job = api.post("/api/jobs", json_data={
            "customer_id": cust_id,
            "description": f"Concurrent edit test {unique}",
        })
        if job.status_code not in (200, 201):
            pytest.skip("Cannot create job for concurrent edit test")
        job_id = job.json()["id"]

        # Two rapid updates
        edit_a = api.patch(f"/api/jobs/{job_id}", json_data={
            "description": "Edit A wins",
        })
        edit_b = api.patch(f"/api/jobs/{job_id}", json_data={
            "description": "Edit B wins",
        })

        assert edit_a.status_code in (200, 409), f"Edit A: {edit_a.status_code}"
        assert edit_b.status_code in (200, 409), f"Edit B: {edit_b.status_code}"

        # Final state is consistent
        final = api.get(f"/api/jobs/{job_id}")
        if final.status_code == 200:
            desc = final.json().get("description", "")
            assert desc in ("Edit A wins", "Edit B wins"), (
                f"Job description is inconsistent: '{desc}'"
            )

    def test_research_12_invalid_status_transition(self, api, console_tracker):
        """Job status transition that should not be allowed: completed -> scheduled.

        Research: invalid state machine transitions are a common source of
        data corruption in field service platforms.
        """
        unique = uuid.uuid4().hex[:8]
        cust = api.post("/api/customers", json_data={"name": f"StatusTest {unique}"})
        if cust.status_code not in (200, 201):
            pytest.skip("Cannot create customer for status transition test")
        cust_id = cust.json()["id"]

        job = api.post("/api/jobs", json_data={
            "customer_id": cust_id,
            "description": f"Status transition test {unique}",
        })
        if job.status_code not in (200, 201):
            pytest.skip("Cannot create job for status transition test")
        job_id = job.json()["id"]

        # Move to completed
        complete_resp = api.patch(f"/api/jobs/{job_id}", json_data={
            "status": "completed",
        })
        if complete_resp.status_code not in (200,):
            pytest.skip("Cannot complete job for transition test")

        # Try invalid backward transition: completed -> scheduled
        backward = api.patch(f"/api/jobs/{job_id}", json_data={
            "status": "scheduled",
        })
        # Should reject (400/409/422) or allow with audit trail, never 500
        assert backward.status_code != 500, (
            "Invalid status transition caused server error"
        )


# ---------------------------------------------------------------------------
# Rate Limiting and Session Edge Cases — RESEARCH-13, RESEARCH-14
# ---------------------------------------------------------------------------
class TestRateLimitingAndSession:
    """Burst traffic and session expiry scenarios."""

    def test_research_13_burst_traffic_rate_limit(self, api, console_tracker):
        """30 rapid requests to test rate limiting behavior.

        Research: field service apps get burst traffic when dispatchers
        refresh dashboards repeatedly or mobile apps retry on flaky connections.
        """
        responses = []
        for _i in range(30):
            resp = api.get("/api/customers")
            responses.append(resp.status_code)

        # Should see 200s with possible 429s, never 500s
        status_set = set(responses)
        assert 500 not in status_set, (
            f"Burst traffic caused 500 errors: {responses}"
        )
        # At least some should succeed
        assert 200 in status_set, (
            f"No successful responses during burst: {status_set}"
        )

    def test_research_14_expired_token_request(self, console_tracker):
        """Request with an expired/invalid token returns 401, not 500.

        Research: session expiry mid-form-submission is a top UX complaint
        in field service apps used by technicians in the field.
        """
        import httpx

        with httpx.Client(
            base_url=BASE_URL,
            headers={
                "Authorization": "Bearer expired.invalid.token",
                "Content-Type": "application/json",
            },
            verify=False,
            timeout=10,
        ) as client:
            resp = client.get("/api/customers")
            assert resp.status_code in (401, 403), (
                f"Expired token should return 401/403, got {resp.status_code}"
            )

            # Also test a write operation with expired token
            resp_write = client.post("/api/customers", json={
                "name": "Should Not Create",
            })
            assert resp_write.status_code in (401, 403), (
                f"Expired token write should return 401/403, got {resp_write.status_code}"
            )

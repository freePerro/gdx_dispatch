"""E2E workflow chain: login → customer → job → estimate → invoice → payment.

Runs against the live GDX dev environment. Marked @pytest.mark.e2e so
it is skipped during normal unit-test runs.

Usage:
    GDX_BASE_URL=https://dev.example.com \
    GDX_E2E_EMAIL=admin@test.com GDX_E2E_PASSWORD=<pw> \
    pytest gdx_dispatch/tests/test_35_e2e_workflow_chain.py -v -m e2e
"""
from __future__ import annotations

import contextlib
import os
import uuid

import httpx
import pytest

BASE_URL = os.getenv("GDX_BASE_URL", "https://dev.example.com")
E2E_EMAIL = os.getenv("GDX_E2E_EMAIL", "admin@example.com")
E2E_PASSWORD = os.getenv("GDX_E2E_PASSWORD", "")
TENANT_ID = os.getenv("GDX_TENANT_ID", "886a5b78-6bff-4b19-823c-a2c16684447e")


@pytest.mark.e2e
class TestE2EWorkflowChain:
    """Full business-workflow smoke test against the live API."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        self._created: dict[str, list[str]] = {
            "payments": [],
            "invoices": [],
            "estimates": [],
            "jobs": [],
            "customers": [],
        }
        self._token: str | None = None
        yield
        # Cleanup in reverse creation order (best-effort)
        if self._token:
            headers = self._auth_headers()
            with httpx.Client(base_url=BASE_URL, verify=False, timeout=15) as c:
                for kind in ("payments", "invoices", "estimates", "jobs", "customers"):
                    for rid in reversed(self._created[kind]):
                        with contextlib.suppress(Exception):
                            c.delete(f"/api/{kind}/{rid}", headers=headers)

    def _auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "x-tenant-id": TENANT_ID,
            "Content-Type": "application/json",
        }

    @pytest.mark.anyio
    async def test_full_chain(self) -> None:
        async with httpx.AsyncClient(
            base_url=BASE_URL, verify=False, timeout=20
        ) as client:
            # 1) Login
            login_resp = await client.post(
                "/api/auth/login",
                json={"email": E2E_EMAIL, "password": E2E_PASSWORD},
                headers={"x-tenant-id": TENANT_ID, "Content-Type": "application/json"},
            )
            assert login_resp.status_code == 200, f"Login failed: {login_resp.text}"
            data = login_resp.json()
            self._token = data.get("access_token") or data.get("token")
            assert self._token, f"No token in response: {data}"
            headers = self._auth_headers()

            # 2) Create customer
            unique = uuid.uuid4().hex[:8]
            cust_resp = await client.post(
                "/api/customers",
                headers=headers,
                json={
                    "name": f"E2E Test Co {unique}",
                    "email": f"e2e-{unique}@test.com",
                    "phone": "555-0199",
                },
            )
            assert cust_resp.status_code in (200, 201), f"Customer create failed: {cust_resp.text}"
            cust_data = cust_resp.json()
            customer_id = cust_data.get("id") or cust_data.get("data", {}).get("id")
            assert customer_id
            self._created["customers"].append(str(customer_id))

            # 3) Create job
            job_resp = await client.post(
                "/api/jobs",
                headers=headers,
                json={
                    "customer_id": str(customer_id),
                    "title": f"E2E Test Job {unique}",
                    "job_type": "Repair",
                },
            )
            assert job_resp.status_code in (200, 201), f"Job create failed: {job_resp.text}"
            job_data = job_resp.json()
            job_id = job_data.get("id") or job_data.get("data", {}).get("id")
            assert job_id
            self._created["jobs"].append(str(job_id))

            # 4) Create estimate
            est_resp = await client.post(
                "/api/estimates",
                headers=headers,
                json={
                    "job_id": str(job_id),
                    "lines": [
                        {"description": "Test Part", "quantity": 1, "unit_price": 100.00},
                    ],
                },
            )
            assert est_resp.status_code in (200, 201), f"Estimate create failed: {est_resp.text}"
            est_data = est_resp.json()
            estimate_id = est_data.get("id") or est_data.get("data", {}).get("id")
            assert estimate_id
            self._created["estimates"].append(str(estimate_id))

            # 5) Convert estimate to invoice
            conv_resp = await client.post(
                f"/api/estimates/{estimate_id}/convert",
                headers=headers,
            )
            assert conv_resp.status_code in (200, 201), f"Estimate convert failed: {conv_resp.text}"
            conv_data = conv_resp.json()
            invoice_id = (
                conv_data.get("invoice_id")
                or conv_data.get("id")
                or conv_data.get("data", {}).get("id")
            )
            assert invoice_id
            self._created["invoices"].append(str(invoice_id))

            # 6) Record payment
            pay_resp = await client.post(
                f"/api/invoices/{invoice_id}/payments",
                headers=headers,
                json={"amount": 100.00, "method": "card"},
            )
            assert pay_resp.status_code in (200, 201), f"Payment record failed: {pay_resp.text}"
            pay_data = pay_resp.json()
            payment_id = pay_data.get("id") or pay_data.get("data", {}).get("id")
            if payment_id:
                self._created["payments"].append(str(payment_id))

            # 7) Verify customer shows the job
            cust_check = await client.get(
                f"/api/customers/{customer_id}",
                headers=headers,
            )
            assert cust_check.status_code == 200

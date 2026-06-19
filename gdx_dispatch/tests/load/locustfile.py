"""Load test for GDX API -- simulates concurrent field service users.

Run headless:
    locust -f gdx_dispatch/tests/load/locustfile.py --headless -u 50 -r 5 -t 60s

Run with web UI:
    locust -f gdx_dispatch/tests/load/locustfile.py
    Then open http://localhost:8089
"""
from locust import HttpUser, between, task


class TechnicianUser(HttpUser):
    wait_time = between(1, 3)
    host = "https://gdx.example.com"

    def on_start(self):
        # Login
        resp = self.client.post("/auth/login", json={
            "email": "e2e_admin@example.com",
            "password": "E2E_Test_2026!",
        }, headers={"x-tenant-id": "886a5b78-6bff-4b19-823c-a2c16684447e"})
        if resp.status_code == 200:
            self.token = resp.json().get("access_token", "")
        else:
            self.token = ""

    @property
    def auth_headers(self):
        return {
            "Authorization": f"Bearer {self.token}",
            "x-tenant-id": "886a5b78-6bff-4b19-823c-a2c16684447e",
            "x-e2e-test": "true",
        }

    @task(5)
    def view_jobs(self):
        self.client.get("/api/jobs", headers=self.auth_headers)

    @task(3)
    def view_customers(self):
        self.client.get("/api/customers", headers=self.auth_headers)

    @task(2)
    def view_dashboard(self):
        self.client.get("/api/reports/summary", headers=self.auth_headers)

    @task(1)
    def view_invoices(self):
        self.client.get("/api/invoices", headers=self.auth_headers)

    @task(1)
    def check_timeclock(self):
        self.client.get("/api/timeclock/status", headers=self.auth_headers)

"""Tests for qa_tier1.py exit code logic.

Every scenario here comes from a real incident. If you add a test,
cite the date and what actually happened.
"""
import sys

sys.path.insert(0, "gdx_dispatch/tools")
from qa_tier1 import analyze


def _r(status, endpoint="/api/test"):
    return {"endpoint": endpoint, "status": status}


class TestRealIncidents:
    """Scenarios from actual production failures."""

    def test_2026_04_08_missing_module_grants(self):
        """3 endpoints returned 403 because company_module_grants was missing
        vendors, reviews, purchase-orders. Same root cause (missing grants),
        same status code. This WAS systemic — one INSERT fixed all three."""
        results = [
            _r(200, "/api/customers"), _r(200, "/api/estimates"),
            _r(200, "/api/jobs"), _r(200, "/api/leads"),
            _r(200, "/api/payments"), _r(200, "/api/technicians"),
            _r(403, "/api/vendors"), _r(403, "/api/purchase-orders"),
            _r(403, "/api/reviews"),
        ]
        # 3 failures, all 403 — dominant code is 403 (3/3 = 100%)
        # But only 3 total failures — is that enough to call systemic?
        # Yes: 3/3 same code, > 2 failures = systemic
        assert analyze(results) == 2

    def test_2026_04_08_holding_area_id_crash(self):
        """Only /api/jobs returned 500 due to missing holding_area_id column.
        Everything else worked. This was an independent bug, not systemic."""
        results = [
            _r(200, "/api/customers"), _r(200, "/api/estimates"),
            _r(500, "/api/jobs"), _r(200, "/api/leads"),
            _r(200, "/api/payments"), _r(200, "/api/technicians"),
            _r(200, "/api/vendors"), _r(200, "/api/reviews"),
        ]
        assert analyze(results) == 1

    def test_2026_04_08_mixed_independent(self):
        """One 500 (holding_area_id) and one 403 (missing grant) at the same
        time. Different status codes = independent bugs, not systemic."""
        results = [
            _r(200, "/api/customers"), _r(200, "/api/estimates"),
            _r(500, "/api/jobs"), _r(200, "/api/leads"),
            _r(403, "/api/vendors"), _r(200, "/api/technicians"),
        ]
        assert analyze(results) == 1

    def test_2026_04_08_app_restarting(self):
        """During container restart, all endpoints return connection errors.
        This is not a code bug — the app is just down."""
        results = [_r(0, ep) for ep in [
            "/api/customers", "/api/estimates", "/api/jobs",
            "/api/leads", "/api/payments", "/api/technicians",
        ]]
        assert analyze(results) == 3

    def test_2026_04_08_healthy_after_fixes(self):
        """After fixing holding_area_id, module grants, and tenant alias —
        all endpoints returned 200."""
        results = [
            _r(200, "/api/customers"), _r(200, "/api/estimates"),
            _r(200, "/api/jobs"), _r(200, "/api/leads"),
            _r(200, "/api/payments"), _r(200, "/api/technicians"),
            _r(200, "/api/vendors"), _r(200, "/api/reviews"),
            _r(200, "/api/purchase-orders"),
        ]
        assert analyze(results) == 0


class TestEdgeCases:
    """Edge cases derived from real scenarios above."""

    def test_single_failure_is_never_systemic(self):
        """One broken endpoint is always independent, even if it's a 500."""
        results = [_r(200) for _ in range(9)] + [_r(500)]
        assert analyze(results) == 1

    def test_two_same_code_not_systemic(self):
        """Two 403s could be coincidence. Need > 2 to call systemic."""
        results = [_r(200) for _ in range(8)] + [_r(403), _r(403)]
        assert analyze(results) == 1

    def test_all_same_code_is_systemic(self):
        """Every endpoint failing with 401 = auth is broken."""
        results = [_r(401) for _ in range(10)]
        assert analyze(results) == 2

    def test_half_unreachable_half_ok_not_down(self):
        """Some endpoints timeout but others work — app is degraded, not down."""
        results = [_r(200) for _ in range(6)] + [_r(0) for _ in range(4)]
        assert analyze(results) != 3

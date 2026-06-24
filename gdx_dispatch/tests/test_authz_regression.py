"""Authorization regression tests (2026-06-24 authz sweep).

Locks the fixes from the authorization audit so the holes can't silently
reopen:
  1. Module routers that gated only on require_module() were reachable with NO
     token (require_module authenticates nobody). They must now require auth.
  2. Pricing-policy / labor / payroll / tenant-wide-photo writes must reject a
     low-privilege (technician) caller.
  3. Ownership: a technician may only touch their own jobs (assert_job_access);
     dispatch/admin may touch any.
  4. is_dispatch_manager is the shared predicate for "may act on others".
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from gdx_dispatch.app import create_app
from gdx_dispatch.routers.auth.core import _issue

_TENANT = "00000000-0000-0000-0000-000000000001"
_TENANT_HDR = {"X-Tenant-ID": _TENANT}


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(create_app())


@pytest.fixture(scope="module")
def tech_hdr() -> dict[str, str]:
    """Headers for an authenticated *technician* (lowest privileged staff)."""
    tok, _ = _issue("11111111-1111-1111-1111-111111111111", _TENANT, "technician", 300, "access")
    return {"Authorization": f"Bearer {tok}", "X-Tenant-ID": _TENANT}


# 1. Previously-anonymous routers must reject unauthenticated requests.
#    (require_module() is NOT authentication.)
@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/equipment"),
        ("POST", "/api/equipment"),
        ("GET", "/api/workflows"),
        ("POST", "/api/workflows"),
        ("GET", "/api/fleet/vehicles"),
        ("POST", "/api/fleet/vehicles"),
        ("GET", "/api/purchase-orders"),
        ("POST", "/api/purchase-orders"),
        ("POST", "/api/dispatch/location"),
        ("GET", "/api/dispatch/locations"),
    ],
)
def test_anonymous_mutation_routes_require_auth(client: TestClient, method: str, path: str) -> None:
    resp = client.request(method, path, json={}, headers=_TENANT_HDR)
    assert resp.status_code in (401, 403), (
        f"{method} {path} returned {resp.status_code} with NO token — "
        "require_module() is not authentication; an auth dependency must gate this."
    )


# 2. Privileged writes must reject an authenticated technician (401/403 = denied;
#    must never be 2xx for a low-priv caller).
@pytest.mark.parametrize(
    "method,path",
    [
        ("PATCH", "/api/pricing/settings"),
        ("POST", "/api/pricing/vendor-lists"),
        ("PATCH", "/api/pricing/seasonal"),
        ("PATCH", "/api/time-entries/00000000-0000-0000-0000-0000000000aa"),
        ("DELETE", "/api/time-entries/00000000-0000-0000-0000-0000000000aa"),
        ("GET", "/api/reports/labor-summary"),
        ("GET", "/api/timeclock/payroll?start=2026-01-01&end=2026-01-02"),
        ("GET", "/api/photos/recent"),
    ],
)
def test_privileged_routes_deny_technician(client: TestClient, tech_hdr: dict, method: str, path: str) -> None:
    resp = client.request(method, path, json={}, headers=tech_hdr)
    assert resp.status_code in (401, 403), (
        f"{method} {path} returned {resp.status_code} for a technician — must be denied (401/403)."
    )


# 3. is_dispatch_manager — the shared "may act on others" predicate.
@pytest.mark.parametrize(
    "role,expected",
    [
        ("owner", True), ("admin", True),
        # BOTH spellings of dispatcher must be privileged — users.role stores the
        # legacy "dispatch"; the RBAC catalog uses "dispatcher".
        ("dispatch", True), ("dispatcher", True),
        ("manager", True), ("superadmin", True), ("super_admin", True),
        # Technicians are NOT dispatch managers (both legacy + canonical spelling).
        ("tech", False), ("technician", False),
        ("viewer", False), ("user", False), ("", False),
    ],
)
def test_is_dispatch_manager(role: str, expected: bool) -> None:
    from gdx_dispatch.core.permissions import is_dispatch_manager

    assert is_dispatch_manager({"role": role}) is expected


# 4. assert_job_access — dispatch bypass + technician denial (no DB needed).
class _FakeResult:
    def scalar(self):  # noqa: ANN201
        return None


class _FakeDB:
    """Stands in for a Session whose every ownership query finds nothing."""
    def execute(self, *_a, **_k):  # noqa: ANN002, ANN003, ANN201
        return _FakeResult()


def test_assert_job_access_dispatch_bypass() -> None:
    from gdx_dispatch.core.job_access import assert_job_access

    assert_job_access(None, "t1", {"role": "admin"}, "job-1")  # no DB touched, no raise
    assert_job_access(None, "t1", {"role": "dispatcher"}, "job-1")


def test_assert_job_access_denies_unowned_technician() -> None:
    from gdx_dispatch.core.job_access import assert_job_access, job_belongs_to_user

    db = _FakeDB()
    assert job_belongs_to_user(db, "t1", "job-x", "tech-user-1") is False
    with pytest.raises(HTTPException) as ei:
        assert_job_access(db, "t1", {"role": "technician", "user_id": "tech-user-1"}, "job-x")
    assert ei.value.status_code == 404


def _seeded_session():
    """In-memory DB with one technician (user-owner) assigned to one job."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from gdx_dispatch.core.audit import TenantBase
    from gdx_dispatch.models import tenant_models  # noqa: F401  (register models)

    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    TenantBase.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    db.execute(text("INSERT INTO technicians (id, company_id, user_id) VALUES ('tech-1','t1','user-owner')"))
    db.execute(
        text(
            "INSERT INTO jobs (id, title, dispatch_status, company_id, assigned_to) "
            "VALUES ('job-1','J','scheduled','t1','tech-1')"
        )
    )
    db.commit()
    return db


def test_financial_module_endpoints_require_dispatch() -> None:
    """change-order create/approve/reject and proposal-accept are financial
    actions — they must be gated by a dispatch dependency, not bare auth.
    (These modules aren't always mounted in create_app(), so introspect the
    routers directly.)"""
    from gdx_dispatch.modules.change_orders import router as co
    from gdx_dispatch.modules.proposals import router as pr

    def _dep_names(router_obj, path_frag, method):
        for rt in router_obj.routes:
            if path_frag in getattr(rt, "path", "") and method in (rt.methods or set()):
                return [getattr(d.call, "__name__", "") for d in rt.dependant.dependencies]
        raise AssertionError(f"route not found: {method} {path_frag}")

    assert "_require_dispatch" in _dep_names(co.router, "/change-orders/{co_id}/approve", "POST")
    assert "_require_dispatch" in _dep_names(co.router, "/change-orders/{co_id}/reject", "POST")
    assert "_require_dispatch" in _dep_names(co.router, "/jobs/{job_id}/change-orders", "POST")
    assert "_require_dispatch" in _dep_names(pr.router, "/proposal/accept", "POST")


def test_job_owner_via_assigned_technician_is_allowed() -> None:
    """Regression: jobs.assigned_to holds a technician.id — the owner (mapped via
    technicians.user_id) MUST be allowed, and a different tech denied. This is the
    over-block case that locked techs out of their own jobs."""
    from gdx_dispatch.core.job_access import assert_job_access, job_belongs_to_user

    db = _seeded_session()
    try:
        assert job_belongs_to_user(db, "t1", "job-1", "user-owner") is True
        assert job_belongs_to_user(db, "t1", "job-1", "user-other") is False
        # owner passes the gate; a different technician is 404'd
        assert_job_access(db, "t1", {"role": "tech", "user_id": "user-owner"}, "job-1")
        with pytest.raises(HTTPException) as ei:
            assert_job_access(db, "t1", {"role": "tech", "user_id": "user-other"}, "job-1")
        assert ei.value.status_code == 404
    finally:
        db.close()

"""
Tests for gdx_dispatch.core.onboarding wizard — test_24_onboarding.py

Covers:
  - get_onboarding_status
  - complete_step
  - get_next_step
  - is_onboarding_complete
  - HTTP page renders (GET /onboarding/{step})
  - Auth required (redirects unauthenticated users)
  - Step validation (POST with missing required fields)
  - Resume partial onboarding (next_step skips completed steps)
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gdx_dispatch.core.onboarding import (
    WIZARD_STEPS,
    OnboardingStep,
    OnboardingStepState,
    complete_step,
    get_next_step,
    get_onboarding_status,
    is_onboarding_complete,
    reset_onboarding,
    ui_router,
)

# ── Fixtures ───────────────────────────────────────────────────────────────────

TENANT_ID = "test-tenant-wizard-99"


@pytest.fixture(autouse=True)
def _clean_state():
    """Reset in-memory onboarding state before and after every test."""
    reset_onboarding(TENANT_ID)
    yield
    reset_onboarding(TENANT_ID)


def _make_app(authenticated: bool = True) -> FastAPI:
    """Build a minimal FastAPI app that mounts the onboarding ui_router."""
    app = FastAPI()

    @app.middleware("http")
    async def _mock_middleware(request, call_next):
        request.state.tenant = {"id": TENANT_ID, "slug": "testco"}
        if authenticated:
            request.state.current_user = {"email": "owner@testco.com", "role": "owner"}
        else:
            request.state.current_user = None
        request.state.flash_messages = []
        return await call_next(request)

    app.include_router(ui_router)
    return app


@pytest.fixture
def client():
    with TestClient(_make_app(authenticated=True), follow_redirects=False) as c:
        yield c


@pytest.fixture
def anon_client():
    with TestClient(_make_app(authenticated=False), follow_redirects=False) as c:
        yield c


# ── 1. get_onboarding_status ──────────────────────────────────────────────────

def test_get_onboarding_status_returns_all_steps():
    steps = get_onboarding_status(TENANT_ID)
    assert len(steps) == len(WIZARD_STEPS)
    assert all(isinstance(s, OnboardingStepState) for s in steps)


def test_get_onboarding_status_all_incomplete_initially():
    steps = get_onboarding_status(TENANT_ID)
    assert all(not s.is_complete for s in steps)


def test_get_onboarding_status_step_names_match_wizard():
    steps = get_onboarding_status(TENANT_ID)
    names = [s.step_name for s in steps]
    assert names == WIZARD_STEPS


# ── 2. complete_step ──────────────────────────────────────────────────────────

def test_complete_step_marks_step_done():
    result = complete_step(TENANT_ID, "company_info")
    assert result.is_complete is True
    assert result.completed_at is not None
    assert result.step_name == "company_info"


def test_complete_step_persists_across_calls():
    complete_step(TENANT_ID, "company_info")
    steps = get_onboarding_status(TENANT_ID)
    company = next(s for s in steps if s.step_name == "company_info")
    assert company.is_complete is True


def test_complete_step_raises_on_unknown_step():
    with pytest.raises(ValueError, match="Unknown onboarding step"):
        complete_step(TENANT_ID, "nonexistent_step")


def test_complete_step_returns_onboarding_step_state():
    result = complete_step(TENANT_ID, "branding")
    assert isinstance(result, OnboardingStepState)
    assert result.position == WIZARD_STEPS.index("branding")


# ── 3. get_next_step ──────────────────────────────────────────────────────────

def test_get_next_step_returns_first_step_initially():
    assert get_next_step(TENANT_ID) == WIZARD_STEPS[0]


def test_get_next_step_advances_past_completed():
    complete_step(TENANT_ID, "company_info")
    assert get_next_step(TENANT_ID) == "first_technician"


def test_get_next_step_returns_none_when_all_done():
    for step in WIZARD_STEPS:
        complete_step(TENANT_ID, step)
    assert get_next_step(TENANT_ID) is None


# ── 4. is_onboarding_complete ─────────────────────────────────────────────────

def test_onboarding_complete_false_initially():
    assert is_onboarding_complete(TENANT_ID) is False


def test_onboarding_complete_false_partial():
    complete_step(TENANT_ID, "company_info")
    complete_step(TENANT_ID, "first_technician")
    assert is_onboarding_complete(TENANT_ID) is False


def test_onboarding_complete_true_when_all_done():
    for step in WIZARD_STEPS:
        complete_step(TENANT_ID, step)
    assert is_onboarding_complete(TENANT_ID) is True


# ── 5. HTML page renders ──────────────────────────────────────────────────────

def test_onboarding_page_renders_company_info(client):
    resp = client.get("/onboarding/company_info")
    assert resp.status_code == 200
    assert b"Company Information" in resp.content


def test_onboarding_page_renders_all_steps(client):
    for step in WIZARD_STEPS:
        resp = client.get(f"/onboarding/{step}")
        assert resp.status_code == 200, f"Step {step!r} returned {resp.status_code}"


def test_onboarding_page_404_on_unknown_step(client):
    resp = client.get("/onboarding/totally_fake_step")
    assert resp.status_code == 404


def test_onboarding_index_redirects_to_first_step(client):
    resp = client.get("/onboarding")
    assert resp.status_code == 302
    assert resp.headers["location"] == f"/onboarding/{WIZARD_STEPS[0]}"


def test_onboarding_index_redirects_to_dashboard_when_complete(client):
    for step in WIZARD_STEPS:
        complete_step(TENANT_ID, step)
    resp = client.get("/onboarding")
    assert resp.status_code == 302
    assert "/dashboard" in resp.headers["location"]


# ── 6. Auth required ──────────────────────────────────────────────────────────

def test_onboarding_requires_auth_get(anon_client):
    resp = anon_client.get("/onboarding/company_info")
    assert resp.status_code == 302
    assert "/auth/login" in resp.headers["location"]


def test_onboarding_requires_auth_post(anon_client):
    resp = anon_client.post("/onboarding/company_info", data={"company_name": "Acme"})
    assert resp.status_code == 302
    assert "/auth/login" in resp.headers["location"]


def test_onboarding_index_requires_auth(anon_client):
    resp = anon_client.get("/onboarding")
    assert resp.status_code == 302
    assert "/auth/login" in resp.headers["location"]


# ── 7. Step validation ────────────────────────────────────────────────────────

def test_company_info_validation_rejects_empty_name(client):
    resp = client.post("/onboarding/company_info", data={"company_name": ""})
    assert resp.status_code == 422
    assert b"Company name is required" in resp.content


def test_company_info_validation_accepts_valid_name(client):
    resp = client.post("/onboarding/company_info", data={"company_name": "Acme LLC"})
    # Should redirect (303) to next step, not re-render with errors
    assert resp.status_code == 303


def test_first_technician_validation_requires_name_and_email(client):
    resp = client.post("/onboarding/first_technician", data={})
    assert resp.status_code == 422
    assert b"Technician name is required" in resp.content
    assert b"Technician email is required" in resp.content


def test_service_area_validation_requires_zip_or_radius(client):
    resp = client.post("/onboarding/service_area", data={})
    assert resp.status_code == 422
    assert b"ZIP code" in resp.content or b"radius" in resp.content


def test_service_area_accepts_zip_codes(client):
    resp = client.post("/onboarding/service_area", data={"zip_codes": "90210"})
    assert resp.status_code == 303


def test_job_type_validation_requires_selection(client):
    resp = client.post("/onboarding/first_job_type", data={})
    assert resp.status_code == 422
    assert b"job type" in resp.content.lower()


def test_payment_setup_and_branding_have_no_required_fields(client):
    """payment_setup and branding steps have no required fields — always advance."""
    for step in ("payment_setup", "branding"):
        reset_onboarding(TENANT_ID)
        resp = client.post(f"/onboarding/{step}", data={})
        assert resp.status_code == 303, f"Step {step!r} should redirect, got {resp.status_code}"


# ── 8. Resume partial onboarding ─────────────────────────────────────────────

def test_resume_skips_completed_steps(client):
    """After completing the first two steps, /onboarding redirects to step 3."""
    complete_step(TENANT_ID, "company_info")
    complete_step(TENANT_ID, "first_technician")
    resp = client.get("/onboarding")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/onboarding/service_area"


def test_resume_middle_of_wizard(client):
    """Completing steps 1, 2, 3 leaves step 4 as next."""
    for step in ("company_info", "first_technician", "service_area"):
        complete_step(TENANT_ID, step)
    assert get_next_step(TENANT_ID) == "first_job_type"


def test_post_advances_to_next_incomplete_step(client):
    """POSTing valid company_info should redirect to first_technician."""
    resp = client.post("/onboarding/company_info", data={"company_name": "Acme"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/onboarding/first_technician"


def test_post_last_step_redirects_to_dashboard(client):
    """Completing all steps except the last then POSTing it sends to /dashboard."""
    for step in WIZARD_STEPS[:-1]:
        complete_step(TENANT_ID, step)
    resp = client.post("/onboarding/branding", data={})
    assert resp.status_code == 303
    assert "/dashboard" in resp.headers["location"]


# ── Legacy OnboardingStep dataclass ──────────────────────────────────────────

def test_legacy_onboarding_step_dataclass():
    step = OnboardingStep("test_key", "Test Title", complete=True)
    assert step.key == "test_key"
    assert step.title == "Test Title"
    assert step.complete is True
    assert step.action_url is None

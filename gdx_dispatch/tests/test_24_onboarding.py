"""
Tests for gdx_dispatch.core.onboarding state — test_24_onboarding.py

Covers the live half of the module — the four functions that the mounted
``GET /api/onboarding`` endpoint is built on:
  - get_onboarding_status
  - complete_step
  - get_next_step
  - is_onboarding_complete

This file used to also hold 19 tests that took a ``client`` / ``anon_client``
fixture and drove the Flask-era Jinja wizard (``ui_router``, its HTML
``GET /onboarding``, ``GET /onboarding/{step}`` and ``POST /onboarding/{step}``
routes, and the form validation behind them). Nothing ever mounted that router,
so those tests stood up a private FastAPI app to exercise code no request could
reach — the Vue SPA owns ``/onboarding``. The wizard was deleted 2026-09-24
(GDXA-24) and 18 of those tests went with it.

The 19th, ``test_resume_middle_of_wizard``, declared the ``client`` fixture and
never used it: its body asserts on ``complete_step`` / ``get_next_step`` only.
It was a state test wearing a wizard test's clothes, so it is kept below
without the fixture — it is the only assertion in this file that walks three
steps deep.

What replaced the deleted HTTP coverage is not more HTTP coverage — there is no
longer a surface to cover. It is an absence-and-presence guard:
``gdx_dispatch/tests/test_onboarding_jinja_wizard_retired.py`` holds that the
wizard stays gone *and* that ``/api/onboarding`` stays mounted.
"""
from __future__ import annotations

import pytest

from gdx_dispatch.core.onboarding import (
    WIZARD_STEPS,
    OnboardingStep,
    OnboardingStepState,
    complete_step,
    get_next_step,
    get_onboarding_status,
    is_onboarding_complete,
    reset_onboarding,
)

# ── Fixtures ───────────────────────────────────────────────────────────────────

TENANT_ID = "test-tenant-wizard-99"


@pytest.fixture(autouse=True)
def _clean_state():
    """Reset in-memory onboarding state before and after every test."""
    reset_onboarding(TENANT_ID)
    yield
    reset_onboarding(TENANT_ID)


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


# ── 5. Resume partial onboarding ─────────────────────────────────────────────

def test_resume_middle_of_wizard():
    """Completing steps 1, 2, 3 leaves step 4 as next."""
    for step in ("company_info", "first_technician", "service_area"):
        complete_step(TENANT_ID, step)
    assert get_next_step(TENANT_ID) == "first_job_type"


# ── Legacy OnboardingStep dataclass ──────────────────────────────────────────

def test_legacy_onboarding_step_dataclass():
    step = OnboardingStep("test_key", "Test Title", complete=True)
    assert step.key == "test_key"
    assert step.title == "Test Title"
    assert step.complete is True
    assert step.action_url is None

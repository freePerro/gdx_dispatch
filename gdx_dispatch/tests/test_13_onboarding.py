from unittest.mock import patch

from gdx_dispatch.core.onboarding import OnboardingStep, get_onboarding_status


def test_onboarding_returns_six_steps():
    with patch("gdx_dispatch.core.onboarding._load_state", return_value={}):
        steps = get_onboarding_status("test-tenant")
    assert len(steps) == 6


def test_onboarding_step_structure():
    step = OnboardingStep('test', 'Test Step', True)
    assert step.key == 'test'
    assert step.complete


def test_onboarding_graceful_on_missing_tables():
    with patch("gdx_dispatch.core.onboarding._load_state", return_value={}):
        steps = get_onboarding_status("test-tenant")  # should not raise
    assert len(steps) == 6
    assert all(not s.is_complete for s in steps)


def test_onboarding_percent_calculation():
    # Simulate 2 of 6 steps complete
    state = {
        "company_info": {"is_complete": True, "completed_at": "2026-01-01T00:00:00"},
        "first_technician": {"is_complete": True, "completed_at": "2026-01-01T00:00:00"},
    }
    with patch("gdx_dispatch.core.onboarding._load_state", return_value=state):
        steps = get_onboarding_status("test-tenant")
    complete = sum(1 for s in steps if s.is_complete)
    assert complete >= 2  # company_info + first_technician

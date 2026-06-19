
import pytest

from gdx_dispatch.main import app  # Assuming app is the FastAPI/Flask instance


@pytest.mark.xfail(strict=False, reason="reproduction — bug not yet fixed")
def test_onboard_endpoint_requires_auth():
    """
    The onboard endpoint should return 401 Unauthorized when no authentication 
    token is provided. Currently, it allows unauthenticated access.
    """
    client = app.test_client()

    # Attempt to access the Stripe Connect onboarding endpoint without credentials
    response = client.get("/onboard")

    # The expected behavior is a 401 Unauthorized status code
    assert response.status_code == 401, (
        f"Expected 401 Unauthorized, but got {response.status_code}. "
        "The onboarding endpoint must require authentication."
    )

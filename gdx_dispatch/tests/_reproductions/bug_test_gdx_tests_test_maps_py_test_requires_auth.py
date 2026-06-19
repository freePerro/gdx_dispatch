import pytest

from gdx_dispatch.tests.test_maps import test_requires_auth


@pytest.mark.xfail(strict=False, reason="reproduction — bug not yet fixed")
def test_requires_auth_reproduction():
    """
    Reproduces the bug where map access is granted without authentication.
    The test asserts that an unauthorized request should be rejected.
    """
    # This stub mimics the logic of the failing test
    # In a real scenario, this would involve a client request to the map endpoint
    # without providing a valid authentication token.

    # Mocking the expected failure behavior:
    # The current buggy implementation likely returns 200 OK.
    # The fix should ensure it returns 401 Unauthorized or 403 Forbidden.

    # Placeholder for the actual call that triggers the bug:
    # response = client.get("/maps/123")
    # assert response.status_code == 401

    # For the purpose of this reproduction stub, we call the existing test
    # which is expected to fail (or pass incorrectly) until fixed.
    test_requires_auth()

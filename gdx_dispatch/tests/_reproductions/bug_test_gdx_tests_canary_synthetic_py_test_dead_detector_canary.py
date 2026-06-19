import pytest
from gdx_dispatch.tests._canary_synthetic import test_dead_detector_canary


@pytest.mark.xfail(strict=False, reason="reproduction — bug not yet fixed")
def test_reproduce_dead_detector_bug():
    """
    Reproduction stub for gdx_dispatch/tests/_canary_synthetic.py::test_dead_detector_canary
    This test asserts the expected correct behavior once the bug is resolved.
    """
    # Call the failing logic/endpoint
    result = test_dead_detector_canary()

    # Assert the behavior that SHOULD happen (e.g., detector returns expected status)
    assert result is True, f"Expected detector to return True, but got {result}"

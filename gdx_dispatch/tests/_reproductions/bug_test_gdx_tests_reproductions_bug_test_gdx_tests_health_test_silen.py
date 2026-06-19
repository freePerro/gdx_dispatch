import pytest


@pytest.mark.xfail(strict=False, reason="reproduction — bug not yet fixed")
def test_vps_in_sync_with_origin():
    """
    Reproduction stub for the silent failure in test_vps_in_sync_with_origin.
    The test should fail if the VPS is out of sync, but currently passes silently.
    """
    # This call is expected to trigger the logic that currently fails silently
    # We assert that the underlying condition (VPS in sync) is met.
    # In a real reproduction, we would mock the out-of-sync state to force a failure.
    result = test_vps_in_sync_with_origin()

    assert result is True, "The VPS should be in sync with the origin."

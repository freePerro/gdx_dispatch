import pytest


@pytest.mark.xfail(strict=False, reason="reproduction — bug not yet fixed")
def test_database_schema_conformance():
    """
    Reproduces the silent failure where database schema mismatches 
    are not being caught by the silent failure detectors.
    """
    from gdx_dispatch.tests.health.test_silent_failure_detectors import check_database_schema_conformance

    # This call is expected to raise a specific SchemaMismatchError
    # or return a failure status when the schema is invalid.
    # Currently, it fails silently or returns True incorrectly.
    result = check_database_schema_conformance()

    assert result is True, "Database schema does not conform to the expected model"

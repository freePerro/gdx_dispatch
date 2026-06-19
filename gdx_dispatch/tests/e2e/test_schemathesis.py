"""Auto-generated API tests from OpenAPI spec using Schemathesis.

Generates thousands of test cases by fuzzing every endpoint defined in /openapi.json.
Catches edge cases that hand-written tests miss.

TD-027 (2026-04-11): schemathesis 3.x `schemathesis.from_uri(uri, base_url=...)`
was removed in 4.x. The replacement is `schemathesis.openapi.from_url(url)`
which reads the base URL from the spec's `servers` entry. Previously this
test blocked pytest collection on any fresh install of the pinned schemathesis
>=4.15.0 dependency.
"""
import pytest
import schemathesis

# Load schema from live server.
schema = schemathesis.openapi.from_url(
    "https://gdx.example.com/openapi.json",
)

@schema.parametrize()
@pytest.mark.e2e
def test_api_endpoint(case):
    """Every endpoint should not return 500."""
    case.headers = {
        "x-tenant-id": "886a5b78-6bff-4b19-823c-a2c16684447e",
        "x-e2e-test": "true",
    }
    response = case.call()
    # Any status except 500 is acceptable (401, 403, 404, 422 are valid responses)
    assert response.status_code != 500, f"{case.method} {case.path} returned 500"

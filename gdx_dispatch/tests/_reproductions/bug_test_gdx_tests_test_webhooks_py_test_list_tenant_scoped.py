import pytest


@pytest.mark.xfail(strict=False, reason="reproduction — bug not yet fixed")
def test_reproduce_list_tenant_scoped_bug(client, tenant):
    """
    Reproduces the failure in test_list_tenant_scoped.
    The endpoint should return only webhooks belonging to the specific tenant.
    """
    # Setup: Create a webhook for the target tenant and one for a different tenant
    client.post("/webhooks/", json={"tenant_id": tenant.id, "url": "https://a.com"})
    client.post("/webhooks/", json={"tenant_id": "other-tenant", "url": "https://b.com"})

    # Action: List webhooks for the current tenant
    response = client.get(f"/tenants/{tenant.id}/webhooks/")

    # Assert: The response should only contain the 1 webhook belonging to this tenant
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["tenant_id"] == tenant.id

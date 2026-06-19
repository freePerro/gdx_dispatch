"""Tests for SCIM translators (ss22-a)."""

from gdx_dispatch.core.scim_translate import (
    identity_to_scim_user,
    scim_group_to_membership_dicts,
    scim_user_to_identity_dict,
    unsupported_operation_error,
)


def test_scim_user_to_identity_active_true():
    scim = {"userName": "a@x", "emails": [{"value": "a@x"}], "active": True}
    result = scim_user_to_identity_dict(scim)
    assert result["status"] == "active"
    assert result["email"] == "a@x"
    assert result["provider_subject"] == "a@x"


def test_scim_user_to_identity_active_false_soft_deletes():
    scim = {"userName": "a@x", "emails": [{"value": "a@x"}], "active": False}
    result = scim_user_to_identity_dict(scim)
    assert result["status"] == "deleted"


def test_identity_to_scim_user_roundtrip():
    identity = {
        "id": "u1",
        "email": "a@x",
        "display_name": "Ada Lovelace",
        "status": "active",
    }
    result = identity_to_scim_user(identity, provider_subject="a@x")
    assert result["emails"][0]["value"] == "a@x"
    assert result["name"]["givenName"] == "Ada"
    assert result["name"]["familyName"] == "Lovelace"
    assert result["active"] is True
    assert "urn:ietf:params:scim:schemas:core:2.0:User" in result["schemas"]


def test_scim_group_to_memberships_produces_one_per_member():
    group = {"members": [{"value": "u1"}, {"value": "u2"}, {"value": "u3"}]}
    result = scim_group_to_membership_dicts(group, tenant_id="t1")
    assert len(result) == 3
    assert [m["identity_id"] for m in result] == ["u1", "u2", "u3"]
    assert all(m["tenant_id"] == "t1" for m in result)


def test_unsupported_operation_error_shape():
    result = unsupported_operation_error("PATCH not supported; use PUT")
    assert result == {
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:Error"],
        "status": "501",
        "detail": "PATCH not supported; use PUT",
    }

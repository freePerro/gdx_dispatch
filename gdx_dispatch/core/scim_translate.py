"""SCIM 2.0 ↔ platform Identity translators (ss22-a).

Pure stdlib functions. No sqlalchemy, fastapi, or gdx_dispatch.models imports.
Shape-only: mapping between SCIM User/Group dicts and platform Identity/
Membership dicts. Persistence lives elsewhere (SS-22 router).

Soft-delete semantics: SCIM `active=False` maps to `status="deleted"`.
Callers must NEVER hard-delete on deprovision.

References:
- RFC 7643 (SCIM Core Schema) User / Group
- RFC 7644 (SCIM Protocol) Error response
"""

_USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
_ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"


def scim_user_to_identity_dict(scim: dict) -> dict:
    """Map a SCIM 2.0 User payload to a platform Identity dict.

    `active=False` is a soft-delete signal → status="deleted".
    """
    user_name = scim.get("userName")
    emails = scim.get("emails") or []
    email = emails[0].get("value") if emails else None

    name = scim.get("name") or {}
    given = (name.get("givenName") or "").strip()
    family = (name.get("familyName") or "").strip()
    display_name = f"{given} {family}".strip() or user_name

    active = bool(scim.get("active", True))
    status = "active" if active else "deleted"

    external_id = scim.get("externalId")
    provider_subject = external_id if external_id else user_name

    out: dict = {
        "display_name": display_name,
        "status": status,
        "provider_subject": provider_subject,
    }
    if email is not None:
        out["email"] = email
    return out


def identity_to_scim_user(identity: dict, provider_subject: str) -> dict:
    """Render a platform Identity dict as an RFC 7643 SCIM 2.0 User."""
    display_name = identity.get("display_name") or ""
    parts = display_name.split(None, 1)
    if len(parts) == 0:
        given, family = "", ""
    elif len(parts) == 1:
        given, family = parts[0], ""
    else:
        given, family = parts[0], parts[1]

    return {
        "schemas": [_USER_SCHEMA],
        "id": identity["id"],
        "userName": provider_subject,
        "emails": [{"value": identity["email"], "primary": True}],
        "name": {"givenName": given, "familyName": family},
        "active": identity["status"] == "active",
        "meta": {"resourceType": "User"},
    }


def scim_group_to_membership_dicts(scim_group: dict, tenant_id: str) -> list:
    """Flatten SCIM Group.members into one membership dict per member."""
    members = scim_group.get("members") or []
    return [
        {
            "identity_id": m["value"],
            "tenant_id": tenant_id,
            "role": "member",
        }
        for m in members
    ]


def unsupported_operation_error(detail: str) -> dict:
    """Return an RFC 7644 SCIM Error response body with status 501."""
    return {
        "schemas": [_ERROR_SCHEMA],
        "status": "501",
        "detail": detail,
    }

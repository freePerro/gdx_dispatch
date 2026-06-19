"""SS-22 SCIM router tests.

Covers:
  * Bearer auth: missing / malformed / invalid token all 401 with the
    RFC 7644 SCIM error schema.
  * Capability gating: read-only token cannot POST/PUT/DELETE.
  * Users CRUD: POST → 201 + Location, GET one, GET list, PUT, DELETE
    (soft-delete; row retained with status=deleted + deleted_at set).
  * Pagination (startIndex + count) and filter (userName eq, email eq).
  * Re-POST of a soft-deleted user reactivates (idempotent reprovision).
  * PATCH → 501 with SCIM error body (User + Group, collection + item).
  * Groups CRUD end-to-end using Membership rows.
  * ServiceProviderConfig / ResourceTypes / Schemas shape.
"""
from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# Import platform mappers so metadata.create_all sees them.
import gdx_dispatch.models.platform  # noqa: F401
import gdx_dispatch.models.platform_extensions  # noqa: F401
from gdx_dispatch.control.models import Base as ControlBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.routers.auth.scim import register_scim_exception_handlers, router as scim_router

TENANT = "acme"
# D97: SCIM principals carry UUID-stringified tenant_id post-031. Tokens
# below are bound to deterministic UUIDs so the fixture can pre-seed
# tenants(id=...) rows that the router can resolve back to slugs.
TENANT_UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OTHER_TENANT_UUID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
WRITE_TOKEN = "tok_write_123"
READ_TOKEN = "tok_read_456"
OTHER_TENANT_TOKEN = "tok_other_789"

SCIM_TOKENS = {
    WRITE_TOKEN: {
        "tenant_id": TENANT_UUID,
        "capabilities": [
            "read:scim_config",
            "read:identity",
            "write:identity",
            "read:membership",
            "write:membership",
        ],
    },
    READ_TOKEN: {
        "tenant_id": TENANT_UUID,
        "capabilities": [
            "read:scim_config",
            "read:identity",
            "read:membership",
        ],
    },
    OTHER_TENANT_TOKEN: {
        "tenant_id": OTHER_TENANT_UUID,
        "capabilities": [
            "read:identity",
            "write:identity",
            "read:membership",
            "write:membership",
        ],
    },
}


# ─── fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    ControlBase.metadata.create_all(eng)

    # Seed a tenants row — Membership.tenant_id FKs tenants.slug.
    with eng.begin() as conn:
        from gdx_dispatch.control.models import Tenant  # lazy import, after Base

        # Some deployments name Tenant differently; fall back to direct SQL.
        conn.execute(
            Tenant.__table__.insert().values(
                id=UUID(TENANT_UUID),
                slug=TENANT,
                name="Acme Corp",
            )
        )
        conn.execute(
            Tenant.__table__.insert().values(
                id=UUID(OTHER_TENANT_UUID),
                slug="other-co",
                name="Other Co",
            )
        )

    yield eng
    eng.dispose()


@pytest.fixture
def SessionLocal(engine):
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


@pytest.fixture
def client(SessionLocal):
    app = FastAPI()
    app.state.scim_tokens = SCIM_TOKENS
    app.include_router(scim_router)
    register_scim_exception_handlers(app)

    def _get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _get_db
    with TestClient(app) as c:
        yield c


def _auth(token: str = WRITE_TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _user_body(user_name: str, **extra) -> dict:
    body = {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
        "userName": user_name,
        "name": {"givenName": "Ada", "familyName": "Lovelace"},
        "emails": [{"value": user_name, "primary": True}],
        "active": True,
    }
    body.update(extra)
    return body


# ─── auth tests ────────────────────────────────────────────────────────────


def test_missing_authorization_returns_scim_401(client):
    r = client.get("/scim/v2/Users")
    assert r.status_code == 401
    body = r.json()
    assert body["schemas"] == [
        "urn:ietf:params:scim:api:messages:2.0:Error"
    ]
    assert body["status"] == "401"


def test_malformed_authorization_returns_scim_401(client):
    r = client.get("/scim/v2/Users", headers={"Authorization": "NotBearer xyz"})
    assert r.status_code == 401
    assert r.json()["schemas"] == [
        "urn:ietf:params:scim:api:messages:2.0:Error"
    ]


def test_unknown_bearer_token_returns_scim_401(client):
    r = client.get(
        "/scim/v2/Users", headers={"Authorization": "Bearer nope"}
    )
    assert r.status_code == 401
    assert r.json()["status"] == "401"


def test_read_token_cannot_write(client):
    r = client.post(
        "/scim/v2/Users",
        json=_user_body("read@example.com"),
        headers=_auth(READ_TOKEN),
    )
    assert r.status_code == 403
    body = r.json()
    assert body["schemas"] == [
        "urn:ietf:params:scim:api:messages:2.0:Error"
    ]
    assert body["status"] == "403"


# ─── discovery endpoints ──────────────────────────────────────────────────


def test_service_provider_config_advertises_no_patch_no_bulk(client):
    r = client.get("/scim/v2/ServiceProviderConfig", headers=_auth(READ_TOKEN))
    assert r.status_code == 200
    body = r.json()
    assert body["patch"]["supported"] is False
    assert body["bulk"]["supported"] is False
    assert body["filter"]["supported"] is True
    assert any(
        s.endswith("ServiceProviderConfig")
        for s in body["schemas"]
    )


def test_resource_types_lists_user_and_group(client):
    r = client.get("/scim/v2/ResourceTypes", headers=_auth(READ_TOKEN))
    assert r.status_code == 200
    body = r.json()
    ids = {res["id"] for res in body["Resources"]}
    assert ids == {"User", "Group"}


def test_schemas_endpoint_returns_user_and_group_schemas(client):
    r = client.get("/scim/v2/Schemas", headers=_auth(READ_TOKEN))
    assert r.status_code == 200
    body = r.json()
    schema_ids = {res["id"] for res in body["Resources"]}
    assert "urn:ietf:params:scim:schemas:core:2.0:User" in schema_ids
    assert "urn:ietf:params:scim:schemas:core:2.0:Group" in schema_ids


# ─── PATCH → 501 ──────────────────────────────────────────────────────────


def test_patch_users_collection_returns_501(client):
    r = client.patch("/scim/v2/Users", json={}, headers=_auth())
    assert r.status_code == 501
    body = r.json()
    assert body["schemas"] == [
        "urn:ietf:params:scim:api:messages:2.0:Error"
    ]
    assert body["status"] == "501"
    assert "PATCH" in body["detail"]


def test_patch_user_item_returns_501(client):
    r = client.patch(
        "/scim/v2/Users/00000000-0000-0000-0000-000000000000",
        json={},
        headers=_auth(),
    )
    assert r.status_code == 501


def test_patch_groups_returns_501(client):
    r = client.patch("/scim/v2/Groups/any", json={}, headers=_auth())
    assert r.status_code == 501


# ─── Users CRUD ───────────────────────────────────────────────────────────


def test_create_user_returns_201_with_location(client):
    r = client.post(
        "/scim/v2/Users",
        json=_user_body("ada@example.com"),
        headers=_auth(),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["userName"] == "ada@example.com"
    assert body["name"]["givenName"] == "Ada"
    assert body["active"] is True
    assert "Location" in r.headers
    assert body["id"] in r.headers["Location"]


def test_get_user_roundtrip(client):
    created = client.post(
        "/scim/v2/Users",
        json=_user_body("bob@example.com"),
        headers=_auth(),
    ).json()
    r = client.get(f"/scim/v2/Users/{created['id']}", headers=_auth())
    assert r.status_code == 200
    assert r.json()["userName"] == "bob@example.com"


def test_get_user_unknown_returns_404(client):
    r = client.get(
        f"/scim/v2/Users/{uuid4()}",
        headers=_auth(),
    )
    assert r.status_code == 404
    assert r.json()["schemas"] == [
        "urn:ietf:params:scim:api:messages:2.0:Error"
    ]


def test_list_users_pagination_startindex_and_count(client):
    for i in range(5):
        client.post(
            "/scim/v2/Users",
            json=_user_body(f"u{i}@example.com"),
            headers=_auth(),
        )
    r = client.get("/scim/v2/Users?startIndex=1&count=2", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["totalResults"] == 5
    assert body["startIndex"] == 1
    assert body["itemsPerPage"] == 2
    assert len(body["Resources"]) == 2

    r2 = client.get("/scim/v2/Users?startIndex=3&count=10", headers=_auth())
    assert r2.json()["itemsPerPage"] == 3


def test_list_users_filter_by_username(client):
    client.post(
        "/scim/v2/Users",
        json=_user_body("alpha@example.com"),
        headers=_auth(),
    )
    client.post(
        "/scim/v2/Users",
        json=_user_body("beta@example.com"),
        headers=_auth(),
    )
    r = client.get(
        '/scim/v2/Users?filter=userName eq "alpha@example.com"',
        headers=_auth(),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["totalResults"] == 1
    assert body["Resources"][0]["userName"] == "alpha@example.com"


def test_list_users_filter_by_email(client):
    client.post(
        "/scim/v2/Users",
        json=_user_body("gamma@example.com"),
        headers=_auth(),
    )
    r = client.get(
        '/scim/v2/Users?filter=email eq "gamma@example.com"',
        headers=_auth(),
    )
    assert r.status_code == 200
    assert r.json()["totalResults"] == 1


def test_put_user_soft_deletes_when_active_false(client):
    created = client.post(
        "/scim/v2/Users",
        json=_user_body("del@example.com"),
        headers=_auth(),
    ).json()
    updated_body = _user_body("del@example.com", active=False)
    r = client.put(
        f"/scim/v2/Users/{created['id']}",
        json=updated_body,
        headers=_auth(),
    )
    assert r.status_code == 200
    assert r.json()["active"] is False
    # Row retained — GET still works, shows inactive.
    r2 = client.get(f"/scim/v2/Users/{created['id']}", headers=_auth())
    assert r2.status_code == 200
    assert r2.json()["active"] is False


def test_delete_user_soft_deletes(client, SessionLocal):
    from gdx_dispatch.models.platform import Identity

    created = client.post(
        "/scim/v2/Users",
        json=_user_body("gone@example.com"),
        headers=_auth(),
    ).json()
    uid = created["id"]

    r = client.delete(f"/scim/v2/Users/{uid}", headers=_auth())
    assert r.status_code == 204

    # Verify row retained + status flipped + deleted_at set.
    db = SessionLocal()
    try:
        from uuid import UUID as _UUID

        row = db.get(Identity, _UUID(uid))
        assert row is not None, "soft-delete must not hard-delete"
        assert row.status == "deleted"
        assert row.deleted_at is not None
    finally:
        db.close()


def test_re_post_soft_deleted_user_reactivates(client):
    created = client.post(
        "/scim/v2/Users",
        json=_user_body("revive@example.com"),
        headers=_auth(),
    ).json()
    client.delete(f"/scim/v2/Users/{created['id']}", headers=_auth())

    r = client.post(
        "/scim/v2/Users",
        json=_user_body("revive@example.com"),
        headers=_auth(),
    )
    # 200 on reactivation, not 201 (same identity id).
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == created["id"]
    assert body["active"] is True


# ─── Groups CRUD ──────────────────────────────────────────────────────────


def _create_user(client, user_name: str) -> str:
    return client.post(
        "/scim/v2/Users",
        json=_user_body(user_name),
        headers=_auth(),
    ).json()["id"]


def test_create_group_adds_memberships(client):
    uid1 = _create_user(client, "g1@example.com")
    uid2 = _create_user(client, "g2@example.com")

    r = client.post(
        "/scim/v2/Groups",
        json={
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"],
            "displayName": "engineers",
            "members": [{"value": uid1}, {"value": uid2}],
        },
        headers=_auth(),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["displayName"] == "engineers"
    member_ids = {m["value"] for m in body["members"]}
    assert {uid1, uid2}.issubset(member_ids)


def test_list_groups(client):
    uid = _create_user(client, "list@example.com")
    client.post(
        "/scim/v2/Groups",
        json={"displayName": "ops", "members": [{"value": uid}]},
        headers=_auth(),
    )
    r = client.get("/scim/v2/Groups", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    names = {g["displayName"] for g in body["Resources"]}
    assert "ops" in names


def test_put_group_replaces_members(client):
    uid1 = _create_user(client, "p1@example.com")
    uid2 = _create_user(client, "p2@example.com")
    uid3 = _create_user(client, "p3@example.com")
    client.post(
        "/scim/v2/Groups",
        json={"displayName": "team", "members": [{"value": uid1}, {"value": uid2}]},
        headers=_auth(),
    )

    r = client.put(
        f"/scim/v2/Groups/{TENANT_UUID}:team",
        json={"displayName": "team", "members": [{"value": uid3}]},
        headers=_auth(),
    )
    assert r.status_code == 200
    body = r.json()
    member_ids = {m["value"] for m in body["members"]}
    assert member_ids == {uid3}


def test_delete_group_revokes_memberships(client):
    uid = _create_user(client, "d@example.com")
    client.post(
        "/scim/v2/Groups",
        json={"displayName": "temp", "members": [{"value": uid}]},
        headers=_auth(),
    )
    r = client.delete(f"/scim/v2/Groups/{TENANT_UUID}:temp", headers=_auth())
    assert r.status_code == 204

    r2 = client.get(f"/scim/v2/Groups/{TENANT_UUID}:temp", headers=_auth())
    assert r2.status_code == 200
    assert r2.json()["members"] == []


# ─── tenant isolation ────────────────────────────────────────────────────


def test_cross_tenant_user_invisible(client):
    _create_user(client, "visible@example.com")
    r = client.get("/scim/v2/Users", headers=_auth(OTHER_TENANT_TOKEN))
    assert r.status_code == 200
    assert r.json()["totalResults"] == 0

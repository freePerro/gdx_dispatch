"""P3.10 — OAuth client management + code exchange."""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from gdx_dispatch.modules.phone_com.client import BASE_URL, PhoneComAPIError, PhoneComClient

_VID = 1000000


def _envelope(items):
    return {"filters": {}, "sort": {}, "total": len(items),
            "limit": 25, "offset": None, "items": items}


# ── code exchange ──────────────────────────────────────────────────────


@respx.mock
def test_exchange_auth_code_posts_canonical_body():
    route = respx.post(f"{BASE_URL}/oauth/access-token").mock(
        return_value=httpx.Response(
            200, json={"access_token": "phc-AT", "refresh_token": "phc-RT"},
        )
    )
    out = PhoneComClient.exchange_auth_code(
        client_id="cid", client_secret="csec",
        code="C123", redirect_uri="https://gdx.gdx/auth/cb",
    )
    body = json.loads(route.calls.last.request.read())
    assert body == {
        "grant_type": "authorization_code",
        "client_id": "cid", "client_secret": "csec",
        "code": "C123", "redirect_uri": "https://gdx.gdx/auth/cb",
    }
    assert out["access_token"] == "phc-AT"


@respx.mock
def test_exchange_auth_code_raises_on_400():
    respx.post(f"{BASE_URL}/oauth/access-token").mock(
        return_value=httpx.Response(400, json={"error": "invalid_grant"})
    )
    with pytest.raises(PhoneComAPIError):
        PhoneComClient.exchange_auth_code(
            client_id="x", client_secret="y", code="z", redirect_uri="https://a/b",
        )


@respx.mock
def test_refresh_access_token_posts_grant():
    respx.post(f"{BASE_URL}/oauth/access-token").mock(
        return_value=httpx.Response(200, json={"access_token": "new"})
    )
    out = PhoneComClient.refresh_access_token(
        client_id="cid", client_secret="csec", refresh_token="r",
    )
    assert out["access_token"] == "new"


# ── client + redirect-URI management ───────────────────────────────────


@respx.mock
def test_list_oauth_clients_calls_correct_endpoint():
    respx.get(f"{BASE_URL}/accounts/{_VID}/oauth/clients").mock(
        return_value=httpx.Response(200, json=_envelope([{"id": "c1"}]))
    )
    c = PhoneComClient(token="t", voip_id=_VID)
    out = c.list_oauth_clients()
    assert out["total"] == 1


@respx.mock
def test_get_oauth_client_returns_payload():
    respx.get(f"{BASE_URL}/accounts/{_VID}/oauth/clients/c1").mock(
        return_value=httpx.Response(200, json={"id": "c1", "name": "GDX App"})
    )
    c = PhoneComClient(token="t", voip_id=_VID)
    out = c.get_oauth_client(client_id="c1")
    assert out["name"] == "GDX App"


@respx.mock
def test_create_redirect_uri_posts_payload():
    route = respx.post(
        f"{BASE_URL}/accounts/{_VID}/oauth/clients/c1/redirect-uris"
    ).mock(return_value=httpx.Response(200, json={"id": 1}))
    c = PhoneComClient(token="t", voip_id=_VID)
    out = c.create_oauth_client_redirect_uri(
        client_id="c1", redirect_uri="https://gdx.gdx/cb",
    )
    body = json.loads(route.calls.last.request.read())
    assert body == {"redirect_uri": "https://gdx.gdx/cb"}
    assert out["id"] == 1

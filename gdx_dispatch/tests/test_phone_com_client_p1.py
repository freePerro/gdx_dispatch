"""Phase 1 client surface — listener filters, PATCH callback/listener,
token introspection."""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from gdx_dispatch.modules.phone_com.client import BASE_URL, PhoneComAPIError, PhoneComClient

_VID = 1000000
_LISTENER_ID = 11111


def _envelope(items, total=None):
    return {
        "filters": {}, "sort": {}, "total": total if total is not None else len(items),
        "limit": 25, "offset": None, "items": items,
    }


# ── P1.1: listener filters ──────────────────────────────────────────────


@respx.mock
def test_create_listener_filter_posts_canonical_shape():
    route = respx.post(
        f"{BASE_URL}/accounts/{_VID}/integrations/events/listeners/{_LISTENER_ID}/filters"
    ).mock(return_value=httpx.Response(200, json={"id": 7, "field": "type"}))
    c = PhoneComClient(token="t", voip_id=_VID)
    out = c.create_listener_filter(
        listener_id=_LISTENER_ID, field="type", operator="in",
        value=["phone.call", "phone.message"],
    )
    body = json.loads(route.calls.last.request.read())
    assert body == {
        "field": "type", "operator": "in",
        "value": ["phone.call", "phone.message"],
    }
    assert out["id"] == 7


@respx.mock
def test_ensure_filter_creates_when_absent():
    respx.get(
        f"{BASE_URL}/accounts/{_VID}/integrations/events/listeners/{_LISTENER_ID}/filters"
    ).mock(return_value=httpx.Response(200, json=_envelope([])))
    create = respx.post(
        f"{BASE_URL}/accounts/{_VID}/integrations/events/listeners/{_LISTENER_ID}/filters"
    ).mock(return_value=httpx.Response(200, json={"id": 9}))
    c = PhoneComClient(token="t", voip_id=_VID)
    out = c.ensure_listener_event_filter(listener_id=_LISTENER_ID)
    assert out["created"] is True
    assert out["filter_id"] == 9
    assert "phone.call" in out["event_types"]
    body = json.loads(create.calls.last.request.read())
    assert body["field"] == "type" and body["operator"] == "in"
    assert set(body["value"]) >= {"phone.call", "phone.message", "phone.voicemail"}


@respx.mock
def test_ensure_filter_idempotent_when_match_exists():
    wanted = list(PhoneComClient.DEFAULT_LISTENER_EVENT_TYPES)
    existing = [{"id": 42, "field": "type", "operator": "in", "value": wanted}]
    respx.get(
        f"{BASE_URL}/accounts/{_VID}/integrations/events/listeners/{_LISTENER_ID}/filters"
    ).mock(return_value=httpx.Response(200, json=_envelope(existing)))
    create = respx.post(
        f"{BASE_URL}/accounts/{_VID}/integrations/events/listeners/{_LISTENER_ID}/filters"
    )
    c = PhoneComClient(token="t", voip_id=_VID)
    out = c.ensure_listener_event_filter(listener_id=_LISTENER_ID)
    assert out["created"] is False
    assert out["filter_id"] == 42
    assert create.call_count == 0


@respx.mock
def test_ensure_filter_drops_stale_and_creates_canonical():
    # Wrong value set — should be deleted then re-created.
    stale = [{"id": 50, "field": "type", "operator": "in", "value": ["phone.call"]}]
    respx.get(
        f"{BASE_URL}/accounts/{_VID}/integrations/events/listeners/{_LISTENER_ID}/filters"
    ).mock(return_value=httpx.Response(200, json=_envelope(stale)))
    delete = respx.delete(
        f"{BASE_URL}/accounts/{_VID}/integrations/events/listeners/{_LISTENER_ID}/filters/50"
    ).mock(return_value=httpx.Response(204))
    create = respx.post(
        f"{BASE_URL}/accounts/{_VID}/integrations/events/listeners/{_LISTENER_ID}/filters"
    ).mock(return_value=httpx.Response(200, json={"id": 60}))
    c = PhoneComClient(token="t", voip_id=_VID)
    out = c.ensure_listener_event_filter(listener_id=_LISTENER_ID)
    assert out["created"] is True
    assert out["filter_id"] == 60
    assert delete.call_count == 1
    assert create.call_count == 1


# ── P1.2: PATCH callbacks/listeners ─────────────────────────────────────


@respx.mock
def test_patch_callback_sends_only_provided_fields():
    route = respx.patch(
        f"{BASE_URL}/accounts/{_VID}/integrations/events/callbacks/99"
    ).mock(return_value=httpx.Response(200, json={"id": 99}))
    c = PhoneComClient(token="t", voip_id=_VID)
    out = c.patch_callback(
        callback_id=99, url="https://new.example.com/hook",
    )
    body = json.loads(route.calls.last.request.read())
    assert body == {"config": {"url": "https://new.example.com/hook"}}
    assert out["id"] == 99


@respx.mock
def test_patch_callback_empty_raises():
    c = PhoneComClient(token="t", voip_id=_VID)
    with pytest.raises(PhoneComAPIError, match="nothing to patch"):
        c.patch_callback(callback_id=99)


@respx.mock
def test_patch_listener_sends_callback_id_swap():
    route = respx.patch(
        f"{BASE_URL}/accounts/{_VID}/integrations/events/listeners/22"
    ).mock(return_value=httpx.Response(200, json={"id": 22, "callback_id": 100}))
    c = PhoneComClient(token="t", voip_id=_VID)
    out = c.patch_listener(listener_id=22, callback_id=100)
    body = json.loads(route.calls.last.request.read())
    assert body == {"callback_id": 100}
    assert out["callback_id"] == 100


# ── P1.3: token introspection ───────────────────────────────────────────


@respx.mock
def test_get_access_token_details_returns_payload_on_200():
    payload = {"scope": "voip-api", "expires_at": "2030-01-01T00:00:00Z"}
    respx.get(f"{BASE_URL}/oauth/access-token/details").mock(
        return_value=httpx.Response(200, json=payload)
    )
    c = PhoneComClient(token="t", voip_id=_VID)
    out = c.get_access_token_details()
    assert out == payload


@respx.mock
def test_get_access_token_details_returns_none_on_404():
    """Permanent tokens minted in the Console don't expose introspection."""
    respx.get(f"{BASE_URL}/oauth/access-token/details").mock(
        return_value=httpx.Response(404, json={"error": "not found"})
    )
    c = PhoneComClient(token="t", voip_id=_VID)
    assert c.get_access_token_details() is None


@respx.mock
def test_get_access_token_details_raises_on_5xx():
    respx.get(f"{BASE_URL}/oauth/access-token/details").mock(
        return_value=httpx.Response(500, json={"error": "boom"})
    )
    c = PhoneComClient(token="t", voip_id=_VID)
    # Drop retry budget to keep the test fast.
    c.retry_max_attempts = 0
    with pytest.raises(PhoneComAPIError):
        c.get_access_token_details()

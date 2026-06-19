"""Sprint phone-com pc-s5 — 2-step webhook setup + tools.phone.com refusal."""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from gdx_dispatch.modules.phone_com.client import BASE_URL, PhoneComAPIError, PhoneComClient

_VID = 1000000
_PLATFORM_URL = "https://test-tenant.example.com/api/webhooks/phone-com/test-tenant/abc"


def _envelope(items, total=None):
    return {"filters": {}, "sort": {"id": "desc"},
            "total": total if total is not None else len(items),
            "limit": 25, "offset": None, "items": items}


@respx.mock
def test_register_callback_body_has_https_mode_and_post():
    route = respx.post(f"{BASE_URL}/accounts/{_VID}/integrations/events/callbacks").mock(
        return_value=httpx.Response(200, json={"id": 99999, "config": {"url": _PLATFORM_URL}}))
    c = PhoneComClient(token="t", voip_id=_VID)
    out = c.register_callback(name="gdx-phone-com", url=_PLATFORM_URL)
    body = json.loads(route.calls.last.request.read())
    assert body["mode"] == "HTTPS"
    assert body["config"]["url"] == _PLATFORM_URL
    assert body["config"]["method"] == "POST"
    assert out["id"] == 99999


@respx.mock
def test_ensure_webhook_idempotent_when_present():
    existing = {"id": 99999, "voip_id": _VID, "enabled": True,
                "config": {"url": _PLATFORM_URL, "method": "POST"}}
    listener = {"id": 11111, "callback_id": 99999, "version": "1.0.0"}
    respx.get(f"{BASE_URL}/accounts/{_VID}/integrations/events/callbacks").mock(
        return_value=httpx.Response(200, json=_envelope([existing])))
    respx.get(f"{BASE_URL}/accounts/{_VID}/integrations/events/listeners").mock(
        return_value=httpx.Response(200, json=_envelope([listener])))
    create_route = respx.post(f"{BASE_URL}/accounts/{_VID}/integrations/events/callbacks")
    c = PhoneComClient(token="t", voip_id=_VID)
    out = c.ensure_webhook(name="gdx-phone-com", url=_PLATFORM_URL)
    assert out["created"] is False
    assert out["callback_id"] == 99999
    assert create_route.call_count == 0  # no creation when present


@respx.mock
def test_ensure_webhook_creates_when_absent():
    respx.get(f"{BASE_URL}/accounts/{_VID}/integrations/events/callbacks").mock(
        return_value=httpx.Response(200, json=_envelope([])))
    create_cb = respx.post(f"{BASE_URL}/accounts/{_VID}/integrations/events/callbacks").mock(
        return_value=httpx.Response(200, json={"id": 11, "config": {"url": _PLATFORM_URL}}))
    create_l = respx.post(f"{BASE_URL}/accounts/{_VID}/integrations/events/listeners").mock(
        return_value=httpx.Response(200, json={"id": 22, "callback_id": 11}))
    c = PhoneComClient(token="t", voip_id=_VID)
    out = c.ensure_webhook(name="gdx-phone-com", url=_PLATFORM_URL)
    assert out["created"] is True
    assert out["callback_id"] == 11 and out["listener_id"] == 22
    assert create_cb.call_count == 1 and create_l.call_count == 1


@respx.mock
def test_ensure_webhook_refuses_tools_phone_com_url():
    create_route = respx.post(f"{BASE_URL}/accounts/{_VID}/integrations/events/callbacks")
    c = PhoneComClient(token="t", voip_id=_VID)
    with pytest.raises(PhoneComAPIError, match="reserved|tools.phone.com"):
        c.ensure_webhook(name="x", url="https://tools.phone.com/foo")
    assert create_route.call_count == 0


@respx.mock
def test_disconnect_webhook_deletes_listeners_then_callback():
    listener = {"id": 22, "callback_id": 11, "version": "1.0.0"}
    cb = {"id": 11, "config": {"url": _PLATFORM_URL}}
    respx.get(f"{BASE_URL}/accounts/{_VID}/integrations/events/callbacks").mock(
        return_value=httpx.Response(200, json=_envelope([cb])))
    respx.get(f"{BASE_URL}/accounts/{_VID}/integrations/events/listeners").mock(
        return_value=httpx.Response(200, json=_envelope([listener])))
    del_l = respx.delete(f"{BASE_URL}/accounts/{_VID}/integrations/events/listeners/22").mock(
        return_value=httpx.Response(204))
    del_cb = respx.delete(f"{BASE_URL}/accounts/{_VID}/integrations/events/callbacks/11").mock(
        return_value=httpx.Response(204))
    c = PhoneComClient(token="t", voip_id=_VID)
    c.disconnect_webhook(callback_id=11)
    assert del_l.call_count == 1 and del_cb.call_count == 1


@respx.mock
def test_disconnect_webhook_refuses_tools_phone_com():
    cb = {"id": 195818, "config": {"url": "https://tools.phone.com/webhooks/call-events-iot"}}
    respx.get(f"{BASE_URL}/accounts/{_VID}/integrations/events/callbacks").mock(
        return_value=httpx.Response(200, json=_envelope([cb])))
    del_route = respx.delete(f"{BASE_URL}/accounts/{_VID}/integrations/events/callbacks/195818")
    c = PhoneComClient(token="t", voip_id=_VID)
    with pytest.raises(PhoneComAPIError, match="reserved|tools.phone.com"):
        c.disconnect_webhook(callback_id=195818)
    assert del_route.call_count == 0

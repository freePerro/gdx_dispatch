"""Sprint phone-com pc-s3 — list_calls/list_messages/list_extensions/list_phone_numbers + paginate."""
from __future__ import annotations

import httpx
import pytest
import respx

from gdx_dispatch.modules.phone_com.client import BASE_URL, PhoneComAPIError, PhoneComClient

_VID = 1000000


def _envelope(items, total=None):
    return {
        "filters": {}, "sort": {"id": "desc"},
        "total": total if total is not None else len(items),
        "limit": 25, "offset": None, "items": items,
    }


@respx.mock
def test_list_calls_envelope_preserved_with_nested_extension():
    sample = {
        "id": "10413a06-e5d7", "caller_id": "+1320", "called_number": "+1800",
        "extension": {"id": 1000100, "name": "Example Owner", "extension": 100, "voip_id": _VID},
        "start_time_epoch": 1777298777, "direction": "in",
        "voicemail_url": "https://api.phone.com/...",
        "voicemail_transcript": "msg",
        "final_action": "type voicemail_received",
    }
    respx.get(f"{BASE_URL}/accounts/{_VID}/call-logs").mock(
        return_value=httpx.Response(200, json=_envelope([sample], total=1)))
    c = PhoneComClient(token="t", voip_id=_VID)
    out = c.list_calls(limit=1)
    assert out["total"] == 1
    item = out["items"][0]
    assert item["extension"]["id"] == 1000100  # nested object preserved
    assert item["voicemail_url"]
    assert item["final_action"].startswith("type voicemail")


@respx.mock
def test_list_calls_filter_params_in_query():
    route = respx.get(f"{BASE_URL}/accounts/{_VID}/call-logs").mock(
        return_value=httpx.Response(200, json=_envelope([])))
    c = PhoneComClient(token="t", voip_id=_VID)
    c.list_calls(from_epoch=1000, to_epoch=2000, limit=50, offset=10)
    qs = dict(route.calls.last.request.url.params)
    assert qs.get("filters[start_time]") == "gt:1000" or "1000" in str(qs)
    assert "limit" in qs and qs["limit"] == "50"
    assert "offset" in qs and qs["offset"] == "10"


@respx.mock
def test_list_messages_empty_no_error():
    respx.get(f"{BASE_URL}/accounts/{_VID}/messages").mock(
        return_value=httpx.Response(200, json=_envelope([], total=0)))
    out = PhoneComClient(token="t", voip_id=_VID).list_messages()
    assert out["total"] == 0
    assert out["items"] == []


@respx.mock
def test_list_extensions_returns_2_gdx_extensions():
    items = [
        {"id": 1000100, "name": "Example Owner", "extension": 100, "voip_id": _VID},
        {"id": 1000500, "name": "Example Garage Doors", "extension": 500, "voip_id": _VID},
    ]
    respx.get(f"{BASE_URL}/accounts/{_VID}/extensions").mock(
        return_value=httpx.Response(200, json=_envelope(items)))
    out = PhoneComClient(token="t", voip_id=_VID).list_extensions()
    assert {e["extension"] for e in out["items"]} == {100, 500}


@respx.mock
def test_list_phone_numbers_returns_e164_strings():
    items = [
        {"phone_number": "+13205550100", "name": "(320) 270-6002"},
        {"phone_number": "+18005550199", "name": "Example Garage Doors"},
    ]
    respx.get(f"{BASE_URL}/accounts/{_VID}/phone-numbers").mock(
        return_value=httpx.Response(200, json=_envelope(items)))
    out = PhoneComClient(token="t", voip_id=_VID).list_phone_numbers()
    assert all(n["phone_number"].startswith("+") for n in out["items"])


@respx.mock
def test_paginate_walks_offset_through_total():
    pages = [_envelope([{"id": str(i)} for i in range(j, j+25)], total=100) for j in range(0, 100, 25)]
    respx.get(f"{BASE_URL}/accounts/{_VID}/call-logs").mock(side_effect=[
        httpx.Response(200, json=p) for p in pages
    ])
    c = PhoneComClient(token="t", voip_id=_VID)
    items = list(c.paginate(c.list_calls, limit=25))
    assert len(items) == 100
    assert [it["id"] for it in items] == [str(i) for i in range(100)]


@respx.mock
def test_paginate_caps_at_10000():
    big = _envelope([{"id": str(i)} for i in range(25)], total=15000)
    respx.get(f"{BASE_URL}/accounts/{_VID}/call-logs").mock(
        return_value=httpx.Response(200, json=big))
    c = PhoneComClient(token="t", voip_id=_VID)
    with pytest.raises(PhoneComAPIError, match="10000|safety"):
        list(c.paginate(c.list_calls, limit=25))


@respx.mock
def test_voip_id_required_no_call_when_unset():
    route = respx.get(f"{BASE_URL}/accounts/123/call-logs")
    c = PhoneComClient(token="t")  # no voip_id
    with pytest.raises(PhoneComAPIError, match="voip_id"):
        c.list_calls()
    assert route.call_count == 0  # no HTTP fired

"""P2.6 — blocked calls client CRUD."""
from __future__ import annotations

import json

import httpx
import respx

from gdx_dispatch.modules.phone_com.client import BASE_URL, PhoneComClient

_VID = 1000000


def _envelope(items):
    return {"filters": {}, "sort": {}, "total": len(items),
            "limit": 25, "offset": None, "items": items}


@respx.mock
def test_list_blocked_calls_paginates():
    items = [
        {"id": 1, "name": "Spam Bot", "number": "+15551112222"},
        {"id": 2, "name": "Robocaller", "number": "+15553334444"},
    ]
    route = respx.get(f"{BASE_URL}/accounts/{_VID}/blocked-calls").mock(
        return_value=httpx.Response(200, json=_envelope(items))
    )
    c = PhoneComClient(token="t", voip_id=_VID)
    out = c.list_blocked_calls()
    assert out["total"] == 2
    assert route.called


@respx.mock
def test_create_blocked_call_posts_canonical_body():
    route = respx.post(f"{BASE_URL}/accounts/{_VID}/blocked-calls").mock(
        return_value=httpx.Response(200, json={"id": 99, "number": "+15555550100"})
    )
    c = PhoneComClient(token="t", voip_id=_VID)
    out = c.create_blocked_call(name="Spam", number="+15555550100")
    body = json.loads(route.calls.last.request.read())
    assert body == {
        "name": "Spam", "number": "+15555550100",
        "direction": "in", "action": "block",
    }
    assert out["id"] == 99


@respx.mock
def test_delete_blocked_call_calls_correct_path():
    route = respx.delete(f"{BASE_URL}/accounts/{_VID}/blocked-calls/99").mock(
        return_value=httpx.Response(204)
    )
    c = PhoneComClient(token="t", voip_id=_VID)
    c.delete_blocked_call(blocked_call_id=99)
    assert route.called

"""Sprint phone-com pc-s4 — send_message (P2P guard) + stream proxies."""
from __future__ import annotations

import httpx
import pytest
import respx

from gdx_dispatch.modules.phone_com.client import BASE_URL, PhoneComAPIError, PhoneComClient

_VID = 1000000


@respx.mock
def test_send_message_account_level_body_shape():
    route = respx.post(f"{BASE_URL}/accounts/{_VID}/messages").mock(
        return_value=httpx.Response(200, json={"id": "msg-1"}))
    c = PhoneComClient(token="t", voip_id=_VID)
    out = c.send_message(from_number="+18005550199", to_number="+13202959628", body="hi")
    body = route.calls.last.request.read().decode()
    assert "+18005550199" in body and "+13202959628" in body and "hi" in body
    assert out["id"] == "msg-1"


@respx.mock
def test_send_message_per_extension_uses_nested_path():
    route = respx.post(f"{BASE_URL}/accounts/{_VID}/extensions/100/messages").mock(
        return_value=httpx.Response(200, json={"id": "msg-2"}))
    c = PhoneComClient(token="t", voip_id=_VID)
    c.send_message(from_number="+18005550199", to_number="+13202959628",
                   body="hi", extension_id=100)
    assert route.call_count == 1


@respx.mock
def test_send_message_body_too_long_raises_no_http():
    route = respx.post(f"{BASE_URL}/accounts/{_VID}/messages")
    c = PhoneComClient(token="t", voip_id=_VID)
    with pytest.raises(PhoneComAPIError, match="too long|P2P"):
        c.send_message(from_number="+1", to_number="+1", body="x" * 1601)
    assert route.call_count == 0


def test_no_batch_send_method_exists():
    """P2P-only is structural — there must be no method that accepts a list of recipients."""
    methods = [m for m in dir(PhoneComClient) if not m.startswith("_")]
    for m in methods:
        assert "bulk" not in m.lower(), f"bulk method forbidden: {m}"
        assert "broadcast" not in m.lower(), f"broadcast method forbidden: {m}"


@respx.mock
def test_stream_voicemail_uses_cp_url_no_auth():
    cp = "https://mds.phone.com/abc/voicemails/x.wav"
    respx.get(cp).mock(return_value=httpx.Response(200, content=b"WAVbytes"))
    c = PhoneComClient(token="secret-token", voip_id=_VID)
    row = {"voicemail_cp_url": cp, "voicemail_url": "https://api.phone.com/...whatever"}
    chunks, ctype = c.stream_voicemail_audio(row)
    data = b"".join(chunks)
    assert data == b"WAVbytes"
    assert ctype.startswith("audio/")
    # Confirm Bearer auth NOT sent for the cp_url
    last = respx.calls.last.request
    assert "Authorization" not in last.headers


@respx.mock
def test_stream_voicemail_falls_back_to_authed_url_when_cp_empty():
    authed = f"{BASE_URL}/accounts/{_VID}/extensions/100/voicemail/abc/download"
    route = respx.get(authed).mock(return_value=httpx.Response(200, content=b"WAVbytes"))
    c = PhoneComClient(token="secret-token", voip_id=_VID)
    row = {"voicemail_cp_url": "", "voicemail_url": authed}
    chunks, _ = c.stream_voicemail_audio(row)
    list(chunks)
    assert route.calls.last.request.headers["Authorization"] == "Bearer secret-token"


def test_stream_call_recording_returns_none_when_url_empty():
    c = PhoneComClient(token="t", voip_id=_VID)
    row = {"call_recording_url": "", "call_recording_cp_url": ""}
    assert c.stream_call_recording(row) is None

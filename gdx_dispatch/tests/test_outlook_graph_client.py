"""Slice outlook-s6 — verify the Graph client routes endpoints + raises typed errors."""
from __future__ import annotations

import pytest
import respx
from httpx import Response

from gdx_dispatch.modules.outlook.graph_client import (
    MailboxIdentity,
    OutlookGraphAPIError,
    OutlookGraphClient,
)


GRAPH = "https://graph.microsoft.com/v1.0"


@pytest.fixture
def client():
    c = OutlookGraphClient("fake-access-token")
    yield c
    c.close()


@respx.mock
def test_validate_token_returns_identity(client):
    respx.get(f"{GRAPH}/me").mock(return_value=Response(200, json={
        "id": "ms-user-guid", "userPrincipalName": "doug@gdx",
        "displayName": "Doug B", "mail": "doug@gdx",
    }))
    ident = client.validate_token()
    assert isinstance(ident, MailboxIdentity)
    assert ident.upn == "doug@gdx"
    assert ident.display_name == "Doug B"
    assert ident.user_id == "ms-user-guid"


@respx.mock
def test_validate_token_401_raises_typed_error(client):
    respx.get(f"{GRAPH}/me").mock(return_value=Response(
        401, json={"error": {"code": "InvalidAuthenticationToken"}}
    ))
    with pytest.raises(OutlookGraphAPIError) as exc:
        client.validate_token()
    assert exc.value.status_code == 401


@respx.mock
def test_get_mailbox_settings_passes_through(client):
    respx.get(f"{GRAPH}/me/mailboxSettings").mock(return_value=Response(
        200, json={"timeZone": "Eastern Standard Time"}
    ))
    body = client.get_mailbox_settings()
    assert body["timeZone"] == "Eastern Standard Time"


@respx.mock
def test_list_messages_inbox_default(client):
    route = respx.get(f"{GRAPH}/me/mailFolders/Inbox/messages").mock(
        return_value=Response(200, json={"value": []})
    )
    client.list_messages()
    call = route.calls[0]
    qp = dict(call.request.url.params)
    assert qp["$top"] == "50"
    assert "receivedDateTime" in qp["$orderby"]


@respx.mock
def test_list_messages_with_delta_token_uses_delta_endpoint(client):
    route = respx.get(f"{GRAPH}/me/mailFolders/Inbox/messages/delta").mock(
        return_value=Response(200, json={"value": [], "@odata.deltaLink": "next-tok"})
    )
    client.list_messages(delta_token="prev-tok")
    assert route.called
    qp = dict(route.calls[0].request.url.params)
    assert qp["$deltatoken"] == "prev-tok"


@respx.mock
def test_get_message_includes_body(client):
    respx.get(f"{GRAPH}/me/messages/abc-123").mock(return_value=Response(
        200, json={"id": "abc-123", "subject": "Re: estimate",
                   "body": {"content": "<html>...</html>", "contentType": "html"}}
    ))
    msg = client.get_message("abc-123")
    assert msg["subject"] == "Re: estimate"
    assert "body" in msg


@respx.mock
def test_download_attachment_returns_bytes(client):
    respx.get(
        f"{GRAPH}/me/messages/m1/attachments/a1/$value"
    ).mock(return_value=Response(200, content=b"PDF-binary-bytes"))
    blob = client.download_attachment("m1", "a1")
    assert blob == b"PDF-binary-bytes"


def test_constructor_rejects_empty_token():
    with pytest.raises(ValueError, match="access_token is required"):
        OutlookGraphClient("")


def test_constructor_rejects_kwargs_outside_spec():
    """Locked signature — no surprise kwargs."""
    with pytest.raises(TypeError):
        OutlookGraphClient("tok", weird_kwarg=True)


def test_context_manager_closes_underlying_client():
    with OutlookGraphClient("tok") as c:
        assert c._client is not None
    # After exit, close was called — underlying httpx.Client._state should be closed
    assert c._client.is_closed


@respx.mock
def test_list_messages_delta_without_token_still_hits_delta_endpoint(client):
    # 2026-07-07 audit: the sync path used list_messages(), whose token-less
    # branch hits the PLAIN listing — Graph never returns a deltaLink there,
    # so no folder could ever bootstrap a token and every sync re-walked the
    # whole mailbox. The delta lister must hit /delta even with no token.
    route = respx.get(f"{GRAPH}/me/mailFolders/AAA1/messages/delta").mock(
        return_value=Response(200, json={"value": [], "@odata.deltaLink": "https://x?$deltatoken=t1"})
    )
    client.list_messages_delta(folder="AAA1")
    assert route.called
    qp = dict(route.calls[0].request.url.params)
    assert "$deltatoken" not in qp
    assert qp["$top"] == "100"
    assert "$select" in qp  # delta rejects $orderby/$skip; select is allowed


@respx.mock
def test_list_messages_delta_with_token_resumes(client):
    route = respx.get(f"{GRAPH}/me/mailFolders/AAA1/messages/delta").mock(
        return_value=Response(200, json={"value": []})
    )
    client.list_messages_delta(folder="AAA1", delta_token="prev-tok")
    qp = dict(route.calls[0].request.url.params)
    assert qp == {"$deltatoken": "prev-tok"}

"""P2.9 — conversations API client + upsert wiring."""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from gdx_dispatch.modules.phone_com.client import BASE_URL, PhoneComAPIError, PhoneComClient

_VID = 1000000


@respx.mock
def test_patch_conversation_uses_extension_scoped_path():
    route = respx.patch(
        f"{BASE_URL}/accounts/{_VID}/extensions/100/conversations/conv-abc"
    ).mock(return_value=httpx.Response(200, json={"id": "conv-abc", "read": True}))
    c = PhoneComClient(token="t", voip_id=_VID)
    out = c.patch_conversation(extension_id=100, conversation_id="conv-abc", read=True)
    body = json.loads(route.calls.last.request.read())
    assert body == {"read": True}
    assert out["read"] is True


@respx.mock
def test_patch_conversation_empty_raises():
    c = PhoneComClient(token="t", voip_id=_VID)
    with pytest.raises(PhoneComAPIError, match="nothing to patch"):
        c.patch_conversation(extension_id=100, conversation_id="c1")


def test_upsert_message_captures_conversation_id():
    """Webhook payload with conversation_id lands on the message row."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from gdx_dispatch.core.audit import TenantBase
    from gdx_dispatch.modules.phone_com import upserts as u
    from gdx_dispatch.modules.phone_com.models import PhoneComMessage
    # Tenant-plane has FKs to customers/jobs/users; create the full schema.
    engine = create_engine("sqlite:///:memory:")
    TenantBase.metadata.create_all(engine)
    sess = sessionmaker(bind=engine, expire_on_commit=False)()

    payload = {
        "id": "msg-001",
        "direction": "in",
        "from": "+15555550001",
        "to": "+15555550002",
        "text": "hello",
        "conversation_id": "conv-xyz",
    }
    row = u.upsert_message(sess, payload)
    assert row is not None
    assert row.phone_com_conversation_id == "conv-xyz"

    # Nested {"conversation": {"id": ...}} shape also works.
    nested = {
        "id": "msg-002",
        "direction": "in",
        "from": "+15555550001",
        "to": "+15555550002",
        "text": "again",
        "conversation": {"id": "conv-nested"},
    }
    row2 = u.upsert_message(sess, nested)
    assert row2.phone_com_conversation_id == "conv-nested"
    sess.close()

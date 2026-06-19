"""P2.7 — fax client list/get/download + upsert + classifier."""
from __future__ import annotations

import httpx
import respx

from gdx_dispatch.modules.phone_com.client import BASE_URL, PhoneComClient

_VID = 1000000


def _envelope(items):
    return {"filters": {}, "sort": {}, "total": len(items),
            "limit": 25, "offset": None, "items": items}


# ── client ─────────────────────────────────────────────────────────────


@respx.mock
def test_list_faxes_calls_v4_fax_endpoint():
    respx.get(f"{BASE_URL}/accounts/{_VID}/fax").mock(
        return_value=httpx.Response(200, json=_envelope([{"id": 1}, {"id": 2}]))
    )
    c = PhoneComClient(token="t", voip_id=_VID)
    out = c.list_faxes()
    assert out["total"] == 2


@respx.mock
def test_get_fax_returns_payload():
    respx.get(f"{BASE_URL}/accounts/{_VID}/fax/77").mock(
        return_value=httpx.Response(200, json={"id": 77, "pages": 3})
    )
    c = PhoneComClient(token="t", voip_id=_VID)
    out = c.get_fax(fax_id=77)
    assert out["pages"] == 3


@respx.mock
def test_stream_fax_pdf_uses_authed_download():
    respx.get(f"{BASE_URL}/accounts/{_VID}/fax/77/download").mock(
        return_value=httpx.Response(
            200, content=b"%PDF-1.4 fake pdf", headers={"content-type": "application/pdf"},
        )
    )
    c = PhoneComClient(token="t", voip_id=_VID)
    chunks, ctype = c.stream_fax_pdf(fax_id=77)
    body = b"".join(chunks)
    assert body.startswith(b"%PDF")
    assert ctype == "application/pdf"


# ── webhook classification ─────────────────────────────────────────────


def test_classifier_routes_fax_payload_to_fax():
    from gdx_dispatch.modules.phone_com.webhook_router import _classify_event
    assert _classify_event({"type": "phone.fax.received"}) == "fax"
    assert _classify_event({"pdf_url": "https://x/y.pdf"}) == "fax"
    assert _classify_event({"pages": 2}) == "fax"
    # Doesn't accidentally route a plain call payload to fax.
    assert _classify_event({"call_id": "abc"}) == "call"


# ── upsert ─────────────────────────────────────────────────────────────


def test_upsert_fax_idempotent_in_memory():
    """Use a per-test in-memory sqlite tenant DB to confirm upsert keys on phone_com_fax_id."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from gdx_dispatch.core.audit import TenantBase
    from gdx_dispatch.modules.phone_com import upserts as u
    from gdx_dispatch.modules.phone_com.models import PhoneComFax

    engine = create_engine("sqlite:///:memory:")
    # Only create the bare tenant tables we need; full schema needs Customers etc.
    PhoneComFax.__table__.create(engine)
    sm = sessionmaker(bind=engine, expire_on_commit=False)
    sess = sm()
    payload = {
        "id": "fax-001",
        "direction": "in",
        "from": "+15555550001",
        "to": "+15555550002",
        "pages": 4,
        "status": "received",
        "received_at": "2026-05-04T10:00:00Z",
        "pdf_url": "https://api.phone.com/v4/accounts/1/fax/1/download",
    }
    # Skip customer matching — Customer table not built in this minimal env.
    from gdx_dispatch.modules.phone_com import upserts as upserts_mod

    def _no_match(*a, **kw):  # noqa: ARG001
        return None
    orig = upserts_mod.match_caller_id
    upserts_mod.match_caller_id = _no_match
    try:
        first = u.upsert_fax(sess, payload)
        assert first is not None
        assert first.pages == 4
        # Re-apply same payload — must update in place, not duplicate.
        again = u.upsert_fax(sess, payload | {"pages": 5})
        assert again.id == first.id
        assert again.pages == 5
        total = sess.query(PhoneComFax).count()
        assert total == 1
    finally:
        upserts_mod.match_caller_id = orig
        sess.close()

"""Outbound MMS — one photo per text (2026-10-15).

The shrinker runs on real Pillow images, not mocks: what matters is the bytes
that come out (under the carrier target, upright, no EXIF), and only a real
encode can show that. The router tests stop at Phone.com's edge with respx and
assert the JSON that would have left — the shape proven against the live API
on 2026-10-15 (``to`` as a list, ``media: [{data, filename}]``).
"""
from __future__ import annotations

import base64
import io
import json
import random

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from PIL import Image

from gdx_dispatch.models.tenant_models import AppSettings
from gdx_dispatch.modules.phone_com import key_storage
from gdx_dispatch.modules.phone_com.client import BASE_URL, PhoneComClient
from gdx_dispatch.modules.phone_com.mms import MAX_PIXELS, TARGET_BYTES, PhotoRejected, shrink_for_mms
from gdx_dispatch.modules.phone_com.models import PhoneComMessage
from gdx_dispatch.tests.conftest import production_sessionmaker
from gdx_dispatch.tests.test_phone_com_router_messages import (  # noqa: F401 — autouse fixtures
    _app,
    _engines,
    _no_audit,
    _seed_tenant,
    fernet_env,
)

_ORIENTATION = 0x0112
_GPS_IFD = 0x8825


def _noisy(w: int, h: int, mode: str = "RGB") -> Image.Image:
    """Noise defeats JPEG compression, so a big frame stays big — the worst
    case a real 12MP photo of gravel and siding approaches."""
    rnd = random.Random(7)  # noqa: S311 — seeded test noise, not a secret
    img = Image.frombytes("RGB", (w, h), bytes(rnd.getrandbits(8) for _ in range(w * h * 3)))
    return img.convert(mode) if mode != "RGB" else img


def _jpeg(img: Image.Image, **save) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95, **save)
    return buf.getvalue()


# ── shrink_for_mms ──────────────────────────────────────────────────────


def test_large_noisy_photo_comes_out_under_target_as_jpeg():
    raw = _jpeg(_noisy(3000, 2000))
    assert len(raw) > TARGET_BYTES * 4  # the input really is too big
    out = shrink_for_mms(raw)
    assert len(out) <= TARGET_BYTES
    img = Image.open(io.BytesIO(out))
    assert img.format == "JPEG"
    assert max(img.size) <= 2000


def test_small_photo_keeps_its_size_and_shape():
    out = shrink_for_mms(_jpeg(Image.new("RGB", (800, 600), (40, 120, 200))))
    assert Image.open(io.BytesIO(out)).size == (800, 600)


def test_sideways_phone_photo_comes_out_upright_with_no_exif():
    # A portrait shot stored as landscape pixels + Orientation=6 (rotate 90°),
    # carrying a GPS block the customer should never receive.
    exif = Image.Exif()
    exif[_ORIENTATION] = 6
    exif.get_ifd(_GPS_IFD)[2] = (45.0, 30.0, 0.0)
    raw = _jpeg(Image.new("RGB", (400, 100), (10, 10, 10)), exif=exif)

    out = Image.open(io.BytesIO(shrink_for_mms(raw)))
    assert out.size == (100, 400)
    got = out.getexif()
    assert _ORIENTATION not in got
    assert _GPS_IFD not in got


def test_big_sideways_photo_is_shrunk_and_still_turned_upright():
    # The shrink now happens before the rotation; the EXIF tag must survive it.
    exif = Image.Exif()
    exif[_ORIENTATION] = 6
    raw = _jpeg(Image.new("RGB", (3000, 1000), (90, 90, 90)), exif=exif)
    out = Image.open(io.BytesIO(shrink_for_mms(raw)))
    assert out.size == (667, 2000)


def test_small_file_that_decodes_huge_is_refused_before_decoding():
    # Audit 2026-10-15: a 690KB flat PNG at 13000x13000 took 2.1GB to decode.
    side = int(MAX_PIXELS ** 0.5) + 1
    buf = io.BytesIO()
    Image.new("L", (side, side), 255).save(buf, format="PNG", optimize=True)
    assert len(buf.getvalue()) < 2 * 1024 * 1024
    with pytest.raises(PhotoRejected, match="too large"):
        shrink_for_mms(buf.getvalue())


def test_transparent_png_is_flattened_onto_white_not_black():
    img = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    out = Image.open(io.BytesIO(shrink_for_mms(buf.getvalue())))
    assert out.format == "JPEG"
    r, g, b = out.convert("RGB").getpixel((100, 100))
    assert min(r, g, b) > 240


def test_iphone_heic_is_converted():
    pillow_heif = pytest.importorskip("pillow_heif")
    pillow_heif.register_heif_opener()
    buf = io.BytesIO()
    Image.new("RGB", (640, 480), (200, 50, 50)).save(buf, format="HEIF")
    out = Image.open(io.BytesIO(shrink_for_mms(buf.getvalue())))
    assert out.format == "JPEG"
    assert out.size == (640, 480)


def test_animated_gif_sends_its_first_frame():
    frames = [Image.new("P", (120, 90), c) for c in (1, 2, 3)]
    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:])
    out = Image.open(io.BytesIO(shrink_for_mms(buf.getvalue())))
    assert (out.format, out.size) == ("JPEG", (120, 90))


@pytest.mark.parametrize(
    "raw",
    [b"", b"%PDF-1.7 not a photo", b"\x00" * 1000],
    ids=["empty", "pdf", "junk"],
)
def test_non_photos_are_refused_with_a_readable_reason(raw):
    with pytest.raises(PhotoRejected) as exc:
        shrink_for_mms(raw)
    assert str(exc.value)  # user-facing; never blank


def test_unlisted_image_format_is_refused():
    buf = io.BytesIO()
    Image.new("RGB", (50, 50)).save(buf, format="TIFF")
    with pytest.raises(PhotoRejected, match="file type"):
        shrink_for_mms(buf.getvalue())


# ── client body shape ───────────────────────────────────────────────────


@respx.mock
def test_client_sends_media_inline_base64_with_to_as_list():
    route = respx.post(f"{BASE_URL}/accounts/42/messages").mock(
        return_value=httpx.Response(201, json={"id": 1}))
    PhoneComClient(token="t", voip_id=42).send_message(
        from_number="+18005550199", to_number="+13205550100", body="",
        media=[(b"JPEGBYTES", "photo.jpg")],
    )
    sent = json.loads(route.calls.last.request.content)
    assert sent["to"] == ["+13205550100"]
    assert sent["media"] == [{"data": base64.b64encode(b"JPEGBYTES").decode(), "filename": "photo.jpg"}]
    assert "text" not in sent  # a photo with no caption sends no empty text
    assert "media_urls" not in sent


@respx.mock
def test_client_plain_text_shape_is_unchanged():
    route = respx.post(f"{BASE_URL}/accounts/42/messages").mock(
        return_value=httpx.Response(201, json={"id": 1}))
    PhoneComClient(token="t", voip_id=42).send_message(
        from_number="+18005550199", to_number="+13205550100", body="hi",
    )
    assert json.loads(route.calls.last.request.content) == {
        "from": "+18005550199", "to": "+13205550100", "text": "hi",
    }


# ── POST /api/phone-com/messages/photo ──────────────────────────────────


def _configured(ce, te):
    csm = production_sessionmaker(ce)
    tid = _seed_tenant(csm)
    cs = csm()
    key_storage.set_token(cs, tid, "phc-token")
    cs.close()
    ts = production_sessionmaker(te)()
    ts.add(AppSettings(phone_com_voip_id="1000000", phone_com_default_caller_id="+18005550199"))
    ts.commit()
    ts.close()
    return tid


@pytest.fixture
def audits(monkeypatch):
    seen: list[dict] = []
    monkeypatch.setattr(
        "gdx_dispatch.core.audit.audit_best_effort",
        lambda db, **kw: seen.append(kw) or True,
    )
    return seen


_PHONE_COM_REPLY = {
    "id": 263339397,
    "media": [{"url": "https://s3.amazonaws.com/files.messages.phone.com/x/photo.jpg",
               "type": "image/jpeg", "size": 1234, "filename": "photo.jpg",
               "has_thumbnail": True}],
    "to": [{"number": "+13205550100", "delivery_status": "processing"}],
}


@respx.mock
def test_photo_send_shrinks_sends_records_and_audits(audits):
    ce, te = _engines()
    tid = _configured(ce, te)
    route = respx.post(f"{BASE_URL}/accounts/1000000/messages").mock(
        return_value=httpx.Response(201, json=_PHONE_COM_REPLY))
    app, _, tsm = _app(ce, te, tid)

    big = _jpeg(_noisy(2400, 1800))
    r = TestClient(app).post(
        "/api/phone-com/messages/photo",
        data={"to": "(320) 555-0100", "body": " Here's your new door "},
        files={"file": ("IMG_0001.HEIC", big, "")},  # Windows Chrome: empty type
    )
    assert r.status_code == 200, r.text

    sent = json.loads(route.calls.last.request.content)
    assert sent["to"] == ["+13205550100"]
    assert sent["text"] == "Here's your new door"
    photo = base64.b64decode(sent["media"][0]["data"])
    assert len(photo) <= TARGET_BYTES < len(big)
    assert Image.open(io.BytesIO(photo)).format == "JPEG"

    s = tsm()
    row = s.query(PhoneComMessage).one()
    assert row.direction == "out"
    assert row.phone_com_message_id == "263339397"
    assert row.attachments == _PHONE_COM_REPLY["media"]
    assert row.sent_by_user_id is not None
    s.close()
    assert r.json()["attachments"] == _PHONE_COM_REPLY["media"]

    assert [a["action"] for a in audits] == ["phone_com_mms_sent"]
    assert audits[0]["entity_id"] == str(row.id)
    assert audits[0]["user_id"]
    assert audits[0]["details"]["photo_bytes"] == [len(photo)]


@respx.mock
def test_not_a_photo_is_refused_before_anything_is_sent(audits):
    ce, te = _engines()
    tid = _configured(ce, te)
    route = respx.post(f"{BASE_URL}/accounts/1000000/messages")
    app, _, tsm = _app(ce, te, tid)
    r = TestClient(app).post(
        "/api/phone-com/messages/photo",
        data={"to": "+13205550100"},
        files={"file": ("invoice.pdf", b"%PDF-1.7", "application/pdf")},
    )
    assert r.status_code == 422
    assert "photo" in r.json()["detail"]
    assert not route.called
    s = tsm()
    assert s.query(PhoneComMessage).count() == 0
    s.close()
    assert audits == []


def test_photo_send_rejects_garbage_number():
    ce, te = _engines()
    tid = _configured(ce, te)
    app, _, _ = _app(ce, te, tid)
    r = TestClient(app).post(
        "/api/phone-com/messages/photo",
        data={"to": "not-a-number"},
        files={"file": ("a.jpg", _jpeg(Image.new("RGB", (10, 10))), "image/jpeg")},
    )
    assert r.status_code == 422


@respx.mock
def test_plain_text_send_now_leaves_an_audit_row(audits):
    ce, te = _engines()
    tid = _configured(ce, te)
    respx.post(f"{BASE_URL}/accounts/1000000/messages").mock(
        return_value=httpx.Response(200, json={"id": "msg-1", "status": "queued"}))
    app, _, _ = _app(ce, te, tid)
    r = TestClient(app).post("/api/phone-com/messages", json={"to": "+13205550100", "body": "hi"})
    assert r.status_code == 200, r.text
    assert [a["action"] for a in audits] == ["phone_com_sms_sent"]
    assert audits[0]["details"]["to_last4"] == "0100"


@respx.mock
def test_timeout_says_check_before_resending_and_leaves_a_trail(audits):
    # Audit 2026-10-15: a read timeout escaped as a bare 500 with no trace,
    # though Phone.com may already have delivered it — an invitation to send twice.
    ce, te = _engines()
    tid = _configured(ce, te)
    route = respx.post(f"{BASE_URL}/accounts/1000000/messages").mock(
        side_effect=httpx.ReadTimeout("slow"))
    app, _, tsm = _app(ce, te, tid)
    r = TestClient(app).post(
        "/api/phone-com/messages/photo",
        data={"to": "+13205550100"},
        files={"file": ("a.jpg", _jpeg(Image.new("RGB", (10, 10))), "image/jpeg")},
    )
    assert r.status_code == 504
    assert "may have gone through" in r.json()["detail"]
    assert route.call_count == 1  # never re-POSTed
    assert [a["action"] for a in audits] == ["phone_com_mms_unconfirmed"]
    assert audits[0]["details"]["error"] == "ReadTimeout"
    # Audit 2026-10-15 (3rd pass): "check the conversation" must find it there,
    # not an empty thread that invites a second send.
    s = tsm()
    row = s.query(PhoneComMessage).one()
    assert row.delivery_status == "unknown"
    assert row.direction == "out" and row.to_number == "+13205550100"
    assert [a["filename"] for a in row.attachments] == ["photo.jpg"]  # the shrunk JPEG's name
    assert "url" not in row.attachments[0]  # the thread shows 📎, not a dead image
    assert row.delivery_failed_reason
    assert audits[0]["entity_id"] == str(row.id)
    s.close()


@respx.mock
def test_refused_connection_says_nothing_was_sent(audits):
    # Phone.com never got the request, so "check before resending" would be
    # wrong advice — and there is no attempt to put in the trail.
    ce, te = _engines()
    tid = _configured(ce, te)
    respx.post(f"{BASE_URL}/accounts/1000000/messages").mock(
        side_effect=httpx.ConnectError("refused"))
    app, _, _ = _app(ce, te, tid)
    r = TestClient(app).post("/api/phone-com/messages", json={"to": "+13205550100", "body": "hi"})
    assert r.status_code == 502
    assert "nothing was sent" in r.json()["detail"]
    assert audits == []


@respx.mock
@pytest.mark.parametrize(
    ("upstream", "code", "said", "trail"),
    [
        # A 4xx is Phone.com refusing it: provably not on the customer's phone.
        (422, 502, None, []),
        # A 5xx may have sent it (core/link_sms.definitely_not_sent) — the same
        # call the scheduled and invoice/estimate senders make.
        (503, 504, "may have gone through", ["phone_com_sms_unconfirmed"]),
    ],
)
def test_upstream_status_decides_sent_or_unconfirmed(audits, upstream, code, said, trail):
    ce, te = _engines()
    tid = _configured(ce, te)
    route = respx.post(f"{BASE_URL}/accounts/1000000/messages").mock(
        return_value=httpx.Response(upstream, json={"error": "x"}))
    app, _, tsm = _app(ce, te, tid)
    r = TestClient(app).post("/api/phone-com/messages", json={"to": "+13205550100", "body": "hi"})
    assert r.status_code == code
    s = tsm()
    # Only a send that may have gone out is put in the thread (as "unknown").
    assert [m.delivery_status for m in s.query(PhoneComMessage).all()] == (["unknown"] if trail else [])
    s.close()
    if said:
        assert said in r.json()["detail"]
    assert route.call_count == 1  # a send is never re-POSTed on a 5xx
    assert [a["action"] for a in audits] == trail

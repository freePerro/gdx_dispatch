"""Every re-encoder in the tree must serve a phone photo upright.

A phone stores a portrait shot as LANDSCAPE pixels plus an EXIF Orientation
tag. Four places in this app re-encode an uploaded photo, and three of them
used to drop that tag without applying it — writing unrotated pixels with the
rotation instruction deleted. Nine job photos reached customers sideways on
invoice PDFs that way, while the same photos stood upright on the pay page
(the one surface that transposed first).

Each test here asserts BOTH halves of the contract in core/images.py:

  1. the pixels came out rotated (portrait in -> portrait out), and
  2. the output carries NO orientation tag.

Half two is the one that looks redundant and is not. ``exif_transpose`` bakes
the rotation into the pixels; if a later change also writes the original EXIF
back, every viewer (browsers default to ``image-orientation: from-image``,
WeasyPrint honors it since 57.0) rotates a SECOND time and the photo is
sideways again — with a test that still passes if it only checked size. The
tag assertion is what makes "let's preserve the metadata" fail loudly.
"""
from __future__ import annotations

import io
import uuid

import pytest

from gdx_dispatch.core.images import upright

Image = pytest.importorskip("PIL.Image")

#: EXIF tag 274 = Orientation. 6 = "rotate 90° CW to display", which is what
#: an iPhone writes for a portrait shot and what all nine of the photos that
#: went out rotated on prod invoices carried.
ORIENTATION_TAG = 274
LANDSCAPE_PIXELS = (64, 48)
UPRIGHT_DISPLAY = (48, 64)


def _portrait_phone_jpeg() -> bytes:
    """Landscape pixels + Orientation=6 — i.e. a portrait photo off a phone."""
    img = Image.new("RGB", LANDSCAPE_PIXELS, (200, 30, 30))
    exif = img.getexif()
    exif[ORIENTATION_TAG] = 6
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif.tobytes())
    return buf.getvalue()


def _assert_upright(data: bytes, label: str) -> None:
    out = Image.open(io.BytesIO(data))
    assert out.size == UPRIGHT_DISPLAY, (
        f"{label}: expected {UPRIGHT_DISPLAY} (portrait), got {out.size} — "
        "the EXIF rotation was not applied to the pixels"
    )
    assert out.getexif().get(ORIENTATION_TAG) in (None, 1), (
        f"{label}: output still carries an orientation tag "
        f"({out.getexif().get(ORIENTATION_TAG)}) — viewers will rotate it a "
        "SECOND time. Transpose must be followed by a save with no exif=."
    )


def test_upright_applies_rotation_and_drops_the_tag():
    img = upright(Image.open(io.BytesIO(_portrait_phone_jpeg())))
    assert img.size == UPRIGHT_DISPLAY
    assert img.getexif().get(ORIENTATION_TAG) in (None, 1)


def test_upright_is_a_no_op_without_exif():
    """No tag, no change — and no exception on an image that has no EXIF."""
    plain = Image.new("RGB", LANDSCAPE_PIXELS, (10, 10, 10))
    assert upright(plain).size == LANDSCAPE_PIXELS


def test_upright_survives_a_transpose_that_raises(monkeypatch):
    """A sideways photo is a defect; a 500 on the invoice PDF is a worse one.

    exif_transpose has a real history of raising on live-camera EXIF
    (Pillow #5580 / #4238 / #3973), so the caller must get an image back.
    """
    from PIL import ImageOps

    def _boom(_img):
        raise KeyError("orientation")

    monkeypatch.setattr(ImageOps, "exif_transpose", _boom)
    src = Image.open(io.BytesIO(_portrait_phone_jpeg()))
    assert upright(src).size == LANDSCAPE_PIXELS  # unrotated, but returned


def test_invoice_pdf_photo_is_upright(tmp_path):
    """routers/pdf._shrink_photo_for_pdf — the instance that reached customers."""
    from gdx_dispatch.routers.pdf import _shrink_photo_for_pdf

    src = tmp_path / "phone.jpg"
    src.write_bytes(_portrait_phone_jpeg())
    # Unique key: the real cache lives in the system tmp dir and persists
    # across runs, so a fixed key could serve a previous run's bytes.
    out = _shrink_photo_for_pdf(src, cache_key=f"test-{uuid.uuid4().hex}")
    _assert_upright(out.read_bytes(), "invoice PDF photo")


def test_office_photo_upload_is_upright():
    """routers/uploads._compress_image — the IRREVERSIBLE instance.

    This one re-encodes on the way IN, so a photo stored by the old code can
    never be straightened afterwards: the pixels are unrotated and the tag is
    already gone. The invoice-PDF bug was recoverable; this one was not.
    """
    from gdx_dispatch.routers.uploads import _compress_image

    data, content_type = _compress_image(_portrait_phone_jpeg(), "image/jpeg")
    assert content_type == "image/jpeg"
    _assert_upright(data, "office photo upload")


def test_door_listing_web_photo_is_upright():
    """modules/door_listings.compress_for_web — lands on the PUBLIC website."""
    from gdx_dispatch.modules.door_listings.service import compress_for_web

    data, _ = compress_for_web(_portrait_phone_jpeg(), "image/jpeg")
    _assert_upright(data, "door listing web photo")


def test_pay_page_data_uri_is_upright(tmp_path):
    """core/job_photos.photo_data_uri — the surface that was always right.

    Kept under test after being refactored onto the shared helper, so the
    refactor cannot quietly regress the one path that had this correct.
    """
    import base64

    from gdx_dispatch.core.job_photos import photo_data_uri

    src = tmp_path / "phone.jpg"
    src.write_bytes(_portrait_phone_jpeg())
    uri = photo_data_uri(str(src), "image/jpeg")
    assert uri and uri.startswith("data:image/jpeg;base64,")
    _assert_upright(base64.b64decode(uri.split(",", 1)[1]), "pay page data uri")


# ── Holes found by the adversarial review of this diff, closed here ──────────


def _big_portrait_phone_jpeg() -> bytes:
    """Large enough to trip the resize/thumbnail caps the small fixture skips.

    Every fixture above is 64x48 — under MAX_IMAGE_DIMENSION (2048) and under
    the PDF's 1200px thumbnail, so none of them ever exercised a resize on a
    rotated image. Real prod photos are 4000px; this is that path.
    """
    img = Image.new("RGB", (3000, 2000), (30, 120, 200))
    exif = img.getexif()
    exif[ORIENTATION_TAG] = 6
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif.tobytes())
    return buf.getvalue()


def test_large_photo_resizes_on_the_rotated_dimensions():
    """The cap must apply to what the viewer SEES, not to the sensor frame.

    3000x2000 tagged Orientation=6 displays as 2000x3000, so the 2048 cap has
    to bite on the 3000-tall axis. Getting this backwards would store a photo
    larger than the cap while looking like it obeyed it.
    """
    from gdx_dispatch.routers.uploads import MAX_IMAGE_DIMENSION, _compress_image

    data, _ = _compress_image(_big_portrait_phone_jpeg(), "image/jpeg")
    out = Image.open(io.BytesIO(data))
    assert out.height > out.width, f"expected portrait, got {out.size}"
    assert max(out.size) <= MAX_IMAGE_DIMENSION, f"{out.size} exceeds the cap"
    assert out.getexif().get(ORIENTATION_TAG) in (None, 1)


def test_large_photo_thumbnails_upright_for_the_pdf(tmp_path):
    from gdx_dispatch.routers.pdf import _shrink_photo_for_pdf

    src = tmp_path / "big.jpg"
    src.write_bytes(_big_portrait_phone_jpeg())
    out = _shrink_photo_for_pdf(src, cache_key=f"test-{uuid.uuid4().hex}")
    img = Image.open(out)
    assert img.height > img.width, f"expected portrait, got {img.size}"
    assert max(img.size) <= 1200


def test_pdf_photo_cache_salt_retires_pre_fix_bytes(tmp_path):
    """The cache key must not collide with what the OLD encoder wrote.

    _shrink_photo_for_pdf invalidates on the SOURCE mtime, which does not move
    when the encoder is fixed — so without the version salt a container that
    had already rendered an invoice would keep serving its sideways JPEG.
    Deleting _PDF_PHOTO_CACHE_VERSION must break this test.
    """
    import tempfile
    from pathlib import Path

    from gdx_dispatch.routers import pdf as pdf_mod

    src = tmp_path / "phone.jpg"
    src.write_bytes(_portrait_phone_jpeg())
    key = f"test-{uuid.uuid4().hex}"

    # Plant what the pre-fix code would have left behind, at the UNSALTED key,
    # and make it look newer than the source so the mtime check would serve it.
    cache_dir = Path(tempfile.gettempdir()) / "gdx_invoice_pdf_photos"
    cache_dir.mkdir(parents=True, exist_ok=True)
    stale = cache_dir / f"{key}.jpg"
    Image.new("RGB", LANDSCAPE_PIXELS, (0, 0, 0)).save(stale, format="JPEG")

    out = pdf_mod._shrink_photo_for_pdf(src, cache_key=key)
    assert out != stale, "served the pre-fix cache entry — the salt is missing"
    _assert_upright(out.read_bytes(), "pdf cache after salt bump")


def test_door_listing_upload_is_not_rejected_when_transpose_raises(monkeypatch):
    """compress_for_web fails CLOSED — so upright() must never raise into it.

    A raising transpose there would turn every door-listing upload into a 422
    (PhotoProcessingError), and no other test in this file touches a production
    caller's error path. This is the guard for that.
    """
    from PIL import ImageOps

    from gdx_dispatch.modules.door_listings.service import compress_for_web

    def _boom(_img):
        raise KeyError("orientation")

    monkeypatch.setattr(ImageOps, "exif_transpose", _boom)
    data, content_type = compress_for_web(_portrait_phone_jpeg(), "image/jpeg")
    assert content_type == "image/jpeg"
    assert Image.open(io.BytesIO(data)).size == LANDSCAPE_PIXELS  # unrotated, but STORED

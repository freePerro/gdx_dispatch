"""Turn any photo someone attaches into one Phone.com will carry as an MMS.

Phone.com takes the picture inline — ``media: [{data: <base64>, filename}]`` on
``POST /accounts/{voip_id}/messages`` — and documents the whole message as
"generally limited to 300KB (MMS 1.2)", 600KB under MMS 1.3, carriers free to
refuse (apidocs.phone.com Send Message, updated 2025-10-16). Measured on our
own number 2026-10-15: 97KB, 283KB and 521KB JPEGs all reached a Verizon phone
byte-for-byte, 1200x900 and 2000x1500. ``TARGET_BYTES`` sits under the largest
proven size, not at the documented floor — a sharper photo is the point.

What arrives is whatever a phone or desktop hands over: a 4MB iPhone HEIC, a
12MP Pixel JPEG lying on its side behind an EXIF tag, a PNG screenshot. Every
one leaves here as an upright JPEG with no EXIF (so no GPS from the customer's
driveway) under the target. Content type is sniffed by decoding, never taken
from the upload's header — Windows Chrome sends HEIC as an empty type.
"""
from __future__ import annotations

import io
import logging

log = logging.getLogger(__name__)

#: Ceiling on what we send. Proven on Verizon at 521KB; this leaves headroom
#: for the message text and for carriers stricter than the one we measured.
TARGET_BYTES = 450 * 1024

#: Longest side we bother sending. Phones show an MMS at screen width; 2000px
#: is sharp when the customer zooms and was proven delivered at full size.
MAX_SIDE = 2000

#: Refused before decoding. A phone photo is 2-12MB; this is a fat-finger and
#: abuse ceiling, not a quality one.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

#: Refused from the header, before any pixel is decoded. The upload cap counts
#: compressed bytes, and a flat-colour 13000x13000 PNG is 690KB that decodes
#: to 2GB (measured 2026-10-15). 64MP still takes the 48MP and 50MP modes of
#: current iPhones and Galaxies; a 108/200MP mode is refused with a reason.
MAX_PIXELS = 64_000_000

#: What Pillow may decode. MPO is the multi-picture JPEG some phones write.
_ALLOWED_FORMATS = {"JPEG", "MPO", "PNG", "WEBP", "HEIF", "GIF"}

_QUALITIES = (85, 78, 70, 62, 55, 48, 40)


class PhotoRejected(ValueError):
    """The upload is not a photo we can text. ``str()`` is user-facing."""


def _register_heif() -> None:
    # Same optional-codec pattern as core/job_photos.py: an iPhone photo is the
    # common case here, so absence is logged, not swallowed silently.
    try:
        from pillow_heif import register_heif_opener

        register_heif_opener()
    except Exception:  # noqa: BLE001 — optional codec; HEIC then fails as "not a photo"
        log.warning("pillow_heif_unavailable — HEIC photos cannot be texted")


def shrink_for_mms(raw: bytes) -> bytes:
    """``raw`` re-encoded as an upright, EXIF-free JPEG of at most
    ``TARGET_BYTES``. Raises :class:`PhotoRejected` for anything that is not a
    decodable photo of an allowed format, or that cannot be brought under the
    target. An animated GIF sends its first frame."""
    from PIL import Image

    from gdx_dispatch.core.images import upright

    if not raw:
        raise PhotoRejected("The photo is empty.")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise PhotoRejected("That photo is over 25MB.")
    _register_heif()
    try:
        img = Image.open(io.BytesIO(raw))
        fmt = (img.format or "").upper()
        if fmt not in _ALLOWED_FORMATS:
            raise PhotoRejected("That file type can't be texted — send a JPEG, PNG, WebP, HEIC or GIF photo.")
        if img.width * img.height > MAX_PIXELS:
            raise PhotoRejected("That photo is too large to text — use the camera's normal size.")
        if fmt in ("JPEG", "MPO"):
            # Decode at a reduced scale when the JPEG is far larger than we
            # send — a 12MP photo decodes in a quarter of the memory.
            img.draft("RGB", (MAX_SIDE, MAX_SIDE))
        # Down to sending size first, so the rotate and the colour conversion
        # below copy a 2000px frame, not the full decode. The box is square,
        # so shrinking before the EXIF rotation lands on the same size.
        img.thumbnail((MAX_SIDE, MAX_SIDE))
        img = upright(img)
        if img.mode in ("RGBA", "LA", "P"):
            # Flatten transparency onto white; a JPEG has no alpha and the
            # default conversion paints it black.
            rgba = img.convert("RGBA")
            flat = Image.new("RGB", rgba.size, (255, 255, 255))
            flat.paste(rgba, mask=rgba.getchannel("A"))
            img = flat
        else:
            img = img.convert("RGB")
    except PhotoRejected:
        raise
    except Image.DecompressionBombError as exc:
        raise PhotoRejected("That image is too large to text.") from exc
    except Exception as exc:  # noqa: BLE001 — any decode failure is "not a photo"
        raise PhotoRejected("That file isn't a photo we can read.") from exc

    side = MAX_SIDE
    while side >= 480:
        frame = img.copy()
        frame.thumbnail((side, side))
        for q in _QUALITIES:
            buf = io.BytesIO()
            # No exif= — upright() baked the rotation in, and dropping EXIF
            # strips the photo's GPS position too (see core/images.py).
            frame.save(buf, format="JPEG", quality=q, optimize=True)
            if buf.tell() <= TARGET_BYTES:
                return buf.getvalue()
        side = int(side * 0.75)
    raise PhotoRejected("That photo can't be made small enough to text.")

"""Orientation-correct image handling, for every place we re-encode a photo.

A phone does not rotate the pixels it captures. It stores the sensor's raw
frame and writes an EXIF ``Orientation`` tag saying how a viewer must turn it.
Every viewer we ship to honors that tag — browsers default to
``image-orientation: from-image``, and WeasyPrint has applied it since 57.0
(prod runs 69.0). So the bytes on disk are sideways and everything renders
them upright, and that works right up until something re-encodes the file.

Pillow's ``save()`` drops EXIF unless you hand it back explicitly. So a
resize/thumbnail pass writes out the *unrotated* pixels with the instruction
deleted: nothing downstream can recover the rotation, because nothing
downstream is told there was any. That is how nine job photos reached
customers sideways on invoice PDFs while the same photos stood upright on the
public pay page — the pay page was the one surface that called
``exif_transpose`` first.

THE CONTRACT, and it has two halves:

1. Call :func:`upright` immediately after ``Image.open``, before measuring
   ``.size``, cropping, resizing or saving.
2. Then save WITHOUT passing ``exif=``.

Half two is not optional tidiness. :func:`upright` bakes the rotation into the
pixels; if you also write the original EXIF back, the tag still says "rotate
90°" and every viewer rotates a second time. Measured on prod, WeasyPrint
69.0, a 400x100 image tagged Orientation=6::

    target (correct display)   : (100, 400)
    transpose + drop exif      : (100, 400)   <- correct
    transpose + PRESERVE exif  : (400, 100)   <- DOUBLE ROTATION

This is also the industry contract, not a local invention: imgproxy documents
that it auto-rotates on EXIF and "the orientation tag will be removed from the
image in all cases".
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from PIL.Image import Image

log = logging.getLogger(__name__)

#: EXIF tag 0x0112 — Orientation. 1 means "already upright, nothing to do".
_ORIENTATION_TAG = 0x0112


def upright(img: Image) -> Image:
    """``img`` with EXIF Orientation applied to the pixels and the tag removed.

    Returns the original image unchanged when Pillow is missing, when there is
    no orientation to apply, or when the transpose itself fails — a photo that
    renders sideways is a defect, but a photo that 500s the invoice PDF is a
    worse one.

    The defensive shape is deliberate. ``ImageOps.exif_transpose`` has a real
    history of raising on live-camera EXIF (Pillow #5580 KeyError while
    removing the orientation tag, #4238 TypeError, #3973) and of returning
    something unexpected (#5527). ``or img`` covers the ``None`` case; the
    ``except`` covers the raising ones. Do not "clean this up" into a bare
    ``img = ImageOps.exif_transpose(img)``.
    """
    try:
        from PIL import ImageOps
    except Exception:  # noqa: BLE001 — Pillow absent is already handled by callers
        return img

    # Short-circuit when there is nothing to apply. exif_transpose ALWAYS
    # returns a new object — `image.transpose(...)` when rotating, plain
    # `image.copy()` otherwise — so without this every upload would peak at two
    # fully decoded frames (~54 MB for a 4000px photo) on the request thread to
    # accomplish nothing. Most images have no orientation tag at all.
    try:
        # Absent tag defaults to 1 — nothing to do, and no copy made.
        orientation = img.getexif().get(_ORIENTATION_TAG, 1)
    except Exception:  # noqa: BLE001 — unreadable EXIF is Pillow's call, not ours
        log.debug("orientation_probe_failed — deferring to exif_transpose", exc_info=True)
        orientation = None  # None means "could not read": fall through, don't skip
    if orientation == 1:
        return img

    try:
        return ImageOps.exif_transpose(img) or img
    except Exception:  # noqa: BLE001 — see docstring; never fail the caller
        log.warning("exif_transpose_failed — serving unrotated pixels", exc_info=True)
        return img

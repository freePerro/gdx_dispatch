"""Serving uploaded bytes back without letting them run as part of the app.

An upload's content type is whatever the client said it was. Served back
under that type, an uploaded ``text/html`` or ``image/svg+xml`` file is a
document of the app's own origin: opened as a blob tab or framed, its script
runs with the session's reach (GDXA-971/972). CSP is Report-Only, so nothing
else stands in the way.

So the stored type is honoured only for types a browser renders inertly. The
rest are served as ``application/octet-stream`` with an ``attachment``
disposition: a blob made from that response carries the octet-stream type
too, so even a caller that opens it in a tab gets a download, not a page.
``nosniff`` on every response stops a browser second-guessing either answer.
"""

from __future__ import annotations

import os
import re

from fastapi.responses import FileResponse

#: What a caller may call a *photo*. Raster formats a phone camera or a photo
#: library hands over, including the HEIC an iPhone library pick produces; a
#: 415 is permanent to the photo queue, so a real camera format refused here
#: is a photo lost. Not svg: a vector "photo" is markup with script in reach.
RASTER_PHOTO_TYPES = frozenset({
    "image/jpeg",
    "image/jpg",
    "image/pjpeg",
    "image/png",
    "image/gif",
    "image/webp",
    "image/heic",
    "image/heif",
    "image/avif",
    "image/bmp",
    "image/tiff",
})

#: Rendered inline (preview pane, image tag, media player): PDF, every raster
#: type accepted as a photo (a photo the upload takes and the strip cannot
#: show is a photo lost too), audio and video. Never svg, html or any xml:
#: those are documents that can carry script, whatever the extension says.
INLINE_SAFE_TYPES = frozenset({"application/pdf"}) | RASTER_PHOTO_TYPES
INLINE_SAFE_PREFIXES = ("audio/", "video/")

#: Kept as stored but always an attachment: inert text a browser shows as
#: text, never as markup.
ATTACHMENT_KEEP_TYPES = frozenset({"text/plain", "text/csv"})


#: Spellings real clients send for a type on the lists above.
_ALIASES = {
    "application/x-pdf": "application/pdf",
    "image/x-png": "image/png",
}

#: One bare `type/subtype`, nothing else. The uploader writes the stored
#: string, and a browser reads `video/mp4, text/html` as text/html (the LAST
#: type of a comma list wins), so a string that is not exactly one token is no
#: type at all. Media subtypes naming xml are markup however they are filed.
_ONE_TYPE = re.compile(r"[a-z0-9][a-z0-9.+-]*/[a-z0-9][a-z0-9.+-]*")


def base_type(content_type: str | None) -> str:
    """``"Text/HTML; charset=utf-8"`` -> ``"text/html"``; ``""`` for anything
    that is not exactly one well-formed type."""
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    if not _ONE_TYPE.fullmatch(ct):
        return ""
    return _ALIASES.get(ct, ct)


def is_raster_photo_type(content_type: str | None) -> bool:
    return base_type(content_type) in RASTER_PHOTO_TYPES


def is_inline_safe_type(content_type: str | None) -> bool:
    ct = base_type(content_type)
    if ct in INLINE_SAFE_TYPES:
        return True
    return ct.startswith(INLINE_SAFE_PREFIXES) and "xml" not in ct


def stored_file_response(
    path: str | os.PathLike[str],
    content_type: str | None,
    filename: str | None,
) -> FileResponse:
    """A FileResponse for user-uploaded bytes that cannot run as the app."""
    ct = base_type(content_type)
    if is_inline_safe_type(ct):
        media_type, disposition = ct, "inline"
    elif ct in ATTACHMENT_KEEP_TYPES:
        media_type, disposition = ct, "attachment"
    else:
        media_type, disposition = "application/octet-stream", "attachment"
    headers = {"X-Content-Type-Options": "nosniff"}
    if not filename:
        # FileResponse only writes Content-Disposition when it has a filename;
        # an attachment must say so either way.
        headers["Content-Disposition"] = disposition
    return FileResponse(
        path=path,
        media_type=media_type,
        filename=filename or None,
        content_disposition_type=disposition,
        headers=headers,
    )

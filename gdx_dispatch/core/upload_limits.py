"""One ceiling for uploads, and an honest account of what it buys.

Seven handlers read an uploaded body. Until 2026-08-26 exactly zero of them
had an application ceiling; the only bound was nginx `client_max_body_size`,
50M on the prod vhost. `routers/documents.py` got one first — this module is
that guard, lifted out so the other six share it instead of each growing a
slightly different copy.

WHAT THIS DOES NOT DO. By the time any handler runs, FastAPI has already
awaited `request.form()` and Starlette has received the whole body and spooled
it into a `SpooledTemporaryFile`, rolled to disk past 1 MB. Measured on this
stack (starlette 1.6.0 / fastapi 0.141.1) at handler entry for a 30 MB post::

    type = SpooledTemporaryFile   _rolled = True
    on_disk_bytes = 31457280      UploadFile.size = 31457280

So no handler can refuse an upload "as it arrives". Only nginx can, and that
is infra, not this repo. What this prevents is the handler then pulling all of
it into process memory and persisting it.

WHY `.size` AND NOT A CHUNKED READ. A chunked read that accumulates and joins
holds the chunk list AND the joined result — measured at 50 MB peak for a
25 MB body, against 25 MB for a plain read. The "safer" loop doubles the cost
of every legitimate upload, and in an `async def` it turns one blocking disk
read into many with no await in between. Starlette has already computed the
size; asking is free.
"""
from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fastapi import UploadFile

log = logging.getLogger(__name__)

#: Document-scale ceiling, shared by every upload route. Matches
#: MAX_DOCUMENT_BYTES in routers/uploads.py. Deliberately NOT that module's
#: tighter 10 MB photo cap: real tech photos in production already reach 10 MB
#: exactly, and an upload refused in a customer's driveway is worse than a
#: large one stored.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024


def assert_upload_within_limit(file: UploadFile, max_bytes: int = MAX_UPLOAD_BYTES) -> None:
    """Raise 413 if ``file`` is larger than ``max_bytes``. O(1), allocates nothing.

    Call it BEFORE reading the body. Unmeasurable size is let through rather
    than rejected blind — a guard that fails closed on its own uncertainty
    would turn an unreadable `size` into an outage.
    """
    from fastapi import HTTPException  # noqa: PLC0415 - keep import cost off module load

    size = getattr(file, "size", None)
    if size is None:
        try:
            pos = file.file.tell()
            size = file.file.seek(0, os.SEEK_END)
            file.file.seek(pos)
        except (OSError, AttributeError):
            log.debug("upload_size_unmeasurable — allowing through")
            return
    if size > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds the {max_bytes // (1024 * 1024)}MB limit",
        )


def assert_body_within_limit(request, max_bytes: int = MAX_UPLOAD_BYTES) -> None:
    """413 if the request's declared ``Content-Length`` exceeds ``max_bytes``.

    For endpoints that read a raw body (``await request.json()``) rather than
    an ``UploadFile``. Weaker than :func:`assert_upload_within_limit`, and the
    weakness is worth stating: ``Content-Length`` is client-supplied, absent on
    a chunked request, and a liar can omit it. It is not a security boundary —
    nginx's ``client_max_body_size`` is the only thing that refuses bytes
    before they arrive.

    What it does buy: the honest oversized case — a 60 MB JSON import posted by
    someone's script — is refused before ``request.json()`` materialises it and
    a parser walks it. A missing or unparseable header falls through rather
    than rejecting blind, for the same reason the sibling does.
    """
    from fastapi import HTTPException  # noqa: PLC0415

    raw = None
    try:
        raw = request.headers.get("content-length")
    except AttributeError:
        return
    if raw is None:
        return
    try:
        declared = int(raw)
    except (TypeError, ValueError):
        return
    if declared > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"Request body exceeds the {max_bytes // (1024 * 1024)}MB limit",
        )

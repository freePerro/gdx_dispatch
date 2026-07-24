"""The branding-logo contract, shared by its three consumers.

One tenant logo, three readers with incompatible URL needs:

  - the upload route  (``routers/settings.py``)      mints the file + URL
  - the serve route   (``routers/branding_public.py``) streams it to ``<img>``
  - the PDF renderer  (``core/pdf_generator.py``)      needs a filesystem path —
    WeasyPrint renders with ``base_url=<templates dir>`` and cannot resolve an
    app-relative ``/api/...`` URL, so PDFs must read the stored file directly.

Keeping the filename pattern + path fencing here means the three can't drift:
the serve route will only ever expose files the upload route minted, and the
PDF resolver finds exactly those files.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

# Filenames minted by upload_branding_logo — and the ONLY names the public
# serving route will read. The uuid4-hex segment means nothing outside the
# upload route can collide with or address these files.
BRANDING_LOGO_RE = re.compile(r"^branding-logo-[0-9a-f]{32}\.(png|jpg)$")
LOGO_URL_PREFIX = "/api/settings/branding/logo/"


def branding_logo_file(filename: str) -> Path | None:
    """Resolve a minted logo filename to its path in the flat upload dir.

    Returns None unless the name matches the minted pattern AND resolves
    inside the upload root (realpath + startswith — the fence form CodeQL's
    py/path-injection recognizes as a barrier).
    """
    if not BRANDING_LOGO_RE.match(filename or ""):
        return None
    base = os.path.realpath(os.getenv("UPLOAD_DIR", "/app/uploads"))
    candidate = os.path.realpath(os.path.join(base, filename))
    if not candidate.startswith(base + os.sep):
        return None
    return Path(candidate)


def resolve_logo_for_pdf(logo: str) -> str:
    """Translate a stored branding.logo value into something WeasyPrint can load.

    - App-relative minted URL (``/api/settings/branding/logo/<file>``) →
      ``file://`` URI of the stored bytes, or "" if the file is gone.
    - Anything else (absolute https URL an admin PATCHed in, or empty) passes
      through unchanged — WeasyPrint fetches absolute URLs itself.
    """
    logo = (logo or "").strip()
    if not logo.startswith(LOGO_URL_PREFIX):
        return logo
    path = branding_logo_file(logo.rsplit("/", 1)[-1])
    if path is not None and path.is_file():
        return path.as_uri()
    return ""

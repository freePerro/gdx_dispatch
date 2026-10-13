"""Which release is this process running?

Two sources, in order:

1. ``APP_VERSION`` — what the operator pinned in compose (``1.145.0``). The
   one-click and selfhost compose files default it to ``latest`` and the dev
   compose to ``dev``; neither names a release.
2. ``GDX_IMAGE_VERSION`` — baked into the image by the Release workflow
   (``Dockerfile`` ARG → ENV), so an install that tracks ``:latest`` still
   knows which release it pulled.

Only a semver string counts as a release. ``installed_version()`` returns
``None`` for a dev build, so nothing (release notes, the update check) treats
``latest`` or a git SHA as a version.
"""
from __future__ import annotations

import os
import re

_SEMVER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def parse_semver(value: str | None) -> tuple[int, int, int] | None:
    """``"1.2.3"`` / ``"v1.2.3"`` → ``(1, 2, 3)``; anything else → ``None``."""
    m = _SEMVER_RE.match((value or "").strip())
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def installed_version() -> str | None:
    """The running release as ``X.Y.Z``, or ``None`` on a dev / unversioned build."""
    for var in ("APP_VERSION", "GDX_IMAGE_VERSION"):
        parts = parse_semver(os.getenv(var))
        if parts:
            return ".".join(map(str, parts))
    return None


def display_version() -> str:
    """What to show a person: the release if known, else ``APP_VERSION`` as set, else ``dev``."""
    return installed_version() or os.getenv("APP_VERSION") or "dev"

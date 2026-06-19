"""SS-25 Slice A — API version resolver.

Parses the ``Accept`` header per the GDX vendor media type:

    Accept: application/vnd.gdx.v<N>+json

Where ``<N>`` is a positive integer. On match the caller gets back the
integer version. If the header is missing, or the media type is not a
``vnd.gdx`` vendor type at all, we fall back to the latest supported
version — that is the friendly, common-case path.

However, if the caller EXPLICITLY names a GDX vendor media type but it
is malformed (``vfoo``, ``v``, ``v0``) or targets an unsupported version
(``v9999``), we refuse: that is a contract violation, not a gentle
preference. The resolver raises :class:`APIVersionError` and the caller
(the middleware) converts it into a 400 response.

Also exposes helpers to render the RFC 8594 ``Sunset`` header (an
HTTP-date per RFC 7231) and the ``Deprecation`` header (IMF-fixdate or
the literal boolean string ``true``; see
draft-ietf-httpapi-deprecation-header).

This module is integration-independent: it does not touch FastAPI's
application singleton. See ``gdx_dispatch/core/middleware/api_versioning.py`` for
the Starlette middleware that wires this resolver into the request
lifecycle.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import format_datetime
from typing import Iterable

# The supported major versions, newest last. When you add v2 you extend
# this tuple; the resolver falls back to ``SUPPORTED_VERSIONS[-1]`` when
# the client does not name a version.
SUPPORTED_VERSIONS: tuple[int, ...] = (1,)

# Accept header pattern for our vendor media type. Case-insensitive by
# use of ``re.IGNORECASE``. We intentionally require the ``+json`` suffix
# because every public GDX response is JSON — clients that want anything
# else today are almost certainly wrong.
_VENDOR_RE = re.compile(
    r"application/vnd\.gdx\.v(?P<version>[0-9A-Za-z_-]+)\+json",
    re.IGNORECASE,
)

# Matches anything that *looks* like our vendor type, even if the version
# token is unparseable. We use this to decide "did the caller try to name
# a GDX version at all?" which drives the strict-vs-fallback decision.
_VENDOR_PREFIX_RE = re.compile(r"application/vnd\.gdx\.", re.IGNORECASE)


class APIVersionError(ValueError):
    """Raised when the client named a GDX vendor type we cannot satisfy.

    The middleware converts this into ``HTTP 400`` with the message in
    the ``detail`` field. Never silently fall back — the client asked
    for something specific and wrong, they need to know.
    """


@dataclass(frozen=True)
class ResolvedVersion:
    """Result of resolving an Accept header.

    Attributes
    ----------
    version:
        The integer major version (e.g. ``1``) the request resolves to.
    explicit:
        ``True`` if the caller named a vendor type, ``False`` if we
        fell back to the latest. Handy for metrics / deprecation
        messaging — we only nag callers who opted in to a version.
    """

    version: int
    explicit: bool


def latest_version() -> int:
    """The current default version clients get when they don't specify."""
    return SUPPORTED_VERSIONS[-1]


def resolve_version(accept_header: str | None) -> ResolvedVersion:
    """Parse an ``Accept`` header and resolve the GDX API version.

    Rules:

    * ``None`` / empty / ``*/*`` / any non-GDX media type → fall back to
      :func:`latest_version`.
    * ``application/vnd.gdx.v<int>+json`` with ``<int>`` in
      :data:`SUPPORTED_VERSIONS` → that version, ``explicit=True``.
    * ``application/vnd.gdx.<garbage>`` (malformed or unsupported
      integer) → :class:`APIVersionError`. The caller opted in to GDX
      versioning and got it wrong; we refuse to guess.
    """
    if not accept_header:
        return ResolvedVersion(version=latest_version(), explicit=False)

    # Accept is a comma-separated list of media ranges. Walk them all;
    # the first matching vendor type wins. We keep track of whether we
    # saw a vendor-shaped prefix so we can tell "no GDX opt-in" apart
    # from "GDX opt-in but malformed".
    saw_vendor_prefix = False
    for raw_entry in accept_header.split(","):
        entry = raw_entry.strip().split(";", 1)[0].strip()  # drop q= etc.
        if not entry:
            continue

        if _VENDOR_PREFIX_RE.match(entry):
            saw_vendor_prefix = True

        match = _VENDOR_RE.fullmatch(entry)
        if not match:
            continue

        version_token = match.group("version")
        version_int = _parse_version_token(version_token)
        if version_int is None:
            # e.g. application/vnd.gdx.vfoo+json
            raise APIVersionError(
                f"unparseable GDX API version in Accept header: {version_token!r}"
            )
        if version_int not in SUPPORTED_VERSIONS:
            raise APIVersionError(
                f"unsupported GDX API version: v{version_int} "
                f"(supported: {_format_supported(SUPPORTED_VERSIONS)})"
            )
        return ResolvedVersion(version=version_int, explicit=True)

    if saw_vendor_prefix:
        # Caller referenced vnd.gdx but not in the +json form we accept.
        # e.g. application/vnd.gdx.v1+xml — don't silently give them JSON.
        raise APIVersionError(
            "GDX vendor media type must use +json suffix "
            "(expected application/vnd.gdx.v<N>+json)"
        )

    return ResolvedVersion(version=latest_version(), explicit=False)


def _parse_version_token(token: str) -> int | None:
    """Return a positive int or ``None`` for unparseable tokens."""
    if not token or not token.isdigit():
        return None
    value = int(token)
    if value < 1:
        return None
    return value


def _format_supported(versions: Iterable[int]) -> str:
    return ", ".join(f"v{v}" for v in versions)


def format_sunset_header(sunset_at: datetime) -> str:
    """Render a ``Sunset`` header value (RFC 8594 → RFC 7231 HTTP-date).

    RFC 7231 HTTP-date is IMF-fixdate in GMT, e.g.
    ``Sun, 06 Nov 1994 08:49:37 GMT``. We accept any timezone-aware
    datetime and coerce to UTC; naive datetimes are rejected because
    Sunset is a customer-facing contract and ambiguity is not safe.
    """
    if sunset_at.tzinfo is None:
        raise ValueError("sunset_at must be timezone-aware")
    as_utc = sunset_at.astimezone(timezone.utc)
    # format_datetime with usegmt=True yields "... GMT" per RFC 7231.
    return format_datetime(as_utc, usegmt=True)


def format_deprecation_header(deprecated_at: datetime | None) -> str:
    """Render a ``Deprecation`` header value.

    Per draft-ietf-httpapi-deprecation-header the value is either:

    * An IMF-fixdate naming the moment deprecation took effect, or
    * The boolean token ``true`` when we don't want to publish the
      effective date.

    We prefer the dated form when we have it.
    """
    if deprecated_at is None:
        return "true"
    if deprecated_at.tzinfo is None:
        raise ValueError("deprecated_at must be timezone-aware")
    as_utc = deprecated_at.astimezone(timezone.utc)
    return format_datetime(as_utc, usegmt=True)

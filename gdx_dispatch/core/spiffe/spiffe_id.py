"""SS-32 slice A — SPIFFE ID parser + validator.

A SPIFFE ID is a URI of the form ``spiffe://<trust-domain>/<path>``
where the trust domain is a DNS-like lowercase name and the path is a
sequence of ``/``-separated URL-safe segments. Per the SPIFFE ID spec
(v1.0) and the stricter platform rules captured in the SS-32 prompt:

* scheme MUST be ``spiffe`` (lowercase, no other schemes accepted)
* trust domain MUST match ``[a-z0-9.-]+`` — no uppercase, no empty
* path MUST be a sequence of at least one segment when present, each
  segment composed of URL-safe characters
  (``A-Z a-z 0-9 . _ ~ ! $ & ' ( ) * + , ; = : @ % -``)
* no trailing ``/`` allowed (``spiffe://td/`` is rejected)
* ``.`` and ``..`` path segments are rejected
* bare ``spiffe://`` with no trust domain is rejected

The ``parse_spiffe_id`` helper returns a :class:`SpiffeID` dataclass; on
invalid input it raises :class:`SpiffeIdError` with a human-readable
reason. ``is_valid_spiffe_id`` is the cheap boolean form.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional


class SpiffeIdError(ValueError):
    """Raised when a SPIFFE ID fails strict validation."""


# Trust-domain charset: lowercase DNS-ish per SPIFFE spec (RFC 8032 Sec 2).
_TRUST_DOMAIN_RE = re.compile(r"^[a-z0-9.-]+$")

# URL-safe path-segment charset (unreserved + sub-delims + pct-encoded + ':' + '@').
_PATH_SEG_RE = re.compile(
    r"^(?:[A-Za-z0-9._~!$&'()*+,;=:@-]|%[0-9A-Fa-f]{2})+$"
)

# Whole-ID gatekeeper regex — the prompt's authoritative form. We still
# decompose afterwards because segment-level rules (``..``, empty) can't
# be expressed cleanly in a single regex.
FULL_RE = re.compile(
    r"^spiffe://[a-z0-9.-]+(/[A-Za-z0-9._~!$&'()*+,;=:@%-]+)*$"
)


@dataclass(frozen=True)
class SpiffeID:
    """A parsed SPIFFE ID.

    ``trust_domain`` is the authority (no scheme, no leading ``//``).
    ``path`` is the joined path INCLUDING the leading ``/`` (or empty
    string when the ID has no path component). ``segments`` is the list
    of decoded path segments for glob-matching convenience.
    """

    trust_domain: str
    path: str
    segments: List[str]

    @property
    def uri(self) -> str:
        return f"spiffe://{self.trust_domain}{self.path}"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.uri


def parse_spiffe_id(raw: object) -> SpiffeID:
    """Parse ``raw`` as a SPIFFE ID or raise :class:`SpiffeIdError`.

    Strict — rejects anything the platform considers ambiguous so that
    capability-map globs never match on a malformed ID.
    """
    if not isinstance(raw, str):
        raise SpiffeIdError("spiffe id must be a string")
    if not raw:
        raise SpiffeIdError("spiffe id is empty")
    if "\x00" in raw or "\n" in raw or "\r" in raw or " " in raw:
        raise SpiffeIdError("spiffe id contains whitespace or control chars")
    if not raw.startswith("spiffe://"):
        raise SpiffeIdError("spiffe id must start with 'spiffe://'")

    remainder = raw[len("spiffe://") :]
    if not remainder:
        raise SpiffeIdError("spiffe id has empty trust domain")
    if remainder.endswith("/"):
        raise SpiffeIdError("spiffe id must not have trailing '/'")

    # Split into trust domain + path.
    if "/" in remainder:
        td, rest = remainder.split("/", 1)
        path_raw = "/" + rest
    else:
        td, path_raw = remainder, ""

    if not td:
        raise SpiffeIdError("spiffe id has empty trust domain")
    if not _TRUST_DOMAIN_RE.match(td):
        raise SpiffeIdError(
            f"invalid trust domain '{td}' — must match [a-z0-9.-]+"
        )
    if td != td.lower():
        # Defensive: regex already enforces lowercase, but keep the
        # explicit check so the error message is clearer.
        raise SpiffeIdError("trust domain must be lowercase")

    segments: List[str] = []
    if path_raw:
        # path_raw starts with '/'. Split and discard the leading empty.
        parts = path_raw.split("/")
        if parts[0] != "":
            # Defensive; path_raw starts with '/'.
            raise SpiffeIdError("invalid path format")
        for seg in parts[1:]:
            if not seg:
                raise SpiffeIdError("empty path segment")
            if seg in (".", ".."):
                raise SpiffeIdError(
                    f"reserved path segment '{seg}' not allowed"
                )
            if not _PATH_SEG_RE.match(seg):
                raise SpiffeIdError(
                    f"invalid character in path segment '{seg}'"
                )
            segments.append(seg)

    return SpiffeID(trust_domain=td, path=path_raw, segments=segments)


def is_valid_spiffe_id(raw: object) -> bool:
    """Cheap boolean form of :func:`parse_spiffe_id`."""
    try:
        parse_spiffe_id(raw)
    except SpiffeIdError:
        return False
    return True


def try_parse_spiffe_id(raw: object) -> Optional[SpiffeID]:
    """Return parsed ID or ``None`` — never raises."""
    try:
        return parse_spiffe_id(raw)
    except SpiffeIdError:
        return None

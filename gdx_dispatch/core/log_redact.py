"""PII redaction helpers for log output.

Callers use these instead of passing raw PII into log messages. The redacted
form is deterministic-per-value (same input → same hash) so investigators can
still correlate "the user whose email hashes to X hit this error at T1 and T2"
without the raw email ever reaching the log archive.

Design per SS-2 P7 + SS-3 P11 sensitivity classification:
    provider_email, identity.email, developer_accounts.contact_email
        → redact_email()   yields `email:<sha256[:12]>` + optional domain hint
    identity_providers.metadata, sso_configs.provider_metadata,
    installations.config, meter_events.dimensions
        → redact_jsonb()   walks the dict; hashes any value whose key looks
                           like a secret or PII (per _SENSITIVE_KEY_RE).

Why a project-specific redactor instead of a generic library:
    - We need to encode the sensitivity taxonomy defined in the platform SSes
      (not just "hash things that look like emails"). The rules come from
      known shapes of gdx data, not from generic PII detectors.
    - We want the hashes to be stable across processes so correlation works —
      that means a single per-process secret salt, NOT a per-call random.
    - We want a callable API that lives with the code, not a sidecar service.

Salt rotation: rotate `GDX_LOG_REDACT_SALT` in .env. All prior correlation
IDs become non-correlatable after rotation — intended when a log leak is
suspected and we want past hashes to stop matching future hashes.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterable
from typing import Any

_SALT = (os.environ.get("GDX_LOG_REDACT_SALT") or "gdx-log-redact-v1").encode("utf-8")

# Keys in a JSONB dict whose VALUES we should redact. These match the
# sensitivity taxonomy from SS-2 P7 and SS-3 P11. Additional tenant-specific
# keys can be registered via register_sensitive_key() at import time.
_SENSITIVE_KEY_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"email", re.IGNORECASE),
    re.compile(r"phone", re.IGNORECASE),
    re.compile(r"ssn", re.IGNORECASE),
    re.compile(r"tax[_-]?id", re.IGNORECASE),
    re.compile(r"(?:api[_-]?key|secret|token|password|credential)", re.IGNORECASE),
    re.compile(r"signing[_-]?cert", re.IGNORECASE),
    re.compile(r"private[_-]?key", re.IGNORECASE),
    re.compile(r"refresh[_-]?token", re.IGNORECASE),
    re.compile(r"access[_-]?token", re.IGNORECASE),
    re.compile(r"authorization", re.IGNORECASE),
]

# Values (regardless of key) that LOOK like secrets get redacted too.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_JWT_RE = re.compile(r"^eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*$")
_BEARER_RE = re.compile(r"^(?:Bearer\s+|sk-|pat_|svc_|ghp_)[A-Za-z0-9_.-]{20,}$", re.IGNORECASE)


def register_sensitive_key(pattern: str) -> None:
    """Add a project-specific key pattern to the redaction set.

    Example:
        from gdx_dispatch.core.log_redact import register_sensitive_key
        register_sensitive_key(r"vpn[_-]?password")
    """
    _SENSITIVE_KEY_PATTERNS.append(re.compile(pattern, re.IGNORECASE))


def _hash(value: Any) -> str:
    """Stable hash of a scalar value — HMAC-style with the module salt."""
    raw = f"{value}".encode()
    return hashlib.blake2b(raw, key=_SALT, digest_size=9).hexdigest()


def redact_email(value: str | None) -> str:
    """Return a correlation-safe token for an email, preserving domain hint.

    >>> redact_email("alice@example.com")     # doctest: +ELLIPSIS
    'email:...:@example.com'
    >>> redact_email(None)
    'email:<none>'
    """
    if value is None:
        return "email:<none>"
    value = str(value).strip()
    if not value:
        return "email:<empty>"
    if "@" in value:
        local, _, domain = value.partition("@")
        return f"email:{_hash(local.lower())}:@{domain.lower()}"
    return f"email:{_hash(value.lower())}"


def redact_scalar(value: Any) -> Any:
    """Return a redacted form for any scalar that LOOKS sensitive, else value unchanged.

    Non-string scalars pass through (numbers, bools, None) — they don't
    carry PII by shape. Strings matching the email/JWT/bearer patterns are
    redacted.
    """
    if not isinstance(value, str):
        return value
    v = value.strip()
    if _EMAIL_RE.match(v):
        return redact_email(v)
    if _JWT_RE.match(v):
        return f"jwt:{_hash(v)}"
    if _BEARER_RE.match(v):
        return f"token:{_hash(v)}"
    return value


def _is_sensitive_key(key: str) -> bool:
    return any(p.search(key) for p in _SENSITIVE_KEY_PATTERNS)


def redact_jsonb(data: Any) -> Any:
    """Recursively redact a JSON-ish structure.

    Rules:
        - dict: for each key, if key name looks sensitive → hash the value;
          else recurse into the value.
        - list/tuple: recurse into each element (preserve container type).
        - scalar: shape-based redaction via redact_scalar().

    Never mutates the input; always returns a fresh object.
    """
    if isinstance(data, dict):
        out: dict[Any, Any] = {}
        for k, v in data.items():
            if isinstance(k, str) and _is_sensitive_key(k):
                if isinstance(v, (dict, list)):
                    out[k] = f"redacted:{_hash(repr(v))}"
                else:
                    out[k] = f"redacted:{_hash(v)}"
            else:
                out[k] = redact_jsonb(v)
        return out
    if isinstance(data, list):
        return [redact_jsonb(x) for x in data]
    if isinstance(data, tuple):
        return tuple(redact_jsonb(x) for x in data)
    return redact_scalar(data)


def redact_many(values: Iterable[Any]) -> list[Any]:
    """Bulk helper for iterables of values (log-aggregation use cases)."""
    return [redact_scalar(v) for v in values]

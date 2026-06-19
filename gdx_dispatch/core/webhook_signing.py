"""
gdx_dispatch/core/webhook_signing.py — SS-21 outbound webhook HMAC-SHA256 signing.

Stripe-pattern dual-active signing:
    During a rotation window, BOTH the old and new secret produce v1 sigs.
    Receivers try each v1 value until one verifies. 7-day window is the
    operational default; the signing function is stateless — it takes
    whichever secrets the caller (webhook_secret_repo) says are active.

Header format (compatible with Stripe's t=<unix>,v1=<hex>[,v1=<hex>] pattern):

    X-GDX-Signature-v1: t=<unix_timestamp>,v1=<sig_old>,v1=<sig_new>

Signed payload (the canonical string):

    f"{timestamp}.{body}"

INTEGRATION_TODO:
    - Envelope encryption of stored secrets via GDX_WEBHOOK_KEK (see SS-21
      plan v3 patch P33 in the spec). Current API takes raw secret bytes;
      wrap at the repo layer.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
from dataclasses import dataclass
from typing import Iterable, Sequence

logger = logging.getLogger(__name__)

SIGNATURE_HEADER = "X-GDX-Signature-v1"
SIGNATURE_VERSION = "v1"


@dataclass(frozen=True)
class SigningSecret:
    """A single signing secret with a stable id (kid) for debugging/rotation.

    `raw` is bytes to discourage accidental string concatenation into logs.
    """

    kid: str
    raw: bytes


def compute_v1_signature(secret_raw: bytes, timestamp: int, body: bytes) -> str:
    """HMAC-SHA256 hex of `"{timestamp}.{body}"` keyed by `secret_raw`."""
    if not isinstance(secret_raw, (bytes, bytearray)):
        raise TypeError("secret_raw must be bytes; do not pass str")
    if not isinstance(body, (bytes, bytearray)):
        raise TypeError("body must be bytes")
    msg = f"{timestamp}.".encode("utf-8") + body
    return hmac.new(secret_raw, msg, hashlib.sha256).hexdigest()


def build_signature_header(
    secrets: Sequence[SigningSecret],
    body: bytes,
    timestamp: int | None = None,
) -> tuple[str, int]:
    """Build the full `t=<ts>,v1=<sig>[,v1=<sig2>]` header value.

    Returns (header_value, timestamp). An empty secrets list signs with no
    v1 parts — still emits t=<ts> so receivers can detect mis-config.
    """
    if timestamp is None:
        timestamp = int(time.time())
    parts = [f"t={timestamp}"]
    for sec in secrets:
        try:
            sig = compute_v1_signature(sec.raw, timestamp, body)
            parts.append(f"{SIGNATURE_VERSION}={sig}")
        except Exception as exc:
            # Signing one secret failing must NOT silently drop it — log and
            # re-raise so the delivery worker sees a real error (do not ship
            # a webhook that claims to be signed but isn't).
            logger.error(
                "webhook signing failed kid=%s err=%s", sec.kid, type(exc).__name__
            )
            raise
    return ",".join(parts), timestamp


def parse_signature_header(header_value: str) -> tuple[int | None, list[str]]:
    """Inverse of build_signature_header.

    Returns (timestamp_or_None, [sig1, sig2, ...]). Malformed segments are
    dropped. Used by the test receiver + verify_signature.
    """
    timestamp: int | None = None
    sigs: list[str] = []
    for raw in (header_value or "").split(","):
        seg = raw.strip()
        if not seg or "=" not in seg:
            continue
        key, _, val = seg.partition("=")
        key = key.strip()
        val = val.strip()
        if key == "t":
            try:
                timestamp = int(val)
            except ValueError:
                # Explicit: do not pretend this is signed — leave ts None.
                logger.warning("webhook signature header: bad t= value")
        elif key == SIGNATURE_VERSION:
            sigs.append(val)
    return timestamp, sigs


def verify_signature(
    header_value: str,
    body: bytes,
    secrets: Iterable[SigningSecret],
    max_age_seconds: int = 300,
) -> bool:
    """Verify a signed webhook.

    Dual-active logic: the webhook is valid if ANY (secret, v1-sig) pair
    matches. Replay protection: reject if |now - t| > max_age_seconds.

    Constant-time comparison via hmac.compare_digest.
    """
    ts, sigs = parse_signature_header(header_value)
    if ts is None or not sigs:
        return False
    now = int(time.time())
    if abs(now - ts) > max_age_seconds:
        return False
    for sec in secrets:
        try:
            expected = compute_v1_signature(sec.raw, ts, body)
        except Exception as exc:
            logger.warning("verify: failed to compute for kid=%s: %s", sec.kid, exc)
            continue
        for sig in sigs:
            if hmac.compare_digest(expected, sig):
                return True
    return False


__all__ = [
    "SIGNATURE_HEADER",
    "SIGNATURE_VERSION",
    "SigningSecret",
    "build_signature_header",
    "compute_v1_signature",
    "parse_signature_header",
    "verify_signature",
]

"""Twilio inbound-webhook signature verification (env-gated).

Inbound Twilio webhooks (SMS receive, missed-call voice) are public endpoints —
without verification anyone can forge customer messages, poison the audit log, or
trigger outbound SMS. Twilio signs each request with X-Twilio-Signature (HMAC-SHA1
over the full URL + sorted POST params, keyed by the account auth token).

Policy (matches the encryption boot-gate): verification is enforced only in a
production-like environment. In dev/test it is skipped, so a fresh clone and the
test suite work without TWILIO_AUTH_TOKEN. In prod the token MUST be set, and a
missing/invalid signature is rejected (fail closed). The ``twilio`` SDK is not a
dependency; the documented algorithm is implemented directly.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os

from fastapi import HTTPException, Request


def _is_prod_env() -> bool:
    return os.getenv("GDX_ENV", "").strip().lower() in ("production", "prod", "staging")


def _expected_signature(url: str, params: dict[str, str], auth_token: str) -> str:
    """Twilio's scheme: URL + each POST param (sorted by key) concatenated, HMAC-SHA1, base64."""
    data = url
    for key in sorted(params):
        data += key + params[key]
    digest = hmac.new(auth_token.encode("utf-8"), data.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("utf-8")


def _public_url(request: Request) -> str:
    """Reconstruct the public URL Twilio signed, honoring reverse-proxy headers."""
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or request.url.netloc
    )
    url = f"{proto}://{host}{request.url.path}"
    if request.url.query:
        url += f"?{request.url.query}"
    return url


async def verify_twilio_signature(request: Request) -> None:
    """FastAPI dependency: verify the Twilio webhook signature in production.

    No-op in dev/test. In prod: require TWILIO_AUTH_TOKEN and a valid
    X-Twilio-Signature, else 403.
    """
    if not _is_prod_env():
        return
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    if not auth_token:
        raise HTTPException(status_code=403, detail="Webhook verification not configured")
    signature = request.headers.get("X-Twilio-Signature", "")
    form = await request.form()
    params = {k: str(v) for k, v in form.items()}
    expected = _expected_signature(_public_url(request), params, auth_token)
    if not signature or not hmac.compare_digest(signature, expected):
        raise HTTPException(status_code=403, detail="Invalid webhook signature")

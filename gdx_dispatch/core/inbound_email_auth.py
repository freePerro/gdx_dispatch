"""Inbound-email webhook authentication (env-gated shared secret).

``POST /api/inbound-email/webhook`` inserts a row into ``inbound_emails``.
It shipped with no authentication of any kind: anyone who knew the path could
insert mail attributed to any sender, and stamp an audit row for it.
(No Vue view reads these rows today — /inbound-comms redirects to /inbox
since #549 — but the write is real and the table is real.) Confirmed on production
2026-09-04 — an empty POST reached pydantic validation (422), proving no gate
ran in front of the handler.

Policy follows ``core/twilio_signature.py`` (which in turn matches the
encryption boot-gate) but inverts its environment test: that module enforces
only for an allowlist of prod-like names, so ``GDX_ENV=prod-eu`` turns it off.
Here the gate is off only for known dev/test names, and on for anything else.
With the gate on and no secret configured, requests are rejected — fail closed.

The provider sends the shared secret in the ``X-GDX-Webhook-Secret`` header.
This is a bearer secret, not a signature: it does not authenticate the payload
body, only the caller. That is the most any of our mail providers (M365
Power Automate, Mailgun routes, SendGrid parse) can send without per-provider
signature schemes, and it is strictly more than the nothing that was there.
"""
from __future__ import annotations

import hmac
import os

from fastapi import HTTPException, Request

# noqa S105: these are the *names* of the header and env var, not a secret.
SECRET_HEADER = "X-GDX-Webhook-Secret"  # noqa: S105
SECRET_ENV = "INBOUND_EMAIL_WEBHOOK_SECRET"  # noqa: S105


# Environments where the gate is deliberately off, so a fresh clone and the
# test suite work with no secret set. Everything else — including an
# unrecognised value like "prod-eu" — enforces. An allowlist of prod-like
# names (the shape core/twilio_signature.py uses) silently disables the check
# for any value nobody thought to list, which is the same fail-open bug this
# module exists to close.
_NON_ENFORCING_ENVS = frozenset({"", "dev", "development", "test", "testing", "local", "ci"})


def _enforced() -> bool:
    """True when the shared secret must be presented.

    Enforced when the environment is anything other than a known dev/test
    name, and also whenever a secret is configured at all — setting one is an
    explicit request to have it checked, whatever the environment says.
    """
    env = os.getenv("GDX_ENV", "").strip().lower()
    if env not in _NON_ENFORCING_ENVS:
        return True
    return bool(os.getenv(SECRET_ENV, ""))


async def verify_inbound_email_secret(request: Request) -> None:
    """FastAPI dependency: require the shared secret in production.

    No-op in dev/test. In prod: require ``INBOUND_EMAIL_WEBHOOK_SECRET`` and a
    matching ``X-GDX-Webhook-Secret`` header, else 403.
    """
    if not _enforced():
        return
    secret = os.getenv(SECRET_ENV, "")
    if not secret:
        # Fail closed. An unset secret is a misconfiguration, not permission
        # to accept anonymous mail into the staff inbox.
        raise HTTPException(status_code=403, detail="Webhook verification not configured")
    presented = request.headers.get(SECRET_HEADER, "")
    # Compare as bytes: hmac.compare_digest raises TypeError on str operands
    # containing non-ASCII, and Starlette latin-1-decodes header values.
    if not hmac.compare_digest(presented.encode("utf-8"), secret.encode("utf-8")):
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

"""Inbound-email webhook authentication (env-gated shared secret).

``POST /api/inbound-email/webhook`` inserts a row into ``inbound_emails``.
It shipped with no authentication of any kind: anyone who knew the path could
insert mail attributed to any sender, and stamp an audit row for it.
(No Vue view reads these rows today — /inbound-comms redirects to /inbox
since #549 — but the write is real and the table is real.) Confirmed on production
2026-09-04 — an empty POST reached pydantic validation (422), proving no gate
ran in front of the handler.

Policy matches the encryption boot-gate's environment test, inverted. The
Twilio signature gate this repo carried until 2026-09-06 enforced only for an
allowlist of prod-like names, so ``GDX_ENV=prod-eu`` turned it off.
Here the gate is off only for known dev/test names, and on for anything else.
With the gate on and no secret configured, requests are rejected — fail closed.

The provider sends the shared secret in the ``X-GDX-Webhook-Secret`` header.
This is a bearer secret, not a signature: it does not authenticate the payload
body, only the caller. That is the most any of our mail providers (M365
Power Automate, Mailgun routes, SendGrid parse) can send without per-provider
signature schemes, and it is strictly more than the nothing that was there.
"""
from __future__ import annotations

from fastapi import Request

from gdx_dispatch.core.webhook_auth import verify_shared_secret

# noqa S105: these are the *names* of the header and env var, not a secret.
SECRET_HEADER = "X-GDX-Webhook-Secret"  # noqa: S105
SECRET_ENV = "INBOUND_EMAIL_WEBHOOK_SECRET"  # noqa: S105


async def verify_inbound_email_secret(request: Request) -> None:
    """FastAPI dependency: require the shared secret in production.

    No-op in dev/test. In prod: require ``INBOUND_EMAIL_WEBHOOK_SECRET`` and a
    matching ``X-GDX-Webhook-Secret`` header, else 403.

    The check itself moved to core/webhook_auth.py 2026-09-18 when the
    cell-gateway webhook became its second consumer; the policy above is
    unchanged and this module keeps the rationale.
    """
    verify_shared_secret(request, header=SECRET_HEADER, secret_env=SECRET_ENV)

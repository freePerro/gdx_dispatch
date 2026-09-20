"""Shared-secret gate for unauthenticated webhook routes (env-gated).

Factored out of core/inbound_email_auth.py 2026-09-18 when the cell-gateway
webhook became the second consumer — one gate, two (header, env-var) bindings,
instead of two drifting copies of a security check. The policy and its
rationale live in inbound_email_auth's module docstring and are unchanged:

- Off only for known dev/test environment names; ON for anything else,
  including unrecognised values ("prod-eu" must enforce, not fail open).
- Setting a secret enforces regardless of environment — configuring one is an
  explicit request to have it checked.
- Enforced + no secret configured = reject (fail closed): a misconfiguration
  is not permission to accept anonymous writes.
- Bearer secret, not a signature: authenticates the caller, not the body.
"""
from __future__ import annotations

import hmac
import os

from fastapi import HTTPException, Request

_NON_ENFORCING_ENVS = frozenset({"", "dev", "development", "test", "testing", "local", "ci"})


def _enforced(secret_env: str) -> bool:
    env = os.getenv("GDX_ENV", "").strip().lower()
    if env not in _NON_ENFORCING_ENVS:
        return True
    return bool(os.getenv(secret_env, ""))


def verify_shared_secret(request: Request, *, header: str, secret_env: str) -> None:
    """Raise 403 unless the request carries the configured shared secret.

    No-op in dev/test with no secret set. Compare as bytes:
    hmac.compare_digest raises TypeError on str operands containing
    non-ASCII, and Starlette latin-1-decodes header values.
    """
    if not _enforced(secret_env):
        return
    secret = os.getenv(secret_env, "")
    if not secret:
        raise HTTPException(status_code=403, detail="Webhook verification not configured")
    presented = request.headers.get(header, "")
    if not hmac.compare_digest(presented.encode("utf-8"), secret.encode("utf-8")):
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

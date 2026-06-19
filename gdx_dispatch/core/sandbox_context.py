"""SS-25 Slice D — per-request sandbox-mode detection.

Sandbox mode is the developer-portal's pressure-relief valve: a call
made in sandbox mode must NEVER hit a real external API, must NEVER
mutate real customer state, and must return synthetic-but-realistic
data so integrators can iterate without fear.

Two independent signals put a request in sandbox mode:

1. **Per-request header** — ``X-GDX-Sandbox: 1`` (any truthy value:
   ``1``, ``true``, ``yes``, case-insensitive). This is the developer
   portal's "dry run" toggle; individual requests opt in.
2. **Tenant flag** — ``request.state.tenant["is_sandbox"] is True``
   (set by the tenant provisioner when the tenant itself is a sandbox
   tenant). Sandbox tenants are sandbox for every request they make,
   full stop.

The two signals compose (OR), not AND: either one wins. The reverse
direction — a production tenant claiming "this one request is
production" — is not supported; sandbox tenants never escape sandbox.

What sandbox MUST substitute for (documented here so integration-time
wiring is unambiguous):

* **Stripe** — never call ``stripe.PaymentIntent.create`` or any other
  live API; instead return a deterministic fake ``pi_sbx_*`` object.
  See ``gdx_dispatch/core/payments.py`` integration hook.
* **Webhook delivery** — never POST to the tenant's real
  ``webhook_url``; instead record the outbound event on the in-memory
  sandbox delivery log and return a fake 2xx result.
* **Email / SMS** — never call SendGrid / Twilio; return synthetic
  ``message_id`` values.
* **QuickBooks Online** — never hit the Intuit API; return synthetic
  entities and mark them ``sandbox=True`` in the response.
* **File delivery / S3 PUT** — write to a sandbox bucket prefix, never
  the production bucket.
* **Payouts** — never originate an ACH; return a fake payout_id.

Anything that reads-only from the tenant's OWN data is fine — sandbox
is about not leaking out, not about paranoid read isolation (that's
SS-10's job, tenant-level DB/Redis namespacing).

This module is pure detection + policy; it has no side-effects and no
knowledge of the external services above. Services consult
:func:`is_sandbox` and branch on it at the integration seam.
"""
from __future__ import annotations

from typing import Any

from starlette.requests import Request

SANDBOX_HEADER = "X-GDX-Sandbox"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def is_sandbox(request: Request) -> bool:
    """Return ``True`` if this request must run in sandbox mode.

    The check is cheap and side-effect-free; call it freely at service
    boundaries. If ``request`` is missing both signals you get
    ``False``.
    """
    if _header_says_sandbox(request):
        return True
    if _tenant_is_sandbox(request):
        return True
    return False


def _header_says_sandbox(request: Request) -> bool:
    raw = request.headers.get(SANDBOX_HEADER)
    if not raw:
        return False
    return raw.strip().lower() in _TRUTHY


def _tenant_is_sandbox(request: Request) -> bool:
    tenant: Any = getattr(request.state, "tenant", None)
    if tenant is None:
        return False
    if isinstance(tenant, dict):
        return bool(tenant.get("is_sandbox"))
    return bool(getattr(tenant, "is_sandbox", False))


def assert_not_sandbox(request: Request, *, operation: str) -> None:
    """Raise if called from a sandbox request.

    Services that have NO sandbox substitute — e.g. a primitive that
    just can't exist in sandbox safely — call this to fail loud. The
    message names the operation so the caller's stack trace reads well.
    """
    if is_sandbox(request):
        raise SandboxOperationBlocked(
            f"operation {operation!r} is not available in sandbox mode"
        )


class SandboxOperationBlocked(RuntimeError):
    """Sandbox mode blocked a side-effecting operation with no substitute."""

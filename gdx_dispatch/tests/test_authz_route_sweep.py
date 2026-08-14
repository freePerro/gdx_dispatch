"""Ratchet: no NEW route may ship without authentication.

Replaces the hand-maintained approach in ``test_authz_regression.py``, whose
18 hardcoded paths were frozen at the 2026-06-24 sweep. Nothing shipped after
that date was covered — which is how six unauthenticated ``/api/payments/*``
endpoints (real ACH debit, cross-invoice payment replay) stayed open for
months while every test stayed green.

This sweeps the REAL ``create_app()`` route table instead of a list someone
has to remember to update.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from gdx_dispatch.tests.authz_sweep import ungated_routes

BASELINE_PATH = Path(__file__).resolve().parents[2] / ".authz_ungated_baseline"


def _baseline() -> set[str]:
    lines = BASELINE_PATH.read_text(encoding="utf-8").splitlines()
    return {ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")}


@pytest.fixture(scope="module")
def current() -> set[str]:
    return set(ungated_routes())


def test_no_new_unauthenticated_routes(current: set[str]) -> None:
    """Every ungated route must already be in the baseline."""
    new = sorted(current - _baseline())
    assert not new, (
        "These routes are reachable with NO authentication and are not in "
        f"{BASELINE_PATH.name}:\n  "
        + "\n  ".join(new)
        + "\n\nAdd an auth dependency (get_current_user, require_permission, "
        "_current_portal_user, ...). If the route is genuinely public — a "
        "signature-verified webhook, a token-authorized customer link, "
        "/health — say so in review and add it to the baseline with a reason.\n"
        "NOTE: a security scheme declared with auto_error=False does NOT "
        "authenticate; it permits anonymous callers through while making the "
        "signature look guarded. That was the /api/payments bug."
    )


def test_baseline_does_not_silently_grow(current: set[str]) -> None:
    """The baseline is a debt list — it must shrink, never quietly expand.

    Stale entries (routes since gated) are fine and are reported, not failed,
    so hardening work never breaks the build.
    """
    stale = sorted(_baseline() - current)
    if stale:
        print(
            f"\n{len(stale)} baseline entries are now authenticated — "
            f"prune them from {BASELINE_PATH.name}:\n  " + "\n  ".join(stale)
        )
    # 91 → 93 (2026-08-13): POST /api/proposals/{token}/accept + /decline —
    # the public estimate approval page. Group 2, token IS the credential
    # (64-char Estimate.public_token, sent_at-gated, uniform 404, row-locked);
    # same authorization model the /api/payments/* endpoints pin below.
    assert len(_baseline()) <= 93, (
        "The ungated-route baseline grew. It is a debt list to work down, not "
        "a place to record new exceptions."
    )


def test_payment_endpoints_are_token_authorized_not_open(current: set[str]) -> None:
    """The /api/payments/* endpoints have no auth dependency BY DESIGN — the
    anonymous customer proves which invoice they may touch with its token.

    That design is only safe because authorization is enforced inside the
    handlers, so pin the guarantee here: the invoice must come from the token,
    and the request must not be able to choose the amount or the payment
    method. The behavioural proof lives in test_payments.py; this asserts the
    endpoints still exist in the shape this reasoning assumes.
    """
    from gdx_dispatch.core.payments import (
        _amount_cents,
        _idempotency_key,
        _resolve_public_invoice,
    )

    assert callable(_resolve_public_invoice)
    assert callable(_amount_cents)
    assert callable(_idempotency_key)

    # The two unauthenticated payment-method endpoints were deleted; if they
    # come back they must not come back ungated.
    assert "GET /api/payments/methods" not in current
    assert "DELETE /api/payments/methods/{pm_id}" not in current

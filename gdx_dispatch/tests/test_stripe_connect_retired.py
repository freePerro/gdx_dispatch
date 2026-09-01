"""Stripe Connect is retired (2026-09-01) and must stay retired.

Connect is a *platform* construct: a destination charge sends money to a
connected account and the platform keeps an application fee. This deployment is
single-tenant — it IS the business receiving the money — so there was never a
second party for the machinery to serve. Nothing in `frontend/src` ever called
any of its nine routes, no connected account was ever configured, and it minted
nothing. See `docs/design/phase-d-saas-residue.md` (S3/S6) and issue #421.

These are **absence assertions on the live route table**, not source-text
checks: a re-added router shows up here whatever file it lives in. They are
written to fail loudly if someone restores the surface, because the endpoint
that prompted the deletion took a client-supplied amount and a client-supplied
destination account against a live Stripe key.

The last three tests are the counterfactual half: they assert the *live* payment
path survived. A deletion that also removed real payment processing would pass
every absence test above and still be a disaster.
"""
from __future__ import annotations

import pathlib

from gdx_dispatch.tests.conftest import iter_app_routes


def _paths() -> set[str]:
    from gdx_dispatch.app import create_app

    return {path for path, _route in iter_app_routes(create_app())}


def test_no_stripe_connect_routes_survive():
    dead = sorted(p for p in _paths() if p.startswith("/api/stripe/connect"))
    assert dead == [], f"the Stripe Connect router is back: {dead}"


def test_no_payments_connect_routes_survive():
    """The second, parallel Connect surface that lived on `routers/payments.py`."""
    dead = sorted(p for p in _paths() if "/connect/" in p)
    assert dead == [], f"a Connect surface is back: {dead}"


def test_connect_modules_are_gone_from_the_tree():
    root = pathlib.Path(__file__).resolve().parents[1]
    for rel in ("routers/stripe_connect.py", "core/stripe_connect.py"):
        assert not (root / rel).exists(), f"{rel} is back"


def test_stripe_connect_module_key_is_gone():
    """The module key gated only the deleted router, and was granted on prod."""
    from gdx_dispatch.core.modules import MODULES

    assert "stripe_connect" not in MODULES


def test_no_destination_charge_helpers_remain():
    """`transfer_data` / `application_fee_amount` are the Connect wire fields.

    Their absence is what proves the *capability* is gone, not just the routes.
    """
    root = pathlib.Path(__file__).resolve().parents[1]
    offenders = []
    for py in root.rglob("*.py"):
        if "tests" in py.parts or "migrations" in py.parts:
            continue
        text = py.read_text(encoding="utf-8", errors="ignore")
        if "transfer_data" in text or "application_fee_amount" in text:
            offenders.append(str(py.relative_to(root)))
    assert offenders == [], f"destination-charge machinery survives in: {offenders}"


# --- counterfactual: the live payment path must NOT have been collateral damage


def test_the_real_payment_intent_route_still_exists():
    assert "/api/payments/intent" in _paths()


def test_the_public_pay_page_still_exists():
    """The token-scoped customer pay page — the surface real cards go through."""
    assert "/pay/{invoice_token}" in _paths()


def test_ordinary_stripe_payment_helpers_still_import():
    """`core/stripe_payments.py` is the live path and is NOT Connect."""
    from gdx_dispatch.core.stripe_payments import create_payment_intent

    assert callable(create_payment_intent)

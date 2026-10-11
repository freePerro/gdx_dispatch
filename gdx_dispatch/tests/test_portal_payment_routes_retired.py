"""The portal's saved-method / payment-mint routes are gone (GDXA-552, 2026-10-11).

`routers/payments.py` mounted six routes at `/payments/*`: an intent mint, a
SetupIntent, a saved-method list, an off-session charge, a legacy-Sources ACH
setup (`Token.create` + `Customer.create_source`, with no verify step) and a
detach. Every one began with `_require_stripe_customer(user)`, which read
`stripe_customer_id` off a `CustomerUser` that has no such column, so all six
answered 400 to every real caller, and nothing in `frontend/src` called them.
The card mints also carried no card surcharge, which the live pay page does.
`core/stripe_payments.py` existed only to serve that router and went with it,
as did `routers/ui_compat.py`'s `POST /api/payments/intent`, a 501 stub with no
caller.

These are **absence assertions on the live route table**, so a re-added route
fails here whatever file it lives in. If the portal ever needs saved cards or
ACH, that is a new feature: it has to carry the surcharge and use Stripe's
current bank-account flow, and it should be built on the token-scoped pay path
rather than restored from history.

The last test is the counterfactual. A deletion that also took out the live
customer pay path would pass every absence test and still be a disaster.
"""
from __future__ import annotations

import pathlib

from gdx_dispatch.tests.conftest import iter_app_routes

_RETIRED = {
    ("POST", "/payments/intent"),
    ("POST", "/payments/setup"),
    ("GET", "/payments/methods"),
    ("POST", "/payments/methods/{method_id}/charge"),
    ("POST", "/payments/ach/setup"),
    ("DELETE", "/payments/methods/{method_id}"),
    ("POST", "/api/payments/intent"),
}


def _routes() -> set[tuple[str, str]]:
    from gdx_dispatch.app import create_app

    out: set[tuple[str, str]] = set()
    for path, route in iter_app_routes(create_app()):
        for method in getattr(route, "methods", None) or ():
            out.add((method, path))
    return out


def test_no_retired_portal_payment_route_is_mounted():
    back = sorted(_RETIRED & _routes())
    assert back == [], f"a retired portal payment route is mounted again: {back}"


def test_nothing_is_mounted_under_the_old_portal_prefix():
    """The whole `/payments/*` prefix, not just the six paths: a renamed route
    on the same unreachable surface is the same defect."""
    back = sorted(p for _m, p in _routes() if p.startswith("/payments/"))
    assert back == [], f"routes under /payments/ are back: {back}"


def test_the_portal_payment_modules_are_gone_from_the_tree():
    root = pathlib.Path(__file__).resolve().parents[1]
    for rel in ("routers/payments.py", "core/stripe_payments.py"):
        assert not (root / rel).exists(), f"{rel} is back"


def test_the_live_customer_pay_path_survived():
    routes = _routes()
    for want in (
        ("GET", "/pay/{invoice_token}"),
        ("POST", "/api/payments/create-intent"),
        ("POST", "/api/payments/confirm"),
    ):
        assert want in routes, f"the live pay path lost {want}"

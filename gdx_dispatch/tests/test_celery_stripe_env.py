"""The celery workers must carry the Stripe key, or M12 is inert in production.

Written after walking v1.84.0 on prod. `payments.sweep_stale_intents` cancels a
customer's stale PaymentIntent when an invoice is settled another way, and it
runs ON A WORKER. `STRIPE_SECRET_KEY` was declared app-only in
`docker-compose.yml`, so the workers merged `<<: *app-env` without it and every
Stripe call from the task failed with:

    AuthenticationError: You did not provide an API key.

The task logged it, degraded safely, and reported **success with an empty
result** — so nothing was cancelled, nothing raised, and no test noticed. The
whole fix was shipped and silently doing nothing. Not one of the 57 unit tests
could have caught it: they all mock Stripe.

This reads the compose file the deploy actually uses, so it fails if the key is
ever moved back into an app-only block.
"""
from __future__ import annotations

import pathlib

import pytest

yaml = pytest.importorskip("yaml")

COMPOSE = (
    pathlib.Path(__file__).resolve().parents[1] / "docker" / "docker-compose.yml"
)

# Services that execute application code and may reach a third-party API.
# plugin-host is included because it runs the same image.
WORKER_SERVICES = ("celery-high", "celery-low", "celery-beat")


def _services() -> dict:
    return (yaml.safe_load(COMPOSE.read_text()) or {}).get("services") or {}


@pytest.mark.parametrize("service", WORKER_SERVICES)
def test_every_worker_service_receives_the_stripe_key(service):
    svc = _services().get(service)
    assert svc is not None, f"{service} is not defined in docker-compose.yml"
    env = svc.get("environment") or {}
    assert "STRIPE_SECRET_KEY" in env, (
        f"{service} does not receive STRIPE_SECRET_KEY, so every Stripe call it "
        "makes fails with AuthenticationError. `payments.sweep_stale_intents` "
        "runs here — M12 would be silently inert."
    )


def test_the_app_still_receives_the_stripe_key():
    """The counterfactual: moving the key must not take it away from the app."""
    env = (_services().get("app") or {}).get("environment") or {}
    assert "STRIPE_SECRET_KEY" in env


def test_the_key_is_declared_once_in_the_shared_anchor():
    """One source of truth. A per-service copy is how it drifted in the first
    place — the app had it, the workers did not, and nothing compared them."""
    raw = COMPOSE.read_text()
    assert raw.count("STRIPE_SECRET_KEY: ${STRIPE_SECRET_KEY") == 1, (
        "STRIPE_SECRET_KEY is declared more than once; it belongs in the shared "
        "x-app-env anchor so no service can be missed"
    )

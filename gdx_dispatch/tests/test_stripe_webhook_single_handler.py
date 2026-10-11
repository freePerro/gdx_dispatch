"""There is exactly ONE Stripe webhook handler, and it can record money.

Until 2026-09-27 there were two. `routers/stripe_webhook.py` verifies the
signature and dispatches to `core/payments.py::handle_payment_webhook` — the
live chain. Alongside it sat `core/stripe_payments.py::handle_webhook`, reached
by nothing but its own two unit tests, which asserted only its return shape and
so passed whether or not anything called it. GDXA-87 deleted it. Two properties
made it worth a guard rather than a quiet deletion:

1. **It was inert.** It took no `Session` and touched no DB — every branch
   logged and returned a dict. Wiring it to the live route would not have
   recorded money; it would have 200'd every event and dropped the lot, which
   is indistinguishable from working until someone reconciles.
2. **It re-verified the signature itself**, a second copy of the construct-event
   call. Two verifiers in one tree is how the wrong one ends up called, and its
   event map was already wrong in both directions: it handled a charge-level
   success event, which a PaymentIntent integration has no reason to subscribe
   to (Stripe: "use webhooks to monitor the `payment_intent.succeeded` event" —
   https://docs.stripe.com/payments/payment-intents/verifying-status, read
   2026-09-27), and it handled none of the refund or dispute events production
   does receive.

**Read 2026-09-27 from the live endpoint** (`WebhookEndpoint.list` inside
`gdx-app-1`, prod's own key), because the vendor doc states and the live
response proves: `https://gdx.teamgaragedoor.com/stripe/webhook` is `enabled`
with 9 events — `payment_intent.succeeded`, `.processing`,
`.payment_failed`, `charge.failed`, `charge.refunded`,
`charge.dispute.created`, `.funds_withdrawn`, `.funds_reinstated`, and
`account.updated`. That confirms the deleted handler's charge-success branch was
for an event this deployment never receives. It also shows two live gaps that
are NOT this change's to fix and are deliberately not asserted below, because a
test cannot read the dashboard and a hard-coded copy of it would rot:
`payment_intent.requires_action` has a handler branch prod never triggers, and
`account.updated` is subscribed with no branch at all — a leftover of the
Connect retirement (2026-09-01). A real subscription-drift guard needs Stripe
API access at test time; that is still unbuilt.

The tests below are absence assertions plus a counterfactual half. A deletion
that also took out the real dispatch chain would satisfy every absence test and
be a disaster, so the live path is proven too — by **invoking** it and by
reading its **AST**, never by grepping its source for an event name. A
substring search over `core/payments.py` is satisfied by a comment or a
docstring: stripping all nine dispatch branches leaves six of nine such
assertions green (measured 2026-09-27), which is the vacuity .claude/rules/scanners-and-tests.md
names as a sharp edge and which the 2026-08-04 money audit already caught once in this
repo — a guard "satisfied by the *import* line with the call deleted".
"""
from __future__ import annotations

import ast
import inspect
import pathlib

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.tests.conftest import iter_app_routes
from gdx_dispatch.tools.tracked_files import tracked_or_none

_PKG = pathlib.Path(__file__).resolve().parents[1]  # gdx_dispatch/
_REPO_ROOT = _PKG.parent

# Every `event_type == "..."` branch `handle_payment_webhook` dispatches on.
# Losing one means production stops recording that event; gaining one that
# nothing sends means a branch that can never run (see the module docstring).
_EXPECTED_DISPATCH = {
    "payment_intent.processing",
    "payment_intent.requires_action",
    "payment_intent.succeeded",
    "payment_intent.payment_failed",
    "charge.failed",
    "charge.refunded",
    "charge.dispute.created",
    "charge.dispute.funds_withdrawn",
    "charge.dispute.funds_reinstated",
}


def _production_sources() -> list[pathlib.Path]:
    """Tracked .py files that can run in production.

    Deliberately the tracked set, not a tree walk: an untracked scratch file
    under `core/` must not redden this locally while CI stays green — the
    `docker/demo` class of false positive CLAUDE.md warns about. Matches the
    five sibling guards. `tracked_or_none` returns None only where there is no
    `.git` at all (the shipped image, a tarball) and warns loudly; walking the
    tree is correct there.
    """
    tracked = tracked_or_none(_REPO_ROOT)
    if tracked is None:
        candidates = [p for p in _PKG.rglob("*.py")]
    else:
        candidates = [
            p
            for rel in sorted(tracked)
            if rel.endswith(".py") and (p := _REPO_ROOT / rel).is_file()
        ]
    return [
        p
        for p in candidates
        if _PKG in p.parents and "tests" not in p.parts and "migrations" not in p.parts
    ]


def _paths() -> set[str]:
    from gdx_dispatch.app import create_app

    return {path for path, _route in iter_app_routes(create_app())}


@pytest.fixture
def db_session():
    """In-memory tenant schema built from the ORM, per .claude/rules/database.md."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield session
    session.close()
    engine.dispose()


# --- absence: the second handler must stay gone


def test_no_second_webhook_handler_or_server_side_attach():
    """Neither deleted helper may come back, in any production file.

    Until 2026-10-11 these were `hasattr` checks on `core/stripe_payments.py`.
    That module served only the unreachable portal router and was deleted with
    it (GDXA-552), so a check on it would now pass vacuously. This reads every
    production file's AST for the two function names instead, and its source
    for a server-side `PaymentMethod.attach` (`save_payment_method` made that
    call; nothing asked for a second way to save a card).
    """
    offenders = []
    for py in _production_sources():
        text = py.read_text(encoding="utf-8", errors="ignore")
        rel = str(py.relative_to(_PKG))
        if "PaymentMethod.attach" in text:
            offenders.append(f"{rel}: PaymentMethod.attach")
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in (
                "handle_webhook",
                "save_payment_method",
            ):
                offenders.append(f"{rel}: def {node.name}")
    assert offenders == [], (
        f"a second Stripe webhook handler or server-side attach is back: {offenders}. "
        "Route Stripe events to core/payments.py::handle_payment_webhook."
    )


def test_only_one_signature_verifier_in_the_tree():
    """A second `construct_event` is a second webhook entry point in waiting."""
    offenders = sorted(
        str(py.relative_to(_PKG))
        for py in _production_sources()
        if "Webhook.construct_event" in py.read_text(encoding="utf-8", errors="ignore")
    )
    assert offenders == ["routers/stripe_webhook.py"], (
        f"expected exactly one Stripe signature verifier, found: {offenders}"
    )


def test_no_production_code_branches_on_charge_succeeded():
    """The fingerprint of the deleted handler's event map.

    Prod does not subscribe to it (read 2026-09-27), and the live handler keys
    off `payment_intent.succeeded`. A production file that branches on it is
    either the old handler restored or a fresh copy of its wrong map.
    """
    offenders = sorted(
        str(py.relative_to(_PKG))
        for py in _production_sources()
        if "charge.succeeded" in py.read_text(encoding="utf-8", errors="ignore")
    )
    assert offenders == [], f"a stale Stripe event map survives in: {offenders}"


# --- counterfactual: the live chain must NOT have been collateral damage


def test_the_live_webhook_route_still_exists():
    assert "/stripe/webhook" in _paths()


def test_the_live_handler_still_takes_a_session():
    """The property the deleted handler lacked, and the reason it was inert."""
    from gdx_dispatch.core.payments import handle_payment_webhook

    params = list(inspect.signature(handle_payment_webhook).parameters)
    assert params == ["event", "db"], params


def test_the_live_handler_still_dispatches_on_every_expected_event():
    """AST, not grep: the set of `event_type == "..."` comparisons in the function.

    Deleting a branch removes its comparison and turns this red; a comment or a
    docstring naming the event cannot satisfy it. That is the whole difference
    from the presence-in-source assertion this replaced.
    """
    tree = ast.parse((_PKG / "core" / "payments.py").read_text(encoding="utf-8"))
    fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "handle_payment_webhook"
    )
    dispatched = {
        node.comparators[0].value
        for node in ast.walk(fn)
        if isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Name)
        and node.left.id == "event_type"
        and len(node.ops) == 1
        and isinstance(node.ops[0], ast.Eq)
        and isinstance(node.comparators[0], ast.Constant)
        and isinstance(node.comparators[0].value, str)
    }
    assert dispatched == _EXPECTED_DISPATCH, (
        f"the live handler's dispatch set changed: "
        f"lost {sorted(_EXPECTED_DISPATCH - dispatched)}, "
        f"gained {sorted(dispatched - _EXPECTED_DISPATCH)}"
    )


def test_the_live_handler_really_dispatches(db_session):
    """One real invocation, because a static read cannot prove the wiring runs.

    `payment_intent.processing` with no `invoice_id` returns before any Stripe
    read or DB write, so this exercises the dispatch itself and nothing else. An
    event type with no branch must fall through to `ignored` — that contrast is
    what makes the first assertion meaningful rather than tautological.
    """
    from gdx_dispatch.core.payments import handle_payment_webhook

    handled = handle_payment_webhook(
        {"type": "payment_intent.processing", "data": {"object": {}}}, db_session
    )
    assert handled == {"status": "no_invoice_id"}, handled

    unhandled = handle_payment_webhook(
        {"type": "customer.created", "data": {"object": {"id": "cus_x"}}}, db_session
    )
    assert unhandled == {"status": "ignored"}, unhandled

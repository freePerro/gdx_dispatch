"""Close pay pages left open on an invoice that has since been settled (M12).

A PaymentIntent **freezes its amount when it is minted**, and `/confirm` only
*records* what Stripe.js already charged in the browser — the server gets no
veto at charge time. So once a customer's tab holds a $500 intent, the only
place to stop a double collection is before they click.

**Why a task and not an inline call.** The sweep makes two to six Stripe calls,
and stripe-python 11.6.0 retries twice by default with a long socket timeout.
Doing that inside `record_payment` or the Stripe webhook would hold invoice,
payment and ledger locks across a third-party outage — the two-commit window
this repo ranks as its highest defect class — and would put the webhook at risk
of Stripe's own timeout, producing a retry storm on top of an outage. The money
is committed first; this runs after, on its own.

**What it costs when the worker is down.** The stale intent is not cancelled and
can still overcharge. That is not silent: `_mark_invoice_paid` writes a
`payment_exceeds_receivable` audit event and logs at ERROR when a charge exceeds
the receivable, which is the backstop for exactly this case.

**There is a deploy window where this task does not exist yet, and it lies.**
`update.sh` starts the new app FIRST and health-gates it before recreating the
celery containers, so for the length of that gate a new web app can enqueue
`payments.sweep_stale_intents` at a worker running the previous image, which has
never heard of it. Celery logs it as unregistered and drops it; it is not
retried.

This is **worse than the worker being down**, and an earlier version of this
comment wrongly called them the same. A dead broker makes `apply_async` raise,
`enqueue_stale_intent_sweep` returns False, and the audit row honestly records
`stale_intent_sweep_queued: false`. Here the broker is fine, the enqueue
succeeds, and the audit row records `true` for a sweep that will never run — a
false record, which is the defect class this repo ranks highest. The
`payment_exceeds_receivable` backstop still catches the money, but the trail is
wrong for the duration of that gate.

The mitigation is operational, not code: on the release that first ships this,
recreate the celery containers before or with the app, or accept one health-gate
of possibly-false `queued: true` rows and know why.
"""
from __future__ import annotations

import logging

from gdx_dispatch.core.celery_app import celery_app
from gdx_dispatch.core.database import SessionLocal

log = logging.getLogger(__name__)


def _connected_account_for(db, invoice) -> str:
    """The Stripe Connect account this invoice's money lives on, or "".

    Mint sites stamp intents onto the connected account via `_stripe_extra`, so
    a scan of the platform account for one of those returns nothing — which is
    indistinguishable from "all clear". Reading it here means no call site can
    forget to.

    Never raises: a lookup failure falls back to the platform account, which is
    correct for the single-tenant deployments that have no connected account at
    all, and is logged when it is not.
    """
    try:
        from sqlalchemy import text as _text

        row = db.execute(
            _text("SELECT stripe_connect_account_id FROM companies WHERE id = :cid"),
            {"cid": str(getattr(invoice, "company_id", "") or "")},
        ).first()
        return str((row[0] if row else "") or "")
    except Exception:
        log.warning(
            "stale_intent_sweep_connect_lookup_failed invoice=%s — scanning the platform "
            "account; a Connect-minted intent would not be seen.",
            getattr(invoice, "id", "?"),
        )
        return ""


def _audit_sweep(db, invoice, *, why: str, results: list[dict]) -> None:
    """Record what the sweep did to the customer's open checkouts.

    Cancelling an intent is a state change on a money object, and invariant #1
    says every one of those answers who/what/when. A first version logged at
    WARNING and stopped there, from a task that was holding a session the whole
    time — a state change with no trail. Nothing to reconstruct from later when
    a customer says "the payment page stopped working".

    Only writes when something actually happened: a row per ordinary sweep that
    found nothing would bury the ones that did.
    """
    acted = [r for r in results if r.get("result") != "left_alone"]
    if not acted:
        return
    try:
        from gdx_dispatch.core.audit import log_audit_event_sync

        log_audit_event_sync(
            db=db,
            tenant_id=None,
            user_id="stripe-sweep",
            action="stale_payment_intents_canceled",
            entity_type="invoice",
            entity_id=str(invoice.id),
            details={"why": why, "intents": acted},
        )
    except Exception:
        log.exception("stale_intent_sweep_audit_failed invoice=%s", getattr(invoice, "id", "?"))


@celery_app.task(
    name="payments.sweep_stale_intents",
    queue="priority:high",
    # Fire and forget: nothing reads this return value, and storing it
    # makes `apply_async` touch the result backend — which is where the
    # measured ~19s stall on an unreachable Redis actually came from,
    # not the broker ("Retry limit exceeded while trying to reconnect to
    # the Celery result store backend"). The dict is for tests and logs.
    ignore_result=True,
)
def sweep_stale_intents(
    invoice_id: str,
    *,
    why: str,
    settled: bool = False,
    connected_account: str = "",
) -> dict:
    """Cancel any open intent on ``invoice_id`` that could now overcharge.

    `priority:high` — the window this closes is the seconds while a customer
    still has the pay page open. A low-priority queue behind a nightly job
    would miss it.
    """
    import uuid as _uuid

    from gdx_dispatch.core.payments import cancel_open_intents_for_invoice
    from gdx_dispatch.models.tenant_models import Invoice

    # Celery serialises the id as a string; `invoices.id` is a UUID column and
    # a bare string raises rather than missing, which would read as "the sweep
    # failed" instead of "no such invoice".
    try:
        pk = _uuid.UUID(str(invoice_id))
    except (ValueError, AttributeError, TypeError):
        log.warning("stale_intent_sweep_bad_invoice_id invoice=%r why=%s", invoice_id, why)
        return {"invoice_id": str(invoice_id), "results": [], "error": "invoice_not_found"}

    try:
        with SessionLocal() as db:
            invoice = db.get(Invoice, pk)
            if invoice is None:
                log.warning("stale_intent_sweep_invoice_gone invoice=%s why=%s", invoice_id, why)
                return {"invoice_id": invoice_id, "results": [], "error": "invoice_not_found"}
            # The balance is read HERE, not carried from the caller.
            #
            # An earlier version passed a `remaining_cents` computed at enqueue
            # time and took `min(passed, fresh)`, defending it as "safe in the
            # dangerous direction". It was not: a balance that moves UP between
            # enqueue and execution — a payment deleted, a line added, a credit
            # reversed, none of which enqueue a sweep — made the task trust the
            # stale LOW figure and cancel a correctly-sized live checkout. The
            # only thing the caller actually knows that this task cannot read is
            # whether the invoice was SETTLED outright, which is one bit, so it
            # is passed as one bit.
            remaining_cents = 0 if settled else max(
                0, int(round(float(invoice.balance_due or 0) * 100))
            )
            # Resolve the Connect account HERE rather than asking every caller
            # to pass it. The Stripe-driven paths (webhook envelope, /confirm,
            # ACH, portal) know it and pass it; the four office paths
            # (record_payment, credit memo, applied credit, void) have no reason
            # to and did not — so on a Connect tenant the sweep scanned the
            # PLATFORM account and silently found nothing, no-opping on exactly
            # the call site this fix exists for ("the office records a check").
            account = connected_account or _connected_account_for(db, invoice)
            # Say WHY, distinguishably. The prod walk of v1.84.0 hit
            # `AuthenticationError: You did not provide an API key` because the
            # celery containers did not carry STRIPE_SECRET_KEY, and this task
            # logged, degraded, and returned success with an empty result — so
            # the sweep was inert in production and the task result said
            # nothing was wrong. An unconfigured Stripe is a deployment fault,
            # not "no stale intents".
            from gdx_dispatch.core.payments import stripe_configured

            if not stripe_configured():
                log.error(
                    "stale_intent_sweep_stripe_unconfigured invoice=%s why=%s — this worker "
                    "has no STRIPE_SECRET_KEY, so no stale intent can be cancelled anywhere. "
                    "Check that the celery services receive it.",
                    invoice_id, why,
                )
                return {"invoice_id": invoice_id, "results": [], "error": "stripe_unconfigured"}

            results = cancel_open_intents_for_invoice(
                invoice,
                why=why,
                remaining_cents=remaining_cents,
                connected_account=account,
            )
            _audit_sweep(db, invoice, why=why, results=results)
            db.commit()
        return {"invoice_id": invoice_id, "results": results}
    except Exception:  # noqa: BLE001 — the payment is already committed; a
        # sweep that cannot run must never look like a failed payment.
        log.exception("stale_intent_sweep_failed invoice=%s why=%s", invoice_id, why)
        return {"invoice_id": invoice_id, "results": [], "error": "sweep_failed"}


def enqueue_stale_intent_sweep(
    invoice, *, why: str, settled: bool = False, connected_account: str = ""
) -> bool:
    """Queue the sweep. Never raises — a dead broker must not cost a payment.

    ``invoice`` is an Invoice or its id as a plain string. Callers that queue
    after a ``db.commit()`` must pass the id captured BEFORE the commit: the
    commit expires the instance, so ``invoice.id`` here would be a lazy refresh
    SELECT on a session the caller has finished with, and a failed refresh
    would either lose the sweep or — via the log line below — raise out of a
    function whose money is already committed.

    Returns whether it was queued, so a caller can say so honestly rather than
    reporting a cancellation that never happened.
    """
    invoice_id = invoice if isinstance(invoice, str) else "?"
    try:
        if not isinstance(invoice, str):
            invoice_id = str(invoice.id)
        # The timeouts are load-bearing, not style. Measured on this image:
        # `.delay()` against an unreachable broker takes ~19s, and `retry=False`
        # alone does not fix it — kombu retries the CONNECTION underneath. That
        # latency would land back in `record_payment` and the Stripe webhook,
        # which is the exact thing moving this work onto a task removes. A
        # bounded connection plus `retry=False` makes a broker outage cost
        # milliseconds here instead of seconds.
        with celery_app.connection_for_write(
            transport_options={
                "socket_connect_timeout": 2,
                "socket_timeout": 2,
                "max_retries": 0,
            }
        ) as conn:
            sweep_stale_intents.apply_async(
                args=[invoice_id],
                kwargs={
                    "why": why,
                    "settled": bool(settled),
                    "connected_account": connected_account,
                },
                retry=False,
                connection=conn,
            )
        return True
    except Exception:  # noqa: BLE001
        log.exception(
            "stale_intent_sweep_not_queued invoice=%s why=%s — an open pay page may still "
            "overcharge; payment_exceeds_receivable remains the backstop.",
            invoice_id, why,
        )
        return False

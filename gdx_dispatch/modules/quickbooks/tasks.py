from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

_task_log = logging.getLogger(__name__)

from gdx_dispatch.core.celery_app import celery_app
from gdx_dispatch.core.database import SessionLocal
from gdx_dispatch.models.tenant_models import Customer, Invoice
from gdx_dispatch.modules.quickbooks.client import QBAPIError, QBAuthError
from gdx_dispatch.modules.quickbooks.oauth import connection_healthy, get_qb_client
from gdx_dispatch.modules.quickbooks.sync import (
    QBRateLimitError,
    QBSyncError,
    pull_accounts,
    pull_customers,
    pull_invoices,
    pull_items,
    pull_payments,
    pull_vendors,
    push_customer,
    push_invoice,
)


# ─── S122-15: error classification for Celery retry decisions ────────────────
#
# Pre-fix the per-row push only caught QBRateLimitError; any other exception
# (network blip on row 3, validation error on row 7, etc.) killed the entire
# task — rows 4..N never tried. Now we classify per-row failures and let
# transient errors retry the whole task (current Celery contract) while
# permanent errors get logged + collected + the loop continues.
#
# Classification:
#   TRANSIENT — re-raise so Celery retries the whole task with backoff:
#     QBRateLimitError (429), QBAPIError with status >= 500, httpx network
#     errors (ConnectError, TimeoutException, ReadError, WriteError).
#   PERMANENT — log + collect + continue to next row:
#     QBSyncError (logic error: missing customer ref, empty Id),
#     QBAPIError with status 4xx (validation, malformed payload),
#     QBAuthError (auth failure — but S122-13's connection_healthy gate
#     already short-circuits the whole task before we reach here).
#   UNKNOWN — log + collect + continue. Treat as permanent because we
#     can't reason about its retry-safety.
def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, QBRateLimitError):
        return True
    if isinstance(exc, QBAPIError):
        return exc.status_code >= 500
    # httpx network failures — import locally to avoid coupling tasks.py to
    # httpx at module-import time
    try:
        import httpx  # noqa: PLC0415
        if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError,
                            httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError)):
            return True
    except ImportError:
        pass
    return False


def _tenant_session(tenant_id: str):
    """Open a session on the single application database."""
    return SessionLocal()


async def _push_customer_async(tenant_id: str, customer_id: str, db) -> None:
    """Legacy single-row push that opens its own QBClient. Kept for tests + any
    out-of-task callers; the main full-sync task now uses _run_push_loop to
    share one QBClient across the whole loop (S122-16).
    """
    qb = await get_qb_client(tenant_id, db)
    try:
        await push_customer(tenant_id, customer_id, db, qb)
    finally:
        await qb.close()


async def _push_invoice_async(tenant_id: str, invoice_id: str, db) -> None:
    """Legacy single-row push — same caveats as _push_customer_async."""
    qb = await get_qb_client(tenant_id, db)
    try:
        await push_invoice(tenant_id, invoice_id, db, qb)
    finally:
        await qb.close()


async def _run_push_loop(
    tenant_id: str,
    db,
    row_ids: list[str],
    push_fn,
    *,
    entity_label: str,
) -> tuple[int, list[dict]]:
    """S122-16: open the QBClient ONCE, iterate every row through a single
    long-lived ``httpx.AsyncClient`` to reuse the TCP + TLS connection. Per
    Intuit/httpx best practice — recreating clients per request kills
    keep-alive and adds a fresh handshake on every row.

    Per-row error policy mirrors S122-15:
      * Transient (rate-limit, network, 5xx) — re-raise so the task retries.
      * Permanent — log + collect + continue.
    """
    succeeded = 0
    failed_permanent: list[dict] = []
    qb = await get_qb_client(tenant_id, db)
    try:
        for row_id in row_ids:
            try:
                await push_fn(tenant_id, row_id, db, qb)
                succeeded += 1
            except Exception as exc:
                if _is_transient(exc):
                    # Bubble — task-level handler decides retry policy.
                    raise
                _task_log.exception(
                    "sync_%s_permanent_error tenant=%s id=%s class=%s",
                    entity_label, tenant_id, row_id, exc.__class__.__name__,
                )
                failed_permanent.append({
                    f"{entity_label}_id": row_id,
                    "error_class": exc.__class__.__name__,
                    "message": str(exc)[:200],
                })
    finally:
        await qb.close()
    return succeeded, failed_permanent


async def _pull_payments_async(tenant_id: str, db) -> dict:
    qb = await get_qb_client(tenant_id, db)
    try:
        return await pull_payments(tenant_id, db, qb)
    finally:
        await qb.close()


def _skip_if_unhealthy(tenant_id: str, db, task_name: str) -> bool:
    """S122-13: short-circuit sync when the connection is in
    ``needs_reconnect`` or ``refresh_failed`` state. Pre-fix every Celery
    task burned an Intuit call + the configured retry count against a dead
    refresh_token. Returns True when the task should no-op.
    """
    if not connection_healthy(tenant_id, db):
        _task_log.info(
            "qb_sync_skipped_unhealthy_auth_state task=%s tenant=%s — user must reconnect QB",
            task_name, tenant_id,
        )
        return True
    return False


@celery_app.task(bind=True, max_retries=5, queue="low")
def sync_all_customers_task(self, tenant_id: str) -> dict:
    """Push every Customer to QuickBooks.

    S122-15: per-row error capture — transient errors retry the task;
    permanent errors log + continue.
    S122-16: ONE QBClient (one TCP + TLS handshake) shared across all rows
    via ``_run_push_loop`` — was previously a fresh client per row.
    """
    with _tenant_session(tenant_id) as db:
        if _skip_if_unhealthy(tenant_id, db, "sync_all_customers_task"):
            return {"skipped_unhealthy": True}
        # S122-17: only push customers that have changed since the last
        # successful sync. push_customer clears qb_dirty after each success;
        # the before_update listener re-flips it on any non-internal change.
        row_ids = [
            str(c.id)
            for c in db.query(Customer).filter(Customer.qb_dirty.is_(True)).all()
        ]
        try:
            succeeded, failed = asyncio.run(_run_push_loop(
                tenant_id, db, row_ids, push_customer, entity_label="customer",
            ))
        except Exception as exc:
            if _is_transient(exc):
                raise self.retry(
                    exc=exc, countdown=min(2 ** self.request.retries, 60),
                ) from None
            raise  # non-transient should have been caught inside the loop
    return {
        "succeeded": succeeded,
        "failed_permanent": failed,
        "failed_count": len(failed),
    }


@celery_app.task(bind=True, max_retries=5, queue="low")
def sync_all_invoices_task(self, tenant_id: str) -> dict:
    """Push every dirty Invoice to QuickBooks.

    S122-14: filter is ``Invoice.qb_dirty == True``.
    S122-15: per-row error capture.
    S122-16: one shared QBClient across all rows.
    """
    with _tenant_session(tenant_id) as db:
        if _skip_if_unhealthy(tenant_id, db, "sync_all_invoices_task"):
            return {"skipped_unhealthy": True}
        row_ids = [
            str(i.id) for i in db.query(Invoice).filter(Invoice.qb_dirty.is_(True)).all()
        ]
        try:
            succeeded, failed = asyncio.run(_run_push_loop(
                tenant_id, db, row_ids, push_invoice, entity_label="invoice",
            ))
        except Exception as exc:
            if _is_transient(exc):
                raise self.retry(
                    exc=exc, countdown=min(2 ** self.request.retries, 60),
                ) from None
            raise
    return {
        "succeeded": succeeded,
        "failed_permanent": failed,
        "failed_count": len(failed),
    }


@celery_app.task(bind=True, max_retries=5, queue="low")
def pull_payments_task(self, tenant_id: str) -> dict:
    with _tenant_session(tenant_id) as db:
        if _skip_if_unhealthy(tenant_id, db, "pull_payments_task"): return {}  # noqa: E701
        # `since` filtering is handled inside pull_payments using the sync-state timestamp
        _ = datetime.now(timezone.utc) - timedelta(hours=24)
        try:
            return asyncio.run(_pull_payments_async(tenant_id, db))
        except QBRateLimitError as e:
            raise self.retry(exc=e, countdown=min(2 ** self.request.retries, 60)) from None


# ─── S122-11 + S122-12: per-entity pull tasks for webhook dispatch ──────────
#
# Webhooks always mean "the QB side changed" — so the response is PULL FROM
# QB, not PUSH TO QB. Pre-fix the webhook routed Customer/Invoice events to
# sync_all_customers_task / sync_all_invoices_task, both of which PUSH every
# GDX row to QB. Wrong direction. The 5 other supported entity types
# (Payment, Item, Vendor, Account, JournalEntry) were silently dropped.
#
# Today each per-entity task wraps the existing full-table pull function;
# the ``entity_id`` argument is an optimization hint for the future single-
# entity-pull refactor (filed as D-S122-12-single-entity-pull). Calling the
# full pull means a "Customer X changed" webhook re-syncs every QB customer
# — wasteful, but correct. The waste is bounded by the webhook event volume
# (Intuit caps webhooks at ~600/hour/realm per their published policy).
#
# JournalEntry has no pull_* function in sync.py; the dispatcher logs that
# entity type as unhandled (filed D-S122-12-journalentry-pull).

async def _pull_customers_async(tenant_id: str, db) -> dict:
    qb = await get_qb_client(tenant_id, db)
    try:
        return await pull_customers(tenant_id, db, qb)
    finally:
        await qb.close()


async def _pull_invoices_async(tenant_id: str, db) -> dict:
    qb = await get_qb_client(tenant_id, db)
    try:
        return await pull_invoices(tenant_id, db, qb)
    finally:
        await qb.close()


async def _pull_items_async(tenant_id: str, db) -> dict:
    qb = await get_qb_client(tenant_id, db)
    try:
        return await pull_items(tenant_id, db, qb)
    finally:
        await qb.close()


async def _pull_vendors_async(tenant_id: str, db) -> dict:
    qb = await get_qb_client(tenant_id, db)
    try:
        return await pull_vendors(tenant_id, db, qb)
    finally:
        await qb.close()


async def _pull_accounts_async(tenant_id: str, db) -> dict:
    qb = await get_qb_client(tenant_id, db)
    try:
        return await pull_accounts(tenant_id, db, qb)
    finally:
        await qb.close()


@celery_app.task(bind=True, max_retries=5, queue="low")
def sync_customer_task(self, tenant_id: str, entity_id: str | None = None) -> dict:
    """Pull Customer changes from QB → GDX. Called by webhook for QB-side
    Customer create/update/delete events. ``entity_id`` is the QB customer id
    (hint for future single-entity pull).
    """
    with _tenant_session(tenant_id) as db:
        if _skip_if_unhealthy(tenant_id, db, "sync_customer_task"): return {}  # noqa: E701
        try:
            return asyncio.run(_pull_customers_async(tenant_id, db))
        except QBRateLimitError as e:
            raise self.retry(exc=e, countdown=min(2 ** self.request.retries, 60)) from None


@celery_app.task(bind=True, max_retries=5, queue="low")
def sync_invoice_task(self, tenant_id: str, entity_id: str | None = None) -> dict:
    """Pull Invoice changes from QB → GDX."""
    with _tenant_session(tenant_id) as db:
        if _skip_if_unhealthy(tenant_id, db, "sync_invoice_task"): return {}  # noqa: E701
        try:
            return asyncio.run(_pull_invoices_async(tenant_id, db))
        except QBRateLimitError as e:
            raise self.retry(exc=e, countdown=min(2 ** self.request.retries, 60)) from None


@celery_app.task(bind=True, max_retries=5, queue="low")
def sync_payment_task(self, tenant_id: str, entity_id: str | None = None) -> dict:
    """Pull Payment changes from QB → GDX."""
    with _tenant_session(tenant_id) as db:
        if _skip_if_unhealthy(tenant_id, db, "sync_payment_task"): return {}  # noqa: E701
        try:
            return asyncio.run(_pull_payments_async(tenant_id, db))
        except QBRateLimitError as e:
            raise self.retry(exc=e, countdown=min(2 ** self.request.retries, 60)) from None


@celery_app.task(bind=True, max_retries=5, queue="low")
def sync_item_task(self, tenant_id: str, entity_id: str | None = None) -> dict:
    """Pull Item (catalog) changes from QB → GDX."""
    with _tenant_session(tenant_id) as db:
        if _skip_if_unhealthy(tenant_id, db, "sync_item_task"): return {}  # noqa: E701
        try:
            return asyncio.run(_pull_items_async(tenant_id, db))
        except QBRateLimitError as e:
            raise self.retry(exc=e, countdown=min(2 ** self.request.retries, 60)) from None


@celery_app.task(bind=True, max_retries=5, queue="low")
def sync_vendor_task(self, tenant_id: str, entity_id: str | None = None) -> dict:
    """Pull Vendor changes from QB → GDX."""
    with _tenant_session(tenant_id) as db:
        if _skip_if_unhealthy(tenant_id, db, "sync_vendor_task"): return {}  # noqa: E701
        try:
            return asyncio.run(_pull_vendors_async(tenant_id, db))
        except QBRateLimitError as e:
            raise self.retry(exc=e, countdown=min(2 ** self.request.retries, 60)) from None


@celery_app.task(bind=True, max_retries=5, queue="low")
def sync_account_task(self, tenant_id: str, entity_id: str | None = None) -> dict:
    """Pull Chart-of-Accounts changes from QB → GDX."""
    with _tenant_session(tenant_id) as db:
        if _skip_if_unhealthy(tenant_id, db, "sync_account_task"): return {}  # noqa: E701
        try:
            return asyncio.run(_pull_accounts_async(tenant_id, db))
        except QBRateLimitError as e:
            raise self.retry(exc=e, countdown=min(2 ** self.request.retries, 60)) from None


# ---------------------------------------------------------------------------
# Banking + scheduled-sync dispatcher
# ---------------------------------------------------------------------------


@celery_app.task(bind=True, max_retries=3, queue="low")
def qb_banking_sync_task(self, tenant_id: str, start_date: str = "") -> dict:
    """Pull all banking data for one tenant (accounts + purchases + deposits
    + transfers). Called by the schedule dispatcher OR ad-hoc."""
    from gdx_dispatch.modules.quickbooks import banking as _banking
    from gdx_dispatch.modules.quickbooks.sync import pull_accounts, pull_bank_transactions

    async def _run(db):
        out: dict = {}

        async def _try(key, coro_factory):
            try:
                out[key] = await coro_factory()
            except Exception as exc:
                _task_log.exception("qb_banking_sync_pull_failed entity=%s tenant=%s", key, tenant_id)
                out[key] = {"created": 0, "updated": 0, "deleted": 0, "errors": [
                    {"qb_id": "*", "error": f"{type(exc).__name__}: {str(exc)[:240]}"},
                ]}

        async with await get_qb_client(tenant_id, db) as qb:
            await _try("accounts",        lambda: pull_accounts(tenant_id, db, qb))
            await _try("purchases",       lambda: pull_bank_transactions(tenant_id, db, qb, start_date, ""))
            await _try("deposits",        lambda: _banking.pull_deposits(tenant_id, db, qb, start_date, ""))
            await _try("transfers",       lambda: _banking.pull_transfers(tenant_id, db, qb, start_date, ""))
            await _try("bill_payments",   lambda: _banking.pull_bill_payments(tenant_id, db, qb, start_date, ""))
            await _try("sales_receipts",  lambda: _banking.pull_sales_receipts(tenant_id, db, qb, start_date, ""))
            await _try("refund_receipts", lambda: _banking.pull_refund_receipts(tenant_id, db, qb, start_date, ""))
            await _try("journal_entries", lambda: _banking.pull_journal_entries(tenant_id, db, qb, start_date, ""))
            await _try("customer_payments", lambda: _banking.pull_customer_payments(tenant_id, db, qb, start_date, ""))
            await _try("vendor_credits", lambda: _banking.pull_vendor_credits(tenant_id, db, qb, start_date, ""))
        return out

    with _tenant_session(tenant_id) as db:
        if _skip_if_unhealthy(tenant_id, db, "qb_banking_sync_task"):
            return {}
        try:
            result = asyncio.run(_run(db))
            _banking.record_scheduled_run(db, status="ok")
            return result
        except QBRateLimitError as e:
            # Don't roll next_run_at forward on a retryable failure — Celery
            # will re-invoke this task body, and an early record_scheduled_run
            # would advance the schedule before the actual sync succeeds.
            raise self.retry(exc=e, countdown=min(2 ** self.request.retries, 60)) from None
        except Exception as exc:
            _banking.record_scheduled_run(db, status="error", error=str(exc))
            raise


@celery_app.task(queue="low")
def qb_sync_schedule_dispatcher() -> dict:
    """Beat-fired dispatcher: walks every tenant DB, picks rows whose
    next_run_at has passed, queues a per-tenant banking sync. Frequency
    is encoded in how far next_run_at jumps after each successful run."""
    from sqlalchemy import text as _text

    from gdx_dispatch.modules.quickbooks.banking import FREQ_MANUAL

    queued: list[str] = []
    skipped_manual = 0
    skipped_not_due = 0

    from gdx_dispatch.core.tenant import single_tenant
    from gdx_dispatch.modules.quickbooks.banking import compute_next_run_at

    _t = single_tenant()
    tenants = [(_t["id"], _t["slug"])]

    for tid, slug in tenants:
        try:
            with _tenant_session(str(tid)) as db:
                row = db.execute(_text(
                    "SELECT frequency, next_run_at FROM qb_sync_schedule LIMIT 1"
                )).first()
                if row is None:
                    continue
                frequency, next_run_at = row[0], row[1]
                if frequency == FREQ_MANUAL or next_run_at is None:
                    skipped_manual += 1
                    continue
                if next_run_at > datetime.now(timezone.utc):
                    skipped_not_due += 1
                    continue
                # Advance next_run_at BEFORE queuing. If the sync is slow
                # (>5 min — longer than our dispatcher tick), the next
                # dispatcher pass sees next_run_at > now and skips this
                # tenant — preventing concurrent sync racing on upsert
                # and burning Intuit rate-limit. The post-sync
                # record_scheduled_run then rolls forward from the real
                # completion time.
                nra = compute_next_run_at(frequency)
                db.execute(_text(
                    "UPDATE qb_sync_schedule SET next_run_at = :nra, "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = (SELECT id FROM qb_sync_schedule LIMIT 1)"
                ), {"nra": nra})
                db.commit()
            qb_banking_sync_task.delay(str(tid))
            queued.append(slug)
        except Exception:
            _task_log.exception("qb_dispatcher_tenant_failed tenant=%s", tid)

    return {"queued": queued, "skipped_manual": skipped_manual, "skipped_not_due": skipped_not_due}

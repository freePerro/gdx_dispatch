"""QuickBooks sync errors — a leaf module: it imports nothing from gdx_dispatch.

`quickbooks/tasks.py` is imported by `core/celery_app.py` while models are
still loading, and `quickbooks/sync.py` is often the half-built module on that
chain. Importing these names from `sync` there failed, and
`core/webhooks/__init__.py` swallowed the ImportError by binding
`emit_webhook`, `deliver_webhook_task` and `retry_failed_webhooks_task` to
None (GDXA-1922). `sync` re-exports them, so `from ...sync import QBSyncError`
still works.

`client.QBRateLimitError` is a different class (an HTTP 429 from the API,
a `QBAPIError`); this one is a sync-level rate limit. Keep them apart.
"""
from __future__ import annotations


class QBSyncError(Exception):
    pass


class QBRateLimitError(QBSyncError):
    pass


class QBPullDisabledError(QBSyncError):
    """A money-mutating QB pull was attempted while ledger posting is on.

    GL S9 (spec §5.4): once ``ledger_posting_enabled`` the GDX ledger is the
    book of record — pulls that mutate Invoice/Payment rows would write money
    around the posting chokepoint, so they fail loudly instead. Read-only
    mirror pulls (accounts, bank transactions) and non-money entity pulls
    (customers, items, vendors) are unaffected. QBO-side corrections flow
    forward from GDX (credit memo / void / adjustment → push), never back.
    """

"""Sprint 1a — domain-event emission + delivery-core repair.

Covers the adversarial-audit blockers:
  F1  fan-out to two receivers must not raise IntegrityError
  —   duplicate re-emit is swallowed by the SAVEPOINT (no-op)
  F2  after_commit dispatch fires only on commit; rollback drops it;
      the retry sweep rescues rows stranded pending/NULL next_retry_at
  F5  redirect targets are re-validated by the SSRF guard
  QC1 async subscription deliveries land in webhook_delivery_logs (UI table)
  F7  a subscription without a secret is auto-signed; NULL secret never sent
"""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import utcnow
from gdx_dispatch.core.ssrf_guard import OutboundURLBlocked
from gdx_dispatch.core.webhooks import tasks as tasks_mod
from gdx_dispatch.core.webhooks.delivery import _ValidatingRedirectHandler, deliver_webhook
from gdx_dispatch.core.webhooks.emit import emit_domain_event, install_webhook_dispatch_hook
from gdx_dispatch.core.webhooks.models import AIAction, WebhookDelivery, WebhookEndpoint
from gdx_dispatch.routers.webhooks import WebhookDeliveryLog, WebhookSubscription

TENANT = "tenant-emit-001"


def _session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    # SQLAlchemy's documented SQLite-SAVEPOINT recipe: pysqlite's implicit BEGIN
    # otherwise breaks nested-transaction rollback, so a SAVEPOINT release would
    # wrongly persist. Production is Postgres (correct natively); this makes the
    # test faithfully exercise emit_domain_event's begin_nested() containment.
    @event.listens_for(engine, "connect")
    def _sqlite_no_implicit_begin(dbapi_conn, _rec):  # noqa: ANN001
        dbapi_conn.isolation_level = None

    @event.listens_for(engine, "begin")
    def _emit_real_begin(conn):  # noqa: ANN001
        conn.exec_driver_sql("BEGIN")

    for tbl in (
        WebhookSubscription.__table__,
        WebhookDeliveryLog.__table__,
        WebhookDelivery.__table__,
        WebhookEndpoint.__table__,
        AIAction.__table__,
    ):
        tbl.create(bind=engine, checkfirst=True)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _sub(db, events, *, secret="s3cr3t", active=True, deleted=False, tenant=TENANT):
    sub = WebhookSubscription(
        company_id=tenant,
        name="hook",
        url="https://example.test/hook",
        secret=secret,
        events=json.dumps(events),
        active=active,
        deleted_at=utcnow() if deleted else None,
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


# ---------------------------------------------------------------------------
# F1 — fan-out
# ---------------------------------------------------------------------------

def test_fanout_two_receivers_no_integrityerror():
    db = _session()
    install_webhook_dispatch_hook()
    _sub(db, ["invoice.paid"])
    _sub(db, ["invoice.paid"])
    with patch.object(tasks_mod.deliver_webhook_task, "delay") as delay:
        n = emit_domain_event(db, "invoice.paid", "inv-1", {"invoice_id": "inv-1"}, tenant_id=TENANT)
        db.commit()
    assert n == 2
    assert db.execute(select(WebhookDelivery)).scalars().all().__len__() == 2
    assert delay.call_count == 2  # dispatched on commit, once per receiver


def test_idempotency_key_not_truncated_for_uuid_ids():
    # Real tenant/entity ids are UUIDs; a raw "{tenant}:{event}:{entity}:{sub}"
    # string overruns the 100-char column. Two subscribers must still receive
    # DISTINCT keys — a [:100] slice would chop the sub-id suffix and merge them
    # (audit F(c), silently swallowed by the SAVEPOINT). Hashing fixes it.
    db = _session()
    install_webhook_dispatch_hook()
    big_tenant = str(uuid4())
    _sub(db, ["invoice.paid"], tenant=big_tenant)
    _sub(db, ["invoice.paid"], tenant=big_tenant)
    with patch.object(tasks_mod.deliver_webhook_task, "delay"):
        n = emit_domain_event(db, "invoice.paid", str(uuid4()), {}, tenant_id=big_tenant)
        db.commit()
    assert n == 2
    keys = {d.idempotency_key for d in db.execute(select(WebhookDelivery)).scalars().all()}
    assert len(keys) == 2  # distinct per subscription — not collapsed by truncation
    assert all(len(k) <= 100 for k in keys)


def test_only_matching_subscriptions_receive():
    db = _session()
    install_webhook_dispatch_hook()
    _sub(db, ["invoice.paid"])
    _sub(db, ["estimate.accepted"])
    _sub(db, ["invoice.paid"], active=False)
    _sub(db, ["invoice.paid"], deleted=True)
    with patch.object(tasks_mod.deliver_webhook_task, "delay"):
        n = emit_domain_event(db, "invoice.paid", "inv-2", {}, tenant_id=TENANT)
        db.commit()
    assert n == 1  # only the active, non-deleted, matching one


def test_suppress_and_no_tenant_emit_nothing():
    db = _session()
    _sub(db, ["invoice.paid"])
    assert emit_domain_event(db, "invoice.paid", "x", {}, tenant_id=TENANT, suppress=True) == 0
    assert emit_domain_event(db, "invoice.paid", "x", {}, tenant_id=None) == 0
    assert db.execute(select(WebhookDelivery)).scalars().all() == []


# ---------------------------------------------------------------------------
# duplicate re-emit — SAVEPOINT swallow
# ---------------------------------------------------------------------------

def test_duplicate_emit_swallowed():
    db = _session()
    install_webhook_dispatch_hook()
    _sub(db, ["invoice.paid"])
    with patch.object(tasks_mod.deliver_webhook_task, "delay"):
        assert emit_domain_event(db, "invoice.paid", "inv-1", {}, tenant_id=TENANT) == 1
        db.commit()
        # same entity+subscription again → duplicate idempotency_key → swallowed
        assert emit_domain_event(db, "invoice.paid", "inv-1", {}, tenant_id=TENANT) == 0
        db.commit()
    assert len(db.execute(select(WebhookDelivery)).scalars().all()) == 1
    # the caller's session is still usable after the swallowed IntegrityError
    assert db.execute(select(WebhookSubscription)).scalars().first() is not None


# ---------------------------------------------------------------------------
# F2 — commit/rollback dispatch semantics
# ---------------------------------------------------------------------------

def test_rollback_drops_dispatch():
    db = _session()
    install_webhook_dispatch_hook()
    _sub(db, ["invoice.paid"])
    with patch.object(tasks_mod.deliver_webhook_task, "delay") as delay:
        emit_domain_event(db, "invoice.paid", "inv-1", {}, tenant_id=TENANT)
        db.rollback()
    assert delay.call_count == 0
    assert db.execute(select(WebhookDelivery)).scalars().all() == []


def test_retry_sweep_rescues_stranded_null_next_retry():
    db = _session()
    old = utcnow() - timedelta(minutes=5)
    stranded = WebhookDelivery(
        company_id=TENANT, subscription_id=str(uuid4()), event_type="invoice.paid",
        payload={}, idempotency_key="k-stranded", status="pending",
        next_retry_at=None, created_at=old,
    )
    fresh = WebhookDelivery(  # within grace — must NOT be swept yet
        company_id=TENANT, subscription_id=str(uuid4()), event_type="invoice.paid",
        payload={}, idempotency_key="k-fresh", status="pending",
        next_retry_at=None, created_at=utcnow(),
    )
    due = WebhookDelivery(  # normal backoff elapsed
        company_id=TENANT, subscription_id=str(uuid4()), event_type="invoice.paid",
        payload={}, idempotency_key="k-due", status="pending",
        next_retry_at=utcnow() - timedelta(seconds=1), created_at=old,
    )
    db.add_all([stranded, fresh, due])
    db.commit()
    swept: list[str] = []

    class _CM:
        def __enter__(self_):  # noqa: N805
            return db
        def __exit__(self_, *a):  # noqa: N805
            return False

    with patch.object(tasks_mod, "_tenant_session", return_value=_CM()), \
         patch.object(tasks_mod.deliver_webhook_task, "delay", side_effect=lambda i: swept.append(i)):
        total = tasks_mod.retry_failed_webhooks_task()
    assert total == 2
    assert str(stranded.id) in swept
    assert str(due.id) in swept
    assert str(fresh.id) not in swept  # grace window protects the happy path


# ---------------------------------------------------------------------------
# F5 — redirect re-validation
# ---------------------------------------------------------------------------

def test_redirect_handler_revalidates_target():
    handler = _ValidatingRedirectHandler()
    with pytest.raises(OutboundURLBlocked):
        # a redirect pointing at an internal host must be refused
        handler.redirect_request(None, None, 307, "x", {}, "http://127.0.0.1:8000/internal/events")


# ---------------------------------------------------------------------------
# QC1 — UI delivery-log visibility + F7 signing
# ---------------------------------------------------------------------------

def test_subscription_delivery_logs_and_signs():
    db = _session()
    sub = _sub(db, ["invoice.paid"], secret="mysecret")
    delivery = WebhookDelivery(
        company_id=TENANT, subscription_id=str(sub.id), event_type="invoice.paid",
        payload={"event": "invoice.paid", "data": {"invoice_id": "inv-9"}},
        idempotency_key="k1", status="pending",
    )
    db.add(delivery)
    db.commit()

    sent = {}

    def _fake_post(url, payload, headers):
        sent["url"] = url
        sent["headers"] = headers
        return 200

    with patch("gdx_dispatch.core.webhooks.delivery._post", _fake_post):
        asyncio.run(deliver_webhook(str(delivery.id), db))

    db.refresh(delivery)
    assert delivery.status == "delivered"
    assert sent["url"] == sub.url
    assert sent["headers"]["X-GDX-Signature"].startswith("sha256=")  # always signed
    # the delivery is visible in the table the WebhooksView "Deliveries" tab reads
    logs = db.execute(select(WebhookDeliveryLog)).scalars().all()
    assert len(logs) == 1
    assert logs[0].delivery_status == "delivered"
    assert str(logs[0].subscription_id) == str(sub.id)


def test_null_secret_delivery_fails_closed():
    db = _session()
    sub = _sub(db, ["invoice.paid"], secret=None)
    delivery = WebhookDelivery(
        company_id=TENANT, subscription_id=str(sub.id), event_type="invoice.paid",
        payload={"event": "invoice.paid", "data": {}}, idempotency_key="k2", status="pending",
    )
    db.add(delivery)
    db.commit()
    with patch("gdx_dispatch.core.webhooks.delivery._post", return_value=200) as post:
        asyncio.run(deliver_webhook(str(delivery.id), db))
    db.refresh(delivery)
    assert delivery.status == "failed"  # never sent unsigned
    assert post.call_count == 0

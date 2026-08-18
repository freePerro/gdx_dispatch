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
from types import SimpleNamespace
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


def test_suppress_domain_events_contextvar_silences():
    from gdx_dispatch.core.webhooks.emit import suppress_domain_events

    db = _session()
    install_webhook_dispatch_hook()
    _sub(db, ["invoice.paid"])
    with patch.object(tasks_mod.deliver_webhook_task, "delay"):
        with suppress_domain_events():
            assert emit_domain_event(db, "invoice.paid", "x", {}, tenant_id=TENANT) == 0
        # outside the block it emits normally
        assert emit_domain_event(db, "invoice.paid", "x", {}, tenant_id=TENANT) == 1
        db.commit()


# ---------------------------------------------------------------------------
# Sprint 1b — choke-point wiring (invoice.paid at transition_invoice_status)
# ---------------------------------------------------------------------------

def test_transition_to_paid_emits_invoice_paid():
    from gdx_dispatch.modules.ledger import service as ledger_service

    inv = SimpleNamespace(id="inv-1", company_id="t1", status="sent", total=100.0, invoice_number="INV-1")
    fake_session = SimpleNamespace(info={})
    calls = []
    with patch.object(ledger_service, "ledger_posting_enabled", return_value=False), \
         patch("gdx_dispatch.core.webhooks.emit.emit_domain_event",
               side_effect=lambda *a, **k: calls.append((a, k)) or 0):
        old = ledger_service.transition_invoice_status(fake_session, inv, "paid")
    assert old == "sent"
    assert len(calls) == 1
    assert calls[0][0][1] == "invoice.paid"      # event_type (positional)
    assert calls[0][1]["tenant_id"] == "t1"


def test_transition_to_sent_does_not_emit():
    from gdx_dispatch.modules.ledger import service as ledger_service

    inv = SimpleNamespace(id="inv-2", company_id="t1", status="draft", total=100.0, invoice_number="INV-2")
    fake_session = SimpleNamespace(info={})
    calls = []
    with patch.object(ledger_service, "ledger_posting_enabled", return_value=False), \
         patch("gdx_dispatch.core.webhooks.emit.emit_domain_event",
               side_effect=lambda *a, **k: calls.append(1)):
        ledger_service.transition_invoice_status(fake_session, inv, "sent")
    assert calls == []  # only the paid transition emits


def test_transition_paid_to_paid_is_noop():
    from gdx_dispatch.modules.ledger import service as ledger_service

    inv = SimpleNamespace(id="inv-3", company_id="t1", status="paid", total=1.0, invoice_number="INV-3")
    fake_session = SimpleNamespace(info={})
    calls = []
    with patch.object(ledger_service, "ledger_posting_enabled", return_value=False), \
         patch("gdx_dispatch.core.webhooks.emit.emit_domain_event",
               side_effect=lambda *a, **k: calls.append(1)):
        ledger_service.transition_invoice_status(fake_session, inv, "paid")
    assert calls == []  # already paid → no re-emit


def test_transition_to_paid_stages_real_delivery_unmocked():
    # The mocked tests above prove the choke point CALLS emit; this one runs the
    # REAL emit on a REAL session with a live subscription — exercising the
    # select, the SAVEPOINT, and after_commit dispatch (audit: "tests are theater"
    # otherwise). Only emit_domain_event is unmocked; ledger flag + Celery delay
    # are stubbed (no gl_settings table / no broker in the unit harness).
    from gdx_dispatch.modules.ledger import service as ledger_service

    db = _session()
    install_webhook_dispatch_hook()
    _sub(db, ["invoice.paid"])
    inv = SimpleNamespace(
        id=str(uuid4()), company_id=TENANT, status="sent",
        total=250.0, invoice_number="INV-77", billing_type="standard",
    )
    with patch.object(ledger_service, "ledger_posting_enabled", return_value=False), \
         patch.object(tasks_mod.deliver_webhook_task, "delay") as delay:
        ledger_service.transition_invoice_status(db, inv, "paid")
        db.commit()
    rows = db.execute(select(WebhookDelivery)).scalars().all()
    assert len(rows) == 1
    assert rows[0].event_type == "invoice.paid"
    assert rows[0].payload["data"]["invoice_id"] == inv.id
    assert rows[0].payload["data"]["billing_type"] == "standard"
    assert delay.call_count == 1  # dispatched on the business commit


def test_customer_id_is_flush_time_default():
    # Documents the BLOCKER root cause + why create_customer must flush before
    # emitting: Customer.id is a flush-time uuid4 default, None at construction.
    from gdx_dispatch.models.tenant_models import Customer

    c = Customer(name="Acme", company_id=TENANT)
    assert c.id is None  # → emit before flush would ship customer_id="None"


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


def test_emit_still_dispatches_when_consent_probe_savepoint_rolls_back():
    # Regression: on a FRESH box the plugin_consent table doesn't exist, so
    # any_event_consent's begin_nested SELECT fails and rolls back its SAVEPOINT.
    # after_rollback fires on savepoint rollbacks too — an earlier version cleared
    # the staged webhook dispatch there, so webhooks silently NEVER fired on a
    # fresh Postgres box. The after_soft_rollback + `nested` guard keeps pending
    # across savepoint rollbacks. This _session() has NO plugin_consent table, so
    # the probe savepoint really does roll back.
    db = _session()
    install_webhook_dispatch_hook()
    _sub(db, ["invoice.paid"])
    with patch.object(tasks_mod.deliver_webhook_task, "delay") as delay:
        n = emit_domain_event(db, "invoice.paid", "inv-fresh", {}, tenant_id=TENANT)
        db.commit()
    assert n == 1
    assert delay.call_count == 1  # dispatched despite the probe savepoint rollback


def test_emit_never_commits_callers_txn_with_consent_table_present():
    # Regression: the plugin-sink consent check must be READ-ONLY. An earlier
    # version called ensure_consent_table() (which commits) in the hot path,
    # which would commit the caller's half-built invoice/job mid-emit. With the
    # consent table present so the real SELECT runs, a rollback must still
    # discard the staged delivery — proving emit committed nothing.
    from gdx_dispatch.core.plugin_consent import ensure_consent_table

    db = _session()
    install_webhook_dispatch_hook()
    ensure_consent_table(db)  # commits; happens BEFORE emit
    _sub(db, ["invoice.paid"])
    with patch.object(tasks_mod.deliver_webhook_task, "delay"):
        emit_domain_event(db, "invoice.paid", "inv-x", {}, tenant_id=TENANT)
        db.rollback()
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


def test_webhook_private_allow_is_exact_host_only(monkeypatch):
    from gdx_dispatch.core.ssrf_guard import (
        OutboundURLBlocked,
        validate_outbound_url,
        webhook_allow_hosts,
    )

    monkeypatch.setenv("GDX_WEBHOOK_PRIVATE_ALLOW", "n8n")
    allow = webhook_allow_hosts()
    assert allow == frozenset({"n8n"})
    # exact set membership only — look-alikes never match
    assert "n8n" in allow and "eviln8n" not in allow and "n8n.attacker.com" not in allow
    # the allowlisted internal host is permitted (short-circuits before DNS)
    validate_outbound_url("http://n8n:5678/webhook/abc", allow)  # no raise
    # non-allowlisted literal private/link-local addresses stay blocked
    with pytest.raises(OutboundURLBlocked):
        validate_outbound_url("http://169.254.169.254/latest/meta-data", allow)
    with pytest.raises(OutboundURLBlocked):
        validate_outbound_url("http://127.0.0.1:6379/", allow)


def test_webhook_private_allow_empty_by_default(monkeypatch):
    from gdx_dispatch.core.ssrf_guard import webhook_allow_hosts

    monkeypatch.delenv("GDX_WEBHOOK_PRIVATE_ALLOW", raising=False)
    assert webhook_allow_hosts() == frozenset()


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

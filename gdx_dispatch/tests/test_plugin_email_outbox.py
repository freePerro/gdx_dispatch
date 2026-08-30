"""Plugin email outbox — full access, consent-gated, auditable (Phase 6).

Locked 2026-08-18: plugins get email through an outbox on the shared DB
(plugin-host has no egress); core drains it through the unified pipeline;
everything is auditable; the automation toggle does NOT gate plugin sends —
the "email" install consent does.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select

from gdx_dispatch.models.tenant_models import Customer, PluginEmailOutbox
from gdx_dispatch.plugin_api.email import queue_email
from gdx_dispatch.tasks import plugin_email_outbox as drain_mod

TENANT = "33333333-3333-3333-3333-333333333333"


@pytest.fixture()
def db(tenant_db):
    return tenant_db


def _drain(db, monkeypatch, *, consented=True, send_result=(True, "outlook_graph", None)):
    """Run the drain body against the test session (the celery task uses
    SessionLocal; tests drive the internals directly)."""
    captured: dict = {}

    def _fake_send(**kw):
        captured.update(kw)
        return send_result

    import gdx_dispatch.core.transactional_email as te
    monkeypatch.setattr(te, "send_transactional_email", _fake_send)
    monkeypatch.setattr(drain_mod, "_consented", lambda _db, key: consented)

    from gdx_dispatch.core.audit import utcnow
    rows = db.execute(
        select(PluginEmailOutbox).where(PluginEmailOutbox.status == "queued")
        .order_by(PluginEmailOutbox.created_at)
    ).scalars().all()
    counts = {"sent": 0, "failed": 0, "retried": 0}
    for row in rows:
        ok, reason = drain_mod._deliver(db, row)
        row.attempts = (row.attempts or 0) + 1
        row.processed_at = utcnow()
        if ok:
            row.status, row.last_error = "sent", None
            counts["sent"] += 1
        elif reason in drain_mod._PERMANENT or row.attempts >= drain_mod.MAX_ATTEMPTS:
            row.status, row.last_error = "failed", (reason or "send_failed")[:120]
            counts["failed"] += 1
        else:
            row.last_error = (reason or "send_failed")[:120]
            counts["retried"] += 1
        db.commit()
    return counts, captured


def test_queue_email_validates_and_is_idempotent(db):
    r1 = queue_email(db, tenant_id=TENANT, plugin_key="p1", delivery_id="d-1",
                     subject="Hello", body_text="hi", to_email="x@example.com")
    assert r1["queued"] is True
    r2 = queue_email(db, tenant_id=TENANT, plugin_key="p1", delivery_id="d-1",
                     subject="Hello", body_text="hi", to_email="x@example.com")
    assert r2 == {"queued": False, "reason": "duplicate_delivery_id"}
    assert queue_email(db, tenant_id=TENANT, plugin_key="p1", delivery_id="d-2",
                       subject="s", body_text="a", body_html="<b>a</b>",
                       to_email="x@example.com")["reason"] == "exactly_one_of_body_text_or_body_html"
    assert queue_email(db, tenant_id=TENANT, plugin_key="p1", delivery_id="d-3",
                       subject="s", body_text="a")["reason"] == "no_recipient"


def test_drain_sends_branded_body_text_with_plugin_identity(db, monkeypatch):
    queue_email(db, tenant_id=TENANT, plugin_key="gdx-plugin-n8n", delivery_id="d-10",
                subject="Job update", body_text="Line one\n\nLine two",
                to_email="cust@example.com", entity_type="job", entity_id="j-1")
    counts, captured = _drain(db, monkeypatch)
    assert counts == {"sent": 1, "failed": 0, "retried": 0}
    assert captured["initiator_kind"] == "plugin"
    assert captured["initiator_ref"] == "gdx-plugin-n8n"
    assert captured["kind"] == "plugin"
    assert captured["entity_type"] == "job"
    # body_text rides the branded shell
    assert '<table role="presentation"' in captured["html_body"]
    assert "Line one" in captured["html_body"]


def test_drain_body_html_is_sent_raw(db, monkeypatch):
    queue_email(db, tenant_id=TENANT, plugin_key="p1", delivery_id="d-11",
                subject="Raw", body_html="<div>my exact markup</div>",
                to_email="cust@example.com")
    counts, captured = _drain(db, monkeypatch)
    assert counts["sent"] == 1
    # Full access: no shell wrapper imposed on body_html.
    assert captured["html_body"] == "<div>my exact markup</div>"


def test_drain_without_consent_fails_closed(db, monkeypatch):
    queue_email(db, tenant_id=TENANT, plugin_key="rogue", delivery_id="d-12",
                subject="s", body_text="b", to_email="x@example.com")
    counts, captured = _drain(db, monkeypatch, consented=False)
    assert counts == {"sent": 0, "failed": 1, "retried": 0}
    row = db.execute(select(PluginEmailOutbox).where(
        PluginEmailOutbox.delivery_id == "d-12")).scalars().one()
    assert row.status == "failed" and row.last_error == "consent_missing"
    assert not captured  # nothing was sent


def test_drain_resolves_customer_through_recipient_resolver(db, monkeypatch):
    cust = Customer(id=uuid4(), name="Acme Lumber Yard",
                    email="front@acme.example", company_id=TENANT)
    db.add(cust)
    db.commit()
    queue_email(db, tenant_id=TENANT, plugin_key="p1", delivery_id="d-13",
                subject="s", body_text="Hi {name}!", customer_id=str(cust.id))
    counts, captured = _drain(db, monkeypatch)
    assert counts["sent"] == 1
    assert captured["to_email"] == "front@acme.example"
    assert "Hi Acme Lumber Yard!" in captured["html_body"]


def test_transient_failure_requeues_then_fails(db, monkeypatch):
    queue_email(db, tenant_id=TENANT, plugin_key="p1", delivery_id="d-14",
                subject="s", body_text="b", to_email="x@example.com")
    for expected in ("retried", "retried", "failed"):
        counts, _ = _drain(db, monkeypatch,
                           send_result=(False, None, "outlook_send_failed"))
        assert counts[expected] == 1, expected


def test_real_drain_claims_rows_atomically(db, monkeypatch):
    """Audit round 2: an unclaimed batch let an overlapping beat re-send the
    still-queued tail. Rows are now claimed queued→sending before delivery,
    and a fresh 'sending' claim is NOT picked up by another drain."""
    from datetime import timedelta

    from gdx_dispatch.core.audit import utcnow

    class _Ctx:
        def __enter__(self):
            return db

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(drain_mod, "SessionLocal", lambda: _Ctx())
    monkeypatch.setattr(drain_mod, "_consented", lambda _db, key: True)
    import gdx_dispatch.core.transactional_email as te
    monkeypatch.setattr(te, "send_transactional_email",
                        lambda **kw: (True, "outlook_graph", None))

    queue_email(db, tenant_id=TENANT, plugin_key="p1", delivery_id="claim-1",
                subject="s", body_text="b", to_email="x@example.com")
    out = drain_mod.drain_plugin_email_outbox()
    assert out == {"sent": 1, "failed": 0, "retried": 0}
    row = db.execute(select(PluginEmailOutbox).where(
        PluginEmailOutbox.delivery_id == "claim-1")).scalars().one()
    assert row.status == "sent"

    # A row another worker JUST claimed must be skipped...
    queue_email(db, tenant_id=TENANT, plugin_key="p1", delivery_id="claim-2",
                subject="s", body_text="b", to_email="x@example.com")
    claimed = db.execute(select(PluginEmailOutbox).where(
        PluginEmailOutbox.delivery_id == "claim-2")).scalars().one()
    claimed.status = "sending"
    claimed.processed_at = utcnow()
    db.commit()
    out = drain_mod.drain_plugin_email_outbox()
    assert out == {"sent": 0, "failed": 0, "retried": 0}
    # ...but a STALE claim (crashed worker) is reclaimed and delivered.
    claimed.processed_at = utcnow() - timedelta(minutes=drain_mod.STALE_CLAIM_MINUTES + 1)
    db.commit()
    out = drain_mod.drain_plugin_email_outbox()
    assert out["sent"] == 1


def test_delivery_id_scoped_per_plugin(db):
    """Two plugins may use the same natural key without eating each other's
    mail (globally-unique delivery_id was the audit's finding)."""
    a = queue_email(db, tenant_id=TENANT, plugin_key="plugin-a", delivery_id="welcome:c1",
                    subject="s", body_text="b", to_email="x@example.com")
    b = queue_email(db, tenant_id=TENANT, plugin_key="plugin-b", delivery_id="welcome:c1",
                    subject="s", body_text="b", to_email="x@example.com")
    assert a["queued"] is True and b["queued"] is True
    dup = queue_email(db, tenant_id=TENANT, plugin_key="plugin-a", delivery_id="welcome:c1",
                      subject="s", body_text="b", to_email="x@example.com")
    assert dup["reason"] == "duplicate_delivery_id"


# ── "Leave us a Google review" footer (2026-08-30) ───────────────────────────

_REVIEW_URL = "https://search.google.com/local/writereview?placeid=TEST_PLACE_ID"


def _set_review_url(db):
    from gdx_dispatch.models.tenant_models import AppSettings

    row = db.query(AppSettings).first()
    if row is None:
        row = AppSettings(company_name="Acme Door Co")
        db.add(row)
    row.google_review_url = _REVIEW_URL
    db.commit()


def test_drain_to_customer_record_carries_review_footer(db, monkeypatch):
    _set_review_url(db)
    cust = Customer(id=uuid4(), name="Review Customer",
                    email="rc@example.com", company_id=TENANT)
    db.add(cust)
    db.commit()
    queue_email(db, tenant_id=TENANT, plugin_key="p1", delivery_id="d-30",
                subject="s", body_text="Hi {name}!", customer_id=str(cust.id))
    counts, captured = _drain(db, monkeypatch)
    assert counts["sent"] == 1
    assert "Leave us a Google review" in captured["html_body"]
    assert _REVIEW_URL in captured["html_body"]


def test_drain_raw_override_address_gets_no_review_footer(db, monkeypatch):
    """A plugin's own notification (digest to the office, alert to a tech)
    is addressed by raw to_email with no customer — it must not ask the
    office to review the company."""
    _set_review_url(db)
    queue_email(db, tenant_id=TENANT, plugin_key="p1", delivery_id="d-31",
                subject="Nightly digest", body_text="3 jobs closed today",
                to_email="office@example.com")
    counts, captured = _drain(db, monkeypatch)
    assert counts["sent"] == 1
    assert '<table role="presentation"' in captured["html_body"], "still the branded shell"
    assert "Leave us a Google review" not in captured["html_body"]
    assert _REVIEW_URL not in captured["html_body"]

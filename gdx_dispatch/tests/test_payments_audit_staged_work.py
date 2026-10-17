"""GDXA-67: a refused audit row must not cost a Stripe webhook its money.

``_audit_payment_reversal`` (reversal, dispute reinstatement) and
``_audit_money_event`` (payment on a void invoice, overcharge) both audit while
their caller still has the money movement staged, then the caller commits. They
used to hand-roll the write: a bare ``try/except`` that handed back a
deactivated session (so the caller's commit raised and the reversal was lost),
and a savepoint behind a hoisted ``ensure_audit_table`` that committed the
payment early on first use. Both now go through
``core/audit.audit_best_effort_precommit``.

Rules these tests keep (GDXA-59/63/67):

* The refusal comes from the storage layer, a real ``BEFORE INSERT`` trigger
  on ``audit_logs``, never a Python patch, and a control proves it fires.
* Success is the MUTATION, read back through a second engine on the same
  file, so "committed" means committed and not "still in this session's
  identity map". "No audit row and no exception" also passes when the money
  is gone, so it is never the success signal here.
* Trail rows are asserted by content, and each reversal test says which shape
  it is: A (one payment, the invoice re-opens) or B (two live payments, the
  invoice stays paid).
* Every test runs on SQLite and on Postgres (GDXA-1394). The Postgres database
  is built from the ORM, which carries no audit immutability guard (migration
  107 installs it), so ``ensure_audit_table`` installs it on its own
  connection while the payment is staged: the ``guard_present=false`` path
  neither prod nor demo can show.
  The Postgres arm skips without a reachable server and fails under CI.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core.audit import AuditLog, TenantBase, audit_guard_present, log_audit_event_sync
from gdx_dispatch.core.payments import handle_payment_webhook
from gdx_dispatch.models.tenant_models import Customer, Invoice, InvoiceLine, Payment

COMPANY = "tenant-test"


def _sqlite_refusal(table, event, message):
    return [
        f"""
        CREATE TRIGGER {table}_refuse_{event.lower()}
        BEFORE {event} ON {table}
        BEGIN
            SELECT RAISE(ABORT, '{message}');
        END;
        """
    ]


def _pg_refusal(table, event, message):
    name = f"{table}_refuse_{event.lower()}"
    return [
        f"""
        CREATE FUNCTION {name}() RETURNS trigger AS $$
        BEGIN RAISE EXCEPTION '{message}'; END;
        $$ LANGUAGE plpgsql
        """,
        f"""
        CREATE TRIGGER {name} BEFORE {event} ON {table}
            FOR EACH ROW EXECUTE FUNCTION {name}()
        """,
    ]


@pytest.fixture(params=["sqlite", "postgresql"])
def dbfile(request, tmp_path):
    """A tenant DB the test does not share: the session under test, plus a
    reader on a second engine. Yields (session, reader, refuse) where
    ``refuse(table="audit_logs", event="INSERT", message=...)`` installs a
    storage-layer refusal in the dialect's own trigger syntax."""
    if request.param == "sqlite":
        url = f"sqlite:///{tmp_path / 'tenant.db'}"
        kwargs = {"connect_args": {"check_same_thread": False}}
        refusal = _sqlite_refusal
    else:
        url = request.getfixturevalue("pg_test_db")
        kwargs = {}
        refusal = _pg_refusal
    engine = create_engine(url, **kwargs)
    if request.param == "postgresql":
        # structure.sql has drifted from the ORM (missing ``gl_settings``,
        # missing ``customers`` columns, a varchar ``users.id``), so the clone
        # is emptied and built from the ORM like the SQLite file.
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
    TenantBase.metadata.create_all(engine, checkfirst=True)
    if request.param == "postgresql":
        with engine.connect() as conn:
            assert audit_guard_present(conn) is False, (
                "setup: the database must start unguarded, or the guard-install "
                "path under a staged payment is never exercised"
            )
    reader_engine = create_engine(url, **kwargs)
    # Production's sessionmaker leaves expire_on_commit at its default True.
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    reader = sessionmaker(bind=reader_engine)

    def refuse(table="audit_logs", event="INSERT", message="audit storage refuses this row"):
        with engine.begin() as conn:
            for stmt in refusal(table, event, message):
                conn.execute(text(stmt))

    yield session, reader, refuse
    session.close()
    engine.dispose()
    reader_engine.dispose()


def _customer(db):
    """A real customer: Postgres enforces ``invoices.customer_id``. No job —
    the webhook path never reads ``invoice.job_id``."""
    cust = Customer(id=uuid.uuid4(), name="GDXA-67 Customer", company_id=COMPANY)
    db.add(cust)
    db.flush()
    return cust.id


def _invoice(db, number, amount=500.00):
    """A sent invoice with a real line, so `_recalculate_invoice` keeps its total."""
    inv = Invoice(
        customer_id=_customer(db), invoice_number=number,
        billing_type="standard", subtotal=amount, tax_amount=0, total=amount,
        balance_due=amount, status="sent", public_token=uuid.uuid4().hex,
        company_id=COMPANY,
    )
    db.add(inv)
    db.flush()
    db.add(InvoiceLine(
        invoice_id=inv.id, description="Work", quantity=1, unit_price=amount,
        line_total=amount, taxable=False, company_id=COMPANY,
    ))
    db.commit()
    return inv.id


def _void_invoice(db, number):
    inv = Invoice(
        customer_id=_customer(db), invoice_number=number,
        billing_type="standard", subtotal=150.00, tax_amount=12.00, total=162.00,
        balance_due=162.00, status="void", public_token=uuid.uuid4().hex,
        company_id=COMPANY,
    )
    db.add(inv)
    db.commit()
    return inv.id


def _succeeded(invoice_id, reference, cents):
    return {
        "type": "payment_intent.succeeded",
        "data": {"object": {
            "id": reference, "metadata": {"invoice_id": str(invoice_id)},
            "amount_received": cents,
        }},
    }


def _refunded(reference, cents):
    return {
        "type": "charge.refunded",
        "data": {"object": {
            "id": f"ch_{reference}", "payment_intent": reference,
            "amount": cents, "amount_refunded": cents,
        }},
    }


def _dispute(kind, reference, cents):
    return {
        "type": f"charge.dispute.funds_{kind}",
        "data": {"object": {"id": f"dp_{reference}", "payment_intent": reference, "amount": cents}},
    }


def _committed_invoice(reader, invoice_id):
    with reader() as r:
        inv = r.get(Invoice, invoice_id)
        return inv.status, float(inv.balance_due), inv.paid_at is not None


def _committed_payments(reader, invoice_id):
    with reader() as r:
        rows = r.execute(
            select(Payment.reference, Payment.amount, Payment.voided_at)
            .where(Payment.invoice_id == invoice_id)
            .order_by(Payment.reference)
        ).all()
        return [(ref, float(amt), voided is not None) for ref, amt, voided in rows]


def _committed_audit(reader, action):
    with reader() as r:
        return r.execute(select(AuditLog).where(AuditLog.action == action)).scalars().all()


# ── positive control: the refusal is real ─────────────────────────────────────


def test_control_the_storage_layer_refuses_an_audit_row(dbfile):
    """If this stops raising, every refused-path test below measures nothing."""
    db, _, refuse = dbfile
    refuse()
    with pytest.raises(Exception, match="audit storage refuses this row"):
        log_audit_event_sync(
            db, tenant_id=None, user_id="t", action="probe",
            entity_type="thing", entity_id="1", details={},
        )


# ── site 1: _audit_payment_reversal ───────────────────────────────────────────


def test_shape_a_healthy_reversal_records_the_reopen(dbfile):
    """Shape A, healthy: one payment, the void re-opens the invoice, and the
    trail row says exactly that."""
    db, reader, _ = dbfile
    inv = _invoice(db, "INV-67-A-OK")
    handle_payment_webhook(_succeeded(inv, "pi_67_a_ok", 50000), db)

    out = handle_payment_webhook(_refunded("pi_67_a_ok", 50000), db)

    assert out["status"] == "reversed", out
    assert _committed_payments(reader, inv) == [("pi_67_a_ok", 500.0, True)]
    assert _committed_invoice(reader, inv) == ("sent", 500.0, False)
    (row,) = _committed_audit(reader, "payment_reversed")
    assert row.entity_type == "payment"
    assert row.user_id == "stripe-webhook"
    assert row.details["invoice_id"] == str(inv)
    assert row.details["amount"] == pytest.approx(500.0)
    assert row.details["reference"] == "pi_67_a_ok"
    assert row.details["stripe_event"] == "charge.refunded"
    assert row.details["invoice_reopened"] is True


def test_shape_a_refused_trail_the_reversal_still_commits(dbfile, caplog):
    """Shape A, refused. Before GDXA-67 the swallowed flush left the session
    dead: the webhook raised PendingRollbackError, the payment stayed live and
    the invoice stayed paid, a reversal Stripe had already made."""
    db, reader, refuse = dbfile
    inv = _invoice(db, "INV-67-A-REF")
    handle_payment_webhook(_succeeded(inv, "pi_67_a_ref", 50000), db)
    assert _committed_invoice(reader, inv) == ("paid", 0.0, True), "setup: paid"
    refuse()

    with caplog.at_level("ERROR"):
        out = handle_payment_webhook(_refunded("pi_67_a_ref", 50000), db)

    assert out["status"] == "reversed", out
    assert _committed_payments(reader, inv) == [("pi_67_a_ref", 500.0, True)], (
        "the void must be durable even though its trail row was refused"
    )
    assert _committed_invoice(reader, inv) == ("sent", 500.0, False), "the invoice re-opened"
    assert "payment_reversed_audit_failed" in caplog.text, "a lost row is never silent"


def test_shape_b_two_live_payments_the_row_does_not_claim_a_reopen(dbfile):
    """Shape B: a split payment. Voiding one leaves the invoice owing nothing,
    so it correctly stays paid, and the trail row used to say
    ``invoice_reopened: True`` regardless."""
    db, reader, _ = dbfile
    inv = _invoice(db, "INV-67-B")
    handle_payment_webhook(_succeeded(inv, "pi_67_b1", 50000), db)
    # An overpayment, recorded in full: the invoice is still paid if one goes.
    handle_payment_webhook(_succeeded(inv, "pi_67_b2", 50000), db)

    out = handle_payment_webhook(_refunded("pi_67_b1", 50000), db)

    assert out["status"] == "reversed", out
    assert _committed_payments(reader, inv) == [
        ("pi_67_b1", 500.0, True), ("pi_67_b2", 500.0, False),
    ]
    assert _committed_invoice(reader, inv) == ("paid", 0.0, True), "shape B: still paid"
    (row,) = _committed_audit(reader, "payment_reversed")
    assert row.details["invoice_reopened"] is False


def test_refused_trail_the_dispute_reinstatement_still_commits(dbfile, caplog):
    """The second caller: winning a dispute un-voids the payment. Refused
    trail, and the un-void must still be durable."""
    db, reader, refuse = dbfile
    inv = _invoice(db, "INV-67-WON")
    handle_payment_webhook(_succeeded(inv, "pi_67_won", 50000), db)
    handle_payment_webhook(_dispute("withdrawn", "pi_67_won", 50000), db)
    assert _committed_payments(reader, inv) == [("pi_67_won", 500.0, True)], "setup: voided"
    refuse()

    with caplog.at_level("ERROR"):
        out = handle_payment_webhook(_dispute("reinstated", "pi_67_won", 50000), db)

    assert out["status"] == "reinstated", out
    assert _committed_payments(reader, inv) == [("pi_67_won", 500.0, False)], (
        "the reinstatement must be durable even though its trail row was refused"
    )
    assert _committed_invoice(reader, inv) == ("paid", 0.0, True)
    assert "payment_reinstated_audit_failed" in caplog.text


# ── site 2: _audit_money_event ────────────────────────────────────────────────


def test_healthy_payment_on_a_void_lands_with_its_row(dbfile):
    db, reader, _ = dbfile
    inv = _void_invoice(db, "INV-67-VOID-OK")

    handle_payment_webhook(_succeeded(inv, "pi_67_void_ok", 16200), db)

    assert _committed_payments(reader, inv) == [("pi_67_void_ok", 162.0, False)]
    assert _committed_invoice(reader, inv)[0] == "void"
    (row,) = _committed_audit(reader, "payment_on_voided_invoice")
    assert row.entity_type == "invoice" and row.entity_id == str(inv)
    assert row.user_id == "stripe-webhook"
    assert row.details["amount"] == pytest.approx(162.0)
    assert row.details["reference"] == "pi_67_void_ok"
    assert row.details["payment_id"], "names the payment it is about"


def test_refused_trail_the_payment_on_a_void_still_commits(dbfile, caplog):
    """The `_mark_invoice_paid` call before the recalc and the ledger post
    (#422). The money moved and must stay refundable, so the payment row is
    committed even when the operator's alert cannot be written."""
    db, reader, refuse = dbfile
    inv = _void_invoice(db, "INV-67-VOID-REF")
    refuse()

    with caplog.at_level("ERROR"):
        out = handle_payment_webhook(_succeeded(inv, "pi_67_void_ref", 16200), db)

    assert out["status"] == "paid", out
    assert _committed_payments(reader, inv) == [("pi_67_void_ref", 162.0, False)]
    assert _committed_invoice(reader, inv)[0] == "void", "a void is terminal"
    assert "payment_on_voided_invoice_audit_failed" in caplog.text


def test_refused_trail_an_overcharge_still_commits(dbfile, caplog):
    """The `_mark_invoice_paid` call after the ledger post (#661)."""
    db, reader, refuse = dbfile
    inv = _invoice(db, "INV-67-OVER", amount=162.00)
    refuse()

    with caplog.at_level("ERROR"):
        out = handle_payment_webhook(_succeeded(inv, "pi_67_over", 50000), db)

    assert out["status"] == "paid", out
    assert _committed_payments(reader, inv) == [("pi_67_over", 500.0, False)]
    assert _committed_invoice(reader, inv)[:2] == ("paid", 0.0)
    assert "payment_exceeds_receivable_audit_failed" in caplog.text


def test_a_later_refusal_takes_the_payment_and_its_row_together(dbfile):
    """`_mark_invoice_paid`'s one commit: the payment, the recalc and the
    #422 row land together or not at all. Refuse the recalc's invoice UPDATE,
    which runs after the audit call, and nothing of the three may be
    committed. An audit helper that commits lands the payment early and
    fails this (measured by swapping in `audit_best_effort`). It passes on
    the pre-GDXA-67 code too: since GDXA-352 the hoisted
    `ensure_audit_table` no longer commits, so this pins the contract
    rather than catching a live defect."""
    db, reader, refuse = dbfile
    inv = _void_invoice(db, "INV-67-ATOMIC")
    refuse("invoices", "UPDATE", "invoice storage refuses this update")

    with pytest.raises(Exception, match="invoice storage refuses this update"):
        handle_payment_webhook(_succeeded(inv, "pi_67_atomic", 16200), db)

    assert _committed_payments(reader, inv) == [], "the payment rode the one commit"
    assert _committed_audit(reader, "payment_on_voided_invoice") == []
    assert _committed_invoice(reader, inv) == ("void", 162.0, False)
    if db.get_bind().dialect.name == "postgresql":
        # The guard went in on its own connection mid-transaction, and the
        # caller's rollback did not take it: it was never in the caller's work.
        with reader() as r:
            assert audit_guard_present(r) is True

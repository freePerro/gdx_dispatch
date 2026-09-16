"""A failed email-log write must not break the request that sent the email.

`send_transactional_email` records every attempt in `outbound_emails`, on the
caller's session. It used to add the row and flush inside a bare
`try/except`: when that insert failed, the exception was logged and swallowed,
but the caller's session was left mid-failed-transaction. The email had
already gone out; the caller's next write — `sent_at` on an invoice, `status`
on an estimate — raised `PendingRollbackError`, the office saw an error for a
delivered email, and a retry sent it again (the duplicate guard reads the very
log row that was never written).

The row is now written inside a SAVEPOINT, as `core/webhooks/emit.py` stages
its delivery rows: a failure rolls back only the savepoint and the caller's
transaction — including work it has not committed yet — carries on.

These tests break the real log insert (a `before_insert` listener nulls a
NOT NULL column) and let only the provider be faked, so every other line of the
send path is the production code. The engine uses SQLAlchemy's documented
SQLite SAVEPOINT recipe; without it pysqlite's implicit BEGIN keeps a savepoint
out of the enclosing transaction and the test would not be faithful to
Postgres.
"""
from __future__ import annotations

import contextlib
import secrets
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models.tenant_models import Customer, Invoice, InvoiceLine, OutboundEmail

COMPANY = "22222222-2222-2222-2222-222222222222"
USER = {"sub": "33333333-3333-3333-3333-333333333333", "tenant_id": COMPANY}


@pytest.fixture
def engine(tmp_path):
    # A file, not StaticPool: the checks read through a second connection, and a
    # shared single connection would see the caller's uncommitted work. A short
    # lock wait: the send path's fallback writer opens its own connection, which
    # SQLite (unlike Postgres) makes wait on the caller's write lock.
    eng = create_engine(
        f"sqlite:///{tmp_path / 'email_log.db'}",
        connect_args={"check_same_thread": False, "timeout": 0.5},
    )

    # https://docs.sqlalchemy.org/en/20/dialects/sqlite.html (2.0.53):
    # "Serializable isolation / Savepoints / Transactional DDL".
    @event.listens_for(eng, "connect")
    def _no_implicit_begin(dbapi_conn, _rec):  # noqa: ANN001
        dbapi_conn.isolation_level = None

    @event.listens_for(eng, "begin")
    def _real_begin(conn):  # noqa: ANN001
        conn.exec_driver_sql("BEGIN")

    import gdx_dispatch.models  # noqa: F401 — registers every mapper

    TenantBase.metadata.create_all(eng, checkfirst=True)
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine, monkeypatch):
    monkeypatch.delenv("GDX_ENV", raising=False)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield session
    session.close()


@pytest.fixture
def provider_accepts(monkeypatch):
    """The provider accepts the message — the one thing faked."""
    import gdx_dispatch.core.transactional_email as te

    monkeypatch.setattr(te, "_try_outlook_graph", lambda **_kw: (False, "outlook_not_connected"))
    monkeypatch.setattr(te, "_try_smtp", lambda **_kw: (True, None))


@pytest.fixture
def email_log_insert_fails():
    def _null_subject(_mapper, _conn, target):
        target.subject = None  # NOT NULL — the insert fails in the database

    event.listen(OutboundEmail, "before_insert", _null_subject)
    yield
    event.remove(OutboundEmail, "before_insert", _null_subject)


def _read(engine, fn):
    """Read through a separate, closed-afterwards session: a read left open
    holds SQLite's shared lock and blocks the caller's commit."""
    with sessionmaker(bind=engine)() as session:
        return fn(session)


def test_the_callers_session_survives_and_keeps_its_uncommitted_work(
    db, engine, provider_accepts, email_log_insert_fails
):
    from gdx_dispatch.core.transactional_email import send_transactional_email

    pending = Customer(name="Staged before the send", company_id=COMPANY)
    db.add(pending)  # not flushed, not committed

    sent, provider, skip = send_transactional_email(
        tenant_db=db, tenant_id=COMPANY, user_id=USER["sub"],
        to_email="someone@example.invalid", to_name="Someone",
        subject="Hello", html_body="<p>hi</p>", kind="document",
        entity_type="customer", entity_id=str(uuid4()),
    )
    assert (sent, provider, skip) == (True, "smtp", None)

    assert db.is_active, "a swallowed log failure left the caller's session unusable"
    db.commit()
    names = _read(engine, lambda s: [c.name for c in s.execute(select(Customer)).scalars()])
    assert "Staged before the send" in names


def test_a_successful_log_write_is_still_in_the_callers_transaction(db, engine, provider_accepts):
    """The savepoint must not change the normal case: the row rides the
    caller's transaction and lands when the caller commits — not before."""
    from gdx_dispatch.core.transactional_email import send_transactional_email

    entity = str(uuid4())
    send_transactional_email(
        tenant_db=db, tenant_id=COMPANY, user_id=USER["sub"],
        to_email="someone@example.invalid", to_name="Someone",
        subject="Hello", html_body="<p>hi</p>", kind="document",
        entity_type="customer", entity_id=entity,
    )
    def rows(s):
        return [(r.status, r.subject) for r in s.execute(select(OutboundEmail).where(OutboundEmail.entity_id == entity)).scalars()]

    assert _read(engine, rows) == []
    db.commit()
    assert _read(engine, rows) == [("sent", "Hello")]


def test_send_invoice_reports_the_delivered_email_and_stamps_sent_at(
    db, engine, provider_accepts, email_log_insert_fails, monkeypatch
):
    from gdx_dispatch.routers.invoices import send_invoice

    monkeypatch.setattr("gdx_dispatch.core.pdf_generator.generate_invoice_pdf", lambda **_kw: b"%PDF-1.4 tiny")
    customer = Customer(name="Invoice Customer", email="billing@example.invalid", company_id=COMPANY)
    db.add(customer)
    db.flush()
    inv = Invoice(
        id=uuid4(), customer_id=customer.id, invoice_number=f"INV-{uuid4().hex[:6]}",
        status="draft", subtotal=Decimal("250"), tax_amount=Decimal("0"), total=Decimal("250"),
        balance_due=Decimal("250"), verified_at=datetime.now(UTC),
        public_token=secrets.token_urlsafe(48)[:64], company_id=COMPANY,
    )
    db.add(inv)
    db.flush()
    db.add(InvoiceLine(invoice_id=inv.id, description="Spring", quantity=1, unit_price=Decimal("250"),
                       line_total=Decimal("250"), taxable=False, company_id=COMPANY))
    db.commit()

    out = send_invoice(invoice_id=inv.id, _=USER, db=db)

    assert out["email_sent"] is True
    sent_at, sent_via = _read(engine, lambda s: (lambda i: (i.sent_at, i.sent_via))(s.get(Invoice, inv.id)))
    assert sent_at is not None and sent_via == "email"


def test_send_estimate_reports_the_delivered_email_and_marks_it_sent(
    db, engine, provider_accepts, email_log_insert_fails, monkeypatch
):
    from gdx_dispatch.modules.proposals.models import Estimate
    from gdx_dispatch.routers.estimates import send_estimate

    monkeypatch.setattr("gdx_dispatch.core.pdf_generator.generate_estimate_pdf", lambda **_kw: b"%PDF-1.4 tiny")
    customer = Customer(name="Estimate Customer", email="owner@example.invalid", company_id=COMPANY)
    db.add(customer)
    db.flush()
    est = Estimate(
        customer_id=customer.id, estimate_number=f"EST-{uuid4().hex[:6]}", status="draft",
        total=Decimal("900"), public_token=secrets.token_urlsafe(48)[:64], company_id=COMPANY,
    )
    db.add(est)
    db.commit()

    out = send_estimate(estimate_id=est.id, _=USER, db=db)

    assert out["email_sent"] is True
    status, sent_at = _read(engine, lambda s: (lambda e: (e.status, e.sent_at))(s.get(Estimate, est.id)))
    assert status == "sent" and sent_at is not None


def test_a_failed_bounce_stamp_does_not_break_the_rest_of_the_batch(db, engine):
    """Bounce detection stamps rows mid-batch, with the batch's other writes
    still uncommitted on the same session."""
    from gdx_dispatch.modules.outlook.bounce_detect import _stamp_bounced

    row = OutboundEmail(
        company_id=COMPANY, to_email="gone@example.invalid", subject="Invoice 1 from Us",
        body_html="<p>x</p>", status="sent", kind="document",
    )
    db.add(row)
    db.commit()
    db.add(Customer(name="Flipped earlier in the batch", company_id=COMPANY))  # uncommitted

    def _null_subject(_mapper, _conn, target):
        target.subject = None  # NOT NULL — the UPDATE fails in the database

    event.listen(OutboundEmail, "before_update", _null_subject)
    try:
        _stamp_bounced(db, row, datetime.now(UTC))
    finally:
        event.remove(OutboundEmail, "before_update", _null_subject)

    assert db.is_active, "a swallowed stamp failure left the batch's session unusable"
    db.commit()
    names = _read(engine, lambda s: [c.name for c in s.execute(select(Customer)).scalars()])
    assert "Flipped earlier in the batch" in names
    assert _read(engine, lambda s: s.get(OutboundEmail, row.id).bounced_at) is None


# ---------------------------------------------------------------------------
# Postgres — what production runs. SQLite cannot show the two behaviours that
# matter most: a failed statement aborting the whole transaction, and psycopg2
# answering a COMMIT on an aborted transaction by rolling back WITHOUT raising.
# ---------------------------------------------------------------------------

@pytest.fixture
def pg_engine(pg_test_engine, monkeypatch):
    """The Postgres fixture's schema dump predates the tables this path touches
    (it has no outbound_emails at all), and a full create_all over it fails on
    stale foreign keys. So this per-test clone gets exactly the send path's
    tables rebuilt from the ORM, plus a probe table standing in for the
    caller's own work."""
    from sqlalchemy import text

    import gdx_dispatch.models  # noqa: F401
    from gdx_dispatch.modules.workflows.models import WorkflowRule
    from gdx_dispatch.routers.webhooks import WebhookSubscription

    monkeypatch.delenv("GDX_ENV", raising=False)
    tables = [OutboundEmail.__table__, WebhookSubscription.__table__, WorkflowRule.__table__]
    with pg_test_engine.begin() as conn:
        for table in tables:
            conn.execute(text(f'DROP TABLE IF EXISTS "{table.name}" CASCADE'))
        conn.execute(text("CREATE TABLE caller_work_probe (label text NOT NULL)"))
    TenantBase.metadata.create_all(pg_test_engine, tables=tables)
    return pg_test_engine


def _pg_session(engine):
    return sessionmaker(bind=engine, autoflush=False)()


def _probe_labels(engine):
    from sqlalchemy import text

    return _read(engine, lambda s: [r[0] for r in s.execute(text("SELECT label FROM caller_work_probe"))])


def _stage_caller_work(db, label):
    from sqlalchemy import text

    db.execute(text("INSERT INTO caller_work_probe (label) VALUES (:l)"), {"l": label})


def test_postgres_a_failed_log_write_leaves_the_callers_transaction_usable(
    pg_engine, provider_accepts, email_log_insert_fails
):
    from gdx_dispatch.core.transactional_email import send_transactional_email

    db = _pg_session(pg_engine)
    try:
        _stage_caller_work(db, "staged before the send")
        result = send_transactional_email(
            tenant_db=db, tenant_id=COMPANY, user_id=USER["sub"],
            to_email="someone@example.invalid", to_name="Someone",
            subject="Hello", html_body="<p>hi</p>", kind="document",
            entity_type="customer", entity_id=str(uuid4()),
        )
        assert result == (True, "smtp", None)
        db.commit()
    finally:
        db.close()
    assert _probe_labels(pg_engine) == ["staged before the send"]


def test_postgres_a_transaction_already_aborted_stays_loud(pg_engine, provider_accepts):
    """An earlier failure on the caller's transaction was swallowed before the
    send. Postgres refuses the SAVEPOINT; swallowing that too would let the
    caller's commit return normally while its work is rolled back. It must
    raise — as it did before the savepoint existed — and the send must still be
    recorded."""
    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError

    from gdx_dispatch.core.transactional_email import send_transactional_email

    entity = str(uuid4())
    db = _pg_session(pg_engine)
    try:
        _stage_caller_work(db, "lost if the commit is silent")
        # A caller swallowing a failed read — the transaction is now aborted.
        with contextlib.suppress(SQLAlchemyError):
            db.execute(text("SELECT * FROM no_such_table_statement_probe"))
        send_transactional_email(
            tenant_db=db, tenant_id=COMPANY, user_id=USER["sub"],
            to_email="someone@example.invalid", to_name="Someone",
            subject="Hello", html_body="<p>hi</p>", kind="document",
            entity_type="customer", entity_id=entity,
        )
        with pytest.raises(SQLAlchemyError):
            db.commit()
    finally:
        db.close()
    assert _probe_labels(pg_engine) == []  # lost — but loudly, not behind a normal response
    logged = _read(pg_engine, lambda s: s.execute(
        select(OutboundEmail).where(OutboundEmail.entity_id == entity)).scalars().all())
    assert len(logged) == 1  # the fallback session still recorded the send

"""The office bell's "never raises" contract holds on a dead connection (GDXA-391).

`notify_office`, `notify_estimate_decision` and `notify_payment_received` run
after their caller has already committed — a customer's estimate accept on the
portal or the proposal page, a Stripe payment, a quote request. Each wraps its
work in `except Exception:` and cleans up with `db.rollback()`. When the error
that opened the `except` is not a disconnect and the rollback then fails, that
raise escapes the very block meant to contain it: the committed accept answers
a 500 and a background task (payroll delivery, bounce detection, the Phone.com
sync) dies.

What this seam is NOT: a real disconnect. The refused statements here raise a
plain RuntimeError, so SQLAlchemy never invalidates the connection — which is
exactly what lets the rollback reach `dialect.do_rollback`. A genuine
disconnect as the FIRST error invalidates the connection and turns the
rollback into a no-op (measured on SQLAlchemy 2.0.54), so these tests prove
the guard, not how often an outage reaches it.

The failure is injected where the `dead_after_commit` seam (conftest) puts it:
a refusing cursor does not touch the rollback, because SQLAlchemy rolls back
through `dialect.do_rollback` — on a real engine and a real Session, not a
Session mock.

Each entry point is driven so that ITS OWN `except` runs: the wrappers fail on
their customer-name SELECT, before they ever reach `notify_office`, so a
suppressed rollback in `notify_office` alone cannot turn their test green.
"""
from __future__ import annotations

import contextlib
import logging
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from gdx_dispatch.core.office_notifications import (
    notify_estimate_decision,
    notify_office,
    notify_payment_received,
)

TENANT = "11111111-1111-1111-1111-111111111111"


def _engine(tmp_path):
    """A file engine per test with NullPool, so a failed reset cannot discard a
    database another test is using (the StaticPool hazard the conftest seam has
    to cap its failures around). Warmed: dialect initialisation on first
    connect issues statements and a rollback of its own, which would otherwise
    trip the seam before the code under test runs."""
    from gdx_dispatch.models.tenant_models import Notification  # noqa: PLC0415

    engine = create_engine(f"sqlite:///{tmp_path / 'dead.db'}", poolclass=NullPool)
    Notification.__table__.create(engine)
    with engine.connect():
        pass
    return engine


@contextlib.contextmanager
def _dead_rollback(engine, state):
    real_rollback = engine.dialect.do_rollback

    def _refuse_rollback(dbapi_connection):
        state["rollbacks_failed"] += 1
        raise RuntimeError("dead rollback")

    engine.dialect.do_rollback = _refuse_rollback
    try:
        yield
    finally:
        engine.dialect.do_rollback = real_rollback


@pytest.fixture
def dead_session(tmp_path):
    """Every statement and every rollback raises: the wrappers' customer-name
    SELECT is the first thing that dies, so their own `except` runs."""
    engine = _engine(tmp_path)
    state = {"refused": 0, "rollbacks_failed": 0}

    def _refuse(conn, cursor, statement, parameters, context, executemany):
        state["refused"] += 1
        raise RuntimeError("server closed the connection unexpectedly")

    event.listen(engine, "before_cursor_execute", _refuse)
    db = Session(engine)
    try:
        with _dead_rollback(engine, state):
            yield db, state
    finally:
        with contextlib.suppress(Exception):
            db.close()
        engine.dispose()


@pytest.fixture
def refused_commit_session(tmp_path):
    """The commit is refused BEFORE SQLAlchemy reaches the DBAPI, with the
    caller's connection still checked out, and every rollback raises.

    Why not simply fail the INSERT or the COMMIT: on SQLAlchemy 2.0 a failed
    flush or a failed DBAPI commit runs SQLAlchemy's own rollback and closes
    the session transaction, so `notify_office`'s later `db.rollback()` is a
    no-op that never reaches the DBAPI (measured on 2.0.54: one DBAPI rollback,
    and a refusing-statement or refusing-commit seam passed against the
    unguarded code). A `before_commit` listener that raises leaves the
    transaction open — this repo installs one, strict by default
    (`core/invoice_invariants.py`), which raises on any session carrying a
    dirty Invoice. The checked-out connection is the caller's post-commit read
    (`notify_payment_received`'s docstring: every attribute read after the
    caller's commit is a refresh SELECT).
    """
    engine = _engine(tmp_path)
    state = {"refused": 0, "rollbacks_failed": 0}
    db = Session(engine)
    db.execute(text("SELECT 1"))

    @event.listens_for(db, "before_commit")
    def _refuse_commit(session):
        state["refused"] += 1
        raise RuntimeError("commit refused before it reached the connection")

    try:
        with _dead_rollback(engine, state):
            yield db, state
    finally:
        with contextlib.suppress(Exception):
            db.close()
        engine.dispose()


def _assert_contained(state, caplog, needle):
    assert state["refused"] >= 1, "nothing was refused — the seam proved nothing"
    assert state["rollbacks_failed"] >= 1, (
        "no rollback was refused — the seam proved nothing about the cleanup path"
    )
    assert any(
        needle in r.getMessage() and r.exc_info for r in caplog.records
    ), f"the failure was swallowed without its log line ({needle!r})"


def test_notify_office_returns_when_commit_and_rollback_both_die(
    refused_commit_session, caplog
):
    db, state = refused_commit_session
    with caplog.at_level(logging.ERROR, logger="gdx_dispatch.core.office_notifications"):
        notify_office(db, TENANT, title="Probe", message="m", category="system")
    _assert_contained(state, caplog, "office notification write failed")


def test_notify_estimate_decision_returns_when_its_own_rollback_dies(dead_session, caplog):
    db, state = dead_session
    estimate = SimpleNamespace(id=uuid4(), customer_id=uuid4(), estimate_number="EST-1")
    with caplog.at_level(logging.ERROR, logger="gdx_dispatch.core.office_notifications"):
        notify_estimate_decision(db, TENANT, estimate, verb="accepted", amount=10.0)
    _assert_contained(state, caplog, "estimate decision notification failed")
    assert not any(
        "office notification write failed" in r.getMessage() for r in caplog.records
    ), "the SELECT should fail first, so the wrapper's own except is what ran"


def test_notify_payment_received_returns_when_its_own_rollback_dies(dead_session, caplog):
    db, state = dead_session
    invoice = SimpleNamespace(
        id=uuid4(), company_id=TENANT, customer_id=uuid4(),
        invoice_number="INV-1", balance_due=0,
    )
    with caplog.at_level(logging.ERROR, logger="gdx_dispatch.core.office_notifications"):
        notify_payment_received(db, invoice, amount=10.0, method="card")
    _assert_contained(state, caplog, "payment notification failed")
    assert not any(
        "office notification write failed" in r.getMessage() for r in caplog.records
    ), "the SELECT should fail first, so the wrapper's own except is what ran"

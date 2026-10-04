"""``contained_read`` at the money sites — GDXA-160, child of GDXA-86.

Twelve sites in money-billing's territory are fixed here. ELEVEN are reads and
take ``contained_read``; ONE is a write — ``record_payment``'s rolling-volume
refresh — and takes ``db.begin_nested()`` instead, so the ORM's unit of work
participates (rule 2). On SQLite a swallowed failure is honest; on Postgres the
failed statement aborts the whole transaction, so the caller got its default
back and then lost its work at ``commit()`` — the money version of GDXA-86.

**That count is hand-maintained, and this change is about what that costs.** It
read "Twelve", then "Fifteen", then "Fourteen" before it read twelve again, each
correction forced by an adversarial audit rather than by a test. The
``contained_read`` count in ``core/database.py`` is machine-pinned by
``test_contained_read.py``; this one is not, so prefer the code.

**There is deliberately no grep recipe here, and the two that were tried are
worth more than the recipe would have been.** The first hand-wrote a pathspec
(``core/*.py routers/*.py modules/** tasks/*.py``) and an audit pointed out that
a real call site already lives in ``gdx_dispatch/api/public_router.py``, outside
those globs — so the twelfth wrap landing there would have read as eleven. The
second dropped the pathspec and matched the whole tree, which then counted **the
recipe's own line in this docstring**, because the line contained the string it
searched for. That is the third instance of the class in this repo's history —
``core/database.py`` records the first two — and it happened inside the change
whose whole thesis is that a hand-maintained number is the defect.

So: the repo-wide count is pinned by AST in
``test_contained_read.py::test_the_docstring_call_site_count_is_not_stale``.
For the per-change number, read the diff.

**Two wraps were written and then REMOVED under audit**, which is the most
useful thing in this file: ``routers/payments.py::charge_method`` and
``send_invoice``. Both are real instances; ``db.begin_nested()`` is the wrong
tool for both, and for reasons that generalise —

* a callee that ``commit()``s closes the SessionTransaction from inside the
  savepoint (``charge_method``: the wrap broke EVERY successful card charge,
  measured), and
* a callee that swallows its own failure exits the block CLEAN, so RELEASE
  raises 25P02 on an aborted transaction and nothing is contained
  (``send_invoice``) — ``contained_read`` rule 5, which applies to
  ``db.begin_nested()`` just as much.

There is no rule-set for ``begin_nested`` the way there is for
``contained_read``. Writing one is the follow-up those two sites want. The
reasoning is at each call site so the next person does not re-write the wrap
this change already removed.

**Every test that can prove any of this is in the PG arm**, and that arm SKIPS
green with no reachable Postgres (it fails under CI, per #440). Pre-fix and
post-fix are IDENTICAL on SQLite, so a green local matrix is not evidence here.
Run it with a real database::

    docker run --rm --entrypoint python -v "$PWD":/app -w /app \\
      -e PYTHONPATH=/app -e JWT_SECRET=test-secret-key-at-least-32-bytes-long-x \\
      -e GDX_TEST_PG_HOST=172.17.0.2 -e GDX_TEST_PG_PORT=5432 \\
      docker-app -m pytest -rs gdx_dispatch/tests/test_contained_read_money_billing.py

Each test forces the read to fail the way production can — a table or column
that is not there, never a monkeypatched session — and then asserts the thing
that actually matters: **the caller's committed work survives**. Answering
``SELECT 1`` afterwards would prove nothing; data loss is the consequence.

**Calibration, and read this before the call-site comments.** Those comments
describe consequences in incident language ("money captured on a tech's phone,
gone, reported as success") because that is what the mechanism does when it
fires. **No production instance of any of it has been produced** — for this
territory or, per ``core/database.py``'s own docstring, for any call site of
``contained_read`` at all. Prod is PG 16.13 with no statement timeout and READ
COMMITTED, and the entrypoint migrates before serving, so the live triggers are
narrow: a migration window, a genuinely raw table, a future ``statement_timeout``.
This is cheap insurance on a real mechanism. It is not a post-mortem.

``tests/fixtures/structure.sql`` is the lever for most of that. It is generated
from the migrated schema and genuinely lacks ``tenant_settings``,
``labor_price_items``, ``tax_config`` and ``tax_exemptions`` (they are declared
on ``TenantBase``, which ``migrations/env.py`` does not autogenerate against),
so those reads fail on their own. Where the table IS present the test drops the
column the query selects.

What is NOT covered, and deliberately:

* ``core/billing_lanes.py::install_labor_line`` — a false positive for this
  class. Its ``except (ValueError, AttributeError)`` is for ``_as_uuid``; a DB
  error propagates, and the one caller with pending work behind it
  (``closeout_job``) already wraps it in ``db.begin_nested()``. The reasoning is
  at the call site.
* FOUR rule-5 deferrals — ``core/closeout_billing.py``'s and
  ``routers/invoices.py``'s ``_load_tax_labor_flag`` reads, and
  ``modules/deposits/service.py``'s two ``compute_estimate_totals`` reads. Real
  instances, but the callee swallows its own failure, so ``contained_read``
  would RELEASE a savepoint on a dead transaction and raise 25P02 out of the
  ``with`` (its rule 5). The fix belongs in ``modules/proposals/totals.py``,
  which is estimates-pricing's file. Counted at each call site, not wrapped.
"""
from __future__ import annotations

import uuid as _uuid
from contextlib import suppress
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import Column, Integer, String, text
from sqlalchemy.orm import declarative_base, sessionmaker

_Base = declarative_base()


class _Row(_Base):
    """Stands in for the caller's half-built invoice/payment/closeout."""

    __tablename__ = "gdxa160_row"
    id = Column(Integer, primary_key=True)
    v = Column(String)


def _sessions(engine):
    # autoflush=False mirrors core.database.SessionLocal, which is what every
    # session reaching these helpers in the app actually is.
    _Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _break(engine, ddl: str) -> None:
    """Make a read fail the way production can: the column/table is gone."""
    with engine.begin() as conn:
        conn.execute(text(ddl))


def _reconcile(engine, model) -> None:
    """Add any ORM column ``structure.sql`` lacks, so a real read can run.

    The PG template is generated from the migrated schema and lags the models
    in places (``job_parts_needed.sku`` is missing today). A test that wants to
    drive real code through a table needs it READABLE; the shape it is missing
    is not the shape under test, and letting the wrong column blow up would
    prove nothing about containment.
    """
    from sqlalchemy import inspect as sa_inspect

    have = {c["name"] for c in sa_inspect(engine).get_columns(model.__tablename__)}
    with engine.begin() as conn:
        for col in model.__table__.columns:
            if col.name not in have:
                conn.execute(text(
                    f'ALTER TABLE {model.__tablename__} '
                    f'ADD COLUMN "{col.name}" {col.type.compile(engine.dialect)}'
                ))


def _survivors(engine) -> int:
    with engine.connect() as other:
        return other.execute(text("SELECT count(*) FROM gdxa160_row")).scalar()


# ---------------------------------------------------------------------------
# The premise, pinned. If Postgres ever stops poisoning, everything below is
# cargo cult and this test says so first.
# ---------------------------------------------------------------------------


def test_pg_the_defect_reproduces_a_swallowed_read_loses_the_callers_row(pg_test_engine):
    """No helper here — just the shape, asserted broken on purpose."""
    Session = _sessions(pg_test_engine)
    db = Session()
    db.add(_Row(id=1, v="the caller's work"))

    with suppress(Exception):  # the swallow every one of these helpers used to do
        db.execute(text("SELECT 1 FROM table_that_does_not_exist"))

    try:
        db.commit()
    except Exception as exc:
        assert "aborted" in str(exc).lower(), f"expected 25P02, got {exc}"
    db.rollback()
    db.close()

    assert _survivors(pg_test_engine) == 0, (
        "the caller's row survived — Postgres did not poison, and the premise "
        "of every test below needs re-reading rather than quietly surviving"
    )


# ---------------------------------------------------------------------------
# tasks/stale_intent_sweep.py — _connected_account_for
# ---------------------------------------------------------------------------


def test_pg_stale_intent_sweep_connect_lookup_leaves_the_audit_row_writable(pg_test_engine):
    """The sharpest consequence in this file, and the reason the site is real.

    The sweep cancels real PaymentIntents at Stripe and then writes the audit
    row that is the only record it did. Before the fix a failed Connect lookup
    aborted the transaction, ``_audit_sweep``'s ``log_audit_event_sync`` died on
    the dead session and was swallowed by its OWN handler, and money objects
    changed state at a third party with nothing on the record — invariant #1,
    broken by the read that was supposed to be harmless.

    Stand-in for the audit row: a pending ``_Row``, which is the same question
    ("can this session still commit the work behind it?") without dragging the
    audit machinery in.
    """
    from gdx_dispatch.tasks.stale_intent_sweep import _connected_account_for

    _break(pg_test_engine, "ALTER TABLE companies DROP COLUMN stripe_connect_account_id")
    Session = _sessions(pg_test_engine)
    db = Session()
    db.add(_Row(id=1, v="the sweep's audit row"))

    invoice = SimpleNamespace(id=_uuid.uuid4(), company_id=str(_uuid.uuid4()))
    assert _connected_account_for(db, invoice) == "", "the degraded return changed"

    assert len(db.new) == 1, "the contained read flushed the caller's pending work"
    db.commit()
    db.close()
    assert _survivors(pg_test_engine) == 1, (
        "the sweep cancelled intents and then could not record that it had"
    )


# ---------------------------------------------------------------------------
# core/payments.py — card_surcharge_rate + refuses_debit_cards
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("order", [("rate", "debit"), ("debit", "rate")])
def test_pg_card_surcharge_rate_really_does_fail_open(pg_test_engine, order):
    """"Fails OPEN to zero" is what the docstring promises the pay page.

    It was true on SQLite only: on Postgres the customer got a zero fee and
    then a 500 on the next, unrelated statement. ``tenant_settings`` is absent
    from the PG template on its own (it is declared on ``TenantBase``), so this
    is the real read failing, not a contrived one.

    Run as the PAIR every production caller runs, in both orders those callers
    use (pay page render: rate then debit; ``create_intent``: debit then rate).
    The first version called ``card_surcharge_rate`` alone, a shape no caller
    runs, and stayed green while the unwrapped ``refuses_debit_cards`` beside
    it still lost the row.
    """
    from decimal import Decimal

    from gdx_dispatch.core.payments import card_surcharge_rate, refuses_debit_cards

    Session = _sessions(pg_test_engine)
    db = Session()
    db.add(_Row(id=1, v="the payment the pay page is recording"))

    tid = str(_uuid.uuid4())
    for read in order:
        if read == "rate":
            assert card_surcharge_rate(db, tid) == Decimal(0)
        else:
            assert refuses_debit_cards(db, tid) is False

    assert len(db.new) == 1, "the contained read flushed the caller's pending work"
    db.commit()
    db.close()
    assert _survivors(pg_test_engine) == 1, "a surcharge read cost the payment"


# ---------------------------------------------------------------------------
# core/closeout_billing.py — autodraft_invoice_for_closeout's tax resolution
# ---------------------------------------------------------------------------


def test_pg_autodraft_survives_a_failed_tax_read_in_closeout_jobs_own_frame(pg_test_engine):
    """The autodraft is KEPT when tax resolution fails — not the closeout.

    Be exact about the claim, because the first version of this test was not
    and an audit measured it down. ``closeout_job`` already flushes and calls
    ``autodraft_invoice_for_closeout`` inside ``db.begin_nested()``, and
    SQLAlchemy recovers from the failed RELEASE by rolling back to that
    savepoint — so the tech's closeout committed with or without this fix (1
    survivor both ways, PG 15.17). Testing it bare, as this file first did,
    proved containment in the one configuration where containment already
    existed: a shape production never runs.

    So this replicates ``routers/jobs.py``'s frame verbatim, and asserts the
    thing that actually changes — whether the draft comes back or is rolled
    away and logged ``closeout_autodraft_failed``, leaving Ready-for-Billing
    showing a blank form.

    ``tax_config`` is created and ``tax_exemptions`` deliberately is not, so
    ``resolve_rate`` gets past ``_load_tax_labor_flag`` (the deferred sibling,
    which would otherwise abort the transaction first and make the wrap inert)
    and fails inside ``is_customer_exempt``. That one RAISES rather than
    swallowing, which is what makes ``contained_read`` the right tool here.
    """
    from gdx_dispatch.core.closeout_billing import autodraft_invoice_for_closeout
    from gdx_dispatch.models.tenant_models import Invoice, InvoiceLine, JobPartNeeded
    from gdx_dispatch.modules.tax.models import TaxConfig

    TaxConfig.__table__.create(pg_test_engine)
    _reconcile(pg_test_engine, JobPartNeeded)
    # `core/invoice_invariants.py` registers a before_commit listener that reads
    # the invoice's lines, so the commit at the end of this test needs the table
    # readable too.
    _reconcile(pg_test_engine, InvoiceLine)
    Session = _sessions(pg_test_engine)
    db = Session()

    tenant_id = str(_uuid.uuid4())
    invoice_id = _uuid.uuid4()
    customer_id = _uuid.uuid4()
    with pg_test_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO customers (id, name, company_id) "
                "VALUES (:id, 'GDXA160 customer', :co)"
            ),
            {"id": str(customer_id), "co": tenant_id},
        )
        conn.execute(
            text(
                "INSERT INTO invoices (id, invoice_number, billing_type, sequence_number, "
                "status, subtotal, tax_amount, total, balance_due, locked, public_token, "
                "customer_id, company_id, created_at) VALUES "
                "(:id, 'GDXA160-1', 'standard', 1, 'draft', 0, 0, 0, 0, false, :tok, "
                ":cid, :co, now())"
            ),
            {
                "id": str(invoice_id), "cid": str(customer_id), "co": tenant_id,
                "tok": _uuid.uuid4().hex,
            },
        )
    inv = db.get(Invoice, invoice_id)
    db.add(_Row(id=1, v="the tech's closeout"))

    job = SimpleNamespace(
        id=_uuid.uuid4(), customer_id=_uuid.uuid4(), job_type="inspection",
        not_billable_at=None,
    )
    closeout = SimpleNamespace(labor_matrix_item_id=None, hours_worked=0, techs_on_site=1)

    # routers/jobs.py:2678-2690, verbatim: flush first, then the ORM savepoint,
    # then swallow. The flush is what puts the closeout inside the OUTER
    # transaction so the savepoint rollback can only undo the draft.
    db.flush()
    autodraft = None
    try:
        with db.begin_nested():
            autodraft = autodraft_invoice_for_closeout(
                db, tenant_id=tenant_id, job=job, closeout=closeout,
                reuse_invoice=inv,
            )
    except Exception:  # what closeout_job logs as closeout_autodraft_failed
        autodraft = None

    assert autodraft is not None, (
        "the draft was rolled away: the uncontained read aborted the "
        "transaction, so begin_nested's RELEASE raised and closeout_job "
        "discarded the autodraft it had just built"
    )
    assert autodraft.tax_rate is None, "a failed exemption read must not invent a rate"

    db.commit()
    db.close()
    # Not the discriminator — the closeout survives either way, which is the
    # correction this test carries. Kept so that stays on the record.
    assert _survivors(pg_test_engine) == 1


# ---------------------------------------------------------------------------
# modules/ledger/reports.py — _source_descriptor
# ---------------------------------------------------------------------------


def test_pg_journal_source_lookup_failure_does_not_kill_the_page(pg_test_engine):
    """``journal_page`` calls this in a LOOP, so one broken source row took the
    whole GL journal down on a later, unrelated query rather than degrading to
    ``source_lookup_failed`` the way the ``except`` says it does."""
    from gdx_dispatch.modules.ledger.models import GlJournalEntry
    from gdx_dispatch.modules.ledger.reports import _source_descriptor

    _break(pg_test_engine, "ALTER TABLE invoices DROP COLUMN invoice_number")
    Session = _sessions(pg_test_engine)
    db = Session()
    db.add(_Row(id=1, v="the rest of the journal page"))

    entry = GlJournalEntry(source_type="invoice", source_id=str(_uuid.uuid4()))
    out = _source_descriptor(db, entry)
    assert out["source_lookup_failed"] is True, "the degraded return changed"

    assert len(db.new) == 1, "the contained read flushed the caller's pending work"
    db.commit()
    db.close()
    assert _survivors(pg_test_engine) == 1


# ---------------------------------------------------------------------------
# routers/invoices.py — _labor_price_was_overridden and _plan_out
# ---------------------------------------------------------------------------


def test_pg_labor_provenance_lookup_failure_does_not_cost_the_invoice(pg_test_engine):
    """This runs while the caller is copying estimate lines onto a NEW invoice,
    with those lines pending in the session. ``labor_price_items`` is absent
    from the PG template on its own, so the real read fails."""
    from gdx_dispatch.routers.invoices import _labor_price_was_overridden

    Session = _sessions(pg_test_engine)
    db = Session()
    db.add(_Row(id=1, v="the invoice line being copied"))

    line = SimpleNamespace(unit_price=900)
    assert _labor_price_was_overridden(line, _uuid.uuid4(), db) is False

    assert len(db.new) == 1, "the contained read flushed the caller's pending work"
    db.commit()
    db.close()
    assert _survivors(pg_test_engine) == 1


def test_pg_begin_nested_contains_the_rolling_volume_refresh(pg_test_engine):
    """A MECHANISM guard, and it says so: it proves ``db.begin_nested()`` is
    the right and sufficient tool for ``refresh_cached_volume``, not that
    ``record_payment`` uses it. Removing the wrap from ``routers/invoices.py``
    leaves this green, so it is not a regression net for that placement —
    same limit as the ``create_invoice`` note below, declared rather than
    papered over.

    It is still worth running, because which tool applies here is the
    non-obvious part. ``refresh_cached_volume`` mutates ``Customer`` and
    flushes, so ``contained_read`` is WRONG for it (rule 2: it opens its
    SAVEPOINT below the ORM and the unit of work would not know what to
    un-stage) and the ORM-level ``db.begin_nested()`` is right. This measures
    that on Postgres rather than inferring it.

    The site it stands behind: ``record_payment`` refreshes the rolling-volume
    cache after ``post_payment_received`` has posted the GL, then commits on
    the next line. Before the fix a failed refresh aborted the transaction and
    that ``db.commit()`` took the payment, the GL posting and the audit row
    with it — money captured on a tech's phone, reported as success.
    ``customers.cached_rolling_volume_paid_12mo`` is absent from the PG
    template on its own, and that column is exactly what the helper loads and
    writes — so the real read fails here with no DDL, the way it would during
    the migration window that adds it.
    """
    from gdx_dispatch.services.customer_rolling_volume import refresh_cached_volume

    Session = _sessions(pg_test_engine)
    db = Session()
    db.add(_Row(id=1, v="the payment + its GL posting"))

    try:
        with db.begin_nested():
            refresh_cached_volume(_uuid.uuid4(), db)
    except Exception:
        pass  # record_payment's `log.exception("rolling_volume_refresh_failed_post_payment")`

    db.commit()
    db.close()
    assert _survivors(pg_test_engine) == 1, (
        "a cache refresh cost the customer's payment"
    )


# SIX of the twelve fixed sites have no direct guard here, all inside FastAPI
# route bodies: two in `create_invoice` (the raw `customers` read and the
# `_resolve_tax` swallow) and three enrichment reads in `get_invoice`, plus
# `record_payment`'s `db.begin_nested()`, which has the mechanism test above
# but no test of its PLACEMENT. The six tests above cover the other six sites.
# That is a stated gap, not an oversight. Driving them means
# standing the route up against the PG template with auth and a full payload;
# anything cheaper — inlining the wrap in a test — passes whether or not the
# wrap is in `routers/invoices.py`, which is not a regression net, and this file
# deleted one such test rather than ship it.
#
# What they rest on instead: the GDXA-160 audit measured each pre-fix shape on
# PG and got `commit -> InFailedSqlTransaction`, survivors 0, for both
# `create_invoice` and `record_payment`; the callees are the same ones the tests
# above exercise for real (`resolve_rate` in the closeout test,
# `refresh_cached_volume` in the mechanism test); and `get_invoice` loses no
# data — it 500s a GET.
#
# Note also what `create_invoice`'s wrap does NOT fix: on the estimate-copy path
# the deferred `_load_tax_labor_flag` reads the same `tax_config` table AFTER it,
# so a tax_config failure still costs the invoice. A wrap helps per call PATH,
# not per site.


def test_pg_payment_plan_paid_to_date_failure_does_not_500_on_the_next_line(pg_test_engine):
    """The "unrelated table" symptom, exactly. ``_plan_out`` swallows the
    payments SUM and then calls ``shop_today_from_settings(db)`` — a settings
    read — on the very next line. Before the fix THAT is what raised, naming
    ``app_settings``, for a failure in ``payments``."""
    from gdx_dispatch.routers.invoices import _plan_out

    _break(pg_test_engine, "ALTER TABLE payments DROP COLUMN amount")
    Session = _sessions(pg_test_engine)
    db = Session()
    db.add(_Row(id=1, v="the caller's work"))

    plan = SimpleNamespace(
        id=_uuid.uuid4(), invoice_id=_uuid.uuid4(), status="active",
        num_installments=3, total_amount=300, start_date=date.today() - timedelta(days=1),
    )
    out = _plan_out(plan, [], invoice=SimpleNamespace(id=_uuid.uuid4()), db=db)
    assert out["status"] == "active", "the degraded path changed shape"

    assert len(db.new) == 1, "the contained read flushed the caller's pending work"
    db.commit()
    db.close()
    assert _survivors(pg_test_engine) == 1

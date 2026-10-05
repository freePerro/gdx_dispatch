"""A swallowed read on the truck's screen must not cost the tech their work.

GDXA-156, child of GDXA-86 (``core.database.contained_read``). Every helper
under test takes a session it does not own, reads inside a ``try``, and returns
a degraded value from the ``except`` — ``set()`` for a schema probe, ``True``
for "already billed", ``None`` for a deposit summary, ``False`` for an email
that did not go. On SQLite that degradation is honest. On Postgres a failed
statement aborts the whole transaction, so the work the helper was protecting
is gone: pending ORM work dies at ``commit()`` with 25P02 under an error naming
a table the tech never touched, and raw-SQL or already-flushed work is lost
silently, because psycopg2's COMMIT on an aborted transaction returns normally.
That is why every test counts rows from a separate connection.

**Every test here is Postgres-only, and that is the point.** Pre-fix and
post-fix are indistinguishable on SQLite, so the SQLite arm can never redden for
this class; these skip green with no reachable Postgres and fail under CI
(#440). The failure is always forced at the database — ``DROP TABLE``,
``DROP SCHEMA``, ``ALTER TABLE ... DROP COLUMN``, a refusing trigger — never by
monkeypatching the session, because a monkeypatched read does not poison a
transaction and would prove nothing.

What each test asserts is the *caller's committed row*, not that the session
still answers ``SELECT 1``. Data loss is the consequence that matters, and it is
read back on a **separate connection** so a commit that only appeared to happen
cannot pass.

Load-bearing, and falsified rather than asserted: revert the router changes to
HEAD, re-run, and every test here fails EXCEPT
``test_pg_a_bare_billed_check_loses_the_techs_row`` — which passes in both states
*because* it describes the pre-fix behaviour, asserting the row is LOST. So the
net is pinned from both directions: each site proven to need its containment, and
one control proving the same read without any is what costs the tech the work.
(Measured twice, 2026-09-27 on PG 15 and again under the GDXA-156 round-2 audit.
Deliberately no count written here: a hand-maintained number inside a change
whose own parent exists because a hand-maintained number went stale is a joke
with a delay on it, and the first draft of this docstring said "8 of the 9" when
there were 10.)

Some guards cover WRITE sites, which use ``db.begin_nested()`` rather than
``contained_read`` (rule 2 — the ORM has to know what to un-stage):
``_audit_state_change`` in ``routers/mobile.py`` and the per-recipient
``send_push`` in ``routers/mobile_chat.py``.
"""
from __future__ import annotations

import uuid
from contextlib import contextmanager, suppress
from datetime import UTC, datetime

import pytest
from sqlalchemy import Column, Integer, String, text
from sqlalchemy.orm import declarative_base, sessionmaker

from gdx_dispatch.routers import mobile as mobile_router
from gdx_dispatch.routers import mobile_chat as chat_router
from gdx_dispatch.routers import mobile_invoicing as inv_router
from gdx_dispatch.routers import mobile_quoting as quote_router

TENANT = "11111111-1111-4111-8111-111111111111"
USER = "u-tech-1"

_Base = declarative_base()


class _Row(_Base):
    """Stands in for whatever the caller still has to run on this session.

    For the write routes (audit, invoice send, chat, quote) that is the tech's
    own staged work. For the three billing helpers it is not: their only caller
    is the read-only GET /job/{job_id}, so there the row is a probe for "the
    transaction is still alive" — the thing every later read on the job screen
    needs, starting with the next billing gate. Its own table rather than a
    real one: a purpose-built row makes "did it survive" a one-column question
    no schema drift can confuse.
    """

    __tablename__ = "gdxa156_caller_row"
    id = Column(Integer, primary_key=True)
    v = Column(String)


def _sessions(engine):
    """``autoflush=False`` because that is what ``SessionLocal`` sets, and rule 3
    of ``contained_read`` turns on it: with autoflush on, the read inside the
    savepoint would flush the caller's row INTO the savepoint and the rollback
    would take it, converting a loud 25P02 into silent loss."""
    _Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, future=True)


def _committed_rows(engine) -> int:
    """Read the caller's row back on a SEPARATE connection — the session that
    wrote it is exactly the one whose word cannot be taken here."""
    with engine.connect() as other:
        return other.execute(
            text("SELECT count(*) FROM gdxa156_caller_row")
        ).scalar()


@contextmanager
def _no_containment(_db):
    """What the code did before GDXA-156: the read, unwrapped."""
    yield


# ---------------------------------------------------------------------------
# routers/mobile.py — the billing gates on the job screen
# ---------------------------------------------------------------------------


def test_pg_a_bare_billed_check_loses_the_techs_row(pg_test_engine, monkeypatch):
    """The control. Without containment the degraded `True` leaves a dead
    transaction behind: on the job screen the next gate (`_job_not_billable`)
    then fails on it and reads False, and every read after that degrades."""
    # raising=False so this test still describes the pre-fix code when the fix is
    # reverted for a falsification run — the name is gone then, and the point of
    # this test is the behaviour without it.
    monkeypatch.setattr(mobile_router, "contained_read", _no_containment, raising=False)
    Session = _sessions(pg_test_engine)
    with pg_test_engine.begin() as c:
        c.execute(text("DROP TABLE invoices CASCADE"))

    with Session() as db:
        db.add(_Row(id=1, v="the caller's next statement"))
        assert mobile_router._job_is_billed(db, uuid.uuid4()) is True
        with pytest.raises(Exception):  # noqa: B017 — 25P02 InFailedSqlTransaction
            db.commit()

    assert _committed_rows(pg_test_engine) == 0


def test_pg_contained_billed_check_keeps_the_techs_row(pg_test_engine):
    """The fix. Same degraded answer, and the transaction survives it."""
    Session = _sessions(pg_test_engine)
    with pg_test_engine.begin() as c:
        c.execute(text("DROP TABLE invoices CASCADE"))

    with Session() as db:
        db.add(_Row(id=1, v="the caller's next statement"))
        # Unchanged behaviour: failing to "billed" hides Bill rather than
        # inviting a second invoice. That was always the intent; on Postgres it
        # was never what happened.
        assert mobile_router._job_is_billed(db, uuid.uuid4()) is True
        db.commit()

    assert _committed_rows(pg_test_engine) == 1


def test_pg_contained_not_billable_check_keeps_the_techs_row(pg_test_engine):
    """`_job_not_billable` degrades the OTHER way — False, Bill stays available —
    and that direction is just as dependent on the transaction surviving.

    No DDL here: `fixtures/structure.sql` has no `jobs.not_billable_at` at all,
    so the ORM read raises `UndefinedColumn` (42703) unaided. That is the same
    drift class the fix is insurance against — a column the code selects and the
    live schema does not have — so it is used as-is rather than papered over.
    """
    Session = _sessions(pg_test_engine)
    with pg_test_engine.connect() as c:
        assert c.execute(
            text(
                "SELECT count(*) FROM information_schema.columns WHERE"
                " table_name = 'jobs' AND column_name = 'not_billable_at'"
            )
        ).scalar() == 0, "structure.sql grew the column — force the failure with DDL instead"

    with Session() as db:
        db.add(_Row(id=1, v="the caller's next statement"))
        assert mobile_router._job_not_billable(db, uuid.uuid4()) is False
        db.commit()

    assert _committed_rows(pg_test_engine) == 1


def test_pg_the_deposit_summarys_second_read_is_inside_the_savepoint(
    pg_test_engine,
):
    """The per-invoice payment sum, not just the first SELECT.

    `_job_deposit_summary` reads `invoices` once and then `payments` once PER
    deposit invoice, and the loop is where a naive fix stops short: a failure on
    the sum aborts the caller's transaction exactly as thoroughly as one on the
    outer read. Seeded with a live deposit invoice so the loop actually runs,
    then `payments` is dropped so only the inner read can fail.
    """
    Session = _sessions(pg_test_engine)
    job_id = uuid.uuid4()
    with pg_test_engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO jobs (id, title, dispatch_status, company_id)"
                " VALUES (:j, 'Door off track', 'assigned', :t)"
            ),
            {"j": job_id, "t": TENANT},
        )
        c.execute(
            text(
                "INSERT INTO invoices (id, job_id, invoice_number, billing_type,"
                " sequence_number, subtotal, tax_amount, total, balance_due,"
                " status, locked, public_token, company_id, created_at)"
                " VALUES (gen_random_uuid(), :j, 'INV-GDXA156', 'deposit', 1,"
                " 6000, 0, 6000, 3000, 'sent', false, 'tok-gdxa156', :t, now())"
            ),
            {"j": job_id, "t": TENANT},
        )
        c.execute(text("DROP TABLE payments CASCADE"))

    with Session() as db:
        db.add(_Row(id=1, v="the caller's next statement"))
        assert mobile_router._job_deposit_summary(db, job_id) is None
        db.commit()

    assert _committed_rows(pg_test_engine) == 1


def test_pg_the_schema_drift_probe_keeps_the_techs_row(pg_test_engine):
    """`_table_columns` degrades to `set()`, which every caller reads as "the
    live schema lacks that column, drop the clause" — so an uncontained failure
    is wrong twice over on Postgres.

    Note what it takes to redden this, because it is the measurement behind the
    helper's own docstring: `DROP SCHEMA information_schema CASCADE`. A missing
    table or column is exactly what this read CANNOT fail on — it returns 0 rows
    — so `_table_columns` is the LEAST likely site in this class to fire, not
    the likeliest, and an earlier draft of both this docstring and the code
    comment had that backwards. What is left is a statement timeout or the
    window during a migration. Cheap insurance on a real mechanism.
    """
    Session = _sessions(pg_test_engine)
    with pg_test_engine.begin() as c:
        c.execute(text("DROP SCHEMA information_schema CASCADE"))

    with Session() as db:
        db.add(_Row(id=1, v="the caller's next statement"))
        assert mobile_router._table_columns(db, "time_entries") == set()
        db.commit()

    assert _committed_rows(pg_test_engine) == 1


def test_pg_a_refused_audit_row_does_not_cost_the_tech_the_state_change(
    pg_test_engine,
):
    """`_audit_state_change` is the one WRITE site here, so it is guarded by
    `db.begin_nested()` and not `contained_read` — the ORM has to know what to
    un-stage (contained_read rule 2). Both halves are exercised: the
    `log_audit_event` insert and the raw-SQL fallback insert below it, refused at
    the database by a real BEFORE INSERT trigger rather than by a patched
    session.

    The assertion that matters is the tech's state change, not the audit row.
    An unaudited change is a defect; a change that silently evaporates while the
    route answers 200 is a worse one, and before this fix Postgres did the
    second.
    """
    from gdx_dispatch.core.audit import ensure_audit_table

    Session = _sessions(pg_test_engine)
    with Session() as boot:
        ensure_audit_table(boot)
        boot.commit()
    with pg_test_engine.begin() as c:
        c.execute(
            text(
                "CREATE FUNCTION gdxa156_refuse() RETURNS trigger AS $$ BEGIN "
                "RAISE EXCEPTION 'audit storage refuses this row'; END; $$ "
                "LANGUAGE plpgsql"
            )
        )
        c.execute(
            text(
                "CREATE TRIGGER gdxa156_refuse_audit BEFORE INSERT ON audit_logs "
                "FOR EACH ROW EXECUTE FUNCTION gdxa156_refuse()"
            )
        )

    with Session() as db:
        db.add(_Row(id=1, v="arrived on site"))
        mobile_router._audit_state_change(
            db,
            event_type="mobile_job_arrived",
            actor_id=USER,
            entity_type="job",
            entity_id=str(uuid.uuid4()),
            payload={"at": datetime.now(UTC).isoformat()},
            request=None,
            actor_role="technician",
        )
        db.commit()

    assert _committed_rows(pg_test_engine) == 1


# ---------------------------------------------------------------------------
# routers/mobile_invoicing.py — money
# ---------------------------------------------------------------------------


def test_pg_the_contained_customer_read_does_not_lose_the_invoice_send(pg_test_engine):
    """`_send_invoice_email` returning False reads to its caller as "no customer
    email" — and that caller goes on to transition the invoice to `sent` (a GL
    posting), stamp `sent_at`, write an audit row and commit. Uncontained on
    Postgres the read kills all four and 500s the send.

    **Scoped to the customer read, and the name says so on purpose.** Two other
    statements in the same `try` are still uncontained and cannot be covered
    here: `_prepare_invoice_email` ends in `db.commit()` and
    `send_transactional_email` WRITES, so neither may go under `contained_read`
    (rules 2 and 5). A green test called
    `..._a_failed_customer_read_does_not_lose_the_invoice_send` would read as
    coverage for a property this function does NOT have — caught by the GDXA-156
    round-3 audit. The gap is named in `_send_invoice_email`'s own docstring and
    is money-billing's to close.
    """
    from gdx_dispatch.models.tenant_models import Invoice

    Session = _sessions(pg_test_engine)
    with pg_test_engine.begin() as c:
        c.execute(text("DROP TABLE customers CASCADE"))

    with Session() as db:
        db.add(_Row(id=1, v="the invoice transition the route is about to commit"))
        invoice = Invoice(id=uuid.uuid4(), customer_id=uuid.uuid4())
        assert inv_router._send_invoice_email(db, invoice, tenant_id=TENANT) is False
        db.commit()

    assert _committed_rows(pg_test_engine) == 1


# ---------------------------------------------------------------------------
# routers/mobile_chat.py — the push that must not eat the message
# ---------------------------------------------------------------------------


def test_pg_a_failed_push_lookup_does_not_break_the_chat_reply(
    pg_test_engine, monkeypatch
):
    """`send_push` READS `push_subscriptions` and then WRITES to it, so
    `_push_other_party` guards it with `db.begin_nested()` — the ORM mutations it
    stages have to be un-staged by the rollback (contained_read rule 2).

    `push_subscriptions` is absent from `fixtures/structure.sql`, so the read
    fails as 42P01 with no DDL needed — the same missing-table shape production
    sees during a migration window. Uncontained, that abort rode out of
    `_push_other_party` into `send_job_chat`'s `except Exception: log` and left
    the session dead for whatever the caller did next — which is what `_Row`
    stands in for here.

    It does NOT 500 the send on this call path, and an earlier draft of this
    docstring said it did. The GDXA-156 round-2 audit measured it: by the time
    this line runs `msg` is already refreshed, so `_serialize_message` emits no
    SQL. The 500-on-a-stored-message belongs to that earlier refresh, which is
    outside every savepoint here and is not fixed by this change.
    """
    monkeypatch.setenv("VAPID_PUBLIC_KEY", "test-public")
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "test-private-pem")
    Session = _sessions(pg_test_engine)
    role_id = uuid.uuid4()
    with pg_test_engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO tenant_roles (id, company_id, name, permissions,"
                " is_system, created_at, updated_at) VALUES (:r, :t, 'dispatcher',"
                " '[]', false, now(), now())"
            ),
            {"r": role_id, "t": TENANT},
        )
        c.execute(
            text(
                "INSERT INTO user_role_assignments (id, company_id, user_id,"
                " role_id, assigned_at) VALUES (gen_random_uuid(), :t,"
                " 'u-dispatcher-1', :r, now())"
            ),
            {"r": role_id, "t": TENANT},
        )

    class _Msg:
        """Only the attributes `_push_other_party` reads. A real JobChatMessage
        would need a table `structure.sql` does not have, and would not make the
        assertion any stronger."""

        sender_role = "tech"
        body = "door is off track, need a 2in roller"
        id = uuid.uuid4()

    with Session() as db:
        db.add(_Row(id=1, v="the chat message send_job_chat already committed"))
        chat_router._push_other_party(
            db,
            job_id=str(uuid.uuid4()),
            msg=_Msg(),
            user={"user_id": USER},
            request=None,
        )
        db.commit()

    assert _committed_rows(pg_test_engine) == 1


# ---------------------------------------------------------------------------
# routers/mobile_quoting.py — the quote the tech just built
# ---------------------------------------------------------------------------


class _Req:
    """The two attributes the quoting router reads off a Request."""

    def __init__(self, tenant_id: str) -> None:
        self.state = type("S", (), {"tenant": {"id": tenant_id}, "tenant_id": tenant_id})()


def test_pg_a_failed_jobsite_seed_does_not_lose_the_techs_quote(pg_test_engine):
    """`build_quote`'s jobsite seed is `except Exception: log` with the comment
    "a seed miss must not block quoting" — a promise that was false on Postgres,
    because everything after the swallow BUILDS AND COMMITS the quote.

    `db` here is the route's own session, so this is not the caller-owned shape
    the sweep was scoped to; it is included because the consequence is the
    tech's work, which is the thing this whole class is about.

    No DDL: `fixtures/structure.sql` has no `jobs.location_id`, so the seed's
    `SELECT location_id, customer_id FROM jobs` raises `UndefinedColumn` (42703)
    unaided while the access gate above it — which selects a literal `1` — still
    passes. Exactly one statement in the handler can fail, which is what makes
    the assertion at the end unambiguous.
    """
    from gdx_dispatch.models.labor_pricing import LaborPriceItem
    from gdx_dispatch.models.tenant_models import AppSettings
    from gdx_dispatch.modules.proposals.models import (
        Estimate,
        EstimateLine,
        ProposalTier,
    )
    from gdx_dispatch.routers.mobile_quoting import BuildQuoteIn, QuoteLineIn, QuoteTierIn

    # `structure.sql` has drifted from the ORM on both tables this handler
    # touches OUTSIDE the savepoint under test — `app_settings` predates
    # `google_maps_api_key` and `estimates` predates `jobsite_address` — so
    # either one raises UndefinedColumn and masks the real assertion. Rebuilt
    # from the ORM, which is truth, on a throwaway per-test clone. The same drift
    # and the same remedy are documented in test_timeclock_audit_refusal.py's PG
    # test. CASCADE because `invoices.estimate_id` references `estimates`; this
    # test does not use invoices.
    AppSettings.__table__.drop(bind=pg_test_engine, checkfirst=True)
    AppSettings.__table__.create(bind=pg_test_engine)
    with pg_test_engine.begin() as c:
        c.execute(
            text("DROP TABLE IF EXISTS estimate_lines, proposal_tiers, estimates CASCADE")
        )
    # create_all, not three .create() calls: estimates.accepted_tier_id and
    # proposal_tiers.estimate_id are a FK cycle, which only the sorted path knows
    # to break with a post-hoc ADD CONSTRAINT.
    # create_all over an explicit table list, not three .create() calls:
    # estimates.accepted_tier_id and proposal_tiers.estimate_id are a FK cycle
    # that only the sorted path breaks with a post-hoc ADD CONSTRAINT. The list
    # is explicit rather than the whole metadata because an unfiltered create_all
    # trips on drift in tables this test never touches (`users.id`'s type differs
    # from the ORM's, so user_tour_progress's FK cannot be built) — the same
    # "create_all tables diverge from the ORM" hazard, from the other side.
    Estimate.metadata.create_all(
        bind=pg_test_engine,
        tables=[
            LaborPriceItem.__table__,  # estimate_lines.labor_price_item_id
            Estimate.__table__,
            EstimateLine.__table__,
            ProposalTier.__table__,
        ],
        checkfirst=True,
    )

    Session = _sessions(pg_test_engine)
    job_id = uuid.uuid4()
    with pg_test_engine.begin() as c:
        assert c.execute(
            text(
                "SELECT count(*) FROM information_schema.columns WHERE"
                " table_name = 'jobs' AND column_name = 'location_id'"
            )
        ).scalar() == 0, "structure.sql grew the column — force the failure with DDL instead"
        c.execute(
            text(
                "INSERT INTO jobs (id, title, dispatch_status, company_id, assigned_to)"
                " VALUES (:j, 'Door off track', 'assigned', :t, :u)"
            ),
            {"j": job_id, "t": TENANT, "u": USER},
        )

    payload = BuildQuoteIn(
        label="Spring replacement",
        tiers=[
            QuoteTierIn(
                tier_name="good",
                line_items=[QuoteLineIn(description="Torsion spring pair", unit_price=420.0)],
            )
        ],
    )

    # `build_quote` commits the Estimate and only THEN serializes it, and the
    # serializer's `compute_estimate_totals` reads tax tables structure.sql does
    # not carry. That is a fixture artifact strictly downstream of both commits,
    # so suppressing it cannot weaken the assertion below: if the seed read were
    # uncontained, the FIRST commit would be the thing that died and the count
    # would be 0. Measured — with the fix reverted this test fails on exactly
    # that.
    with Session() as db, suppress(Exception):
        quote_router.build_quote(
            str(job_id),
            payload,
            _Req(TENANT),
            current_user={"user_id": USER},
            db=db,
        )

    # The quote is durable, which is the whole point — read back on its own
    # connection so a commit that only appeared to happen cannot pass.
    with pg_test_engine.connect() as other:
        assert other.execute(
            text("SELECT count(*) FROM estimates WHERE job_id = :j"), {"j": job_id}
        ).scalar() == 1


def test_pg_a_failed_recipient_lookup_does_not_500_an_already_sent_message(
    pg_test_engine, monkeypatch
):
    """The recipient SELECT, not just `send_push` — found by the GDXA-156 audit.

    This read has no `try` of its own, which is exactly why the first pass
    walked past it. Following the error out to the frame that DOES catch it is
    the check the parent issue insists on: `send_job_chat` wraps the whole call
    in `except Exception: log`, so uncontained the abort rides out of here
    silently and the session is dead for anything the caller does next.

    Not a 500 on this path — see the sibling test above for the measurement, and
    for which statement the 500 story actually belongs to.

    `user_role_assignments` is dropped rather than absent because
    `structure.sql` does carry it, and the tech branch is the one that reads it.
    """
    monkeypatch.setenv("VAPID_PUBLIC_KEY", "test-public")
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "test-private-pem")
    Session = _sessions(pg_test_engine)
    with pg_test_engine.begin() as c:
        c.execute(text("DROP TABLE user_role_assignments CASCADE"))

    class _Msg:
        sender_role = "tech"
        body = "spring snapped, need the 2in roller after all"
        id = uuid.uuid4()

    with Session() as db:
        db.add(_Row(id=1, v="the chat message send_job_chat already committed"))
        # Mirrors send_job_chat's own handler: it swallows and moves on.
        with suppress(Exception):
            chat_router._push_other_party(
                db,
                job_id=str(uuid.uuid4()),
                msg=_Msg(),
                user={"user_id": USER},
                request=None,
            )
        db.commit()

    assert _committed_rows(pg_test_engine) == 1

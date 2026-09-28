"""``_query_candidates`` must not cost its caller the transaction — GDXA-162.

Child of GDXA-86 (``core.database.contained_read``). ``routers/winback.py``'s
candidate read takes the *route's* session, reads ``customers``/``jobs`` through
raw SQL inside a ``try``, and returns ``[]`` from the handler. Its docstring has
always promised that ``[]`` means "no candidates, or the tables are missing". On
Postgres that promise was false: a failed statement aborts the whole
transaction, so the caller got its ``[]`` on a session that could no longer
commit, and ``send_campaign`` then died at ``db.commit()`` naming
``winback_campaigns`` — a table the failing query never mentions.

Containing the transaction is only half of it, and the second half is the half
that would have shipped a defect. With the savepoint in and nothing else
changed, ``send_campaign``'s commit *succeeds*: the campaign flips
``draft -> sent`` with zero sends, 200 OK, no route back (``send`` 400s on a
non-draft; the router has no PATCH or DELETE for campaigns) — and ``_audit``
writes ``winback_campaign_sent`` with ``enqueued: 0``, so the trail affirms a
send that never happened. The poisoned transaction had been doing that job by
accident.

That state has **four** entrances, not one, and three of them need no failed read
at all (measured, GDXA-162 audit round 2: a readable-but-empty candidate list,
an override list of malformed ids, an override list the UUID loop drops). So the
protection sits at the write — ``if not enqueued: 409`` immediately above the
status flip — and the ``(ok, rows)`` tuple from ``_query_candidates_checked``
earns the *distinction* rather than the protection: 503 "could not read, retry"
against 409 "nobody qualifies". All of it is tested here.

Four groups, reddening for different reasons:

- **the no-flush property** (``contained_read`` rule 3) — dialect-independent,
  so it is in the SQLite section. ``db.begin_nested()`` flushes on entry via
  ``SessionTransaction._take_snapshot``; ``contained_read`` opens its SAVEPOINT
  on the Connection under ``no_autoflush`` and writes nothing.
- **the poisoning** — Postgres only. SQLite does not abort a transaction on a
  failed statement, so pre-fix and post-fix are *identical* there and a green
  SQLite run is not evidence for this fix at all. The PG arm skips (green) with
  no reachable Postgres and fails under CI (#440), so read ``-rs``.
- **the refusal** — drives the real ``send_campaign``, not a copy of it, so a
  future change to that function is covered rather than shadowed.
- **the query still works** — a savepoint around a read is easy to get right
  and still break the read it wraps.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core.audit import utcnow
from gdx_dispatch.models.tenant_models import WinbackCampaign
from gdx_dispatch.routers.winback import (
    _query_candidates,
    _query_candidates_checked,
    send_campaign,
)

TENANT = "22222222-2222-4222-8222-222222222222"
USER = {"sub": "office@example.test"}


class _State:
    tenant = {"id": TENANT}


class _Request:
    """What `_tenant_id` and `core.audit` actually read off a request — both use
    `getattr` with defaults throughout, so this is the whole contract."""

    state = _State()
    headers: dict[str, str] = {}
    client = None


def _sessions(engine):
    # autoflush=False mirrors core.database.SessionLocal, which is what every
    # session reaching this router in the app actually is. With autoflush=True
    # the read inside the savepoint would flush the caller's pending row INTO
    # the savepoint and the rollback would take it — see contained_read's
    # closing note on `no_autoflush`.
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _a_campaign(name: str) -> WinbackCampaign:
    """The caller's pending work, as the real caller stages it."""
    return WinbackCampaign(
        company_id=TENANT,
        name=name,
        status="draft",
        channel="sms",
        body_template="We miss you",
        inactivity_months=6,
    )


def _break_the_candidate_query(engine) -> None:
    """Make the read fail the way production could, on a separate connection.

    ``jobs`` is renamed rather than dropped: PG's FKs follow a rename, so this
    needs no cascade and holds no lock the caller wants. That is the window
    during a migration, or a drifted schema.

    Calibrate the severity honestly, because the first draft of this file did
    not. Checked on prod 2026-09-27: ``statement_timeout = 0``, 307 customers,
    ``winback_campaigns = 0``, ``winback_sends = 0``. So the trigger this file
    used to call "the likeliest real one" — a statement timeout on the
    ``LIMIT 5000`` scan — cannot fire as configured, and the feature has never
    been used at all. Nothing here describes harm that has happened; it is cheap
    insurance on a real mechanism, and the same wrong answer would be given the
    first time someone did use it.

    Nothing restores the table: ``pg_test_db`` clones a fresh database from the
    template per test (``tests/fixtures/pg.py``), so there is nothing to leak.
    """
    with engine.begin() as c:
        c.execute(text("ALTER TABLE jobs RENAME TO jobs_gdxa162_gone"))


def _audit_rows(engine, action: str) -> int:
    with engine.connect() as other:
        return other.execute(
            text("SELECT count(*) FROM audit_logs WHERE action = :a"), {"a": action}
        ).scalar()


# ---------------------------------------------------------------------------
# Either dialect: the probe must not flush, and must not commit
# ---------------------------------------------------------------------------


@pytest.fixture
def sqlite_sessions(tmp_path):
    """A schema with the winback tables but NO ``customers``/``jobs``.

    That is not a contrivance: it is the state the candidate read's own
    docstring names ("test DB without those tables"), and it is the only way to
    make the read fail without monkeypatching the session.
    """
    engine = create_engine(f"sqlite:///{tmp_path}/t.db", future=True)

    # SQLAlchemy's documented SQLite-SAVEPOINT recipe, as
    # test_plugin_consent_contained_read.py explains: pysqlite's implicit BEGIN
    # otherwise breaks nested-transaction rollback, so a SAVEPOINT release would
    # wrongly persist and the assertion below could pass because pysqlite is
    # wrong rather than because the helper is right.
    @event.listens_for(engine, "connect")
    def _sqlite_no_implicit_begin(dbapi_conn, _rec):  # noqa: ANN001
        dbapi_conn.isolation_level = None

    @event.listens_for(engine, "begin")
    def _emit_real_begin(conn):  # noqa: ANN001
        conn.exec_driver_sql("BEGIN")

    WinbackCampaign.__table__.create(engine)
    yield _sessions(engine), engine
    engine.dispose()


def test_a_failed_candidate_read_does_not_flush_the_callers_pending_work(sqlite_sessions):
    """The one-line version of rule 3, and it needs no Postgres.

    Restore ``db.begin_nested()`` in ``_query_candidates_checked`` and this
    fails on ``len(db.new) == 1``: the snapshot flush writes the caller's
    half-built campaign out at the moment of a read that used to write nothing.
    """
    Session, engine = sqlite_sessions
    db = Session()
    db.add(_a_campaign("the caller's pending work"))

    assert _query_candidates_checked(db, TENANT, 6) == (False, [])  # no customers table

    assert len(db.new) == 1, "the candidate read flushed the caller's pending work"
    db.rollback()
    db.close()
    with engine.connect() as other:
        assert other.execute(text("SELECT count(*) FROM winback_campaigns")).scalar() == 0, (
            "the candidate read committed work the caller rolled back"
        )


def test_the_degraded_flag_distinguishes_empty_from_unreadable(sqlite_sessions):
    """The second half of the fix, and the reason it is a tuple.

    A screen cannot tell these apart and does not need to; ``send_campaign``
    must. Collapse the two back into a bare ``[]`` and this is the test that
    goes red — ``_query_candidates`` keeps returning ``[]`` either way, which is
    exactly why the distinction lives in the checked variant.
    """
    Session, engine = sqlite_sessions
    db = Session()

    unreadable_ok, unreadable_rows = _query_candidates_checked(db, TENANT, 6)
    assert (unreadable_ok, unreadable_rows) == (False, [])
    assert _query_candidates(db, TENANT, 6) == [], "the screen's contract changed"

    # Now give it a readable-but-empty schema: same rows, opposite flag.
    db.close()
    with engine.begin() as c:
        c.execute(text("CREATE TABLE customers (id text, name text, email text, "
                       "phone text, company_id text, deleted_at text)"))
        c.execute(text("CREATE TABLE jobs (id text, customer_id text, "
                       "created_at text, deleted_at text)"))
    db = Session()
    assert _query_candidates_checked(db, TENANT, 6) == (True, [])
    db.close()


# ---------------------------------------------------------------------------
# Postgres only: the poisoning, and the fix for it
# ---------------------------------------------------------------------------


def test_pg_a_bare_swallowed_read_loses_the_callers_row(pg_test_engine):
    """The control: the pre-fix mechanism, inline, so the contrast survives this
    branch.

    Deliberately NOT a copy of the router's SQL — a hand copy would drift, and
    the poisoning is a property of *any* failed statement on the session, not of
    that query's text. What this pins is the consequence and the confusing shape
    of the error: it names ``winback_campaigns``, the CALLER's table, and says
    nothing about ``jobs``. That is why the class survived into 41 files.
    """
    _break_the_candidate_query(pg_test_engine)
    Session = _sessions(pg_test_engine)
    db = Session()
    db.add(_a_campaign("the row a bare swallow costs you"))

    try:  # the GDXA-152 shape: no savepoint, degraded return
        db.execute(text("SELECT count(*) FROM jobs")).scalar()
        rows = ["unreachable"]
    except Exception:
        rows = []
    assert rows == [], "the read was supposed to fail — the fixture proves nothing"

    with pytest.raises(DBAPIError) as exc:
        db.commit()
    assert "InFailedSqlTransaction" in str(exc.value), f"not the poisoning: {exc.value}"
    assert "winback_campaigns" in str(exc.value), (
        "the error should name the CALLER's table, not the failed read's"
    )
    db.rollback()
    db.close()
    with pg_test_engine.connect() as other:
        assert other.execute(text("SELECT count(*) FROM winback_campaigns")).scalar() == 0, (
            "expected the bare swallow to lose the caller's row"
        )


def test_pg_contained_candidate_read_keeps_the_callers_transaction_committable(pg_test_engine):
    """The fix, against the control above. Remove ``contained_read`` from
    ``_query_candidates_checked`` and this reddens at ``db.commit()`` with
    ``PendingRollbackError``.

    Asserting the caller's work is DURABLE, not merely that the session still
    answers ``SELECT 1`` — data loss is the consequence that matters.
    """
    _break_the_candidate_query(pg_test_engine)
    Session = _sessions(pg_test_engine)
    db = Session()
    db.add(_a_campaign("the caller's pending work"))

    assert _query_candidates_checked(db, TENANT, 6) == (False, [])

    assert len(db.new) == 1, "the candidate read flushed the caller's pending work"
    assert db.is_active, "the candidate read handed back a deactivated session"
    db.commit()
    db.close()
    with pg_test_engine.connect() as other:
        assert other.execute(text("SELECT count(*) FROM winback_campaigns")).scalar() == 1, (
            "a failed candidate read cost the caller its campaign"
        )


def test_pg_repeated_failed_candidate_reads_survive(pg_test_engine):
    """``winback_stats`` reads campaigns and then calls the candidate query; a
    dashboard that polls calls it again on the next request against the same
    pool. One contained failure must not make the next savepoint unopenable."""
    _break_the_candidate_query(pg_test_engine)
    Session = _sessions(pg_test_engine)
    db = Session()
    db.add(_a_campaign("survivor"))

    for _ in range(3):
        assert _query_candidates(db, TENANT, 6) == []

    db.commit()
    db.close()
    with pg_test_engine.connect() as other:
        assert other.execute(text("SELECT count(*) FROM winback_campaigns")).scalar() == 1


# ---------------------------------------------------------------------------
# Postgres: the refusal — driving the real send_campaign
# ---------------------------------------------------------------------------


def test_pg_send_campaign_refuses_rather_than_burning_the_campaign(pg_test_engine):
    """The half that would have shipped a defect without GDXA-162's audit.

    This calls the REAL ``send_campaign`` — not a hand-copied sequence — so that
    a future change to it is covered here rather than shadowed. Drop the ``ok``
    guard and every assertion below flips: 200 instead of 503, ``sent`` instead
    of ``draft``, and an ``audit_logs`` row claiming ``enqueued: 0``.

    The audit row is the sharpest part. Invariant #1 asks whether we could
    reconstruct who did what; ``winback_campaign_sent`` for a campaign that
    reached nobody is worse than no row at all.
    """
    Session = _sessions(pg_test_engine)
    db = Session()
    campaign = _a_campaign("must not burn on a degraded read")
    db.add(campaign)
    db.commit()
    campaign_id = campaign.id
    _break_the_candidate_query(pg_test_engine)

    with pytest.raises(HTTPException) as exc:
        send_campaign(campaign_id, _Request(), USER, db, None)
    assert exc.value.status_code == 503, f"expected a retryable refusal: {exc.value.detail}"

    db.rollback()
    db.close()
    with pg_test_engine.connect() as other:
        assert other.execute(
            text("SELECT status FROM winback_campaigns WHERE id = :i"), {"i": campaign_id}
        ).scalar() == "draft", "the campaign was burned — send 400s on a non-draft forever"
        assert other.execute(text("SELECT count(*) FROM winback_sends")).scalar() == 0
    assert _audit_rows(pg_test_engine, "winback_campaign_sent") == 0, (
        "an audit row affirms a send that never happened"
    )


def test_pg_send_campaign_still_enqueues_when_the_read_works(pg_test_engine):
    """The refusal must not have broken the working path. A real candidate, the
    real route function: one ``winback_sends`` row, campaign ``sent``, one audit
    row.

    Named "enqueues" and not "sends" deliberately. An earlier draft of this test
    was called ``..._still_sends_...`` and its docstring said "the audit row is
    now true" — both wrong, and the GDXA-162 audit caught it. Nothing CONSUMES
    ``winback_sends``: one writer (the router), no reader, no beat task
    (``FOUND_NOT_FILED.md`` 2026-09-20, ``[open]``). The transports themselves do
    exist — Phone.com for SMS, ``core/transactional_email.py`` for email — so the
    missing piece is a drainer, not a sender. So this asserts rows written, which
    is all the code does and all the test can see. ``enqueued: 1`` in the audit
    row means one row, not one customer reached — do not let a green run here be
    read as delivery.
    """
    Session = _sessions(pg_test_engine)
    cid = "66666666-6666-4666-8666-666666666666"
    with pg_test_engine.begin() as c:
        c.execute(
            text("INSERT INTO customers (id, name, company_id, created_at) "
                 "VALUES (:id, 'Inactive Ivan', :t, now())"),
            {"id": cid, "t": TENANT},
        )
    db = Session()
    campaign = _a_campaign("the happy path")
    db.add(campaign)
    db.commit()
    campaign_id = campaign.id

    out = send_campaign(campaign_id, _Request(), USER, db, None)

    assert out["enqueued"] == 1, out
    db.close()
    with pg_test_engine.connect() as other:
        assert other.execute(
            text("SELECT status FROM winback_campaigns WHERE id = :i"), {"i": campaign_id}
        ).scalar() == "sent"
        assert other.execute(
            text("SELECT count(*) FROM winback_sends WHERE customer_id = :c"), {"c": cid}
        ).scalar() == 1
    assert _audit_rows(pg_test_engine, "winback_campaign_sent") == 1


def test_pg_send_campaign_refuses_a_readable_but_empty_candidate_list(pg_test_engine):
    """The second of four entrances to the same harmful state, and the read works
    perfectly here. No inactive customers exist, so the candidate query honestly
    returns ``(True, [])`` — and without the zero-target guard the campaign still
    flips to ``sent`` with a ``winback_campaign_sent`` row claiming
    ``enqueued: 0``. Guarding only the degraded read would have fixed one of the
    four (GDXA-162 audit round 2)."""
    Session = _sessions(pg_test_engine)
    db = Session()
    campaign = _a_campaign("nobody qualifies")
    db.add(campaign)
    db.commit()
    campaign_id = campaign.id

    with pytest.raises(HTTPException) as exc:
        send_campaign(campaign_id, _Request(), USER, db, None)
    assert exc.value.status_code == 409, f"expected a 'nobody to send to' refusal: {exc.value.detail}"

    db.rollback()
    db.close()
    with pg_test_engine.connect() as other:
        assert other.execute(
            text("SELECT status FROM winback_campaigns WHERE id = :i"), {"i": campaign_id}
        ).scalar() == "draft"
    assert _audit_rows(pg_test_engine, "winback_campaign_sent") == 0


def test_pg_send_campaign_refuses_an_override_list_the_uuid_loop_drops(pg_test_engine):
    """Entrances three and four: an override list of ids that are not UUIDs. The
    loop logs and `continue`s past every one, so nothing is queued — and the
    campaign used to be marked ``sent`` anyway. This is the path the read-side
    503 deliberately does not cover, which is why the guard sits at the write."""
    from gdx_dispatch.routers.winback import SendCampaignIn

    Session = _sessions(pg_test_engine)
    db = Session()
    campaign = _a_campaign("malformed overrides")
    db.add(campaign)
    db.commit()
    campaign_id = campaign.id

    with pytest.raises(HTTPException) as exc:
        send_campaign(
            campaign_id,
            _Request(),
            USER,
            db,
            SendCampaignIn(override_customer_ids=["not-a-uuid", ""]),
        )
    assert exc.value.status_code == 409, exc.value.detail

    db.rollback()
    db.close()
    with pg_test_engine.connect() as other:
        assert other.execute(
            text("SELECT status FROM winback_campaigns WHERE id = :i"), {"i": campaign_id}
        ).scalar() == "draft"
        assert other.execute(text("SELECT count(*) FROM winback_sends")).scalar() == 0
    assert _audit_rows(pg_test_engine, "winback_campaign_sent") == 0


def test_pg_an_override_list_does_not_need_the_candidate_query(pg_test_engine):
    """The refusal is scoped to the path that depends on the read. An explicit
    override list is the operator's own answer, so a broken candidate query must
    not block it — and this is where a guard placed one line too high would show
    up.

    Read the `enqueued == 1` below for what it is: this UUID belongs to no
    customer, and it is accepted anyway. There is no FK on
    `winback_sends.customer_id` and no existence or tenant check on override ids,
    so this assertion is a green demonstration of a hole, not approval of it.
    Named here so the next reader is not misled, and on GDXA-162; not fixed,
    because checking override ids is a behaviour change beyond containing a read.
    """
    from gdx_dispatch.routers.winback import SendCampaignIn

    Session = _sessions(pg_test_engine)
    cid = "77777777-7777-4777-8777-777777777777"
    db = Session()
    campaign = _a_campaign("override path")
    db.add(campaign)
    db.commit()
    campaign_id = campaign.id
    _break_the_candidate_query(pg_test_engine)

    out = send_campaign(
        campaign_id, _Request(), USER, db, SendCampaignIn(override_customer_ids=[cid])
    )

    assert out["enqueued"] == 1, out
    db.close()
    with pg_test_engine.connect() as other:
        assert other.execute(text("SELECT count(*) FROM winback_sends")).scalar() == 1


# ---------------------------------------------------------------------------
# Postgres: the query itself still works
# ---------------------------------------------------------------------------


def test_pg_a_successful_candidate_read_is_unchanged(pg_test_engine):
    """The savepoint must be invisible on the success path — a real candidate is
    still found, and the caller's pending work is still pending (NOT written
    early, which is the behaviour change ``db.begin_nested()`` would introduce).
    """
    Session = _sessions(pg_test_engine)
    db = Session()
    cid = "33333333-3333-4333-8333-333333333333"
    with pg_test_engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO customers (id, name, email, phone, company_id, created_at) "
                "VALUES (:id, 'Inactive Ivan', 'i@example.test', '555', :t, now())"
            ),
            {"id": cid, "t": TENANT},
        )
    db.add(_a_campaign("pending while the read succeeds"))

    ok, found = _query_candidates_checked(db, TENANT, 6)

    assert ok is True
    assert [r["id"] for r in found] == [cid], f"the real query stopped working: {found}"
    assert found[0]["last_job_date"] is None
    assert len(db.new) == 1, "the read wrote the caller's pending campaign out early"
    with pg_test_engine.connect() as other:
        assert other.execute(text("SELECT count(*) FROM winback_campaigns")).scalar() == 0, (
            "the read committed the caller's pending campaign"
        )
    db.commit()
    db.close()


def test_pg_the_cutoff_still_excludes_a_recently_worked_customer(pg_test_engine):
    """A savepoint around a read is easy to get right and still break the query
    it wraps. This is the semantic guard: a customer with a recent job is not a
    win-back candidate, savepoint or no savepoint."""
    Session = _sessions(pg_test_engine)
    recent = "44444444-4444-4444-8444-444444444444"
    stale = "55555555-5555-4555-8555-555555555555"
    with pg_test_engine.begin() as c:
        for cid, name in ((recent, "Recent Rita"), (stale, "Stale Stan")):
            c.execute(
                text(
                    "INSERT INTO customers (id, name, company_id, created_at) "
                    "VALUES (:id, :n, :t, now())"
                ),
                {"id": cid, "n": name, "t": TENANT},
            )
        for cid, days in ((recent, 5), (stale, 900)):
            c.execute(
                text(
                    "INSERT INTO jobs (id, customer_id, title, dispatch_status, "
                    "company_id, created_at) VALUES (gen_random_uuid(), :c, 'Door', "
                    "'done', :t, :d)"
                ),
                {"c": cid, "t": TENANT, "d": utcnow() - timedelta(days=days)},
            )

    db = Session()
    found = {r["name"] for r in _query_candidates(db, TENANT, 6)}
    db.close()

    assert found == {"Stale Stan"}, f"the cutoff stopped working: {found}"

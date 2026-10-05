"""``contained_read`` across the comms territory — GDXA-155.

Child of GDXA-86 (``core.database.contained_read``). Twenty-two reads in the
email / Outlook / notification path took the **caller's** session, read inside a
``try``, and returned a degraded default from the ``except``. On SQLite that is
honest. On Postgres the failed statement has already aborted the whole
transaction, so the degraded answer is a lie twice over: the caller believes it
got an answer, and the caller's uncommitted work is already lost — its
``commit()`` dies with ``InFailedSqlTransaction`` (25P02), naming some unrelated
table on some unrelated line.

**Every test that proves the defect lives in the PG arm**, which SKIPS (green)
with no reachable Postgres and fails under CI per #440. A green SQLite-only run
is not evidence for this class — which is exactly how twenty-two of these
shipped unnoticed. Run it with a real Postgres:

    docker run --rm --entrypoint python -v "$PWD":/app -w /app -e PYTHONPATH=/app \\
      -e JWT_SECRET=test-secret-key-at-least-32-bytes-long-x \\
      -e GDX_TEST_PG_HOST=172.17.0.2 -e GDX_TEST_PG_PORT=5432 \\
      docker-app -m pytest -rs gdx_dispatch/tests/test_contained_read_comms.py

What each test asserts is the *consequence*, not the mechanism: the caller's
committed row is still there afterwards. "The session still answers SELECT 1"
would pass against a session whose transaction was silently rolled back, and
data loss is the thing that matters. Each test also pins the helper's degraded
return, so a fix that accidentally turned a degraded read into a 500 (rule 1 —
savepoint OUTSIDE the ``try``) reddens here too.

The failure is injected by renaming the table out from under one SELECT, never
by monkeypatching the session: a mock proves which arguments were passed, never
that the transaction survived. A rename is what a drifted or mid-migration
schema looks like to that one statement.

``table=None`` means the read already fails in this fixture on its own, because
``fixtures/structure.sql`` is a ``TenantBase``-scoped dump and these tables are
mapped on a different Base: ``outbound_emails``, ``customer_contacts``,
``outlook_accounts`` and ``outlook_folders`` are absent from it even though
migration 001 creates them on a real box. That injected failure exercises the
containment exactly as a genuine ``UndefinedTable`` would, but say plainly what
it is — a fixture artifact, not a reproduction of the production failure mode.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import Column, Integer, String, text
from sqlalchemy.orm import declarative_base, sessionmaker

TENANT = "11111111-1111-4111-8111-111111111111"

_Base = declarative_base()


class _Row(_Base):
    """Stands in for the caller's pending BUSINESS work — the estimate status
    flip, the invoice ``sent_at``, the sync's freshly-upserted messages.

    Not the ``outbound_emails`` audit row, which is what this file first said it
    stood for: ``transactional_email._record_outbound`` already commits that on a
    separate Session when the caller's transaction is dead (measured, PG 15.17 —
    audit row 1, caller business rows 0). The audit trail was never the casualty;
    the business write is."""

    __tablename__ = "gdxa155_row"
    id = Column(Integer, primary_key=True)
    v = Column(String)


def _sessions(engine):
    # autoflush=False mirrors core.database.SessionLocal, which is what every
    # session reaching these helpers in the app actually is.
    _Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _caller_survives(pg_test_engine, table: str | None, call):
    """Break ``table``, run the helper on a session holding pending work, and
    assert BOTH halves: the helper still degrades, and the caller still commits.

    Returns whatever the helper returned, so each test can pin its own default.
    """
    Session = _sessions(pg_test_engine)
    if table is not None:
        with pg_test_engine.begin() as c:
            c.execute(text(f"ALTER TABLE {table} RENAME TO {table}_gone"))

    db = Session()
    db.add(_Row(id=1, v="the caller's pending work"))
    result = call(db)
    db.commit()          # uncontained, this is where 25P02 lands
    db.close()

    with pg_test_engine.connect() as other:
        assert other.execute(text("SELECT count(*) FROM gdxa155_row")).scalar() == 1, (
            f"a failed read of {table} cost the caller its row"
        )
    return result


def _reconcile_to_the_orm(engine, model) -> None:
    """Add columns the model declares and ``fixtures/structure.sql`` lacks.

    The dump is a ``TenantBase``-scoped snapshot and it lags the ORM: ``Estimate``
    has a ``jobsite_address`` column that is not in it. An ORM ``SELECT`` names
    every mapped column, so against the dump it fails with ``UndefinedColumn``
    before the read under test is ever reached — which would make these tests
    pass or fail for a reason that has nothing to do with the savepoint.

    Reconciles rather than re-creating from ``model.__table__`` deliberately: the
    dump's ``estimates.status`` is the Postgres enum ``estimate_status``, so
    creating the table from the ORM would CREATE TYPE over one that already
    exists. This is the repo's documented "create_all tables diverge from the
    ORM" edge, met from the other side — the database is the authority, so ask it
    what it has rather than assuming.
    """
    from sqlalchemy import inspect as _inspect

    table = model.__table__
    have = {c["name"] for c in _inspect(engine).get_columns(table.name)}
    missing = [c for c in table.columns if c.name not in have]
    if not missing:
        return
    with engine.begin() as c:
        for col in missing:
            ddl = col.type.compile(engine.dialect)
            c.execute(
                text(f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" {ddl} NULL')
            )


class _Stub:
    """Minimal duck-type for the ORM objects these helpers only read attributes
    off. Deliberately not a real model: the point is the session's transaction,
    and a real Estimate/Customer would drag its own table into every test."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


# ---------------------------------------------------------------------------
# Rule 3 — the early flush. Dialect-independent, so no Postgres needed.
#
# These two exist because of a measurement. Stub `contained_read` to a no-op and
# 23 of this file's 27 tests redden; stub it to `db.begin_nested()` instead and
# only these TWO do. So the PG arm proves containment and says nothing at all
# about WHICH savepoint — without this section the file would be silent on the
# entire reason `contained_read` opens it on the Connection, and swapping the
# implementation for the idiom this repo reached for first would look green.
# A failed flush deactivates the session on every dialect, which is why this half
# needs no Postgres. (Re-measure both numbers if you add or remove a test here;
# they are the file's own falsification record.)
# ---------------------------------------------------------------------------


@pytest.fixture
def sqlite_sessions(tmp_path):
    from sqlalchemy import create_engine, event

    engine = create_engine(f"sqlite:///{tmp_path}/t.db", future=True)

    # SQLAlchemy's documented SQLite-SAVEPOINT recipe, as
    # test_plugin_consent_contained_read.py explains: pysqlite's implicit BEGIN
    # otherwise breaks nested-transaction rollback, so a SAVEPOINT release would
    # wrongly persist and a test could pass because pysqlite is wrong rather than
    # because the helper is right. Production is Postgres, which gets this native.
    @event.listens_for(engine, "connect")
    def _no_implicit_begin(dbapi_conn, _rec):  # noqa: ANN001
        dbapi_conn.isolation_level = None

    @event.listens_for(engine, "begin")
    def _real_begin(conn):  # noqa: ANN001
        conn.exec_driver_sql("BEGIN")

    yield _sessions(engine)
    engine.dispose()


def test_branding_read_does_not_flush_the_callers_pending_work(sqlite_sessions):
    """``email_branding`` is the first db touch on nearly every send path, so the
    caller is reliably holding a half-built row when it runs.

    ``db.begin_nested()`` calls ``SessionTransaction._take_snapshot``, which
    FLUSHES — writing that half-built row out at the moment of a read that used
    to write nothing. Swap ``contained_read`` for ``db.begin_nested()`` and this
    fails on ``pending == 0``.
    """
    Session = sqlite_sessions
    db = Session()
    db.add(_Row(id=1, v="the caller's half-built send"))

    from gdx_dispatch.core.email_layout import email_branding

    # app_settings does not exist on this scratch db, so the read degrades — which
    # is beside the point here: with begin_nested the flush happens on ENTRY,
    # before the read fails at all.
    assert email_branding(db)["company_name"] == "Your Service Company"
    assert len(db.new) == 1, "the savepoint flushed the caller's pending work"

    db.rollback()
    db.close()


def test_branding_read_leaves_the_callers_own_error_for_the_caller(sqlite_sessions):
    """The sharpest consequence of that flush.

    The caller stages a row violating a unique constraint — an ``IntegrityError``
    it was going to handle. With ``db.begin_nested()`` the snapshot flush raises
    that error INSIDE ``email_branding``, whose ``except Exception`` swallows it
    and hands back a deactivated session; the caller's ``commit()`` then raises
    ``PendingRollbackError`` and its own ``except IntegrityError`` never runs. So
    the caller loses not just the row but its own error.

    With ``contained_read`` there is no flush here, so the error stays the
    caller's, raised where the caller is looking for it.
    """
    from sqlalchemy.exc import IntegrityError, PendingRollbackError

    from gdx_dispatch.core.email_layout import email_branding

    Session = sqlite_sessions
    seed = Session()
    seed.add(_Row(id=1, v="already committed"))
    seed.commit()
    seed.close()

    db = Session()
    db.add(_Row(id=1, v="the caller's duplicate — its own error to handle"))

    assert email_branding(db)["company_name"] == "Your Service Company"
    assert len(db.new) == 1, "the read flushed, so the error is no longer the caller's"

    with pytest.raises(Exception) as exc:
        db.commit()
    assert isinstance(exc.value, IntegrityError), f"expected IntegrityError, got {exc.value!r}"
    assert not isinstance(exc.value, PendingRollbackError), (
        "the caller got PendingRollbackError — its own error was swallowed by the read"
    )
    db.rollback()
    db.close()


# ---------------------------------------------------------------------------
# The defect, reproduced once — so the premise is falsifiable
# ---------------------------------------------------------------------------


def test_pg_the_bare_swallow_loses_the_callers_row(pg_test_engine):
    """The shape all twenty-two sites had, on the site where it is worst.

    ``recently_sent`` is the duplicate-send guard. It fails OPEN by design, and
    that part is right — but uncontained, the caller ALSO loses the work it was
    holding. This asserts the BROKEN behaviour on purpose, hand-rolled rather
    than by reverting the fix: if a future Postgres or SQLAlchemy stops poisoning
    the transaction, this reddens and the premise of the whole file needs
    re-reading rather than quietly surviving as cargo cult.
    """
    Session = _sessions(pg_test_engine)
    db = Session()
    db.add(_Row(id=1, v="the caller's work"))

    from contextlib import suppress
    with suppress(Exception):            # what the `except` used to do, unguarded
        db.execute(text("SELECT id FROM outbound_emails LIMIT 1"))

    with pytest.raises(Exception) as exc:
        db.commit()
    assert "aborted" in str(exc.value).lower(), f"expected 25P02, got {exc.value}"

    db.rollback()
    db.close()
    with pg_test_engine.connect() as other:
        assert other.execute(text("SELECT count(*) FROM gdxa155_row")).scalar() == 0, (
            "the caller's row survived — the defect did not reproduce"
        )


# ---------------------------------------------------------------------------
# core/transactional_email.py
# ---------------------------------------------------------------------------


def test_pg_recently_sent_cannot_cost_the_caller_its_work(pg_test_engine):
    """The duplicate-send guard. A swallowed read here returns "not recently
    sent" and the guard fails OPEN — so this one can send a real customer email
    twice. That degradation is deliberate and stays; what must not also happen
    is the caller losing the send it is in the middle of recording.
    """
    from gdx_dispatch.core.transactional_email import recently_sent

    answered = _caller_survives(
        pg_test_engine, None,            # outbound_emails: absent from the fixture
        lambda db: recently_sent(db, "estimate", str(uuid.uuid4())),
    )
    assert answered is False             # fails OPEN, as documented


def test_pg_designated_sender_cannot_cost_the_caller_its_work(pg_test_engine):
    from gdx_dispatch.core.transactional_email import _designated_sender_user_id

    assert _caller_survives(
        pg_test_engine, "app_settings", _designated_sender_user_id,
    ) is None


# ---------------------------------------------------------------------------
# core/customer_views.py
# ---------------------------------------------------------------------------


def test_pg_view_dedupe_probe_cannot_cost_the_caller_its_work(pg_test_engine):
    """``record_customer_view`` WRITES the audit row on this same session right
    after this probe answers. Uncontained, the probe's own comment ("we would
    rather write a duplicate row than lose the event") came out backwards: the
    event was lost and nothing was written.
    """
    from gdx_dispatch.core.customer_views import _recently_recorded

    assert _caller_survives(
        pg_test_engine, "audit_logs",
        lambda db: _recently_recorded(db, "invoice_viewed", str(uuid.uuid4())),
    ) is False


# ---------------------------------------------------------------------------
# core/email_recipients.py
# ---------------------------------------------------------------------------


def test_pg_recipient_resolution_cannot_cost_the_caller_its_work(pg_test_engine):
    """"Resolution must never block a send" — only true on Postgres with the
    savepoint. The fallthrough to the account address still happens, the email
    still goes out, and now the outbound_emails row recording it can still be
    written.
    """
    from gdx_dispatch.core.email_recipients import resolve_recipient

    customer = _Stub(
        id=str(uuid.uuid4()), email="owner@example.test", name="Example Account",
    )
    resolved = _caller_survives(
        pg_test_engine, None,            # customer_contacts: absent from the fixture
        lambda db: resolve_recipient(db, customer),
    )
    assert resolved.source == "account_email"
    assert resolved.email == "owner@example.test"


# ---------------------------------------------------------------------------
# core/email_sender.py
# ---------------------------------------------------------------------------


def test_pg_email_config_read_cannot_cost_the_caller_its_work(pg_test_engine):
    """``None`` reads as "SMTP not configured" and ``send_email`` degrades to a
    warning — while uncontained the caller's status flip and audit row died."""
    from gdx_dispatch.core.email_sender import get_email_config

    assert _caller_survives(
        pg_test_engine, "email_settings",
        lambda db: get_email_config(db, TENANT),
    ) is None


# ---------------------------------------------------------------------------
# core/email_layout.py
# ---------------------------------------------------------------------------


def test_pg_email_branding_read_cannot_cost_the_caller_its_work(pg_test_engine):
    """The first db touch on most send paths — every outbound customer email
    renders through this. The unbranded fallback is the intended degradation."""
    from gdx_dispatch.core.email_layout import email_branding

    branding = _caller_survives(pg_test_engine, "app_settings", email_branding)
    assert branding["company_name"] == "Your Service Company"


# ---------------------------------------------------------------------------
# core/office_notifications.py — NOT wrapped. Counted, with the measurement.
#
# `notify_estimate_decision` and `notify_payment_received` match this class by
# shape and are NOT fixed here, because the premise does not hold at them: every
# production caller commits BEFORE calling them (routers/portal.py
# `portal_estimate_accept` and `portal_estimate_decline`,
# modules/proposals/router.py `public_proposal_accept` and
# `public_proposal_decline`, and core/payments.py `_mark_invoice_paid`, which
# says so out loud — "it is after the commit ... so the badge can never roll
# back the money"). There is no caller-owned pending
# work at those sites to protect.
#
# A first pass here DID wrap them and also deleted the `db.rollback()` from their
# handlers, on the theory that the rollback was discarding the caller's work.
# That was wrong and this diff's adversarial audit measured it, A/B on PG 15.17
# against the same file from HEAD:
#
#     rollback removed : notify raised InternalError, downstream job_rows = 0
#     rollback kept    : notify raised nothing,       downstream job_rows = 1
#
# The rollback is load-bearing: the caller's commit has EXPIRED the estimate or
# invoice, so `getattr(estimate, "id", None)` in the handler's own log line is a
# lazy-refresh SELECT. The rollback un-poisons the transaction so that SELECT can
# run; without it the handler itself raises, which 500s the public
# /proposals/{token} accept page for an acceptance already committed and loses
# the `_create_job_from_estimate` that follows. core/payments.py had already
# written this down, in `_mark_invoice_paid` just before its
# `notify_payment_received` call.
#
# The real exposure in that file is therefore NOT the one explicit
# `select(Customer.name)` this class flags — it is those lazy-refresh attribute
# reads, which `_mark_invoice_paid` names as belonging "inside
# `notify_payment_received`'s guard". Containing them is a different change with
# a different shape, and it is raised on the issue rather than bundled here.

# ---------------------------------------------------------------------------
# modules/outlook/bounce_detect.py
# ---------------------------------------------------------------------------


def test_pg_bounce_outbound_lookup_cannot_cost_the_caller_its_work(pg_test_engine):
    """"never block processing" was not kept: the estimate flip and its audit row
    died at the caller's commit instead.

    Deliberately NOT claiming the bounce is lost for good. An earlier version of
    this docstring said the NDR was "marked processed" and "never seen again";
    nothing in bounce_detect writes a processed marker, the sync caller rolls the
    phase back and logs "sync unaffected", and process_bounces is documented safe
    to re-run. The cost is one sync's bounce processing, redone. See the
    correction at bounce_detect.py's call site."""
    from gdx_dispatch.modules.outlook.bounce_detect import _outbound_row

    assert _caller_survives(
        pg_test_engine, None,            # outbound_emails: absent from the fixture
        lambda db: _outbound_row(
            db, None, "estimate", str(uuid.uuid4()),
            datetime.now(timezone.utc), kind=None,
        ),
    ) is None


# ---------------------------------------------------------------------------
# modules/outlook/resend_detect.py
# ---------------------------------------------------------------------------


def _insert_rejected_estimate(engine) -> str:
    """One rejected estimate, so ``process_resends`` gets past its early return
    and actually reaches the contained read."""
    from gdx_dispatch.modules.proposals.models import Estimate

    _reconcile_to_the_orm(engine, Estimate)
    eid, cid = str(uuid.uuid4()), str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    with engine.begin() as c:
        # estimates.customer_id is FK-constrained, so the customer has to exist
        # before the rename makes it unreadable — which is also the real shape:
        # the row IS there, the SELECT just cannot run.
        c.execute(
            text(
                "INSERT INTO customers (id, name, company_id, created_at) "
                "VALUES (:cid, 'Example Account', :tid, :now)"
            ),
            {"cid": cid, "tid": TENANT, "now": now},
        )
        c.execute(
            text(
                "INSERT INTO estimates (id, estimate_number, proposal_mode, total, "
                "status, company_id, public_token, created_at, customer_id, updated_at) "
                "VALUES (:id, 'EST-9', false, 100.00, 'rejected', :tid, :tok, :now, "
                ":cid, :now)"
            ),
            {
                "id": eid, "tid": TENANT, "tok": uuid.uuid4().hex,
                "now": now, "cid": cid,
            },
        )
    return eid


def test_pg_resend_anchor_walk_cannot_cost_the_caller_its_work(pg_test_engine):
    """The handler ``continue``s to the next estimate — a lie on Postgres, where
    the aborted transaction makes every later statement in the batch fail,
    including the ``est.status = "sent"`` flips and the final ``tdb.commit()``, so
    one unresolvable estimate cost the whole run's detections.

    "Silently" only in the work-empty path: with work non-empty the next statement
    is ``_outbound_since``, which has no handler, so 25P02 propagates loudly into
    the caller's rollback. An earlier version of this docstring called it silent
    outright; see the correction at resend_detect.py's call site.
    """
    from gdx_dispatch.modules.outlook.resend_detect import process_resends

    _insert_rejected_estimate(pg_test_engine)
    result = _caller_survives(
        pg_test_engine, "customers",
        lambda db: process_resends(db, _Stub(id=uuid.uuid4())),
    )
    assert result["rejected_seen"] == 1
    assert result["resent_detected"] == 0


# ---------------------------------------------------------------------------
# modules/outlook/send_router.py
# ---------------------------------------------------------------------------


def test_pg_send_job_number_cannot_cost_the_caller_its_work(pg_test_engine):
    from gdx_dispatch.modules.outlook.send_router import _job_number

    assert _caller_survives(
        pg_test_engine, "jobs", lambda db: _job_number(db, uuid.uuid4()),
    ) is None


def test_pg_send_tech_email_preload_cannot_cost_the_caller_its_work(pg_test_engine):
    from gdx_dispatch.modules.outlook.send_router import _tech_emails

    assert _caller_survives(pg_test_engine, "users", _tech_emails) == set()


# ---------------------------------------------------------------------------
# modules/outlook/tagger.py
# ---------------------------------------------------------------------------


def test_pg_tagger_job_uuid_lookup_cannot_cost_the_caller_its_work(pg_test_engine):
    """The tagger runs inside the sync's per-folder transaction, holding
    freshly-synced OutlookMessage rows. ``job = None`` degrades to "untagged" —
    but uncontained the sync loses every message that page just pulled.

    Not permanently: ``state.delta_token`` is set on the same session and
    committed by the same per-folder commit, so whatever rolls the messages back
    un-advances the token and the next sync re-fetches them. An earlier version of
    this docstring asserted the opposite ("that mail never comes back"); see the
    correction at tagger.py's call site."""
    from gdx_dispatch.modules.outlook.tagger import job_thread_strategy

    msg = _Stub(subject=f"Re: your quote [Job #{uuid.uuid4()}]")
    assert _caller_survives(
        pg_test_engine, "jobs", lambda db: job_thread_strategy(msg, db),
    ) is None


def test_pg_tagger_auto_match_cannot_cost_the_caller_its_work(pg_test_engine):
    """The HOT read in the tagger — once per candidate address per new message.

    Unlike the two job lookups below it, ``auto_match_strategy`` has no ``try`` of
    its own: the swallow is one frame up at ``tasks.py:294`` ("upsert kept"),
    which has no rollback. So uncontained, one failure here poisons the sync's
    per-folder transaction and the flush/commit at the end of that page loses
    EVERY upsert on it — the opposite of what its handler promises. The savepoint
    only has to roll back as the exception crosses it; who catches it is
    irrelevant (contained_read rule 5's corollary).

    The read still fails and still propagates — that is unchanged and correct —
    so this asserts the half that changed: work staged afterwards still commits.
    """
    from gdx_dispatch.modules.outlook.tagger import auto_match_strategy

    Session = _sessions(pg_test_engine)
    with pg_test_engine.begin() as c:
        c.execute(text("ALTER TABLE customers RENAME TO customers_gone"))

    db = Session()
    msg = _Stub(
        from_address="someone@example.test",
        to_addresses=[], cc_addresses=[], bcc_addresses=[],
    )
    with pytest.raises(Exception) as exc:
        auto_match_strategy(msg, db)
    assert "does not exist" in str(exc.value).lower(), f"expected UndefinedTable, got {exc.value}"

    # What tasks.py does next: keep the upsert and commit the page.
    db.add(_Row(id=1, v="the upsert tasks.py promises to keep"))
    db.commit()
    db.close()
    with pg_test_engine.connect() as other:
        assert other.execute(text("SELECT count(*) FROM gdxa155_row")).scalar() == 1, (
            "the page's upserts were lost — 'upsert kept' is not kept"
        )


def test_pg_tagger_settings_requery_cannot_cost_the_caller_its_work(pg_test_engine):
    """``tag_message``'s own OutlookSettings read, taken whenever the caller
    passes no prefetched settings. Also no local ``try``, same reasoning."""
    from gdx_dispatch.modules.outlook.tagger import tag_message

    Session = _sessions(pg_test_engine)
    db = Session()
    msg = _Stub(tag_strategy=None, subject="Re: hello")
    with pytest.raises(Exception) as exc:
        tag_message(msg, db)          # outlook_settings is absent from the fixture
    assert "does not exist" in str(exc.value).lower(), f"expected UndefinedTable, got {exc.value}"

    db.add(_Row(id=1, v="the upsert tasks.py promises to keep"))
    db.commit()
    db.close()
    with pg_test_engine.connect() as other:
        assert other.execute(text("SELECT count(*) FROM gdxa155_row")).scalar() == 1


def test_pg_tagger_job_number_lookup_cannot_cost_the_caller_its_work(pg_test_engine):
    """The second read in the same function — the customer-facing
    ``[Job #JOB-2026-014]`` marker form. It needs its own test: the uuid subject
    above returns before ever reaching this branch."""
    from gdx_dispatch.modules.outlook.tagger import job_thread_strategy

    msg = _Stub(subject="Re: your quote [Job #JOB-2026-014]")
    assert _caller_survives(
        pg_test_engine, "jobs", lambda db: job_thread_strategy(msg, db),
    ) is None


# ---------------------------------------------------------------------------
# modules/outlook/views_router.py
# ---------------------------------------------------------------------------


def test_pg_link_labels_cannot_cost_the_caller_its_work(pg_test_engine):
    from gdx_dispatch.modules.outlook.views_router import _link_labels

    rows = [_Stub(linked_customer_id=str(uuid.uuid4()), linked_job_id=None)]
    customers, jobs, deleted = _caller_survives(
        pg_test_engine, "customers", lambda db: _link_labels(db, rows),
    )
    assert customers == {} and jobs == {} and deleted == set()


def test_pg_mailbox_address_cannot_cost_the_caller_its_work(pg_test_engine):
    from gdx_dispatch.modules.outlook.views_router import _mailbox_address

    msg = _Stub(account_id=uuid.uuid4())
    assert _caller_survives(
        pg_test_engine, None,            # outlook_accounts: absent from the fixture
        lambda db: _mailbox_address(db, msg),
    ) is None


def test_pg_views_tech_email_load_cannot_cost_the_caller_its_work(pg_test_engine):
    from gdx_dispatch.modules.outlook.views_router import _load_tech_emails

    assert _caller_survives(pg_test_engine, "users", _load_tech_emails) == set()


def test_pg_unbadged_folder_read_cannot_cost_the_caller_its_work(pg_test_engine):
    from gdx_dispatch.modules.outlook.views_router import _unbadged_folder_ids

    assert _caller_survives(
        pg_test_engine, None,            # outlook_folders: absent from the fixture
        _unbadged_folder_ids,
    ) == []


# ---------------------------------------------------------------------------
# modules/outlook/visibility.py
# ---------------------------------------------------------------------------


def test_pg_role_is_tech_lookup_cannot_cost_the_caller_its_work(pg_test_engine):
    from gdx_dispatch.modules.outlook.visibility import _accounts_role_is_tech

    assert _caller_survives(
        pg_test_engine, "users",
        lambda db: _accounts_role_is_tech(db, str(uuid.uuid4())),
    ) is False


def test_pg_visibility_role_preload_cannot_cost_the_caller_its_work(pg_test_engine):
    """The preload's handler claims the cost is an N+1 slowdown. On a poisoned
    Postgres transaction the per-row fallback it names cannot run either.

    This one needs its two EARLIER reads to succeed before the third can be the
    failure, and ``outlook_settings``/``outlook_accounts`` are absent from the
    ``TenantBase`` dump — so create them from the ORM and seed one account. That
    is the only way to reach read 3 of 3; renaming a table the first read needs
    would test the wrong line.
    """
    from gdx_dispatch.modules.outlook.models import OutlookAccount, OutlookSettings
    from gdx_dispatch.modules.outlook.visibility import build_visibility_context

    OutlookSettings.__table__.create(pg_test_engine, checkfirst=True)
    OutlookAccount.__table__.create(pg_test_engine, checkfirst=True)
    account_id, user_id = uuid.uuid4(), str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    with pg_test_engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO outlook_accounts (id, user_id, provider, created_at, "
                "updated_at) VALUES (:a, :u, 'outlook', :now, :now)"
            ),
            {"a": account_id, "u": user_id, "now": now},
        )

    messages = [_Stub(account_id=account_id)]
    ctx = _caller_survives(
        pg_test_engine, "users", lambda db: build_visibility_context(messages, db),
    )
    assert ctx.user_is_tech == {}, "the preload did not degrade as its handler says"
    assert ctx.account_owner == {account_id: user_id}, "read 2 of 3 never ran"


# ---------------------------------------------------------------------------
# tasks/plugin_email_outbox.py
# ---------------------------------------------------------------------------


def test_pg_automation_sender_read_cannot_cost_the_caller_its_work(pg_test_engine):
    """``drain_plugin_email_outbox`` commits the row's outcome on this session
    after ``_deliver`` returns. Uncontained that commit raises out of the task:
    the batch is abandoned and ``row.attempts`` is never saved, so MAX_ATTEMPTS
    cannot retire the row and it is reclaimed every STALE_CLAIM_MINUTES for as
    long as the read fails. Not a duplicate send — with no sender id Graph is
    never tried, and the poisoned session cannot read the SMTP config either,
    so nothing went out."""
    from gdx_dispatch.tasks.plugin_email_outbox import _automation_sender

    assert _caller_survives(pg_test_engine, "app_settings", _automation_sender) is None


def test_pg_deliver_customer_read_propagates_but_leaves_the_txn_usable(pg_test_engine):
    """GDXA-155's check 1, as a test.

    ``_deliver``'s ``except`` catches ``(ValueError, TypeError)`` — a malformed
    customer_id — so a DB error does NOT land there. It propagates to
    ``drain_plugin_email_outbox``'s ``except Exception``, which then sets
    ``row.status`` and calls ``db.commit()`` on THIS session. So the assertion
    here is deliberately not "it degrades": it is that the error still escapes
    (that behaviour is unchanged and correct) AND the transaction it escapes
    through is still usable, which is what lets that handler's write land.

    Without the containment the second half fails: the commit standing in for
    drain's raises 25P02 and the row's outcome is never recorded.

    Note what this test does NOT use, and why: not ``_caller_survives``. The
    pending-work framing does not apply at this site, because ``_deliver`` calls
    ``_consented`` first and that reaches ``core/plugin_consent``'s
    ``ensure_consent_table``, which COMMITS — so anything pending is already
    durable before the read under test runs, and "the caller's row survived"
    would pass either way. The discriminator has to be work staged AFTER the
    failure, which is exactly what drain's handler does.
    """
    from gdx_dispatch.tasks.plugin_email_outbox import _deliver

    Session = _sessions(pg_test_engine)
    # Consent, or _deliver returns (False, "consent_missing") before ever
    # reaching the customer read. Seeded as raw SQL rather than via
    # record_consent() so this test does not depend on plugin discovery.
    with pg_test_engine.begin() as c:
        c.execute(
            text(
                "CREATE TABLE IF NOT EXISTS plugin_consent ("
                "plugin_key TEXT PRIMARY KEY, permissions TEXT NOT NULL, "
                "consented_by TEXT, "
                "consented_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                "declared_events TEXT, declared_fingerprint TEXT)"
            )
        )
        c.execute(
            text(
                "INSERT INTO plugin_consent (plugin_key, permissions) "
                "VALUES ('demo', 'email')"
            )
        )
        c.execute(text("ALTER TABLE customers RENAME TO customers_gone"))

    db = Session()
    row = _Stub(
        id=uuid.uuid4(), plugin_key="demo", customer_id=str(uuid.uuid4()),
        to_email="", contact_id=None, subject="s", body_html="<p>b</p>",
        body_text="", company_id=TENANT, entity_type=None, entity_id=None,
    )

    with pytest.raises(Exception) as exc:
        _deliver(db, row)
    assert "does not exist" in str(exc.value).lower(), f"expected UndefinedTable, got {exc.value}"

    # Exactly what drain_plugin_email_outbox's `except Exception` goes on to do:
    # stage the row's outcome and commit it on this same session.
    db.add(_Row(id=2, v="the outcome drain records"))
    db.commit()
    db.close()
    with pg_test_engine.connect() as other:
        assert other.execute(
            text("SELECT count(*) FROM gdxa155_row WHERE id = 2")
        ).scalar() == 1, (
            "drain could not record the row's outcome — the claim leaks as 'sending'"
        )


def test_pg_repeated_contained_failures_on_one_session(pg_test_engine):
    """Several of these run back to back on one real request: a send resolves
    branding, then the recipient, then the designated sender, then the
    duplicate-send guard. One contained failure must not make the next
    savepoint unopenable."""
    from gdx_dispatch.core.email_layout import email_branding
    from gdx_dispatch.core.transactional_email import (
        _designated_sender_user_id,
        recently_sent,
    )

    def _call(db):
        assert email_branding(db)["company_name"] == "Your Service Company"
        assert _designated_sender_user_id(db) is None
        assert recently_sent(db, "estimate", str(uuid.uuid4())) is False
        return True

    assert _caller_survives(pg_test_engine, "app_settings", _call) is True


def test_a_window_constant_is_untouched():
    """Cheap tripwire for the one non-mechanical risk in this change: the
    SAVEPOINT wrapping re-indented blocks, and a mis-scoped edit could have
    moved a statement out of an ``if``. Runs on SQLite too, so it is in the
    default suite rather than only under Postgres."""
    from gdx_dispatch.core.customer_views import VIEW_DEDUPE_WINDOW

    # Written as seconds rather than `== timedelta(minutes=30)`: ruff reads a
    # CONSTANT on the left of `==` as a Yoda condition (SIM300), and the ratchet
    # is a count against a baseline, so one new violation fails the gate.
    assert VIEW_DEDUPE_WINDOW.total_seconds() == 30 * 60

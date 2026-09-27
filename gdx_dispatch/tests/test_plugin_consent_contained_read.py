"""``any_event_consent`` must not disturb the caller's transaction — GDXA-137.

Child of GDXA-86 (``core.database.contained_read``). ``emit_domain_event``
promises, in its own docstring, that "Webhook fan-out must NEVER break that
write". GDXA-86 contained the two reads in ``_emit``; this is the third, in
another owner's file, and the one where containment was never what was missing.

``any_event_consent`` was already SAVEPOINT-wrapped, with ``db.begin_nested()``,
and that genuinely contains the failure it was written for (``UndefinedTable``
on the lazily-created raw ``plugin_consent`` table). What it does not contain is
the caller: ``SessionTransaction._take_snapshot`` FLUSHES on every
``begin_nested()``, and that flush runs while the transaction object is still
being constructed — before the savepoint exists. A caller holding a row that
cannot flush therefore never enters the block, no savepoint is ever emitted, and
the abort lands upstream of any containment, where ``any_event_consent``'s own
``except`` swallows the only evidence of it.

Two halves, and both are tested here because they redden for different reasons:

- **the early flush** — dialect-independent, so it is in the first section. A
  failed flush deactivates the session on SQLite exactly as on Postgres, and
  that is what converts the caller's *handleable* error into
  ``PendingRollbackError``. Swap ``contained_read`` back for
  ``db.begin_nested()`` and both tests in that section fail.
- **the poisoning** — Postgres-only. SQLite does not abort a transaction on a
  failed statement, so the PG arm holds every test of that, and it SKIPS
  (green) with no reachable Postgres, failing under CI per #440. A green
  SQLite-only run is not evidence for that half.

``ensure_consent_table`` is the other site the GDXA-86 census predicate flags in
this file. It is deliberately NOT contained — it commits — and the last test
here is the measurement behind that call rather than an assertion that it is
fine.
"""
from __future__ import annotations

from contextlib import suppress

import pytest
from sqlalchemy import Column, Integer, String, create_engine, event, text
from sqlalchemy.exc import IntegrityError, PendingRollbackError
from sqlalchemy.orm import declarative_base, sessionmaker

from gdx_dispatch.core.plugin_consent import (
    any_event_consent,
    ensure_consent_table,
    record_consent,
)

TENANT = "11111111-1111-4111-8111-111111111111"

_Base = declarative_base()


class _Row(_Base):
    """Stands in for the caller's half-built invoice/job at the choke point."""

    __tablename__ = "gdxa137_row"
    id = Column(Integer, primary_key=True)
    v = Column(String)


def _sessions(engine):
    # autoflush=False mirrors core.database.SessionLocal, which is what every
    # session reaching this helper in the app actually is.
    _Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


@pytest.fixture
def sqlite_sessions(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/t.db", future=True)

    # SQLAlchemy's documented SQLite-SAVEPOINT recipe, copied from
    # test_webhooks_emit.py::_session, which explains why it is load-bearing:
    # pysqlite's implicit BEGIN otherwise breaks nested-transaction rollback, so
    # a SAVEPOINT release would wrongly persist — which is exactly the thing
    # test_any_event_consent_does_not_commit_the_callers_pending_work asserts
    # does not happen. Without this, that test could pass because pysqlite is
    # wrong rather than because the helper is right. Production is Postgres,
    # which gets savepoints right natively.
    @event.listens_for(engine, "connect")
    def _sqlite_no_implicit_begin(dbapi_conn, _rec):  # noqa: ANN001
        dbapi_conn.isolation_level = None

    @event.listens_for(engine, "begin")
    def _emit_real_begin(conn):  # noqa: ANN001
        conn.exec_driver_sql("BEGIN")

    yield _sessions(engine), engine
    engine.dispose()


# ---------------------------------------------------------------------------
# Both dialects: the probe must not flush, and must not commit
# ---------------------------------------------------------------------------


def test_any_event_consent_does_not_flush_the_callers_pending_work(sqlite_sessions):
    """The one-line version of the whole fix. ``db.begin_nested()`` flushes on
    entry; ``contained_read`` opens its SAVEPOINT on the Connection, below the
    ORM, and holds ``no_autoflush``, so it writes nothing.

    Restore ``db.begin_nested()`` in ``any_event_consent`` and this fails on
    ``pending == 0``. No plugin_consent table here on purpose — that is the
    fresh-box state, and the read failing is what the savepoint is for.
    """
    Session, _ = sqlite_sessions
    db = Session()
    db.add(_Row(id=1, v="the caller's pending work"))

    assert any_event_consent(db) is False  # missing table degrades to "no plugins"

    assert len(db.new) == 1, "the consent probe flushed the caller's pending work"
    db.rollback()
    db.close()


def test_any_event_consent_leaves_the_callers_own_error_for_the_caller(sqlite_sessions):
    """The sharpest consequence of the early flush, and it needs no Postgres.

    The caller stages a row that violates a unique constraint — an
    ``IntegrityError`` it was going to handle. With ``db.begin_nested()`` the
    probe's snapshot flush raises that error *inside* ``any_event_consent``,
    whose ``except Exception: return False`` swallows it and hands back a
    deactivated session; the caller's ``commit()`` then raises
    ``PendingRollbackError`` and its own ``except IntegrityError`` never runs.
    Measured on PG 15.17 and reproduced here on SQLite, because a failed flush
    deactivates the session on every dialect.

    With ``contained_read`` the flush does not happen here at all, so the error
    stays the caller's, raised where the caller is looking for it.
    """
    Session, _ = sqlite_sessions
    seed = Session()
    seed.add(_Row(id=1, v="already committed"))
    seed.commit()
    seed.close()

    db = Session()
    db.add(_Row(id=1, v="the caller's duplicate — its own error to handle"))

    assert any_event_consent(db) is False
    assert len(db.new) == 1, "the probe flushed, so the error is no longer the caller's"

    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    db.close()


def test_any_event_consent_does_not_commit_the_callers_pending_work(sqlite_sessions):
    """RELEASE SAVEPOINT must never become a commit.

    On SQLite, releasing a savepoint that is itself the outermost transaction
    boundary DOES commit (``core/audit.py`` documents why there is no
    ``commit=False`` variant for the same reason). A read probe that hardened
    the caller's half-built invoice would be far worse than the bug it fixes.

    The consent table EXISTS here, which is what gives this teeth: the probe's
    savepoint then RELEASES rather than rolling back, which is the only path on
    which a stray commit could happen. The caller flushes first for the same
    reason — with nothing written, "another connection sees 0 rows" is true
    either way and the assertion could not tell the two apart.
    """
    Session, engine = sqlite_sessions
    setup = Session()
    ensure_consent_table(setup)  # commits its own DDL; before the caller starts
    setup.close()

    db = Session()
    db.add(_Row(id=1, v="must not be committed by a read"))
    db.flush()

    assert any_event_consent(db) is False, "an empty consent table means no plugins"

    assert db.execute(text("SELECT count(*) FROM gdxa137_row")).scalar() == 1
    with engine.connect() as other:
        assert other.execute(text("SELECT count(*) FROM gdxa137_row")).scalar() == 0, (
            "the consent probe committed the caller's flushed-but-uncommitted row"
        )
    db.rollback()
    db.close()


def test_any_event_consent_still_answers_yes_when_a_plugin_is_consented(sqlite_sessions):
    """The containment must not cost the probe its answer — a gate that can only
    say False would silently stop every plugin event fan-out, which no
    transaction test here would notice."""
    Session, _ = sqlite_sessions
    db = Session()
    ensure_consent_table(db)
    db.execute(
        text(
            "INSERT INTO plugin_consent (plugin_key, permissions, declared_events, "
            "declared_fingerprint) VALUES ('p1', 'events', '[\"invoice.paid\"]', 'fp')"
        )
    )
    db.commit()

    assert any_event_consent(db) is True
    db.close()


# ---------------------------------------------------------------------------
# Postgres only: the poisoning, and the fix for it
# ---------------------------------------------------------------------------


def test_pg_missing_consent_table_cannot_cost_the_caller_its_row(pg_test_engine):
    """The reported defect's own scenario, with ORM pending work.

    ``test_plugin_events.py::test_any_event_consent_missing_table_does_not_poison_postgres_txn``
    already covers the fresh-box ``UndefinedTable``, but its caller's work is a
    raw ``INSERT`` into a TEMP table — nothing is pending in the Session, so
    ``begin_nested()``'s snapshot flush is a no-op there and that test cannot
    distinguish the two idioms. This one stages through the ORM, which is what
    a real choke point does.

    ``plugin_consent`` is absent from the PG template on purpose and not by
    accident of the fixture: it is lazily created and not an ORM model, so its
    absence IS the fresh-box production state. Delete the containment in
    ``any_event_consent`` entirely and this reddens.
    """
    Session = _sessions(pg_test_engine)
    db = Session()
    db.add(_Row(id=1, v="the caller's pending work"))

    assert any_event_consent(db) is False

    assert len(db.new) == 1, "the probe flushed the caller's pending work"
    db.commit()
    db.close()
    with pg_test_engine.connect() as other:
        assert other.execute(text("SELECT count(*) FROM gdxa137_row")).scalar() == 1, (
            "a failed consent read cost the caller its row"
        )


def test_pg_a_caller_that_cannot_flush_keeps_its_session_and_its_error(pg_test_engine):
    """The case ``begin_nested()`` cannot contain, because its flush runs BEFORE
    the savepoint exists.

    The caller's own table is renamed out from under it, so its pending INSERT
    cannot flush — a not-yet-migrated or drifted schema, or the window during a
    migration. Measured on PG 15.17:

    - ``begin_nested()`` → the probe dies in the snapshot flush without ever
      reading ``plugin_consent``, ``db.new`` is emptied, ``is_active`` goes
      False, and the caller's ``commit()`` raises ``PendingRollbackError``
      naming no cause — the real error was swallowed by the probe's ``except``.
    - ``contained_read`` → asserted below.

    The caller still cannot commit; nothing could make an unflushable row
    flushable. What changes is that it is handed back a live session and its own
    diagnosable error instead of a dead one and a shrug. Restore
    ``db.begin_nested()`` and the ``is_active`` and ``UndefinedTable``
    assertions both fail.
    """
    Session = _sessions(pg_test_engine)
    db = Session()
    db.add(_Row(id=1, v="the caller's unflushable pending work"))
    with pg_test_engine.begin() as c:
        # A separate connection, and before the caller emits any SQL, so this
        # needs no lock the caller is holding.
        c.execute(text("ALTER TABLE gdxa137_row RENAME TO gdxa137_row_gone"))

    assert any_event_consent(db) is False

    assert len(db.new) == 1, "the probe flushed, so the caller's row is already gone"
    assert db.is_active, "the probe handed back a deactivated session"

    with pytest.raises(Exception) as exc:
        db.commit()
    assert not isinstance(exc.value, PendingRollbackError), (
        "the caller got PendingRollbackError — its own error was swallowed by the probe"
    )
    assert "gdxa137_row" in str(exc.value), f"not the caller's own failure: {exc.value}"

    db.rollback()
    db.close()


def test_pg_repeated_probes_survive(pg_test_engine):
    """Two entities in one business transaction is enough to call this twice
    (``test_webhooks_emit.py::test_rollback_drops_plugin_and_workflow_dispatch_too``
    is built on exactly that). One contained failure must not make the next
    probe's savepoint unopenable."""
    Session = _sessions(pg_test_engine)
    db = Session()
    db.add(_Row(id=1, v="survivor"))

    for _ in range(3):
        assert any_event_consent(db) is False

    db.commit()
    db.close()
    with pg_test_engine.connect() as other:
        assert other.execute(text("SELECT count(*) FROM gdxa137_row")).scalar() == 1


def _consent_table_is_a_view(engine):
    """Make the ALTER in ``ensure_consent_table`` fail for real, rather than
    mocking it: a VIEW named ``plugin_consent``. ``CREATE TABLE IF NOT EXISTS``
    skips over a view of that name without an error (measured on PG 15.17 —
    worth stating, because an error there would exercise a different line), and
    ``ALTER TABLE ... ADD COLUMN`` then refuses, which is the swallowed
    statement under test. Returns the sessionmaker; the view is created once.
    """
    Session = _sessions(engine)
    setup = Session()
    setup.execute(text("CREATE VIEW plugin_consent AS SELECT 1 AS plugin_key"))
    setup.commit()
    setup.close()
    return Session


def _emit(db):
    from gdx_dispatch.core.webhooks.emit import emit_domain_event

    return emit_domain_event(
        db, "invoice.paid", "e1a0c3f6-0b1a-4c3d-8e5f-0a1b2c3d4e5f", {},
        tenant_id=TENANT,
    )


def test_pg_a_matching_subscription_still_poisons_upstream_of_this_probe(pg_test_engine):
    """Pins a KNOWN LIMIT, asserting the bad behaviour on purpose.

    Found by the GDXA-137 adversarial audit, not by the tests above, and it is the
    boundary of what this fix is worth: every assertion in this file so far runs
    the probe directly, so none of them can see what ``_emit`` does BEFORE
    reaching it. ``_emit`` stages each delivery row inside its own
    ``db.begin_nested()`` + ``flush()``, and that savepoint snapshot-flushes for
    exactly the same reason this probe used to — so with one active subscription
    matching the event, a caller whose row cannot flush is already dead before
    the consent probe is ever called.

    The two halves side by side, which is the only honest way to state the win:
    no subscription → the caller keeps its own ``UndefinedTable`` (this file's
    other PG tests, via the probe; here, via the whole of ``emit_domain_event``);
    one subscription → ``PendingRollbackError``, the pre-GDXA-137 outcome, and
    ``emit_domain_event``'s promise is still not kept.

    NOT fixed here. That staging savepoint wraps a WRITE, so it genuinely needs
    the ORM-level ``begin_nested()`` and cannot take ``contained_read`` (rule 2);
    binding it correctly is platform-core's change in ``core/webhooks/emit.py``.
    When someone makes it, this test is where it lands — invert the second half.
    """
    from gdx_dispatch.routers.webhooks import WebhookSubscription

    Session = _sessions(pg_test_engine)

    # Half 1 — no subscription. The probe is the last thing in the way, and the
    # caller comes back out of a full emit holding its own error.
    db = Session()
    db.add(_Row(id=1, v="the caller's unflushable pending work"))
    with pg_test_engine.begin() as c:
        c.execute(text("ALTER TABLE gdxa137_row RENAME TO gdxa137_row_gone"))
    assert _emit(db) == 0
    with pytest.raises(Exception) as exc:
        db.commit()
    assert not isinstance(exc.value, PendingRollbackError), (
        "no subscription, yet the caller's own error was still swallowed"
    )
    assert "gdxa137_row" in str(exc.value), f"not the caller's own failure: {exc.value}"
    db.rollback()
    with pg_test_engine.begin() as c:
        c.execute(text("ALTER TABLE gdxa137_row_gone RENAME TO gdxa137_row"))
    db.close()

    # Half 2 — one matching subscription, so _emit's delivery-staging savepoint
    # flushes first. THE LIMIT: the caller is poisoned before the probe runs.
    seed = Session()
    seed.add(WebhookSubscription(
        company_id=TENANT, name="hook", url="https://example.test/hook",
        secret="s3cr3t", events='["invoice.paid"]', active=True,
    ))
    seed.commit()
    seed.close()

    db = Session()
    db.add(_Row(id=2, v="the caller's unflushable pending work"))
    with pg_test_engine.begin() as c:
        c.execute(text("ALTER TABLE gdxa137_row RENAME TO gdxa137_row_gone"))
    assert _emit(db) == 0, "emit still degrades to 'no webhook', as it promises"
    with pytest.raises(PendingRollbackError):
        db.commit()  # the half emit_domain_event's promise does NOT yet cover
    with suppress(Exception):
        db.rollback()
    db.close()


def test_pg_ensure_consent_table_does_not_hand_on_a_poisoned_session(pg_test_engine):
    """Why the OTHER site the GDXA-86 census flags in this file is out of scope.

    ``ensure_consent_table`` has the census shape — a caller-owned session, a
    ``db.execute`` in a ``try``, a swallowing ``except`` — and is not the defect.
    The reason is the ``db.commit()`` two lines below that ``except``, and it cuts
    both ways at once:

    1. It commits, so it can never go inside a savepoint; transaction control
       there would release the very savepoint meant to contain the work.
       ``core/audit.py`` hoists ``ensure_audit_table`` out of
       ``audit_best_effort``'s ``begin_nested()`` for this exact reason,
       documented at its point (1).
    2. That same commit **ends** the aborted transaction, so no caller is handed
       back a session that is still poisoned — the defining harm of the GDXA-86
       class. Measured in all three caller shapes below.

    It does NOT follow that the commit makes this function safe, and shape 3 is
    why: a caller that has already flushed loses its work silently. That is a
    worse defect than the one this file is about; it is simply not the same one,
    and not reachable from any of today's five entry points.

    Asserts today's behaviour on purpose. If a future edit drops the commit or
    wraps this in a savepoint, the abort starts travelling — it has joined the
    class, and needs a real fix rather than this note.
    """
    Session = _consent_table_is_a_view(pg_test_engine)

    # Shape 1 — nothing staged. This is routers/admin_plugins.py's consent
    # route, which reaches here through record_consent before staging anything.
    db = Session()
    ensure_consent_table(db)  # returns quietly: the DDL failed and was swallowed
    assert db.is_active, "the caller was handed a deactivated session"
    assert db.execute(text("SELECT 1")).scalar() == 1, (
        "the caller was handed a poisoned session — this IS the GDXA-86 class now"
    )
    db.rollback()
    db.close()

    # ...and the real caller does not degrade silently either: record_consent's
    # own INSERT raises on its own merits, naming the actual problem.
    db = Session()
    with pytest.raises(Exception) as exc:
        record_consent(db, "p1", ["events"], by="owner")
    assert "permissions" in str(exc.value), f"not the real cause: {exc.value}"
    with suppress(Exception):
        db.rollback()
    db.close()

    # Shape 2 — a caller with pending work. The flush inside this function's own
    # db.commit() raises out of this frame: loud, and in the right place.
    db = Session()
    db.add(_Row(id=1, v="the caller's pending work"))
    with pytest.raises(Exception) as exc:
        ensure_consent_table(db)
    assert "aborted" in str(exc.value).lower(), f"expected 25P02, got {exc.value}"
    with suppress(Exception):
        db.rollback()
    db.close()

    # Shape 3 — a caller that has ALREADY FLUSHED. Found by the GDXA-137
    # adversarial audit after the two above were written and the docstring had
    # already called the pair exhaustive; it is the worst of the three and the
    # one neither of them would have caught.
    #
    # Nothing is pending, so the commit has nothing to flush and raises nothing —
    # but the transaction is already aborted, and Postgres answers COMMIT on an
    # aborted transaction with a ROLLBACK rather than an error. So this returns
    # quietly, hands back a live session, the caller's own commit() reports
    # success, and the caller's row is GONE. A silent write loss, which this repo
    # classes as the highest-severity defect shape — asserted here rather than
    # described, so that the docstring above cannot drift back to claiming the
    # commit makes this safe.
    #
    # NOT live-reachable today: all five entry points were re-walked and none
    # arrives having flushed (the docstring above names them one by one). This is
    # the precondition a future caller breaks, and the reason ensure_consent_table
    # must not be given one — not a defect being shipped.
    db = Session()
    db.add(_Row(id=3, v="the caller's ALREADY FLUSHED work"))
    db.flush()
    ensure_consent_table(db)  # no raise: there was nothing left to flush
    assert db.is_active, "a deactivated session would at least be loud"
    db.commit()  # reports success
    db.close()
    with pg_test_engine.connect() as other:
        assert other.execute(
            text("SELECT count(*) FROM gdxa137_row WHERE id = 3")
        ).scalar() == 0, (
            "the flushed row survived — the silent-loss shape is fixed, so fix "
            "the docstring above and delete this block"
        )

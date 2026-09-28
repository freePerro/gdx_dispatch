"""``contained_read`` — GDXA-86.

The class: a helper takes a session it does not own, reads inside a ``try``,
and returns a default from the ``except``. On Postgres that swallow is a lie —
the failed statement aborted the whole transaction, so the caller's next
``commit()`` raises ``InFailedSqlTransaction`` (25P02) and the work the helper
was protecting is lost.

**The poisoning is Postgres-only.** SQLite does not abort a transaction on a
failed statement, so every test that proves the defect itself lives in the PG
arm, and that arm SKIPS on a laptop with no reachable Postgres (it fails under
CI, per #440). A green SQLite-only run is therefore NOT evidence for this
class — which is how a whole class of these shipped unnoticed.

What the SQLite arm can still prove, and does: that the containment is
behaviour-neutral on the success path (no flush, no commit of the caller's
pending work) and that it re-raises so the caller's own handler still runs.
"""
from __future__ import annotations

import ast
import inspect
from contextlib import suppress
from pathlib import Path

import pytest
from sqlalchemy import Column, Integer, String, create_engine, select, text
from sqlalchemy.orm import declarative_base, sessionmaker

from gdx_dispatch.core.database import contained_read

_Base = declarative_base()


class _Row(_Base):
    __tablename__ = "gdxa86_row"
    id = Column(Integer, primary_key=True)
    v = Column(String)


BAD_READ = text("SELECT 1 FROM table_that_does_not_exist")

# A mapped class on its OWN metadata, so `_Base.metadata.create_all` never
# creates its table. Needed because only an ORM read autoflushes: a raw
# `text()` statement through `Session.execute` does not, so a `text()` probe
# cannot exercise the `no_autoflush` half of the guard at all.
_Unmapped = declarative_base()


class _MissingTable(_Unmapped):
    __tablename__ = "gdxa86_never_created"
    id = Column(Integer, primary_key=True)


def _sessionmaker(engine):
    # autoflush=False mirrors core.database.SessionLocal, which is what every
    # session in the app is. The one test that needs the opposite builds its own
    # autoflush=True session, because nothing else here can reach that branch.
    _Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


@pytest.fixture
def sqlite_sessions(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/t.db", future=True)
    yield _sessionmaker(engine), engine
    engine.dispose()


# ---------------------------------------------------------------------------
# Both dialects: the success path must not disturb the caller
# ---------------------------------------------------------------------------


def test_success_path_does_not_flush_the_callers_pending_work(sqlite_sessions):
    """The whole reason this opens the SAVEPOINT on the CONNECTION.

    ``db.begin_nested()`` — the idiom this repo reached for first — calls
    ``SessionTransaction._take_snapshot``, which flushes. At a site whose
    caller has already ``add()``ed a half-built row, that flush happens BEFORE
    the savepoint exists, so a failure in it is contained by nothing and gets
    swallowed by the handler below. Swap ``contained_read`` for
    ``db.begin_nested()`` and this test fails on ``pending == 1``.
    """
    Session, _ = sqlite_sessions
    db = Session()
    db.add(_Row(id=1, v="the caller's pending work"))

    with contained_read(db):
        assert db.execute(text("SELECT 1")).scalar() == 1

    assert len(db.new) == 1, "the savepoint flushed the caller's pending work"
    db.rollback()
    db.close()


def test_success_path_does_not_commit_the_callers_pending_work(sqlite_sessions):
    """RELEASE SAVEPOINT must never become a commit.

    On SQLite, releasing a savepoint that is itself the outermost transaction
    boundary DOES commit (see ``core/audit.py``'s note on why there is no
    ``commit=False`` flag). A read helper that hardened its caller's half-built
    invoice would be far worse than the bug it fixes, so: prove it doesn't.

    The caller FLUSHES first, which is what gives this test teeth: with nothing
    written, "another connection sees 0 rows" is true whether or not RELEASE
    commits, so the assertion could not tell the two apart. After a flush the
    row exists inside the transaction, and whether an outside connection can see
    it is exactly the question.
    """
    Session, engine = sqlite_sessions
    db = Session()
    db.add(_Row(id=1, v="must not be committed by a read"))
    db.flush()

    with contained_read(db):
        db.execute(text("SELECT 1"))

    assert db.execute(text("SELECT count(*) FROM gdxa86_row")).scalar() == 1
    with engine.connect() as other:
        assert other.execute(text("SELECT count(*) FROM gdxa86_row")).scalar() == 0, (
            "RELEASE SAVEPOINT committed the caller's flushed-but-uncommitted row"
        )

    db.rollback()
    db.close()
    with engine.connect() as other:
        assert other.execute(text("SELECT count(*) FROM gdxa86_row")).scalar() == 0


def test_staging_a_write_inside_the_block_is_warned_about(sqlite_sessions, caplog):
    """Rule 2, checked rather than only documented.

    The savepoint is on the CONNECTION, below the ORM, so a write staged inside
    the block and not yet flushed survives the rollback and lands at the
    caller's commit — a silent write, worse than the abort being contained.
    ``core/audit.py``'s ``audit_best_effort`` is the precedent: "documented
    preconditions that nothing checks are how this defect class got here".

    A warning, not a raise: by the time it fires the read has already run, and
    converting a degraded read into a 500 is the mistake rule 1 names.
    """
    Session, _ = sqlite_sessions
    db = Session()
    db.add(_Row(id=1, v="staged BEFORE the block — must not warn on its own"))

    with caplog.at_level("WARNING"), contained_read(db):
        db.execute(text("SELECT 1"))
    assert "contained_read_staged_a_write" not in caplog.text, (
        "a pure read with pre-existing pending work must not warn"
    )

    caplog.clear()
    with caplog.at_level("WARNING"), contained_read(db):
        db.add(_Row(id=2, v="staged INSIDE the block — the misuse"))
    assert "contained_read_staged_a_write" in caplog.text

    db.rollback()
    db.close()


def test_mutating_a_loaded_row_inside_the_block_is_warned_about(sqlite_sessions, caplog):
    """The guard for the ``dirty`` third of ``_pending_counts``.

    Mutating an already-loaded row stages a write without ``add()``, so ``new``
    and ``deleted`` both stay empty. The connection-level savepoint never knew
    about it either, so the UPDATE is flushed at the caller's own commit — the
    same "looks contained, isn't" shape as a staged insert.

    This test exists because ``dirty`` was dropped mid-change as an
    optimisation, the whole suite stayed green, and only an audit caught it.
    Remove ``len(db.dirty)`` from ``_pending_counts`` and this reddens; without
    it, nothing does.
    """
    Session, _ = sqlite_sessions
    db = Session()
    db.add(_Row(id=1, v="before"))
    db.commit()

    row = db.execute(select(_Row).where(_Row.id == 1)).scalar_one()
    with caplog.at_level("WARNING"), contained_read(db):
        row.v = "MUTATED INSIDE THE BLOCK"
    assert "contained_read_staged_a_write" in caplog.text, (
        "a mutation inside the block staged a write and nothing warned"
    )

    db.rollback()
    db.close()


def test_it_reraises_so_the_callers_handler_still_runs(sqlite_sessions):
    """It contains transaction damage and nothing else. If it swallowed, every
    call site's ``except`` — its log line and its default — would go dead."""
    Session, _ = sqlite_sessions
    db = Session()
    ran = False
    try:
        with contained_read(db):
            db.execute(BAD_READ)
    except Exception:
        ran = True
    assert ran, "contained_read swallowed the failure instead of re-raising"
    db.close()


# ---------------------------------------------------------------------------
# Postgres only: the defect, and the fix for it
# ---------------------------------------------------------------------------


def _pg_sessions(pg_test_engine):
    return _sessionmaker(pg_test_engine)


def test_pg_a_bare_swallowed_read_loses_the_callers_row(pg_test_engine):
    """The defect, reproduced. This is what the rest of the class still does
    (the remaining sites are enumerated per owner in GDXA-86's issue thread and
    in the commit message's sibling-sweep block — not in a PR body, which an
    earlier draft of this line pointed at before one existed).

    Asserts the BROKEN behaviour on purpose: if a future SQLAlchemy or
    Postgres stops poisoning the transaction, this test goes red and the whole
    premise of ``contained_read`` needs re-reading rather than quietly
    surviving as cargo cult.
    """
    Session = _pg_sessions(pg_test_engine)
    db = Session()
    db.add(_Row(id=1, v="the caller's work"))

    with suppress(Exception):              # the helper's `except: return default`
        db.execute(BAD_READ)

    with pytest.raises(Exception) as exc:
        db.commit()
    assert "aborted" in str(exc.value).lower(), f"expected 25P02, got {exc.value}"

    db.rollback()
    db.close()
    with pg_test_engine.connect() as other:
        assert other.execute(text("SELECT count(*) FROM gdxa86_row")).scalar() == 0, (
            "the caller's row survived — the defect did not reproduce"
        )


def test_pg_contained_read_keeps_the_callers_transaction_committable(pg_test_engine):
    """The fix, on the same failure the test above loses a row to."""
    Session = _pg_sessions(pg_test_engine)
    db = Session()
    db.add(_Row(id=1, v="the caller's work"))

    try:
        with contained_read(db):
            db.execute(BAD_READ)
    except Exception:
        pass                              # exactly what the call sites do

    assert len(db.new) == 1, "the caller's pending work was flushed away"
    db.commit()                            # unpoisoned — this is the whole fix
    db.close()

    with pg_test_engine.connect() as other:
        assert other.execute(text("SELECT count(*) FROM gdxa86_row")).scalar() == 1


def test_pg_survives_repeated_failed_reads(pg_test_engine):
    """Several of these helpers run back to back on one request (audit label
    resolution does two). One contained failure must not make the next one's
    savepoint unopenable."""
    Session = _pg_sessions(pg_test_engine)
    db = Session()
    db.add(_Row(id=1, v="survivor"))

    for _ in range(3):
        try:
            with contained_read(db):
                db.execute(BAD_READ)
        except Exception:
            pass

    db.commit()
    db.close()
    with pg_test_engine.connect() as other:
        assert other.execute(text("SELECT count(*) FROM gdxa86_row")).scalar() == 1


def test_pg_a_statement_timeout_is_contained(pg_test_engine):
    """57014, the one transient class the helper's docstring claims is contained.

    Prod runs ``statement_timeout=0`` today (checked live 2026-09-27), so this is
    insurance against a future setting rather than a current failure — but the
    docstring says "proven contained", and that word needs a test behind it or it
    is just a plausible claim. ``SET LOCAL`` so the timeout dies with the
    transaction and cannot leak into another test's session.
    """
    Session = _pg_sessions(pg_test_engine)
    db = Session()
    db.add(_Row(id=1, v="must survive a statement timeout"))
    db.execute(text("SET LOCAL statement_timeout = '50ms'"))

    with pytest.raises(Exception) as exc, contained_read(db):
        db.execute(text("SELECT pg_sleep(2)"))
    assert getattr(exc.value.orig, "pgcode", None) == "57014", f"not a timeout: {exc.value}"

    assert len(db.new) == 1
    db.commit()
    db.close()
    with pg_test_engine.connect() as other:
        assert other.execute(text("SELECT count(*) FROM gdxa86_row")).scalar() == 1


def test_pg_a_flushed_write_inside_the_block_is_the_hole_rule_2_cannot_see(pg_test_engine):
    """Pins a KNOWN LIMIT, asserting the bad behaviour on purpose.

    Rule 2's warning diffs ``new``/``dirty``/``deleted``. A write that is staged
    AND flushed inside the block leaves both snapshots empty, so nothing warns —
    and the INSERT, being inside the connection savepoint, is rolled away. The
    caller's ``commit()`` reports success with the row gone: silent loss, zero
    log lines, which is the more dangerous half of misusing this helper.

    Found by the GDXA-86 adversarial audit, not by this suite — the guard had
    been described as covering rule 2 when it covers one of its two directions.
    Documented here rather than fixed: a DML check could ride the Engine-level
    ``before_cursor_execute`` that ``core/performance.py:169`` already installs,
    but it would police a precondition no PRODUCTION call site violates. Say
    production and mean it: THIS suite violates it deliberately, at the
    ``db.add()`` + ``flush()`` below and again in
    ``test_staging_a_write_inside_the_block_is_warned_about`` — proving the
    hole is what those two are for. A DML check would fire on both, so
    whoever builds it exempts this file and inverts both assertions here.

    That sentence read "none of the eight call sites" and was still reading it
    after GDXA-137 made them nine: a second copy of a number whose first copy
    had already been corrected in ``contained_read``'s docstring. The number is
    gone from here rather than re-synced, because a count kept in two places is
    a count that is wrong in one. It lives in that docstring and is pinned by
    ``test_the_docstring_call_site_count_is_not_stale`` at the end of this file.

    This test has NO teeth about the helper, deliberately, and that is worth
    knowing before citing it: it passes with or without ``contained_read``,
    because on an aborted Postgres transaction ``COMMIT`` is turned into a
    silent ``ROLLBACK`` — so "0 rows" is the answer in both worlds. It pins the
    limit, not the fix.
    """
    Session = _pg_sessions(pg_test_engine)
    db = Session()

    with suppress(Exception), contained_read(db):
        db.add(_Row(id=7, v="flushed inside the block"))
        db.flush()
        db.execute(BAD_READ)

    db.commit()
    db.close()
    with pg_test_engine.connect() as other:
        assert other.execute(text("SELECT count(*) FROM gdxa86_row")).scalar() == 0, (
            "the flushed-write hole closed — update contained_read rule 2 and this test"
        )


def _swallowing_helper(db):
    """A helper of the class that catches its OWN failure — the shape of
    ``core/email_recipients.py``'s ``resolve_recipient`` and
    ``core/email_layout.py``'s ``email_branding``."""
    try:
        db.execute(BAD_READ)
    except Exception:
        return "default"
    return "read"


def test_pg_a_callee_that_swallows_its_own_failure_is_not_contained(pg_test_engine):
    """Rule 5, pinned as a KNOWN LIMIT — asserts the bad behaviour on purpose.

    ``contained_read`` rolls the savepoint back in ``__exit__``, which only runs
    for an exception that REACHES it. Wrap a callee that catches its own failure
    and the block exits clean, so the CM issues RELEASE SAVEPOINT on an aborted
    Postgres transaction — which raises 25P02 out of the ``with`` itself while the
    caller's ``commit()`` stays dead. Wrapping from the call site is therefore
    strictly worse than not wrapping: same lost row, plus a raise where the
    caller has no handler.

    It asserts THIS helper's behaviour, not a general law, and the difference
    matters because the first draft of rule 5 stated the general law and was
    wrong. Only ``RELEASE`` is illegal on an aborted transaction; ``ROLLBACK TO
    SAVEPOINT`` is legal, so an always-rollback variant DOES contain a
    self-swallowing callee from the caller's file (measured on PG 15.17 and
    16.14, GDXA-86 audit round 2). No such variant exists here and none is
    needed — the two call sites that would want one sit below
    ``_automation_email_settings``' ``skipped_disabled`` return in
    ``modules/workflows/engine.py``, on a path prod has switched off. So read
    this as "``contained_read`` is the wrong tool for a swallowing callee", never
    as "a swallowing callee cannot be contained".
    """
    Session = _pg_sessions(pg_test_engine)
    db = Session()
    db.add(_Row(id=1, v="the caller's work"))

    # `pytest.raises` outermost: what fires here is `contained_read.__exit__`
    # itself, on a block that exited without an exception.
    with pytest.raises(Exception) as exc, contained_read(db):
        assert _swallowing_helper(db) == "default"       # it ate its own failure
    assert "aborted" in str(exc.value).lower(), f"expected 25P02 from RELEASE, got {exc.value}"

    with pytest.raises(Exception):
        db.commit()                        # contained nothing — still poisoned
    db.rollback()
    db.close()
    with pg_test_engine.connect() as other:
        assert other.execute(text("SELECT count(*) FROM gdxa86_row")).scalar() == 0, (
            "RELEASE SAVEPOINT rescued an aborted transaction — rewrite rule 5"
        )


def test_pg_no_autoflush_is_what_stops_silent_data_loss(pg_test_engine):
    """The one assertion that covers ``db.no_autoflush``. Delete that clause and
    this test — and only this test — reddens.

    Every session in this app is ``SessionLocal(autoflush=False)``, so the rest
    of the file cannot reach this branch: ``_sessionmaker`` hardcodes autoflush
    off on purpose, which is exactly why the guard had no coverage until now.
    But ``contained_read`` is public in ``core/database.py``, and SQLAlchemy's
    own default — plain ``Session(engine)`` — is ``autoflush=True``. There the
    read inside the savepoint flushes the caller's row INTO the savepoint, the
    failure rolls it away, and the Session has already emptied ``db.new``: the
    caller's ``commit()`` then succeeds with the row GONE. That is silent data
    loss, a worse defect than the loud 25P02 this helper exists to prevent.
    """
    _Base.metadata.create_all(pg_test_engine)
    autoflush_session = sessionmaker(bind=pg_test_engine, autoflush=True)

    db = autoflush_session()
    db.add(_Row(id=1, v="must survive an autoflush session"))
    try:
        with contained_read(db):
            # An ORM read, not `text()`: only the ORM path autoflushes, so this
            # is the one shape that can reach the branch under test.
            db.execute(select(_MissingTable))
    except Exception:
        pass

    assert len(db.new) == 1, "the read flushed the caller's row into the savepoint"
    db.commit()
    db.close()

    with pg_test_engine.connect() as other:
        assert other.execute(text("SELECT count(*) FROM gdxa86_row")).scalar() == 1, (
            "commit reported success and the row is gone — silent data loss"
        )


def _caller_survives(pg_test_engine, table: str | None, call):
    """Rename ``table`` out from under a helper, run it on a session that has
    pending work, and assert BOTH halves: the helper still degrades to its
    default, and the caller can still commit.

    Injecting a real UndefinedTable rather than mocking: a mock proves which
    arguments were passed, never that the transaction survived. Renaming is what
    a not-yet-migrated or drifted schema looks like to that one SELECT.

    ``table=None`` means the read already fails here on its own, because the
    table is absent from this fixture. Be honest about why: ``structure.sql`` is
    a ``TenantBase``-scoped dump, so an ORM table mapped on a *different* Base
    is missing from it even though migration 001 creates it on a real box. The
    injected failure is therefore a fixture artifact — which still exercises the
    containment exactly as a genuine UndefinedTable would, but is not itself the
    production failure mode. For the qb gate that mode is a missing *column*
    (42703) on a pre-049 schema, not a missing table.
    """
    Session = _pg_sessions(pg_test_engine)
    if table is not None:
        with pg_test_engine.begin() as c:
            c.execute(text(f"ALTER TABLE {table} RENAME TO {table}_gone"))

    db = Session()
    db.add(_Row(id=1, v="the caller's pending work"))
    result = call(db)
    db.commit()
    db.close()

    with pg_test_engine.connect() as other:
        assert other.execute(text("SELECT count(*) FROM gdxa86_row")).scalar() == 1, (
            f"a failed read of {table} cost the caller its row"
        )
    return result


def _emit(db):
    from gdx_dispatch.core.webhooks.emit import emit_domain_event

    return emit_domain_event(
        db, "invoice.paid", "e1a0c3f6-0b1a-4c3d-8e5f-0a1b2c3d4e5f", {},
        tenant_id="11111111-1111-4111-8111-111111111111",
    )


def test_pg_emit_subscription_read_cannot_break_the_business_write(pg_test_engine):
    """Named for what it proves, which is narrower than
    ``emit_domain_event``'s own promise ("Webhook fan-out must NEVER break that
    write"). That promise is still not fully kept: the per-row delivery staging
    further down ``_emit`` is a ``db.begin_nested()``, so it snapshot-flushes the
    caller's pending work before its savepoint exists, and a failure in THAT
    flush is uncontained. It is a write, so it needs the ORM-level savepoint and
    is its own change; see the note at the call site. (``any_event_consent`` in
    ``core/plugin_consent.py`` was the same hazard on a read — handed to
    plugins-host as GDXA-137, so do not read this as its current state.)

    What this does prove: delete the ``contained_read`` around the subscription
    SELECT and this reddens.
    """
    staged = _caller_survives(pg_test_engine, "webhook_subscriptions", _emit)
    # Documents the degradation; it is NOT the load-bearing assertion and cannot
    # fail, since emit_domain_event returns 0 on any failure. The row count
    # inside _caller_survives is what bites.
    assert staged == 0


def test_pg_emit_workflow_rule_probe_cannot_break_the_business_write(pg_test_engine):
    """The SECOND containment in ``_emit`` — the active-rule existence probe.

    It needs its own test: with ``webhook_subscriptions`` renamed the function
    returns before ever reaching this read, so the test above cannot cover it.
    Here the subscription table is intact and ``workflow_rules`` is the one
    missing, which is the ordering that actually exercises line 2 of 2.
    """
    staged = _caller_survives(pg_test_engine, "workflow_rules", _emit)
    assert staged == 0  # see the note above: the row count is what bites


def test_pg_qb_money_pull_gate_cannot_poison_the_sync(pg_test_engine):
    """The QB money-pull gate reads `tenant_settings` on the caller's session
    mid-pull, and `modules/quickbooks/sync.py` raises on a True verdict — so
    whatever it answers, it must hand back a session that can still commit.

    The verdict here is False, not the fail-closed True: the helper classifies a
    missing table or column as a pre-049 schema, which "cannot STORE the flag,
    so cannot have it ON". That branch is its own documented contract and the
    one this fixture reaches. The transaction assertion is the point.
    """
    from gdx_dispatch.core.settings_flags import qb_money_pull_paused

    paused = _caller_survives(
        pg_test_engine,
        None,  # absent from the TenantBase-scoped fixture dump; see _caller_survives
        lambda db: qb_money_pull_paused("11111111-1111-4111-8111-111111111111", db),
    )
    assert paused is False, "a missing tenant_settings reads as 'not paused'"


def test_pg_automation_settings_read_cannot_cost_the_workflow_run(pg_test_engine):
    """`execute_rule` writes a WorkflowRun on this session after the action
    returns; that row is the only trace the rule ever fired.

    Read 1 of 6 in that frame. It calls the helper DIRECTLY, so it is green while
    the reads BELOW it are still uncontained — which is exactly what happened:
    the two tests after this one exist because the GDXA-86 audit found
    `_resolve_rule_customer` still poisoning the same commit 14 lines further
    down. Reads 3-6 are comms-email-phone's and remain uncontained; the call site
    enumerates them (`resolve_recipient`, `email_branding`,
    `get_user_tokens`, `_designated_sender_user_id`) and rule 5's test says why
    `contained_read` is the wrong tool for them. Do not take that list from here
    — an earlier draft of this docstring named `recently_sent`, which
    `send_transactional_email` never calls.

    Two honest notes. The whole frame is dead on prod today (checked live
    2026-09-27: `automation_emails_enabled` false, 0 active workflow_rules, 0
    workflow_runs), so this is insurance, not the repair of an observed loss.
    And the RENAME is not what makes THIS read fail: `structure.sql`'s
    `app_settings` predates `automation_emails_enabled`, so the fixture's table
    is missing the column the ORM selects and a 42703 comes first either way.
    The containment assertion inside `_caller_survives` still bites — it reddens
    under the no-op falsifier — but do not cite the rename as the mechanism.
    """
    from gdx_dispatch.modules.workflows.engine import _automation_email_settings

    enabled, sender = _caller_survives(
        pg_test_engine, "app_settings", _automation_email_settings
    )
    assert (enabled, sender) == (False, None), "a failed read must default to OFF"


def _as_execute_rule_does(context):
    """`_resolve_rule_customer` re-raises a DB failure (its own handlers catch
    only ValueError/TypeError). The frame that swallows it is
    `execute_rule`'s `except Exception` around the send_email action, two frames
    up — and then `execute_rule` writes the WorkflowRun on this same session.
    Reproduce that swallow, not a friendlier one."""
    from gdx_dispatch.modules.workflows.engine import _resolve_rule_customer

    def _call(db):
        try:
            return _resolve_rule_customer(db, context)
        except Exception:
            return "send_failed"        # engine.py's own degraded result string
    return _call


def test_pg_workflow_customer_lookup_cannot_cost_the_workflow_run(pg_test_engine):
    """Read 2 of 5, on the `context["customer_id"]` path — the Customer SELECT.

    Delete the `contained_read` around it and this reddens: the caller's commit
    raises InFailedSqlTransaction and the row count is 0. The GDXA-86 audit ran
    exactly this shape against a renamed `customers` before the fix existed and
    confirmed the MECHANISM — a live instance of the shape, not a live incident:
    prod cannot reach this line today (automation off, 0 active rules; see the
    test above).
    """
    got = _caller_survives(
        pg_test_engine,
        "customers",
        _as_execute_rule_does({"customer_id": "e1a0c3f6-0b1a-4c3d-8e5f-0a1b2c3d4e5f"}),
    )
    assert got == "send_failed", "the failure should still reach execute_rule's handler"


def test_pg_workflow_entity_lookup_cannot_cost_the_workflow_run(pg_test_engine):
    """Read 2 of 5 again, on the OTHER branch — the `db.get(Invoice, ...)` entity
    lookup taken when the event payload carries no `customer_id`.

    Its own block, so its own test: with `customers` renamed the function never
    reaches this read, and with `invoices` renamed it never reaches the Customer
    SELECT. One `contained_read` per block, one test per `contained_read`.
    """
    got = _caller_survives(
        pg_test_engine,
        "invoices",
        _as_execute_rule_does({
            "entity_type": "invoice",
            "entity_id": "e1a0c3f6-0b1a-4c3d-8e5f-0a1b2c3d4e5f",
        }),
    )
    assert got == "send_failed", "the failure should still reach execute_rule's handler"


def test_pg_enabled_module_keys_cannot_poison_the_request(pg_test_engine):
    """The plugin proxy's module gate. Fail-closed `set()` was only half the
    promise — the session belongs to the request, so the swallowed failure took
    the caller's transaction with it."""
    from gdx_dispatch.core.modules import enabled_module_keys

    keys = _caller_survives(
        pg_test_engine,
        "company_module_grants",
        lambda db: enabled_module_keys(db, "11111111-1111-4111-8111-111111111111"),
    )
    assert keys == set(), "a failed read must still fail closed"


def test_pg_resolve_author_name_no_longer_costs_the_note(pg_test_engine):
    """End-to-end through a real call site, with a real failure injected.

    ``core/user_display.py``'s docstring promises "a note must not fail to save
    because we couldn't pretty up a name". Not raising was only half of that:
    the write callers resolve the name INTO a row they are about to write
    (``routers/notes.py`` builds the JobNote with it, then ``db.add()`` +
    ``db.commit()``), so on Postgres the swallowed read took the note with it.
    ``mobile_chat._last_read_receipts`` is the one read-only caller and is the
    exception that loses nothing.

    The failure is real, not mocked: ``users`` is renamed out from under the
    resolver in this throwaway database, which is what a not-yet-migrated or
    drifted schema looks like to that SELECT.
    """
    from gdx_dispatch.core.user_display import resolve_author_name

    Session = _pg_sessions(pg_test_engine)
    with pg_test_engine.begin() as c:
        c.execute(text("ALTER TABLE users RENAME TO users_gone"))

    db = Session()
    db.add(_Row(id=1, v="stands in for the JobNote"))

    name = resolve_author_name(db, {"sub": "e1a0c3f6-0b1a-4c3d-8e5f-0a1b2c3d4e5f"})
    assert name is None, "the read should still degrade to no name"

    db.commit()                            # the note survives the failed lookup
    db.close()
    with pg_test_engine.connect() as other:
        assert other.execute(text("SELECT count(*) FROM gdxa86_row")).scalar() == 1


# ── GDXA-152: the audit-viewer call sites ────────────────────────────────────
#
# The same class in the two files GDXA-86 did not reach. Both are read-only
# "decoration" paths, which is exactly why they were left: the swallow looks
# harmless when the function's own contract is "degrade, never raise". It is
# not, because the session is the request's and the request is not over.


def test_pg_get_audit_events_cannot_poison_the_request(pg_test_engine):
    """The audit viewer's list query.

    `get_audit_events` already degrades to an empty page with an `error` key,
    which reads like graceful degradation and is what hid this: on Postgres the
    caller kept a dead transaction, so the *next* thing the request touched
    raised 25P02 and the operator got a 500 naming an unrelated table instead of
    the error in the payload they were handed.

    A renamed COLUMN, not a renamed table: this module calls
    `create_all(checkfirst=True)` before the read, which re-creates a dropped
    table — the read would then succeed and the test would pass vacuously —
    but never adds a missing column back.
    """
    from gdx_dispatch.core.audit_dashboard import get_audit_events

    Session = _pg_sessions(pg_test_engine)
    with pg_test_engine.begin() as c:
        c.execute(text("ALTER TABLE audit_logs RENAME COLUMN event_type TO event_type_gone"))

    db = Session()
    db.add(_Row(id=1, v="the caller's pending work"))
    page = get_audit_events(db, tenant_id="11111111-1111-4111-8111-111111111111")
    assert page["events"] == [] and "error" in page, "must still degrade, not raise"
    db.commit()
    db.close()

    with pg_test_engine.connect() as other:
        assert other.execute(text("SELECT count(*) FROM gdxa86_row")).scalar() == 1, (
            "a failed audit-log read cost the caller its row"
        )


def test_pg_resolve_actors_cannot_poison_the_request(pg_test_engine):
    """Audit actor labels.

    The injected failure is a fixture artifact, not a production state:
    `customer_users` is created by `create_orm_tables()` on every boot regardless
    of the customer_portal module grant, and prod and demo both have it (checked
    live 2026-09-27). It exercises the containment exactly as a genuine
    UndefinedTable would — same caveat `_caller_survives` already documents for
    its other callers.
    """
    from gdx_dispatch.core.audit_labels import resolve_actors

    out = _caller_survives(
        pg_test_engine,
        "customer_users",
        lambda db: resolve_actors(db, {"e1a0c3f6-0b1a-4c3d-8e5f-0a1b2c3d4e5f"}),
    )
    assert isinstance(out, dict), "must still degrade to a dict of what it could resolve"


def test_pg_resolve_entity_labels_cannot_poison_the_request(pg_test_engine):
    """The nine entity-label resolvers, contained at their dispatch frame.

    This is the one that makes the pair above worth anything.
    `decorate_rows` calls `resolve_actors` and then `resolve_entity_labels` one
    line apart, and BOTH read `customer_users`, so whatever makes one fail makes
    the other fail. Containing only the first left the caller's transaction just
    as dead on the second — the fix defeated by the function next to it. Verified
    that way round: with only `resolve_actors` wrapped, this test failed with
    InFailedSqlTransaction.
    """
    from gdx_dispatch.core.audit_labels import resolve_entity_labels

    out = _caller_survives(
        pg_test_engine,
        "customer_users",
        lambda db: resolve_entity_labels(
            db, {("customer_user", "e1a0c3f6-0b1a-4c3d-8e5f-0a1b2c3d4e5f")}
        ),
    )
    assert out == {}, "an unresolvable type must simply be absent from the result"


def test_pg_a_resolver_that_swallows_its_own_read_defeats_the_dispatch_wrap(pg_test_engine):
    """Pins the precondition on `resolve_entity_labels`'s dispatch-frame wrap.

    Not a defect in the nine resolvers — none of them catches. It pins the trap
    the wrap creates for the NEXT one, because this file's two actor resolvers
    are written in exactly the style that breaks it. A resolver that swallows its
    own failure exits the `with` clean on an aborted transaction, __exit__ issues
    RELEASE SAVEPOINT (illegal there), and the dispatch loop's own handler eats
    the 25P02 — so the caller dies with nothing in the log naming the cause.

    Asserting the BROKEN behaviour on purpose: if a future change makes this pass
    (per-resolver wrapping, or a rollback-always variant of contained_read), this
    test should be deleted along with the precondition comment it guards.
    """
    import gdx_dispatch.core.audit_labels as al

    Session = _pg_sessions(pg_test_engine)

    def _self_swallowing(db, etype, ids, out):
        # `suppress(Exception)` rather than try/except/pass only to satisfy
        # SIM105; it is the same swallow, and the same house style as
        # _resolve_staff / _resolve_customer_users.
        with suppress(Exception):
            db.execute(text("SELECT 1 FROM table_that_does_not_exist")).all()

    db = Session()
    db.add(_Row(id=1, v="the caller's pending work"))
    original = dict(al._RESOLVERS)
    al._RESOLVERS["gdxa152_probe"] = _self_swallowing
    try:
        al.resolve_entity_labels(db, {("gdxa152_probe", "x")})
        with pytest.raises(Exception) as caught:
            db.execute(text("SELECT 1")).scalar()
        assert "InFailedSqlTransaction" in str(type(caught.value)) or "aborted" in str(caught.value), (
            "expected the documented rule-5 defeat; if containment now holds, "
            "delete this test and the precondition comment it pins"
        )
    finally:
        al._RESOLVERS.clear()
        al._RESOLVERS.update(original)
        with suppress(Exception):
            db.rollback()
        db.close()


def test_pg_compliance_summary_reports_a_true_failed_login_count_under_drift(pg_test_engine):
    """The one containment in GDXA-152 with an observable consequence.

    `compliance_summary` swallows its 500-row integrity probe and then reads
    AGAIN for `failed_login_24h`. Uncontained, a drifted column aborts the
    transaction at the probe, so the later count degrades to 0 and the SOC2
    dashboard reports zero failed logins in 24h — silently wrong rather than
    visibly broken, which is the worst class in this repo. Worse, the probe's
    query is a superset of `_walk_chain`'s, so the abort happens BEFORE
    `_walk_chain`'s own savepoint is reachable: without the probe's savepoint,
    `SAVEPOINT` itself raises 25P02 on the already-aborted transaction.

    Remove `contained_read` from the probe in `compliance_summary` and this
    fails on `failed_login_24h == 0`.
    """
    import json as _json

    from gdx_dispatch.core.audit_dashboard import compliance_summary

    Session = _pg_sessions(pg_test_engine)

    with pg_test_engine.begin() as c:
        c.execute(
            text(
                # Every NOT NULL column without a default, filled explicitly.
                # row_hash/prev_hash are junk on purpose: this test is about the
                # COUNT surviving, not the chain — and the chain walk reports a
                # break on real rows anyway (see _walk_chain's docstring).
                "INSERT INTO audit_logs "
                "(id, action, event_type, entity_type, entity_id, "
                " row_hash, prev_hash, created_at) "
                "VALUES (gen_random_uuid(), 'login_failed', 'login_failed', "
                "'user', 'u1', 'x', '', now())"
            )
        )
        # A renamed COLUMN, not a table: this module calls
        # create_all(checkfirst=True) first, which would re-create a dropped
        # table and make the probe succeed.
        c.execute(text("ALTER TABLE audit_logs RENAME COLUMN ip_address TO ip_address_gone"))

    db = Session()
    db.add(_Row(id=1, v="the caller's pending work"))
    payload = _json.loads(bytes(compliance_summary(db=db).body))
    assert payload["failed_login_24h"] == 1, (
        "the probe's failure poisoned the transaction and the count silently "
        "degraded to 0 — a SOC2 dashboard reporting no failed logins"
    )
    # NOT asserted: `audit_log_integrity is False`. It reads False here, but it
    # reads False with nothing broken at all — `_walk_chain`'s hash formula does
    # not match the writer's, so row 1 always mismatches (see its docstring).
    # Asserting it would look like a second net and could not fail for the
    # reason it named. `failed_login_24h` above is the only real net in here.
    db.commit()
    db.close()

    with pg_test_engine.connect() as other:
        assert other.execute(text("SELECT count(*) FROM gdxa86_row")).scalar() == 1

def test_no_dispatched_entity_resolver_swallows_its_own_read():
    """`resolve_entity_labels` wraps the DISPATCH frame, so pin its precondition.

    One `contained_read` in `resolve_entity_labels` contains all nine entity
    resolvers, which is only sound while every one of them lets a read failure
    PROPAGATE. A resolver that catches its own failure exits the `with` block
    cleanly on an already-aborted Postgres transaction; `__exit__` then issues
    `RELEASE SAVEPOINT`, which is illegal there, and the 25P02 it raises is
    eaten by the dispatch frame's own `except Exception` — which logs
    `resolve_failed type=<X>`, naming the wrong cause, and leaves the caller
    dead with nothing in the log explaining why. That is strictly worse than
    not wrapping at all.

    The trap is that this file's HOUSE STYLE is the thing that breaks it: the
    two ACTOR resolvers (`_resolve_staff`, `_resolve_customer_users`) each
    swallow, which is correct for them because they wrap their own reads —
    and they are the obvious copy-paste source for a tenth entity resolver.
    A GDXA-152 audit demonstrated the gap by adding a swallowing clone of
    `_resolve_vendor` to `_RESOLVERS`: 83 tests across the label and activity
    suites stayed green. Prose could not hold this; it is a machine check now.

    AST, not grep, and it reads `_RESOLVERS` rather than a hardcoded list, so a
    resolver added to the dict is covered the moment it is registered. No
    marker, no Postgres: this is the SQLite arm's only guard on the dispatch
    wrap, and the PG tests beside it skip by default.
    """
    import gdx_dispatch

    src = (
        Path(gdx_dispatch.__file__).resolve().parent / "core" / "audit_labels.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(src)

    # `_resolve_line` is dispatched too — `resolve_entity_labels` falls back to
    # it for any `*_line` type, and it re-enters `_RESOLVERS` for the parent.
    dispatched: set[str] = {"_resolve_line"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Dict):
            continue
        if not any(getattr(t, "id", None) == "_RESOLVERS" for t in node.targets):
            continue
        for value in node.value.values:
            name = getattr(value, "id", None)
            assert name is not None, (
                "a _RESOLVERS entry is no longer a plain function name "
                f"({ast.dump(value)}) — this guard can no longer see what is "
                "dispatched, so teach it the new shape rather than deleting it."
            )
            dispatched.add(name)

    assert len(dispatched) > 1, (
        "could not find the _RESOLVERS dict in core/audit_labels.py — the guard "
        "resolved nothing, which would make it pass vacuously forever."
    )

    offenders = [
        f"{node.name}() catches at line {sub.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name in dispatched
        for sub in ast.walk(node)
        if isinstance(sub, ast.ExceptHandler)
    ]
    assert not offenders, (
        "These resolvers are dispatched through `resolve_entity_labels`'s "
        "`contained_read` frame but swallow their own read failure:\n    "
        + "\n    ".join(offenders)
        + "\n\nOn Postgres that exits the savepoint clean on an aborted "
        "transaction and RELEASE SAVEPOINT raises 25P02 into the dispatch "
        "handler, which logs the wrong cause. Either let the failure propagate "
        "(the house style for the nine entity resolvers), or give this resolver "
        "its own `with contained_read(db):` around its read the way "
        "`_resolve_staff` and `_resolve_customer_users` do."
    )


# ── the docstring's call-site count is a number, so pin it ──────────────────

_SKIP_DIRS = {"tests", "node_modules", ".git", "frontend", ".venv"}

_ONES = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
    "sixteen", "seventeen", "eighteen", "nineteen",
)
_TENS = (
    "", "", "twenty", "thirty", "forty", "fifty",
    "sixty", "seventy", "eighty", "ninety",
)


def _number_word(n: int) -> str | None:
    """Spell ``n`` the way the docstring sentence spells it, or None past 99.

    This was a hand-written dict that stopped at twelve (GDXA-151). Extending it
    by hand each time a call site lands is the same shape as the count it
    guards: a number a human maintains, that goes stale, and whose staleness
    surfaces as a confusing RED on someone else's unrelated change. GDXA-152
    took the count from nine to nineteen in one commit and ran straight off the
    end of it; the nine sibling delegations of GDXA-46 are each expected to add
    sites to the same sentence, so it would have run off again.

    Generating the word removes that maintenance entirely below 100. It stays
    `None` above that, deliberately — a repo with 100+ contained reads has
    outgrown a prose count, and the assertion's message is the right place to
    find that out rather than silently spelling ever-longer numbers.
    """
    if 0 <= n < 20:
        return _ONES[n]
    if 20 <= n < 100:
        tens, ones = divmod(n, 10)
        return _TENS[tens] if not ones else f"{_TENS[tens]}-{_ONES[ones]}"
    return None


def test_the_docstring_call_site_count_is_not_stale():
    """``contained_read``'s docstring states its own call-site count. Pin it.

    That number is load-bearing: it is the stated reason the helper only warns
    about a staged-but-unflushed write instead of policing DML on the
    connection ("it would police a precondition nothing violates"). Let the
    count drift and the justification is for a world that no longer exists.

    It had gone stale twice before this test — six when it was seven, eight
    when GDXA-137 made it nine — each caught by an adversarial audit rather
    than by CI. The recipe written to stop that was itself wrong in the same
    way (GDXA-151): it told you to subtract one when two of its matches were
    the docstring's own prose, so it answered ten.

    Counts from the FILESYSTEM, deliberately, because the recipe it replaced
    used ``git grep`` and a call site in a brand-new module reads as zero there
    until someone runs ``git add`` — measured in the GDXA-151 audit.

    Counts by PARSING, not by grepping, and that is the whole difference
    between this and the recipe it replaced. An ``ast`` walk sees only real
    calls, so a commented-out line, a docstring example, or any other mention
    inside a string cannot inflate the number — and it catches a site opened
    through ``ExitStack.enter_context(contained_read(db))``, which a
    ``with contained_read(`` grep misses. A text scan got both wrong in the
    GDXA-151 audit: the docstring-example case is the nastier one, because it
    made the guard demand a number that was not the call-site count, so
    following its own error message would have written "ten" into the sentence
    below — the exact defect, by the exact mechanism, as the recipe deleted here.

    Walks the whole repo, not just this package: ``tools/`` and ``scripts/``
    sit outside ``gdx_dispatch/`` and a call site there is still a call site.
    ``core/database.py`` is excluded because it defines the helper and calls it
    nowhere — asserted below, so a real site landing there cannot go uncounted.
    Tests are excluded because they wrap writes on purpose, which is also why
    the docstring's number is about PRODUCTION call sites.

    TWO LIMITS, so nobody mistakes this for more than it is:

    1. It pins the COUNT, not the claim that those sites "wrap pure reads".
       Put a write inside an existing call site and this stays green while the
       docstring goes wrong.
    2. It does not enforce that the number is written down only once. An
       earlier revision tried; it could only match one phrasing, in one
       directory, and missed both historical drifts ("none of the eight call
       sites", "six") and the whole of ``tests/`` — where the copy this commit
       deletes actually lived. A guard that checks the easy half of a rule
       reads as if it checked all of it, so it was removed rather than left
       to reassure. Keeping the number in one place is a review habit here,
       not a machine-checked invariant.
    """
    import gdx_dispatch

    def _count(src: str) -> int:
        """Real calls only — ``ast``, so strings and comments cannot inflate."""
        return sum(
            1
            for node in ast.walk(ast.parse(src))
            if isinstance(node, ast.Call)
            and getattr(node.func, "id", getattr(node.func, "attr", None))
            == "contained_read"
        )

    root = Path(gdx_dispatch.__file__).resolve().parent.parent
    database_py = Path("gdx_dispatch/core/database.py")
    per_file: dict[str, int] = {}
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if _SKIP_DIRS & set(rel.parts):
            continue
        hits = _count(path.read_text(encoding="utf-8"))
        if rel == database_py:
            assert hits == 0, (
                "core/database.py gained a contained_read call site. It is "
                "excluded from the count, so that site would go uncounted — "
                "either move it or stop excluding this file."
            )
            continue
        if hits:
            per_file[rel.as_posix()] = hits

    total = sum(per_file.values())
    word = _number_word(total)
    assert word is not None, (
        f"{total} contained_read call sites {per_file} — past ninety-nine, which "
        "is where a prose count stops being the right instrument. Replace the "
        "sentence in core/database.py with something that is not a number, "
        "rather than teaching _number_word to spell hundreds."
    )

    # Whitespace-collapsed so re-WRAPPING the sentence is free. Re-WORDING it is
    # not, and that is deliberate: this exact phrase is the anchor.
    doc = " ".join((inspect.getdoc(contained_read) or "").split())
    expected = f"{word} current call sites"
    assert expected in doc, (
        f"contained_read has {total} call sites {per_file}, but its docstring "
        f"does not say {expected!r}. Fix the sentence in core/database.py — and "
        "nowhere else. A second copy of this number is what went stale twice."
    )

from __future__ import annotations

import logging
import os
from collections.abc import Generator, Iterator
from contextlib import contextmanager, nullcontext

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

log = logging.getLogger(__name__)

# ─── Single-tenant Database Setup ──────

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./app.db")
engine = create_engine(DATABASE_URL, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

# ─── Older names, still imported ──────────────────────────────────────────
# `app_engine`, `tenant_context` and `get_tenant_db` are the one engine, a
# no-op context and `get_db` under the names call sites still use.
# `auth.core._db_verify_user` reads `app_engine` on EVERY authenticated
# request, so its absence 401s the entire API.
app_engine = engine


def get_db(request=None) -> Generator[Session, None, None]:
    """Dependency for injecting the database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def tenant_context():
    """No-op context under the name older modules import."""
    return nullcontext()

def get_tenant_db(request=None):
    """`get_db` under the name older modules import.
    Must be a generator (not return one) so FastAPI's Depends() injects a Session."""
    yield from get_db(request)


def _pending_counts(db: Session) -> tuple[int, int, int] | None:
    """``(new, dirty, deleted)`` for the caller's session, or None if unreadable.

    Read-only — touching ``new``/``dirty``/``deleted`` never flushes. The same
    three counts as ``core/audit.py``'s ``_staged_work``, counted here rather
    than imported from it — and be accurate about why, because two earlier
    answers in this docstring were wrong and an audit had to knock each down.
    It is NOT a circular import: ``core/database.py`` imports nothing from
    ``core/audit.py`` at any scope, and a module-scope
    ``from gdx_dispatch.core.audit import _staged_work`` here imports fine
    (measured). The real cost is the import GRAPH: ``audit.py`` pulls in
    ``fastapi`` at module scope, and this module is imported by almost
    everything, so importing it here to save four lines puts a web framework
    behind every ``from gdx_dispatch.core.database import ...``.

    ``dirty`` costs more than the other two — it walks the identity map testing
    each state, where ``new``/``deleted`` are dict views — and it is counted
    anyway. Measured at 500 loaded objects (SQLAlchemy 2.0.54, docker-app):
    2.93 µs for all three against 1.18 µs for two. Relatively 2.5x, absolutely
    ~2 µs, which is a rounding error against the SELECT it guards. What it buys
    is a caller that MUTATES an already-loaded row inside the block: otherwise
    invisible to the warning, and flushed at the caller's own commit, because
    the savepoint below never knew about it. Dropping this once, for that cost,
    was a bad trade and was reverted.

    Returns None rather than raising, for a specific reason: this runs INSIDE
    ``contained_read``'s block, so anything it raised would be swallowed by the
    *call site's* ``except`` and become the helper's degraded default.
    """
    try:
        return (len(db.new), len(db.dirty), len(db.deleted))
    # Unreachable as things stand — ``contained_read`` calls ``db.connection()``
    # before this, so a non-Session dies there first. Kept because this must not
    # be the thing that raises inside the block, not because it has a caller.
    except Exception:
        return None


@contextmanager
def contained_read(db: Session) -> Iterator[None]:
    """SAVEPOINT around a READ whose failure must not poison the caller's txn.

    For the helper shape that takes a session it does not own, reads inside a
    ``try``, and returns a default from the ``except`` — "a note must not fail
    to save because we couldn't pretty up a name". On Postgres that swallow is
    a lie: a failed statement aborts the whole transaction, so the caller's
    next ``commit()`` dies with ``InFailedSqlTransaction`` (25P02) and the work
    the helper was protecting is LOST. Measured on PG 15.17, SQLAlchemy 2.0.54
    (GDXA-86) — a swallowed read of a missing table made the caller's already
    ``add()``ed row non-durable. SQLite does not poison, so the entire class is
    invisible to a SQLite-only test run.

    Re-raises. It contains the *transaction* damage and nothing else; the
    caller keeps its own ``except``, its own log line and its own default::

        try:
            with contained_read(db):
                row = db.execute(select(User).where(User.id == key)).scalar_one_or_none()
        except Exception:
            log.exception("resolve_author_name_failed")
            return None

    Five rules, each of which something here got wrong first:

    1. **The savepoint goes INSIDE the existing ``try``**, not around it. Around
       it and the ``except`` never runs, which turns a degraded read into a 500.

    2. **READS ONLY.** A write wants ``db.begin_nested()`` instead, so the ORM's
       unit of work participates in the savepoint and knows what to un-stage
       (``core/webhooks/emit.py``'s per-row delivery staging is the reference).
       This helper deliberately opens the SAVEPOINT on the *Connection*, below
       the ORM, which is precisely why it must not wrap a write. A write inside
       the block goes wrong in one of two directions, and only the first is
       detected:

       - **staged, not flushed** — the Session does not know the savepoint
         exists, so the pending object survives the rollback and lands at the
         caller's commit: a write that looks contained and is not. This logs
         ``contained_read_staged_a_write``, and so does an ``add()``,
         ``delete()`` or a mutation of an already-loaded row.
       - **staged AND flushed** — the INSERT is inside the savepoint and is
         rolled away, while ``new``/``dirty``/``deleted`` are empty at both
         snapshots, so **nothing warns**. The caller's ``commit()`` then reports
         success with the row gone. Measured (GDXA-86 audit): ``add()`` +
         ``flush()`` + a failing read inside one block → no log line, 0 rows.

       So the guard narrows the misuse it can see; it does not make misuse safe,
       and a *silent* loss is the half it cannot see. Catching that one needs a
       ``before_cursor_execute`` listener watching for DML on the connection.
       Not built here — but be honest about the reason, because "that would be
       new machinery" is not it: ``core/performance.py:169`` already registers an
       Engine-level ``before_cursor_execute`` (``SlowQueryMiddleware``, wired at
       ``app.py``), so the hook is already in every query's path and a
       DML-in-savepoint check could ride it. The reason is that all twenty-one
       current call sites wrap pure reads, so it would police a precondition nothing
       violates, and ``tests/test_contained_read.py`` pins the hole so the next
       person does not mistake it for coverage. When a call site does need a
       write contained, it wants ``db.begin_nested()`` — not a louder warning.

       That count is load-bearing — it IS the reason above — and it has gone
       stale twice already, each time caught by an adversarial audit rather than
       by a test: six when it was seven, eight when GDXA-137 made it nine. The
       ``git grep`` recipe that used to sit here was a third instance of the
       same class rather than a cure for it, and it is worth knowing how it
       failed, because all three failures are one shape — a number a human has
       to maintain by hand:

       - It said to subtract one, for "the example at the top of this
         docstring". By the time it shipped this file held TWO matches that are
         not call sites: that example, and the recipe's own line, which
         contained the very string it searched for. Followed in good faith it
         answered ten.
       - ``git grep`` reads TRACKED files, so a call site in a brand-new module
         counted as zero until someone ran ``git add``.

       So the number is pinned by a test now, not by an instruction:
       ``test_the_docstring_call_site_count_is_not_stale`` in
       ``tests/test_contained_read.py``. It parses every Python file in the
       repo, counts the real calls, and fails with the true number and a
       per-file breakdown. It carries no marker, so the default suite runs it
       and there is nothing to remember.

       **This sentence should be the only place the count is written down.**
       Both stalings above were a second copy drifting from a first, and until
       GDXA-151 the test file carried one of its own. That part is a review
       habit, not a machine-checked one — the test pins this number, it does
       not hunt for rival copies; see its own LIMIT 2. (``core/plugin_consent``
       also says "nine" and it is NOT this number — it counts
       ``emit_domain_event`` call sites, which happen to be nine too. A
       GDXA-151 audit mistook one for the other; do not "sync" them.)

       What the guard does NOT pin is the other half of the claim above, that
       those sites wrap PURE READS. Only the count is mechanical; "pure" is the
       precondition the unbuilt ``before_cursor_execute`` check would police,
       so putting a write inside an existing call site keeps this docstring
       green and makes it wrong. Reviewing that is still a human's job.

       A warning and not a raise, either way — by the time it could fire the
       read has already happened, and turning a degraded read into a 500 is
       rule 1's mistake.

    3. **Connection-level, because ``db.begin_nested()`` FLUSHES.**
       ``SessionTransaction._take_snapshot`` flushes on every
       ``begin_nested()`` — measured, not inferred: with ``autoflush=False``
       (what ``SessionLocal`` sets) a plain read leaves pending work pending
       and ``begin_nested()`` writes it out.

       Be precise about what that costs, because the loose version of this
       claim ("strictly worse than the bug") is wrong and was corrected here:
       ``db.begin_nested()`` *does* fix the reported defect — measured, pending
       row persists, caller commits. Two narrower things are true, and they are
       the reason to prefer the connection. First, the early flush is a
       behaviour change: it writes the caller's half-built row at the moment of
       a read that used to write nothing. Second, that flush happens *before*
       the savepoint exists, so if the flush itself fails it is contained by
       nothing, deactivates the session, and is swallowed by the handler below.
       The connection-level SAVEPOINT introduces no flush at all, so wrapping a
       read is behaviour-neutral on the success path: pending work stays
       pending and stays non-durable until the caller commits (verified on
       SQLite AND PG 15).

    4. **No ``db.rollback()`` in the handler.** A full rollback expires every
       object the caller is holding — the same trap ``core/audit.py``'s
       ``audit_best_effort`` documents at point (3). Containing the failure
       leaves the caller holding exactly what it held before.

    5. **The swallow must be OUTSIDE *this* block.** The rollback only happens
       when the exception reaches this context manager's ``__exit__``. Wrap a
       callee that catches its own failure and returns a default, and the block
       exits *clean*: the CM issues RELEASE SAVEPOINT on an already-aborted
       Postgres transaction, which raises 25P02 out of the ``with`` itself while
       the caller's ``commit()`` stays just as dead. Measured (GDXA-86, PG
       15.17): helper returned its default, ``__exit__`` raised
       ``InFailedSqlTransaction``, the caller's ``commit()`` raised it too, 0
       rows persisted — strictly worse than not wrapping, because the raise
       lands somewhere the caller has no handler for. So put this inside the
       function that swallows, not around a call to one.

       **That is a property of THIS helper, not a law**, and the first draft of
       this rule said otherwise — that a swallowing callee "cannot be contained
       from the call site". Wrong, and knocked down by measurement: only
       ``RELEASE`` is illegal on an aborted transaction. ``ROLLBACK TO
       SAVEPOINT`` is legal — that is its whole purpose — and for a pure read it
       is a semantic no-op. A variant that ALWAYS rolls back to the savepoint
       instead of releasing on a clean exit therefore does contain a
       self-swallowing callee from the caller's own file: measured on PG 15.17
       **and 16.14** (GDXA-86 audit round 2), 1 row persisted where this helper
       loses it. Not built here — there is no call site that needs it (see
       ``modules/workflows/engine.py``'s ``_run_send_email_action``, where the
       two candidates sit on a path prod has switched off), and a second
       context manager with a different exit rule is a real API cost. But do
       not cite rule 5 as a reason something is impossible; it is a reason
       ``contained_read`` specifically is the wrong tool.

       The corollary is that a frame between the read and the swallow is fine,
       and two call sites rely on it: ``core/webhooks/emit.py``'s reads are
       swallowed by ``emit_domain_event`` one frame up, and
       ``modules/workflows/engine.py``'s ``_resolve_rule_customer`` re-raises
       into ``execute_rule``'s handler two frames up. What matters is that the
       exception crosses this ``__exit__``, not who eventually catches it. A
       callee whose own read is already ``contained_read``-wrapped is likewise
       safe to wrap again — the inner block re-raises, so the outer one sees it
       (measured, same probe).

       Pinned by
       ``test_pg_a_callee_that_swallows_its_own_failure_is_not_contained``,
       which asserts THIS helper's behaviour, not the general claim.

    ``no_autoflush`` is not decoration — it is rule 3's other half, and the
    precondition here that is *enforced* rather than merely warned about (rule 2
    is warned in one of its two directions; rules 1, 4 and 5 are the call site's to
    keep). Every session
    that reaches a call site today comes from ``SessionLocal``
    (``autoflush=False``), though not every session in the tree does:
    ``tools/bank_statement_acceptance.py`` builds a plain ``sessionmaker(bind=…)``,
    which is ``autoflush=True``. And that is SQLAlchemy's default, so it is what
    the next caller will hand this. There the read inside the savepoint would
    flush the caller's pending row
    INTO the savepoint, the failure rolls it away, and the Session has already
    emptied ``db.new``: the caller's ``commit()`` then succeeds **with the row
    gone**. That converts a loud 25P02 into silent data loss, which is a worse
    defect than the one this fixes. Measured both ways on PG 15.17: without
    ``no_autoflush``, 0 rows persisted; with it, 1.

    How often does this actually fire on prod? Narrowly, and say so rather than
    implying a known incident — nobody has produced one. Checked live
    2026-09-27: prod is PG **16.13** (measurements above are 15.17 and 16.14)
    with ``statement_timeout=0``, ``lock_timeout=0``,
    ``idle_in_transaction_session_timeout=0`` and READ COMMITTED. So the one
    transient class proven contained below (57014) cannot fire as configured;
    READ COMMITTED rules out 40001 on a bare SELECT, and no lock timeout rules
    out 55P03. The entrypoint runs ``alembic upgrade head`` under ``set -e``
    before serving, so 42P01/42703 on a *migrated* table is a fixture artifact,
    not a prod state.

    Be harder on this than the first draft was. It offered "a raw table no model
    declares — the qb gate's ``tenant_settings`` is exactly this" as a live
    trigger, and that is wrong twice over: ``core/tenant_settings.py:148``
    declares ``class TenantSettings(Base)`` with
    ``__tablename__ = "tenant_settings"`` (on the ``Base``
    ``migrations/env.py`` autogenerates against, not ``TenantBase``, which is
    only why ``tests/fixtures/structure.sql`` lacks it), and prod has both the
    table and the column — checked live, 2 rows. So that 42703 is a fixture
    artifact too, by the rule in the paragraph above. What is left after that
    correction: the window during a migration, a table that is genuinely raw
    (``core/plugin_consent.py``'s lazily-created ``plugin_consent`` is the real
    example — no model declares it at all), a future ``statement_timeout``, and
    connection loss, which is NOT contained. Treat this as cheap insurance on a
    real mechanism, not as a fix for an observed outage; no concrete production
    instance has been produced for any call site here.

    Limits, all three real:

    - It does not rescue a read whose caller is ALREADY poisoned.
    - It does not make a *flush* failure safe (rule 3).
    - It does not contain a lost connection. A backend that dies mid-read
      (``pg_terminate_backend``, and the app engine sets no ``pool_pre_ping``)
      leaves the caller's next ``commit()`` raising ``PendingRollbackError``
      anyway — the savepoint it would roll back to died with the socket. Not a
      regression, just outside what a SAVEPOINT can do. A statement timeout
      (57014) IS contained.

    See ``tests/test_contained_read.py``, whose PG arm fails if the containment
    is removed and skips (green) with no reachable Postgres — so a SQLite-only
    run is not evidence for this class.
    """
    # SQLAlchemy's own NestedTransaction context manager: RELEASE on a clean
    # exit, ROLLBACK TO SAVEPOINT on an exception, and it never suppresses.
    # Written this way rather than as an explicit
    # ``savepoint.commit()``/``.rollback()`` pair on purpose — a literal
    # ``.commit()`` here is a savepoint RELEASE, but
    # ``tools/audit_after_commit_scan.py`` reads any ``.commit()`` as a
    # transaction commit, so the explicit form made this a "committer" and that
    # verdict propagated to every caller and then to THEIR callers: seven audit
    # writes in ``modules/deposits/service.py`` that follow an
    # ``emit_domain_event`` were reported as landing after a commit. The
    # scanner was right about the token and wrong about nothing else.
    with db.connection().begin_nested(), db.no_autoflush:
        before = _pending_counts(db)
        try:
            yield
        finally:
            after = _pending_counts(db)
            if before is not None and after is not None and after != before:
                log.warning(
                    "contained_read_staged_a_write before=%r after=%r — this helper's "
                    "SAVEPOINT is on the CONNECTION, below the ORM, so unflushed work "
                    "staged inside it survives the rollback and lands at the caller's "
                    "commit. Reads only: a write wants db.begin_nested(). See "
                    "core.database.contained_read rule 2.",
                    before, after,
                )

"""ai-mcp's swallowed reads must not poison the caller's transaction — GDXA-159.

Child of GDXA-86 (``core.database.contained_read``). Seventeen reads across six
files in the AI/MCP surface took a session they did not own and swallowed a
failed read, in their own ``except`` or in ``invoke_tool``'s one frame up,
returning a degraded value. On SQLite that is
honest; on Postgres the failed statement had already aborted the whole
transaction, so the degraded answer was a lie and whatever the caller did next
was lost.

**Every test here is Postgres-only, and that is the point.** Pre-fix and
post-fix are identical on SQLite, which does not poison a transaction on a
failed statement — so a green SQLite run is not evidence for any of this. With
no reachable Postgres these skip (green) on a laptop and FAIL under CI (#440).

Three sections, one per file, and they redden for three different reasons:

- ``core/ai_quote.py`` — **the caller's row.** The classic shape from GDXA-86:
  a helper with a ``db: Session`` parameter, and a caller holding pending work.
- ``core/mcp_tools/email_{read,move,list,draft}.py`` — **the rest of the
  agent turn.** The
  caller is ``mcp_invoke.invoke_tool``, and ``routers/ai.py``'s ask-loop runs
  every tool of one turn on one session, so a failed attachment read poisoned
  every later tool call in that turn, including the commit that would have
  made the flushed ``mcp.tool_invoke`` audit row durable (see the comment in
  ``email_read.py``; in a single-tool turn nothing commits that row at all).
- ``core/next_action.py`` — **the answer itself.** These reads sit in a frame
  that keeps reading, so containment is not insurance here, it is the feature:
  without it one failed rule takes every later rule with it and the user gets
  an empty queue. This is the only section that asserts a *behaviour* change,
  and it is the strongest of the three for exactly that reason.

The two sites the GDXA-159 census flagged in ``ai_quote.py`` are wrapped
defensively rather than to fix reachable loss — their only production caller is
a route holding nothing pending. That caveat lives in their docstrings; the
tests below assert the containment regardless, because the next caller is the
one it is for.
"""
from __future__ import annotations

import uuid
from contextlib import suppress
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import Column, Integer, String, text
from sqlalchemy.orm import declarative_base, sessionmaker

TENANT = "22222222-2222-4222-8222-222222222222"

_Base = declarative_base()


class _Row(_Base):
    """Stands in for whatever the caller was holding when the helper failed.

    In ``ai_quote``'s case a half-built estimate; in ``email_read``'s, the work
    ``invoke_tool`` and the ask-loop go on to do on the same session after the
    handler returns: a later tool's write, and with it the flushed
    ``mcp.tool_invoke`` audit row that rides that later commit.
    """

    __tablename__ = "gdxa159_row"
    id = Column(Integer, primary_key=True)
    v = Column(String)


def _sessions(engine):
    # autoflush=False mirrors core.database.SessionLocal, which is what every
    # session reaching these helpers in the app actually is. With autoflush on,
    # contained_read's own no_autoflush is what stops the caller's pending row
    # being flushed INTO the savepoint and rolled away (see its docstring).
    _Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _surviving_rows(engine) -> int:
    """Row count from a SEPARATE connection — the only honest test of durability.

    Reading it back on the caller's own session would pass on uncommitted work.
    """
    with engine.connect() as other:
        return other.execute(text("SELECT count(*) FROM gdxa159_row")).scalar()


def _insert_invoice(engine, *, status: str, total: str, sent_days_ago: int | None = None) -> None:
    """Seed one invoice with raw SQL, deliberately NOT through the ORM.

    ``db.add(Invoice(...)) + commit()`` fires the ledger's GL-posting listener,
    which reads ``gl_settings`` — another TenantBase table missing from
    ``fixtures/structure.sql``, so it raises UndefinedTable and the seeding
    fails for a reason that has nothing to do with what is under test. Raw SQL
    bypasses the ORM event entirely. The READS under test are still ORM reads,
    which is the half that matters.

    ``customer_id`` is left NULL: the column is nullable in the database (the
    ORM marks it required) and there is an FK to ``customers``, so a NULL is
    the cheapest way to avoid seeding a customer that no assertion here reads.
    """
    # ``sent_at`` is BOUND, not interpolated. An f-string here is what ruff's
    # S608 flags, and the rule is right even in a test: the statement is a
    # constant, so there is nothing to interpolate.
    sent_at = (
        datetime.now(timezone.utc) - timedelta(days=sent_days_ago)
        if sent_days_ago is not None
        else None
    )
    with engine.begin() as c:
        c.execute(text("""
            INSERT INTO invoices (
                id, invoice_number, billing_type, sequence_number,
                subtotal, tax_amount, total, balance_due, status, locked,
                public_token, company_id, created_at, sent_at
            ) VALUES (
                gen_random_uuid(), :num, 'standard', 1,
                :sub, 0, :total, 0, :status, false,
                :token, :tenant, now(), :sent_at
            )
        """), {
            "num": f"INV-{uuid.uuid4().hex[:8]}", "sub": total, "total": total,
            "status": status, "token": uuid.uuid4().hex, "tenant": TENANT,
            "sent_at": sent_at,
        })


def _break_invoices(engine) -> None:
    """Make the invoice read fail the way schema drift does: 42703.

    A dropped column rather than a dropped table because both helpers select
    ``total`` — one explicitly, one via ``select(Invoice)`` — so this is the
    narrowest change that reaches both. Never monkeypatch the session for this:
    a stubbed failure does not abort a real Postgres transaction, so the test
    would pass with or without the fix.
    """
    with engine.begin() as c:
        c.execute(text("ALTER TABLE invoices DROP COLUMN total CASCADE"))


# ---------------------------------------------------------------------------
# core/ai_quote.py — the caller's row
# ---------------------------------------------------------------------------


def test_pg_a_bare_invoice_read_loses_the_callers_row(pg_test_engine):
    """The defect, reproduced on this file's own failure, with no helper involved.

    Asserts the BROKEN behaviour on purpose. If a future Postgres or SQLAlchemy
    stops poisoning the transaction, this goes red and the premise of the two
    tests below needs re-reading rather than surviving as cargo cult.
    """
    Session = _sessions(pg_test_engine)
    _break_invoices(pg_test_engine)

    db = Session()
    db.add(_Row(id=1, v="the caller's work"))

    with suppress(Exception):  # the helper's `except Exception: log; degrade`
        db.execute(text("SELECT total FROM invoices"))

    with pytest.raises(Exception) as exc:
        db.commit()
    assert "aborted" in str(exc.value).lower(), f"expected 25P02, got {exc.value}"

    db.rollback()
    db.close()
    assert _surviving_rows(pg_test_engine) == 0, (
        "the caller's row survived — the defect did not reproduce"
    )


def test_pg_get_pricing_suggestions_keeps_the_callers_transaction_committable(pg_test_engine):
    """The fix, on the same failure the test above loses a row to.

    Remove the ``contained_read`` in ``get_pricing_suggestions`` and this fails
    at ``db.commit()`` with 25P02.
    """
    from gdx_dispatch.core.ai_quote import get_pricing_suggestions

    Session = _sessions(pg_test_engine)
    _break_invoices(pg_test_engine)

    db = Session()
    db.add(_Row(id=1, v="the caller's work"))

    result = get_pricing_suggestions("Torsion Spring", db)

    assert result["source"] != "historical", "the read was supposed to fail"
    assert len(db.new) == 1, "the helper flushed the caller's pending work"
    assert db.is_active, "the helper handed back a deactivated session"

    db.commit()
    db.close()
    assert _surviving_rows(pg_test_engine) == 1, (
        "a failed pricing read cost the caller its row"
    )


def test_pg_analyze_pricing_health_keeps_the_callers_transaction_committable(pg_test_engine):
    """Same claim for the second helper in the file. It reads whole ORM objects
    rather than one column, so it is a distinct statement and a distinct
    savepoint — not covered by the test above."""
    from gdx_dispatch.core.ai_quote import analyze_pricing_health

    Session = _sessions(pg_test_engine)
    _break_invoices(pg_test_engine)

    db = Session()
    db.add(_Row(id=1, v="the caller's work"))

    result = analyze_pricing_health(TENANT, db)

    assert result["avg_margin"] == 0.35, "expected the documented fallback margin"
    assert len(db.new) == 1, "the helper flushed the caller's pending work"
    assert db.is_active, "the helper handed back a deactivated session"

    db.commit()
    db.close()
    assert _surviving_rows(pg_test_engine) == 1, (
        "a failed health read cost the caller its row"
    )


def test_pg_pricing_helpers_still_answer_when_the_read_succeeds(pg_test_engine):
    """Containment must not cost the helpers their answer.

    Every other test here breaks the table, so all of them would still pass if
    ``contained_read`` swallowed the rows and always returned the fallback. This
    is the one that would notice — the historical path has to still work.
    """
    from gdx_dispatch.core.ai_quote import analyze_pricing_health, get_pricing_suggestions

    Session = _sessions(pg_test_engine)
    _insert_invoice(pg_test_engine, status="paid", total="200")
    db = Session()

    suggestion = get_pricing_suggestions("Torsion Spring", db)
    assert suggestion["source"] == "historical", "the savepoint ate the rows"
    assert suggestion["sample_count"] == 1

    health = analyze_pricing_health(TENANT, db)
    assert health["total_invoices_analyzed"] == 1, "the savepoint ate the rows"
    # _insert_invoice seeds subtotal == total, so at the helper's hardcoded 40%
    # cost estimate: (200 - 200*0.4) / 200 == 0.6. The point of asserting the
    # number rather than just the count is that 0.6 is NOT the 0.35 default the
    # failure path returns — so this cannot pass on a swallowed read.
    assert health["avg_margin"] == 0.6, "the savepoint ate the rows"
    db.close()


# ---------------------------------------------------------------------------
# core/mcp_tools/email_{read,move,list,draft}.py — the rest of the turn
# ---------------------------------------------------------------------------


def _outlook_tables(engine):
    """Create the outlook tables from the ORM.

    They are on ``TenantBase`` but absent from ``fixtures/structure.sql``, which
    predates the module — so the PG template has no ``outlook_*`` at all. Built
    from the ORM rather than hand-written DDL on purpose: hand-written DDL in
    this repo once hid a feature that could not be inserted at all.
    """
    from gdx_dispatch.modules.outlook.models import (
        OutlookAccount,
        OutlookAttachment,
        OutlookMessage,
        OutlookSettings,
    )

    for model in (OutlookAccount, OutlookMessage, OutlookAttachment, OutlookSettings):
        model.__table__.create(engine, checkfirst=True)
    return OutlookAccount, OutlookMessage, OutlookAttachment


def _seed_message(db, engine):
    """One agent-visible message. No ``OutlookSettings`` row, so the agent
    privacy gate takes its defaults and ``visible_to_agent`` returns True —
    which is what lets the handler reach the attachment read at all."""
    Account, Message, _ = _outlook_tables(engine)
    account = Account(user_id=str(uuid.uuid4()), upn="ops@example.test")
    db.add(account)
    db.flush()
    msg = Message(
        account_id=account.id,
        graph_message_id=f"gdxa159-{uuid.uuid4()}",
        subject="quote request",
        from_address="customer@example.test",
        to_addresses=["ops@example.test"],
        direction="inbound",
        is_personal=False,
        has_attachments=True,
    )
    db.add(msg)
    db.commit()
    return msg


@pytest.mark.asyncio
async def test_pg_email_read_keeps_the_invokers_transaction_committable(pg_test_engine):
    """The handler must hand its invoker back a usable transaction.

    ``db`` is the invoker's, not the tool's, and ``routers/ai.py`` reuses it
    for every tool in the turn. Pre-fix: attachment read fails → handler
    degrades to "no attachments" and returns 200 → the invoker's session is
    already aborted → everything it does next dies naming an unrelated table.

    ``_Row`` stands in for that later work, staged after the handler returns.
    The flushed ``mcp.tool_invoke`` audit row shares its fate: only a later
    tool's ``commit()`` makes it durable, so it survives exactly when ``_Row``
    does.

    Remove the ``contained_read`` in ``email_read.handler`` and the commit
    below raises 25P02 and the row count is 0.
    """
    from gdx_dispatch.core.mcp_tools.email_read import handler

    Session = _sessions(pg_test_engine)
    db = Session()
    msg = _seed_message(db, pg_test_engine)

    with pg_test_engine.begin() as c:
        c.execute(text("DROP TABLE outlook_attachments"))

    result = await handler(principal=None, db=db, message_id=str(msg.id))

    assert result["message"]["attachments"] == [], "the read was supposed to fail"
    assert db.is_active, "the handler handed the invoker a deactivated session"

    # What invoke_tool does next.
    db.add(_Row(id=1, v="what the invoker does after the tool returns"))
    db.commit()
    db.close()
    assert _surviving_rows(pg_test_engine) == 1, (
        "the handler cost its invoker the work it did after the tool returned"
    )


@pytest.mark.asyncio
async def test_pg_email_read_contains_its_settings_read_too(pg_test_engine):
    """The other two reads in the handler, which have no local ``try``.

    Found by this change's own audit, not by the census: the predicate looks
    for a read inside a swallowing ``try``, and ``db.get(OutlookMessage)`` and
    ``visible_to_agent`` sit in neither. The frame that swallows is one up —
    ``invoke_tool``'s ``except Exception`` — and ``routers/ai.py`` then keeps
    invoking the turn's remaining tools on this same session, so leaving them
    bare made the fix's own headline claim false: the handler still handed the
    ask-loop a dead transaction.

    ``outlook_settings`` is the table broken here because it is read by
    ``_load_rules`` behind ``visible_to_agent`` — the deepest of the three and
    the one no other test touches. The handler still fails (``contained_read``
    re-raises, and ``invoke_tool`` turns that into ``execution_error``); what
    must survive is the invoker's transaction.

    Honest about its own reach, because the audit measured it: this reddens
    when the ``visible_to_agent`` wrap is removed, and NOT when the
    ``db.get(OutlookMessage)`` wrap alone is removed — ``db.get`` on a primary
    key that is already in the identity map may emit no SQL at all. That wrap
    therefore ships without a guard that can fail for it, which is recorded
    here rather than implied away. An earlier version of this docstring said
    "remove either"; that was false.
    """
    from gdx_dispatch.core.mcp_tools.email_read import handler

    Session = _sessions(pg_test_engine)
    db = Session()
    msg = _seed_message(db, pg_test_engine)

    with pg_test_engine.begin() as c:
        c.execute(text("DROP TABLE outlook_settings"))

    with pytest.raises(Exception):
        await handler(principal=None, db=db, message_id=str(msg.id))

    assert db.is_active, "the handler handed the invoker a deactivated session"

    # What the ask-loop does next, on this same session.
    db.add(_Row(id=1, v="the next tool call in the same agent turn"))
    db.commit()
    db.close()
    assert _surviving_rows(pg_test_engine) == 1, (
        "a failed settings read cost the invoker the rest of the turn"
    )


@pytest.mark.asyncio
async def test_pg_email_read_still_lists_attachments_when_the_read_succeeds(pg_test_engine):
    """The savepoint must not cost the tool its answer. Without this, a
    ``contained_read`` that silently returned nothing would pass every other
    assertion in this section."""
    from gdx_dispatch.core.mcp_tools.email_read import handler

    Session = _sessions(pg_test_engine)
    db = Session()
    msg = _seed_message(db, pg_test_engine)
    _, _, Attachment = _outlook_tables(pg_test_engine)
    db.add(Attachment(
        message_id=msg.id, graph_attachment_id=f"gdxa159-att-{uuid.uuid4()}",
        filename="quote.pdf", content_type="application/pdf",
        size_bytes=1024, is_inline=False,
    ))
    db.commit()

    result = await handler(principal=None, db=db, message_id=str(msg.id))

    assert [a["filename"] for a in result["message"]["attachments"]] == ["quote.pdf"]
    db.close()


@pytest.mark.asyncio
async def test_pg_email_move_read_cannot_cost_the_turn_its_write(pg_test_engine):
    """``email.move`` is the sharpest of the three, because a WRITE is lost.

    Found by audit after ``email_read`` was fixed: the same three-read shape,
    uncontained, in a tool that goes on to ``db.commit()``. Over twenty
    ``mcp_tools/*`` modules commit on the invoker's shared session, so a read
    failure in one tool cost a LATER tool its write. Measured pre-fix on PG
    16.14: ``COMMIT FAILED InFailedSqlTransaction, rows=0``.

    The ``db.commit()`` in the handler is deliberately NOT inside a savepoint
    (rule 2 — a write wants ``db.begin_nested()``); only the reads are.
    """
    from gdx_dispatch.core.mcp_tools.email_move import handler

    Session = _sessions(pg_test_engine)
    db = Session()
    msg = _seed_message(db, pg_test_engine)

    with pg_test_engine.begin() as c:
        c.execute(text("DROP TABLE outlook_settings"))

    with pytest.raises(Exception):
        await handler(
            principal=None, db=db, message_id=str(msg.id), target_folder_id="archive"
        )

    assert db.is_active, "the handler handed the invoker a deactivated session"
    db.add(_Row(id=1, v="a later tool's write in the same turn"))
    db.commit()
    db.close()
    assert _surviving_rows(pg_test_engine) == 1, (
        "email.move's failed read cost a later tool its committed write"
    )


@pytest.mark.asyncio
async def test_pg_email_list_read_cannot_poison_the_turn(pg_test_engine):
    """``email.list``'s ``_load_rules`` read, same class, same session."""
    from gdx_dispatch.core.mcp_tools.email_list import handler

    Session = _sessions(pg_test_engine)
    db = Session()
    _seed_message(db, pg_test_engine)

    with pg_test_engine.begin() as c:
        c.execute(text("DROP TABLE outlook_settings"))

    with pytest.raises(Exception):
        await handler(principal=None, db=db)

    assert db.is_active, "the handler handed the invoker a deactivated session"
    db.add(_Row(id=1, v="a later tool's write in the same turn"))
    db.commit()
    db.close()
    assert _surviving_rows(pg_test_engine) == 1, (
        "email.list's failed read cost a later tool its committed write"
    )


@pytest.mark.asyncio
async def test_pg_email_draft_reply_read_cannot_poison_the_turn(pg_test_engine):
    """``email.draft``'s reply-parent load and privacy gate — the fourth email tool.

    Deferred in an earlier revision on the grounds that its ``except`` catches
    only ``(ValueError, TypeError)`` so containment "would change nothing". The
    re-audit disproved that on PG 16: the ProgrammingError still escapes to the
    invoker, and the turn's later commit died ``InFailedSqlTransaction`` with 0
    rows — the same loss ``email_move``'s test pins.
    """
    from gdx_dispatch.core.mcp_tools.email_draft import handler

    Session = _sessions(pg_test_engine)
    db = Session()
    msg = _seed_message(db, pg_test_engine)

    with pg_test_engine.begin() as c:
        c.execute(text("DROP TABLE outlook_settings"))

    with pytest.raises(Exception):
        await handler(
            principal=None,
            db=db,
            to=["customer@example.test"],
            subject="re: quote request",
            body="thanks",
            in_reply_to_message_id=str(msg.id),
        )

    assert db.is_active, "the handler handed the invoker a deactivated session"
    db.add(_Row(id=1, v="a later tool's write in the same turn"))
    db.commit()
    db.close()
    assert _surviving_rows(pg_test_engine) == 1, (
        "email.draft's failed read cost a later tool its committed write"
    )


# ---------------------------------------------------------------------------
# core/next_action.py — the answer itself
# ---------------------------------------------------------------------------


def _seed_overdue_invoice(engine):
    """An invoice the ``call_overdue_invoice`` auto-rule will pick up: sent,
    more than 14 days ago, not deleted."""
    _insert_invoice(engine, status="sent", total="100", sent_days_ago=30)


def test_pg_get_auto_actions_rules_do_not_kill_each_other(pg_test_engine):
    """The cascade, and the only test here that asserts a behaviour change.

    ``get_auto_actions`` runs three rules in sequence on one session, each in
    its own ``try``. Rules 1 and 3 read ``jobs``; rule 2 reads ``invoices``. Drop
    ``jobs`` and pre-fix the chain is: rule 1 raises 42P01 → swallowed → the
    transaction is aborted → **rule 2's perfectly good invoice read fails too**,
    with 25P02, naming a table that was never the problem → swallowed → empty
    list and three misleading log lines.

    Post-fix rule 1's failure is contained and rule 2 answers. Revert the
    ``contained_read`` in rule 1 and this test fails on an empty list — it is
    the load-bearing one for this file.

    The ``DROP TABLE`` is belt-and-braces, and this change's audit was right to
    say so: ``fixtures/structure.sql`` is already stale against the ``Job`` ORM
    model (no ``jobs.job_number``), so both jobs rules 42703 in this fixture
    whether or not the table is there — visible as ``UndefinedColumn`` in the
    captured warnings of the two ``get_queue`` tests below, which drop nothing.
    Kept because the drop pins the mechanism the docstring claims (42P01)
    rather than relying on a fixture defect that someone will eventually fix.
    """
    from gdx_dispatch.core.next_action import NextActionQueue

    Session = _sessions(pg_test_engine)
    _seed_overdue_invoice(pg_test_engine)
    db = Session()

    with pg_test_engine.begin() as c:
        c.execute(text("DROP TABLE jobs CASCADE"))

    actions = NextActionQueue().get_auto_actions(TENANT, db)

    types = {a["action_type"] for a in actions}
    assert "call_overdue_invoice" in types, (
        "the jobs rule's failure took the invoice rule down with it — "
        f"got {types or 'nothing at all'}"
    )
    db.close()


def test_pg_get_queue_still_returns_auto_actions_when_the_persisted_read_fails(pg_test_engine):
    """The same cascade one frame up, across the two halves of ``get_queue``.

    ``get_queue`` merges persisted actions with ephemeral ones. Drop
    ``next_actions`` and pre-fix the persisted read poisoned the session before
    ``get_auto_actions`` ever ran, so the caller got ``[]`` — the ephemeral half
    was never the thing that was broken.
    """
    from gdx_dispatch.core.next_action import NextActionQueue

    Session = _sessions(pg_test_engine)
    _seed_overdue_invoice(pg_test_engine)
    db = Session()

    with pg_test_engine.begin() as c:
        c.execute(text("DROP TABLE next_actions"))

    queue = NextActionQueue().get_queue(TENANT, str(uuid.uuid4()), db)

    assert {a["action_type"] for a in queue} == {"call_overdue_invoice"}, (
        "the persisted read's failure cost the caller the whole queue"
    )
    db.close()


def test_pg_get_queue_does_not_cost_the_caller_its_row(pg_test_engine):
    """And the GDXA-86 claim proper, for this file: pending ORM work survives.

    ``recommendation_routes`` holds nothing pending today, so this is the
    same defensive case as ``ai_quote``'s — asserted because the shape is what
    is being fixed, not the one caller.
    """
    from gdx_dispatch.core.next_action import NextActionQueue

    Session = _sessions(pg_test_engine)
    db = Session()
    db.add(_Row(id=1, v="the caller's work"))

    with pg_test_engine.begin() as c:
        c.execute(text("DROP TABLE next_actions"))

    NextActionQueue().get_queue(TENANT, str(uuid.uuid4()), db)

    assert len(db.new) == 1, "get_queue flushed the caller's pending work"
    assert db.is_active, "get_queue handed back a deactivated session"
    db.commit()
    db.close()
    assert _surviving_rows(pg_test_engine) == 1, (
        "a failed next-action read cost the caller its row"
    )

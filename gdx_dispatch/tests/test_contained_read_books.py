"""``contained_read`` / per-row SAVEPOINT — the back-office-books call sites (GDXA-165).

Same class as ``tests/test_contained_read.py`` (GDXA-86), which owns the helper
itself and should be read first: a helper takes a session it does not own, does
DB work inside a ``try``, and the ``except`` degrades — returns a default, or
collects a per-row error and carries on. On Postgres the failed statement has
already aborted the WHOLE transaction, so the degraded answer is a lie: every
later statement raises ``InFailedSqlTransaction`` (25P02), and the caller's
``commit()`` raises NOTHING — Postgres answers a COMMIT on an aborted
transaction with a quiet ROLLBACK, so the work it was holding is gone without a
sound (measured, GDXA-165 audit: ``commit_err=None``).

**Every test here is Postgres-only, and that is the point.** SQLite does not
poison a transaction on a failed statement, so pre-fix and post-fix behave
identically there — a green SQLite run is not evidence for any of this. The arm
skips with no reachable Postgres and fails under CI (#440).

Two idioms, and which one a site gets is decided by whether its block writes
(``core.database.contained_read`` rule 2):

* pure reads → ``with contained_read(db):`` — a connection-level SAVEPOINT that
  introduces no flush, so wrapping a read is behaviour-neutral.
* blocks that write → ``with db.begin_nested():`` — the ORM's unit of work has
  to participate in the savepoint so it knows what to un-stage.

Failures are injected the way production produces them — a renamed table
(42P01), a column the ORM selects but the schema lacks (42703), a value too wide
for its column (22001) — never by monkeypatching the session. A mock proves which
arguments were passed, never that the transaction survived.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

from sqlalchemy import Column, Enum, Integer, String, text
from sqlalchemy.orm import declarative_base, sessionmaker

_Base = declarative_base()


class _Row(_Base):
    """The caller's pending work. Its survival is the assertion that matters —
    "the session still answers SELECT 1" would pass on a session whose
    transaction was rolled back, which is the data loss, not the fix."""

    __tablename__ = "gdxa165_row"
    id = Column(Integer, primary_key=True)
    v = Column(String)


def _sessions(engine):
    # autoflush=False mirrors core.database.SessionLocal, which is what every
    # session in the app is.
    _Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _rename(engine, table: str) -> None:
    with engine.begin() as c:
        c.execute(text(f"ALTER TABLE {table} RENAME TO {table}_gone"))


def _caller_survives(pg_test_engine, table: str | None, call):
    """Break ``table`` under a helper, run it on a session that already holds
    pending work, and assert BOTH halves: the helper still degrades to its
    default, AND the caller can still commit.

    ``table=None`` means the read already fails on this fixture unaided —
    ``tests/fixtures/structure.sql`` is a stale ``TenantBase``-scoped dump, so
    some tables and columns migration head creates are simply absent from it.
    Each caller below says which, and whether that absence is the production
    failure mode or a fixture artifact that merely exercises the same
    containment.
    """
    Session = _sessions(pg_test_engine)
    if table is not None:
        _rename(pg_test_engine, table)

    db = Session()
    db.add(_Row(id=1, v="the caller's pending work"))
    result = call(db)
    db.commit()
    db.close()

    with pg_test_engine.connect() as other:
        assert other.execute(text("SELECT count(*) FROM gdxa165_row")).scalar() == 1, (
            f"a failed read of {table} cost the caller its row"
        )
    return result


class _FakeQB:
    """QBClient stand-in — ``query`` returns a canned list per entity. Only the
    HTTP half is faked; every DB statement under test is real, which is the half
    this file is about."""

    def __init__(self, responses):
        self._responses = responses

    async def query(self, entity, where="", max_results=500):
        return self._responses.get(entity, [])


# ---------------------------------------------------------------------------
# Pure reads — contained_read(db)
# ---------------------------------------------------------------------------


def test_pg_delete_sync_gate_cannot_poison_the_pull(pg_test_engine, monkeypatch):
    """``_delete_sync_enabled`` decides whether a soft-DELETE propagates, on the
    caller's mid-pull session. Highest-value site in this file: the caller acts
    on the degraded verdict and only discovers the transaction is dead later.

    The injected failure is the real production mode, not a fixture artifact:
    ``structure.sql``'s ``qb_connections`` has no ``delete_sync_enabled`` column
    at all, so the ORM's SELECT of it raises 42703 — which is exactly what a
    pre-migration schema does to this statement. Hence ``table=None``.
    """
    from gdx_dispatch.modules.quickbooks.sync import _delete_sync_enabled

    monkeypatch.setenv("QB_DELETE_SYNC_ENABLED", "0")
    enabled = _caller_survives(
        pg_test_engine, None, lambda db: _delete_sync_enabled("t-165", db)
    )
    # Documents the degradation — it falls through to the env var. The row count
    # inside _caller_survives is the load-bearing half.
    assert enabled is False, "a failed column read must fall back to the env var"


def test_pg_pnl_last_synced_at_cannot_poison_the_request(pg_test_engine):
    """A failed ``SELECT MAX(synced_at) FROM qb_pnl_monthly`` must not poison the
    request that asked for it.

    Do NOT restate the motive as "qb_pnl_monthly does not exist until the first
    P&L pull". That is false and the comment on the fix in ``routers/budgets.py``
    retracts it at length: it is an ORM model (``models/tenant_models.py``,
    ``__tablename__ = "qb_pnl_monthly"``) that migration head creates, and prod
    has it populated. It is missing only from the TenantBase-scoped
    ``tests/fixtures/structure.sql`` dump — a fixture artifact, not a tenant
    state, which is why this test passes ``table=None`` and lets that absence be
    the injected failure rather than renaming anything.

    So this is cheap insurance on a real mechanism (a migration window, a future
    statement_timeout), not the repair of an observed loss. Honest about
    severity: both callers are read-only GETs that call this last, so today's
    poisoning is absorbed by the session close at request end. This test pins
    the containment so that stops being true by accident the first time a caller
    writes after it.
    """
    from gdx_dispatch.routers.budgets import _pnl_last_synced_at

    assert _caller_survives(pg_test_engine, None, _pnl_last_synced_at) is None


def test_pg_load_qb_accounts_cannot_poison_the_request(pg_test_engine):
    """Same shape as above with a caller that DOES keep working on the session:
    ``list_anomalies`` calls ``get_qb_client(tenant_id, db)`` two statements
    later, which reads the token store and can persist a refreshed OAuth token.

    ``qb_accounts`` exists in the fixture, so this one injects a real 42P01 by
    renaming it rather than relying on an absence.
    """
    from gdx_dispatch.routers.budgets import _load_qb_accounts

    assert _caller_survives(pg_test_engine, "qb_accounts", _load_qb_accounts) == []


def test_pg_tenant_zoneinfo_cannot_cost_the_simplefin_ingest(pg_test_engine):
    """Callers are the SimpleFIN ingest and the 5-minute beat tick — both
    mid-flight with pending transaction rows when they ask for the day boundary,
    so the wrong day boundary was the lesser half of the bug.

    ``app_settings`` and its ``timezone`` column both exist in the fixture, so
    the rename is genuinely what makes this read fail. The fallback zone itself
    is deliberately untouched here (separate open question).
    """
    from zoneinfo import ZoneInfo

    from gdx_dispatch.modules.bank_feeds.service import tenant_zoneinfo

    tz = _caller_survives(pg_test_engine, "app_settings", tenant_zoneinfo)
    assert tz == ZoneInfo("America/New_York"), "a failed read falls back to the model default"


def test_pg_annotate_gl_accounts_cannot_cost_the_expense_list(pg_test_engine):
    """``_annotate_gl_accounts`` is annotation only — "failures never break the
    list" — but the list endpoint holds a request-scoped session that goes on to
    do more work.

    ``gl_settings`` is absent from the fixture dump (a different-Base table), so
    ``ledger_service.get_gl_settings`` fails first and unaided: ``table=None``.
    That is a fixture artifact rather than a production state, and it exercises
    the containment identically — but do not cite it as the prod failure mode.

    ``rows`` is a stand-in carrying only the two attributes the function reads
    (``company_id``, ``category``); every DB statement it makes is real, which is
    what this test is about.
    """
    from gdx_dispatch.routers.expenses import _annotate_gl_accounts

    class _Expense:
        company_id = str(uuid.uuid4())
        category = "materials"

    payloads: list[dict] = [{}]
    _caller_survives(
        pg_test_engine, None, lambda db: _annotate_gl_accounts(db, [_Expense()], payloads)
    )
    assert payloads == [{}], "a failed read must leave the payload unannotated"


# ---------------------------------------------------------------------------
# Blocks that write — db.begin_nested()
# ---------------------------------------------------------------------------
#
# These reproduce the 2026-08-14 v1.59.0 deploy that `pull_bank_transactions`
# already carries a savepoint for: the first row's statement failed, the abort
# cascaded, and a 1,627-row pull went to all-errors. One bad row must cost one
# row.
#
# The failure is injected by narrowing a column so exactly ONE row's value
# overflows it (22001), with the bad row FIRST so the cascade has rows left to
# destroy. Without the savepoint every later row fails with 25P02, the post-loop
# `db.commit()` quietly rolls back, and nothing at all persists, while the
# function still returns created/updated counts — a silent write.


def _narrow(engine, table: str, column: str, width: int = 3) -> None:
    with engine.begin() as c:
        c.execute(text(f"ALTER TABLE {table} ALTER COLUMN {column} TYPE varchar({width})"))


def test_pg_pull_accounts_one_bad_row_does_not_zero_the_pull(pg_test_engine):
    from gdx_dispatch.core.quickbooks import QBConnection
    from gdx_dispatch.modules.quickbooks.sync import pull_accounts

    Session = _sessions(pg_test_engine)
    # The fixture's qb_connections predates delete_sync_enabled; _touch_sync_success
    # SELECTs the whole ORM row after the loop, so without this the pull dies on an
    # unrelated 42703 and the test could not see its own subject.
    with pg_test_engine.begin() as c:
        c.execute(text("ALTER TABLE qb_connections ADD COLUMN delete_sync_enabled boolean"))
    _narrow(pg_test_engine, "qb_accounts", "name")
    assert QBConnection is not None  # import is the point: keeps the ORM shape honest

    db = Session()
    qb = _FakeQB({"Account": [
        {"Id": "A-bad", "Name": "a name far too wide for the narrowed column"},
        {"Id": "A-ok-1", "Name": "ok1"},
        {"Id": "A-ok-2", "Name": "ok2"},
    ]})
    result = asyncio.run(pull_accounts("t-165", db, qb))
    db.close()

    assert len(result["errors"]) == 1, f"expected exactly one bad row, got {result['errors']}"
    assert result["created"] == 2
    with pg_test_engine.connect() as other:
        landed = other.execute(text(
            "SELECT qb_account_id FROM qb_accounts ORDER BY qb_account_id"
        )).scalars().all()
    assert landed == ["A-ok-1", "A-ok-2"], (
        "one bad row cost the whole pull — the per-row savepoint is gone"
    )


def _deposit_rows():
    return [
        {"Id": "D-bad", "TxnDate": "2026-05-01", "TotalAmt": 1.0,
         "DepositToAccountRef": {"value": "A1", "name": "far too wide"}},
        {"Id": "D-ok-1", "TxnDate": "2026-05-02", "TotalAmt": 2.0,
         "DepositToAccountRef": {"value": "A1", "name": "ok"}},
        {"Id": "D-ok-2", "TxnDate": "2026-05-03", "TotalAmt": 3.0,
         "DepositToAccountRef": {"value": "A1", "name": "ok"}},
    ]


def test_pg_pull_deposits_one_bad_row_does_not_zero_the_pull(pg_test_engine):
    """``qb_deposits`` is absent from the fixture dump, so the table is built
    from the ORM here — hand-written DDL in a fixture once hid a feature that
    could not be inserted at all."""
    from gdx_dispatch.modules.quickbooks.banking import QBDeposit, pull_deposits

    Session = _sessions(pg_test_engine)
    QBDeposit.__table__.create(pg_test_engine)
    _narrow(pg_test_engine, "qb_deposits", "deposit_to_account_name")

    db = Session()
    result = asyncio.run(
        pull_deposits("t-165", db, _FakeQB({"Deposit": _deposit_rows()}))
    )
    db.close()

    assert len(result["errors"]) == 1, f"expected exactly one bad row, got {result['errors']}"
    assert result["created"] == 2
    with pg_test_engine.connect() as other:
        landed = other.execute(text(
            "SELECT qb_txn_id FROM qb_deposits ORDER BY qb_txn_id"
        )).scalars().all()
    assert landed == ["D-ok-1", "D-ok-2"], (
        "one bad row cost the whole deposit pull — and _reconcile_tombstones ran after it"
    )


def test_pg_pull_transfers_one_bad_row_does_not_zero_the_pull(pg_test_engine):
    from gdx_dispatch.modules.quickbooks.banking import QBTransfer, pull_transfers

    Session = _sessions(pg_test_engine)
    QBTransfer.__table__.create(pg_test_engine)
    _narrow(pg_test_engine, "qb_transfers", "from_account_name")

    db = Session()
    rows = [
        {"Id": "T-bad", "TxnDate": "2026-05-01", "Amount": 1.0,
         "FromAccountRef": {"value": "A1", "name": "far too wide"},
         "ToAccountRef": {"value": "A2", "name": "b"}},
        {"Id": "T-ok-1", "TxnDate": "2026-05-02", "Amount": 2.0,
         "FromAccountRef": {"value": "A1", "name": "ok"},
         "ToAccountRef": {"value": "A2", "name": "b"}},
    ]
    result = asyncio.run(pull_transfers("t-165", db, _FakeQB({"Transfer": rows})))
    db.close()

    assert len(result["errors"]) == 1, f"expected exactly one bad row, got {result['errors']}"
    assert result["created"] == 1
    with pg_test_engine.connect() as other:
        landed = other.execute(text(
            "SELECT qb_txn_id FROM qb_transfers ORDER BY qb_txn_id"
        )).scalars().all()
    assert landed == ["T-ok-1"], "one bad row cost the whole transfer pull"


def test_pg_banking_entry_upsert_one_bad_row_does_not_zero_the_pull(pg_test_engine):
    """``_upsert_banking_entry`` is the choke point for all SIX bank-touching
    entity pulls, four of which the issue's site list never named
    (``_pull_simple_entity`` for BillPayment/SalesReceipt/RefundReceipt,
    ``pull_customer_payments``, ``pull_vendor_credits``,
    ``pull_journal_entries``). The GDXA-165 audit reproduced the identical
    cascade on ``pull_bill_payments``: reported 2 created, persisted 0, raised
    nothing.

    Driven through ``pull_bill_payments`` rather than the helper directly, so the
    caller's per-row ``except`` — the swallow that makes this class dangerous —
    is the one doing the catching.
    """
    from gdx_dispatch.modules.quickbooks.banking import QBBankingEntry, pull_bill_payments

    Session = _sessions(pg_test_engine)
    QBBankingEntry.__table__.create(pg_test_engine)
    _narrow(pg_test_engine, "qb_banking_entries", "account_name")

    db = Session()
    rows = [
        {"Id": "BP-bad", "TxnDate": "2026-05-01", "TotalAmt": 1.0,
         "CheckPayment": {"BankAccountRef": {"value": "A1", "name": "far too wide"}}},
        {"Id": "BP-ok-1", "TxnDate": "2026-05-02", "TotalAmt": 2.0,
         "CheckPayment": {"BankAccountRef": {"value": "A1", "name": "ok"}}},
        {"Id": "BP-ok-2", "TxnDate": "2026-05-03", "TotalAmt": 3.0,
         "CheckPayment": {"BankAccountRef": {"value": "A1", "name": "ok"}}},
    ]
    result = asyncio.run(pull_bill_payments("t-165", db, _FakeQB({"BillPayment": rows})))
    db.close()

    assert len(result["errors"]) == 1, f"expected exactly one bad row, got {result['errors']}"
    with pg_test_engine.connect() as other:
        landed = other.execute(text(
            "SELECT qb_txn_id FROM qb_banking_entries ORDER BY qb_txn_id"
        )).scalars().all()
    assert landed == ["BP-ok-1", "BP-ok-2"], (
        f"reported {result['created']} created and persisted {landed} — one bad row "
        "cost the whole pull, and the same choke point serves five sibling entities"
    )


def test_pg_apply_qbo_deletes_never_audits_a_delete_that_did_not_happen(pg_test_engine, monkeypatch):
    """The soft-DELETE loop must never audit a deletion that did not happen.

    Every row's ``db.get(model, ...)`` fails here, so NO row is deleted. Be
    precise about WHY, because the obvious reading is wrong: the ``_rename`` of
    ``customers`` below is belt-and-braces, not the cause. The fixture's
    ``customers`` already lacks ``local_edit_at``, a column the ORM selects, so
    the load raises 42703 with or without the rename — removing the rename
    leaves this test passing on the identical error (measured, GDXA-165 audit
    pass 2). The rename is kept so the test keeps failing the same way if that
    fixture gap is ever closed, and this paragraph exists so nobody "cleans up"
    the rename believing it is what breaks the read.

    Three things then have to hold at once, and each fails against a different
    ancestor of this code:

    * ``audited == 0`` — the audit trail does not claim deletions that did not
      happen. The first draft of this fix added the per-row savepoint but left
      the ``_audit`` call ABOVE it, where ``log_audit_event_sync``'s closing
      ``db.flush()`` put the row on the connection before the savepoint existed,
      so the savepoint could not roll it back: 2 audit rows for 0 deletions,
      committed clean. A false audit trail is worse than the abort it replaced.
      The audit now sits inside the same savepoint as the delete.
    * both mappings still present — the failure really did prevent the delete,
      so this test is describing the situation it claims to.
    * the caller's ``commit()`` is clean. Against HEAD this test goes red, but
      NOT here and not on this statement: HEAD's uncontained
      ``_delete_sync_enabled`` read poisons the transaction first, and the
      ``SELECT qb_entity_maps`` at the top of ``_apply_qbo_deletes`` raises
      InternalError out of the call itself, before any commit. Right verdict,
      earlier mechanism — stated exactly because pass 2 of the audit caught this
      docstring claiming the commit was what raised.

    Not asserting ``deleted``: it counts locals actually tombstoned, which is 0
    here for the same reason ``audited`` is.

    The other direction — ``_audit`` itself failing — is the next test.
    """
    from gdx_dispatch.core.quickbooks import QBEntityMap
    from gdx_dispatch.modules.quickbooks.sync import _apply_qbo_deletes

    Session = _sessions(pg_test_engine)
    # qb_entity_maps IS in the fixture dump (plural — the ORM's __tablename__),
    # so it is not built here.
    # qb_connections has no delete_sync_enabled column here, so the gate falls
    # through to the env var — which is the branch this test wants ON.
    monkeypatch.setenv("QB_DELETE_SYNC_ENABLED", "1")

    db = Session()
    for n in (1, 2):
        db.add(QBEntityMap(
            id=uuid.uuid4(), tenant_id="t-165", entity_type="customer",
            qb_id=f"QB-{n}", local_id=str(uuid.uuid4()),
            synced_at=datetime.now(UTC),
        ))
    db.commit()

    _rename(pg_test_engine, "customers")
    _apply_qbo_deletes("t-165", "customer", {"QB-still-there"}, db)
    db.commit()
    db.close()

    with pg_test_engine.connect() as other:
        audited = other.execute(text(
            "SELECT count(*) FROM audit_logs WHERE action = 'qb_delete_sync'"
        )).scalar()
        remaining = other.execute(text(
            "SELECT count(*) FROM qb_entity_maps WHERE tenant_id = 't-165'"
        )).scalar()

    assert remaining == 2, (
        f"expected both mappings to survive a failed delete, got {remaining} — "
        "the injected failure did not actually prevent the delete, so this test "
        "is not describing the situation it claims to"
    )
    assert audited == 0, (
        f"expected 0 qb_delete_sync audit rows, got {audited} — the audit trail "
        "asserts deletions that never happened. The audit must land AFTER the "
        "savepoint releases, or its flush escapes the savepoint that was "
        "supposed to be able to roll it back"
    )


def _align_customers_to_orm(engine) -> None:
    """Add the columns the stale fixture dump's ``customers`` lacks, so the
    ``db.get(Customer, ...)`` in ``_apply_qbo_deletes`` can succeed and the
    failure under test is the one this test injects, not a fixture gap."""
    from gdx_dispatch.models.tenant_models import Customer

    with engine.begin() as c:
        have = set(c.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'customers'"
        )).scalars())
        for col in Customer.__table__.columns:
            if col.name not in have:
                # A named PG enum would need its CREATE TYPE first; no row here
                # populates these columns, so a nullable varchar stands in.
                ddl_type = (
                    "varchar" if isinstance(col.type, Enum)
                    else col.type.compile(dialect=engine.dialect)
                )
                c.execute(text(f'ALTER TABLE customers ADD COLUMN "{col.name}" {ddl_type}'))


def test_pg_apply_qbo_deletes_a_failed_audit_write_costs_one_row_not_the_pull(
    pg_test_engine, monkeypatch,
):
    """A database-level failure writing ``audit_logs`` must cost its own row —
    not the delete-sync, and not the pull whose upserts share its transaction.

    ``_apply_qbo_deletes`` runs at the end of ``pull_customers``,
    ``pull_invoices`` and ``pull_payments``, just before their ``db.commit()``,
    so the session arrives holding the whole pull. Until the GDXA-165 audit the
    ``_audit`` call sat outside the row's savepoint behind its own
    ``except Exception``: a real Postgres error there (here 22001, an
    ``entity_id`` too wide for its column; in production equally a statement
    timeout) aborted the transaction, the ``except`` logged it, and every later
    row failed. The pull was then lost at the caller's commit. With this test's
    22001, which surfaces in the ORM flush, that commit raises
    ``PendingRollbackError`` (run against the pre-fix shape on 2026-10-04).
    An error that surfaces in a raw ``execute`` instead leaves no pending
    rollback, and the commit returns quietly over an aborted transaction.

    With the audit inside the savepoint, the failing row's delete and its audit
    roll back together, the mapping survives for the next sync to retry, and
    everything else commits.
    """
    from gdx_dispatch.core.audit import ensure_audit_table
    from gdx_dispatch.core.quickbooks import QBEntityMap
    from gdx_dispatch.modules.quickbooks.sync import _apply_qbo_deletes

    Session = _sessions(pg_test_engine)
    monkeypatch.setenv("QB_DELETE_SYNC_ENABLED", "1")
    _align_customers_to_orm(pg_test_engine)
    # "QB-1" fits in four characters; "QB-22" overflows — so exactly one row's
    # audit write fails, whichever order the loop visits them in.
    with pg_test_engine.begin() as c:
        c.execute(text("ALTER TABLE audit_logs ALTER COLUMN entity_id TYPE varchar(4)"))

    db = Session()
    for qb_id in ("QB-1", "QB-22"):
        db.add(QBEntityMap(
            id=uuid.uuid4(), tenant_id="t-165", entity_type="customer",
            qb_id=qb_id, local_id=str(uuid.uuid4()),
            synced_at=datetime.now(UTC),
        ))
    db.commit()
    # Prime the audit guard before the pull exists. On a database without
    # ``audit_logs_immutable_guard`` (this fixture's, built from structure.sql)
    # the first ``ensure_audit_table`` installs it and COMMITS — inside
    # ``_apply_qbo_deletes`` that would make the pull durable before the loop
    # runs, and ``pull_rows == 1`` could not fail. Production installs the guard
    # at bootstrap; priming here reproduces that (the call is cached per engine).
    ensure_audit_table(db)
    db.commit()

    db.add(_Row(id=1, v="the pull's upserts, not yet committed"))
    _apply_qbo_deletes("t-165", "customer", {"QB-still-there"}, db)
    db.commit()
    db.close()

    with pg_test_engine.connect() as other:
        pull_rows = other.execute(text("SELECT count(*) FROM gdxa165_row")).scalar()
        remaining = other.execute(text(
            "SELECT qb_id FROM qb_entity_maps WHERE tenant_id = 't-165' ORDER BY qb_id"
        )).scalars().all()
        audited = other.execute(text(
            "SELECT entity_id FROM audit_logs WHERE action = 'qb_delete_sync' ORDER BY entity_id"
        )).scalars().all()

    assert pull_rows == 1, (
        "one failed audit write silently rolled back the caller's whole pull — "
        "the per-row failure handler is rolling back the session instead of "
        "only the row's savepoint (an audit moved back outside the savepoint "
        "fails earlier, at the caller's commit, with PendingRollbackError)"
    )
    assert remaining == ["QB-22"], (
        f"expected only the row whose audit failed to keep its mapping, got {remaining}"
    )
    assert audited == ["QB-1"], (
        f"expected exactly the completed delete to be audited, got {audited}"
    )


# There is deliberately NO SQLite arm in this file, and no placeholder standing
# in for one. An earlier draft had a `test_sqlite_arm_is_deliberately_absent`
# that asserted `1/0` raises ZeroDivisionError — it could not fail for any
# defect, so it inflated the count of guards by one while proving nothing
# (GDXA-165 audit). Every test above is PG-only because SQLite cannot tell the
# fixed code from the bug; the dialect-independent half of the contract lives in
# `tests/test_contained_read.py`.

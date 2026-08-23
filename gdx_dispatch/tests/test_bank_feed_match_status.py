"""Feed transaction ↔ statement line pairing (books-convergence Track 2 item 4).

Every status the Transactions tab can show, plus the traps that separate a
useful column from a lying one.

False accusation — saying the bank contradicts us when it does not:

* a ±1 day window that really is ±1, not ±2;
* opposite signs never pair: both sides store signed cents, and a $500
  deposit is not a $500 withdrawal;
* a VOIDED import stops counting as coverage, so voiding a statement cannot
  silently reclassify everything in it from "feed-only" to "unmatched";
* an unlinked account, and a row with no amount, each say so plainly.

False absolution — saying the bank confirmed something it did not. This is
the more dangerous direction on a money surface, and the first draft of this
module failed all of it, because 15 of the 16 original tests passed a
ONE-element list and a solitary row never has to compete for anything:

* two identical charges against one statement line must NOT both go green —
  a duplicate bank charge is the thing this column exists to catch;
* two claimants must never share one statement line;
* a status must not soften because the other claimant happens to be on
  another page, or filtered out by a date range;
* the same input must produce the same pairing twice running.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from starlette.requests import Request as StarletteRequest

from gdx_dispatch.modules.bank_feeds import oauth
from gdx_dispatch.modules.bank_feeds import router as r
from gdx_dispatch.modules.bank_feeds.feed_match_status import (
    STATUS_AMBIGUOUS,
    STATUS_FEED_ONLY,
    STATUS_MATCHED,
    STATUS_NO_AMOUNT,
    STATUS_PENDING,
    STATUS_STATEMENT_VERIFIED,
    STATUS_UNLINKED,
    STATUS_UNMATCHED,
    compute_statuses,
)
from gdx_dispatch.modules.bank_feeds.models import (
    BankFeedAccount,
    BankFeedTransaction,
    BannoConnection,
    BannoInstitution,
)
from gdx_dispatch.modules.bank_feeds.statement_models import (
    MATCH_CONFIRMED,
    MATCH_SUGGESTED,
    TIE_OUT_PASSED,
    BankAccount,
    BankMatch,
    BankMatchLine,
    BankStatementImport,
    BankStatementLine,
)

PERIOD_START = date(2026, 7, 1)
PERIOD_END = date(2026, 7, 31)
IN_PERIOD = date(2026, 7, 15)


@pytest.fixture
def wired(tenant_db):
    """A feed account, a statement account, and a statement covering July."""
    db = tenant_db
    inst = BannoInstitution(fi_host="fi.example", display_label="Bank")
    db.add(inst)
    db.commit()
    conn = BannoConnection(
        institution_id=inst.id,
        fi_host=inst.fi_host,
        banno_user_id="sub-1",
        access_token_enc=oauth._encrypt("at"),
        refresh_token_enc=oauth._encrypt("rt"),
        access_token_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    db.add(conn)
    db.commit()

    feed_account = BankFeedAccount(connection_id=conn.id, external_account_id="a1")
    bank_account = BankAccount(
        name="Business Checking", kind="checking", institution="Bank", last4="2204"
    )
    db.add_all([feed_account, bank_account])
    db.commit()

    imp = BankStatementImport(
        bank_account_id=bank_account.id,
        file_sha256="0" * 64,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        beginning_balance_cents=0,
        ending_balance_cents=0,
        tie_out_status=TIE_OUT_PASSED,
    )
    db.add(imp)
    db.commit()
    return db, feed_account, bank_account, imp


def _txn(db, feed_account, *, amount_cents=-50_000, posted=IN_PERIOD, pending=False, ext="t1"):
    txn = BankFeedTransaction(
        account_id=feed_account.id,
        external_transaction_id=ext,
        amount_cents=amount_cents,
        posted_date=None if pending else posted,
        pending=pending,
    )
    db.add(txn)
    db.commit()
    return txn


def _line(db, bank_account, imp, *, amount_cents=-50_000, txn_date=IN_PERIOD, h="a"):
    line = BankStatementLine(
        bank_account_id=bank_account.id,
        import_id=imp.id,
        txn_date=txn_date,
        amount_cents=amount_cents,
        description="COFFEE",
        section="debit",
        line_hash=h * 64,
    )
    db.add(line)
    db.commit()
    return line


def _status(db, feed_account, txn):
    return compute_statuses(db, [(txn, feed_account)])[str(txn.id)]


def _link(db, feed_account, bank_account):
    feed_account.bank_account_id = bank_account.id
    db.commit()


# ── the six statuses ───────────────────────────────────────────────────


def test_unlinked_account_says_so_rather_than_accusing(wired):
    db, feed_account, bank_account, imp = wired
    _line(db, bank_account, imp)
    txn = _txn(db, feed_account)
    # Deliberately NOT linked. A statement line exists that would pair.
    assert _status(db, feed_account, txn)["match_status"] == STATUS_UNLINKED


def test_pending_is_not_evaluated(wired):
    db, feed_account, bank_account, imp = wired
    _link(db, feed_account, bank_account)
    txn = _txn(db, feed_account, pending=True)
    assert _status(db, feed_account, txn)["match_status"] == STATUS_PENDING


def test_statement_line_pairs_to_statement_verified(wired):
    db, feed_account, bank_account, imp = wired
    _link(db, feed_account, bank_account)
    line = _line(db, bank_account, imp)
    txn = _txn(db, feed_account)
    out = _status(db, feed_account, txn)
    assert out["match_status"] == STATUS_STATEMENT_VERIFIED
    assert out["statement_line_id"] == str(line.id)


def test_outside_statement_coverage_is_feed_only_not_unmatched(wired):
    db, feed_account, bank_account, imp = wired
    _link(db, feed_account, bank_account)
    # A month after the only statement ends: nothing to contradict it.
    txn = _txn(db, feed_account, posted=date(2026, 8, 20))
    assert _status(db, feed_account, txn)["match_status"] == STATUS_FEED_ONLY


def test_inside_coverage_with_no_line_is_unmatched(wired):
    db, feed_account, bank_account, imp = wired
    _link(db, feed_account, bank_account)
    txn = _txn(db, feed_account, posted=IN_PERIOD)
    # Coverage exists, no line pairs → the feed claims what the statement omits.
    assert _status(db, feed_account, txn)["match_status"] == STATUS_UNMATCHED


def test_confirmed_match_reports_matched_with_classification(wired):
    db, feed_account, bank_account, imp = wired
    _link(db, feed_account, bank_account)
    line = _line(db, bank_account, imp)
    txn = _txn(db, feed_account)
    match = BankMatch(
        bank_account_id=bank_account.id,
        rule="R5",
        status=MATCH_CONFIRMED,
        classification="transfer",
    )
    db.add(match)
    db.commit()
    db.add(BankMatchLine(match_id=match.id, line_id=line.id, match_status=MATCH_CONFIRMED))
    db.commit()

    out = _status(db, feed_account, txn)
    assert out["match_status"] == STATUS_MATCHED
    assert out["match_classification"] == "transfer"
    assert out["match_id"] == str(match.id)


def test_suggested_match_is_not_matched_yet(wired):
    db, feed_account, bank_account, imp = wired
    _link(db, feed_account, bank_account)
    line = _line(db, bank_account, imp)
    txn = _txn(db, feed_account)
    match = BankMatch(bank_account_id=bank_account.id, rule="R2", status=MATCH_SUGGESTED)
    db.add(match)
    db.commit()
    db.add(BankMatchLine(match_id=match.id, line_id=line.id, match_status=MATCH_SUGGESTED))
    db.commit()
    # A suggestion is not evidence of reconciliation.
    assert _status(db, feed_account, txn)["match_status"] == STATUS_STATEMENT_VERIFIED


# ── the traps ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("offset,expected", [
    (-1, STATUS_STATEMENT_VERIFIED),
    (0, STATUS_STATEMENT_VERIFIED),
    (1, STATUS_STATEMENT_VERIFIED),
    (2, STATUS_UNMATCHED),
    (-2, STATUS_UNMATCHED),
])
def test_date_window_is_exactly_one_day(wired, offset, expected):
    db, feed_account, bank_account, imp = wired
    _link(db, feed_account, bank_account)
    _line(db, bank_account, imp, txn_date=IN_PERIOD)
    txn = _txn(db, feed_account, posted=IN_PERIOD + timedelta(days=offset))
    assert _status(db, feed_account, txn)["match_status"] == expected


def test_opposite_sign_never_pairs(wired):
    db, feed_account, bank_account, imp = wired
    _link(db, feed_account, bank_account)
    _line(db, bank_account, imp, amount_cents=50_000)  # money IN
    txn = _txn(db, feed_account, amount_cents=-50_000)  # money OUT
    assert _status(db, feed_account, txn)["match_status"] == STATUS_UNMATCHED


def test_voided_import_stops_counting_as_coverage(wired):
    db, feed_account, bank_account, imp = wired
    _link(db, feed_account, bank_account)
    txn = _txn(db, feed_account, posted=IN_PERIOD)
    assert _status(db, feed_account, txn)["match_status"] == STATUS_UNMATCHED

    imp.voided_at = datetime.now(timezone.utc)
    db.commit()
    # Voiding removes the evidence, so the honest answer reverts to "no
    # statement covers this" — not a discrepancy the void just invented.
    assert _status(db, feed_account, txn)["match_status"] == STATUS_FEED_ONLY


def test_other_accounts_lines_do_not_pair(wired):
    db, feed_account, bank_account, imp = wired
    _link(db, feed_account, bank_account)
    other = BankAccount(name="Savings", kind="savings", institution="Bank", last4="2839")
    db.add(other)
    db.commit()
    other_imp = BankStatementImport(
        bank_account_id=other.id, file_sha256="1" * 64,
        period_start=PERIOD_START, period_end=PERIOD_END,
        beginning_balance_cents=0, ending_balance_cents=0,
        tie_out_status=TIE_OUT_PASSED,
    )
    db.add(other_imp)
    db.commit()
    _line(db, other, other_imp, h="b")
    txn = _txn(db, feed_account)
    assert _status(db, feed_account, txn)["match_status"] == STATUS_UNMATCHED


def test_empty_page_makes_no_queries_and_returns_empty(wired):
    db, _feed_account, _bank_account, _imp = wired
    assert compute_statuses(db, []) == {}


# ── the false-green traps (adversarial audit, 2026-08-23) ──────────────
# The first draft of this module answered per transaction, independently, and
# every one of these returned a green tag for something the bank never
# confirmed. 15 of the 16 tests above pass a ONE-element list, which is why
# none of them caught it.


def _statuses(db, feed_account, txns):
    out = compute_statuses(db, [(t, feed_account) for t in txns])
    return [out[str(t.id)] for t in txns]


def test_two_identical_charges_one_line_do_not_both_go_green(wired):
    """The duplicate-charge case. This is the whole point of the column."""
    db, feed_account, bank_account, imp = wired
    _link(db, feed_account, bank_account)
    _line(db, bank_account, imp, amount_cents=-5_000)
    a = _txn(db, feed_account, amount_cents=-5_000, ext="dup-a")
    b = _txn(db, feed_account, amount_cents=-5_000, ext="dup-b")

    got = _statuses(db, feed_account, [a, b])
    assert [g["match_status"] for g in got] == [STATUS_AMBIGUOUS, STATUS_AMBIGUOUS]
    assert got[0]["ambiguous_claimants"] == 2
    assert got[0]["ambiguous_lines"] == 1
    # And crucially: neither claims the line as its own verification.
    assert not any("statement_line_id" in g for g in got)


def test_two_charges_two_lines_each_take_one(wired):
    db, feed_account, bank_account, imp = wired
    _link(db, feed_account, bank_account)
    _line(db, bank_account, imp, amount_cents=-5_000, h="a")
    _line(db, bank_account, imp, amount_cents=-5_000, h="b")
    a = _txn(db, feed_account, amount_cents=-5_000, ext="two-a")
    b = _txn(db, feed_account, amount_cents=-5_000, ext="two-b")

    got = _statuses(db, feed_account, [a, b])
    assert [g["match_status"] for g in got] == [
        STATUS_STATEMENT_VERIFIED, STATUS_STATEMENT_VERIFIED,
    ]
    # Two claimants must not share one line.
    assert got[0]["statement_line_id"] != got[1]["statement_line_id"]


def test_date_separated_duplicates_are_not_called_ambiguous(wired):
    """Same amount, months apart, one line each — different components."""
    db, feed_account, bank_account, imp = wired
    _link(db, feed_account, bank_account)
    early, late = date(2026, 7, 5), date(2026, 7, 25)
    _line(db, bank_account, imp, amount_cents=-7_500, txn_date=early, h="a")
    _line(db, bank_account, imp, amount_cents=-7_500, txn_date=late, h="b")
    a = _txn(db, feed_account, amount_cents=-7_500, posted=early, ext="sep-a")
    b = _txn(db, feed_account, amount_cents=-7_500, posted=late, ext="sep-b")

    got = _statuses(db, feed_account, [a, b])
    assert [g["match_status"] for g in got] == [
        STATUS_STATEMENT_VERIFIED, STATUS_STATEMENT_VERIFIED,
    ]


def test_null_amount_is_not_an_accusation(wired):
    """``amount_cents`` is nullable and the model tells consumers to filter
    it. Reporting "the statement does not show this" for a row whose amount
    we never received would be a fabricated discrepancy."""
    db, feed_account, bank_account, imp = wired
    _link(db, feed_account, bank_account)
    txn = _txn(db, feed_account, amount_cents=None, posted=IN_PERIOD)
    assert _status(db, feed_account, txn)["match_status"] == STATUS_NO_AMOUNT


def test_status_does_not_depend_on_what_else_is_on_the_page(wired):
    """Claimants are counted globally. Rendering one row alone must not
    absolve it of a contest it is losing elsewhere."""
    db, feed_account, bank_account, imp = wired
    _link(db, feed_account, bank_account)
    _line(db, bank_account, imp, amount_cents=-5_000)
    a = _txn(db, feed_account, amount_cents=-5_000, ext="page-a")
    _txn(db, feed_account, amount_cents=-5_000, ext="page-b")

    alone = compute_statuses(db, [(a, feed_account)])[str(a.id)]
    assert alone["match_status"] == STATUS_AMBIGUOUS


def test_pairing_is_deterministic_across_calls(wired):
    db, feed_account, bank_account, imp = wired
    _link(db, feed_account, bank_account)
    _line(db, bank_account, imp, amount_cents=-5_000, h="a")
    _line(db, bank_account, imp, amount_cents=-5_000, h="b")
    a = _txn(db, feed_account, amount_cents=-5_000, ext="det-a")
    b = _txn(db, feed_account, amount_cents=-5_000, ext="det-b")

    first = _statuses(db, feed_account, [a, b])
    second = _statuses(db, feed_account, [a, b])
    assert [g.get("statement_line_id") for g in first] == [
        g.get("statement_line_id") for g in second
    ]


# ── the statement-link endpoint ────────────────────────────────────────


def _request():
    scope = {
        "type": "http", "method": "PATCH", "path": "/", "headers": [],
        "query_string": b"", "client": ("127.0.0.1", 80), "state": {},
    }
    req = StarletteRequest(scope)
    req.state.tenant = {"id": "11111111-1111-1111-1111-111111111111"}
    return req


USER = {"sub": "tester", "tenant_id": "11111111-1111-1111-1111-111111111111", "role": "admin"}


def test_link_endpoint_sets_and_clears(wired):
    db, feed_account, bank_account, _imp = wired
    out = r.patch_account_statement_link(
        str(feed_account.id),
        r.StatementLinkPatch(bank_account_id=str(bank_account.id)),
        _request(), USER, None, db,
    )
    assert out["bank_account_id"] == str(bank_account.id)
    assert out["bank_account_label"] == "Business Checking ····2204"

    cleared = r.patch_account_statement_link(
        str(feed_account.id), r.StatementLinkPatch(bank_account_id=None),
        _request(), USER, None, db,
    )
    assert cleared["bank_account_id"] is None
    assert cleared["previous_bank_account_id"] == str(bank_account.id)


def test_link_endpoint_refuses_a_second_claimant(wired):
    """One statement account, one feed account. Two feed accounts pointed at
    the same statement would both claim its lines and both go green."""
    db, feed_account, bank_account, _imp = wired
    r.patch_account_statement_link(
        str(feed_account.id),
        r.StatementLinkPatch(bank_account_id=str(bank_account.id)),
        _request(), USER, None, db,
    )
    other = BankFeedAccount(
        connection_id=feed_account.connection_id, external_account_id="a2", name="Second"
    )
    db.add(other)
    db.commit()

    with pytest.raises(HTTPException) as exc:
        r.patch_account_statement_link(
            str(other.id),
            r.StatementLinkPatch(bank_account_id=str(bank_account.id)),
            _request(), USER, None, db,
        )
    assert exc.value.status_code == 409
    assert "already linked" in exc.value.detail


def test_link_endpoint_404s_on_unknown_bank_account(wired):
    db, feed_account, _bank_account, _imp = wired
    with pytest.raises(HTTPException) as exc:
        r.patch_account_statement_link(
            str(feed_account.id),
            r.StatementLinkPatch(bank_account_id="99999999-9999-9999-9999-999999999999"),
            _request(), USER, None, db,
        )
    assert exc.value.status_code == 404


def test_relinking_the_same_account_to_itself_is_allowed(wired):
    """The clash guard must exclude the row being edited, or re-saving the
    same pairing would 409 against itself."""
    db, feed_account, bank_account, _imp = wired
    for _ in range(2):
        out = r.patch_account_statement_link(
            str(feed_account.id),
            r.StatementLinkPatch(bank_account_id=str(bank_account.id)),
            _request(), USER, None, db,
        )
    assert out["bank_account_id"] == str(bank_account.id)

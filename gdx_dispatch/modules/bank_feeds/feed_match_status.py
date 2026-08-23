"""Feed transaction ↔ statement line pairing, for display only.

Books-convergence Track 2 item 4. The Transactions tab shows what the bank's
own statement says about each feed transaction, so an operator can tell
"the bank has confirmed this" from "we are still waiting for the statement"
without opening the Reconcile tab.

**Pairing is computed, never stored.** Like the tie-out, this derives a
verdict on read: amount equality plus a ±1 day window on the same linked
account. It creates no rows and no FK between a feed transaction and a
statement line — the authoritative pairing of evidence to books records is
``BankMatch``, and nothing here touches it.

Reconciliation is an ASSIGNMENT, not an existence test
------------------------------------------------------
The first draft of this module asked "does a statement line exist with this
amount in this window?" and answered per transaction, independently. That is
wrong in the one case the column exists to catch: **two identical charges
against one statement line both came back "statement-verified"**, so a
duplicate bank charge — the exact thing an operator is scanning for — showed
as two green tags.

So lines are a finite resource here. Claimants and lines within a key form a
small bipartite graph; each connected component is solved on its own:

* more claimants than lines in a component → every claimant is ``ambiguous``.
  They are genuinely indistinguishable, and picking a winner would be
  inventing a fact. The count is carried so the UI can say *2 transactions,
  1 statement line*.
* otherwise → deterministic greedy assignment, ordered by (date, id) so the
  answer does not change between two loads of the same page.

The claimant set is queried GLOBALLY, not taken from the page being rendered.
A status that changed depending on pagination or the date filter would be a
different kind of lie.

The account link is explicit (``BankFeedAccount.bank_account_id``) rather
than inferred. The two sides have no natural key: ``bank_accounts`` is keyed
on institution + last4 and ``bank_feed_accounts`` on connection + external
id. On this tenant the SimpleFIN rows carry an EMPTY
``account_number_masked``, so the only place a last-4 survives is inside the
operator-typed account *name* — a string that changes the moment someone
renames an account. Guessing a money surface's account pairing off a display
name is not something this should do, so an unlinked account reports
``unlinked`` and asks for one click instead.

Both sides store signed cents (negative = money out), so amounts compare
directly and a $500 deposit never pairs with a $500 withdrawal.

Statuses
--------
``pending``            not posted at the bank yet; nothing to compare.
``no_amount``          the feed row carries no amount. ``BankFeedTransaction``
                       documents ``amount_cents`` as nullable and tells
                       consumers to filter it out; "we don't know" must not
                       be dressed up as a discrepancy.
``unlinked``           the feed account has no statement account linked.
``ambiguous``          indistinguishable claimants outnumber statement lines.
``matched``            the assigned statement line sits in a CONFIRMED match.
``statement_verified`` a statement line is assigned, not reconciled yet.
``unmatched``          a statement covers this date and no line is available —
                       the feed claims something the statement does not show.
``feed_only``          no imported statement covers this date yet.

``unmatched`` is the only status that asserts a discrepancy, which is why
coverage is checked before it is used: outside a statement period the honest
answer is ``feed_only``, not an accusation. ``ambiguous`` exists because the
opposite failure — absolving falsely — is the more dangerous one on a money
surface.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.modules.bank_feeds.models import BankFeedAccount, BankFeedTransaction
from gdx_dispatch.modules.bank_feeds.statement_models import (
    MATCH_CONFIRMED,
    BankMatch,
    BankMatchLine,
    BankStatementImport,
    BankStatementLine,
)

# The plan's window: a feed posts a day either side of the statement's date.
DATE_WINDOW_DAYS = 1

STATUS_PENDING = "pending"
STATUS_NO_AMOUNT = "no_amount"
STATUS_UNLINKED = "unlinked"
STATUS_AMBIGUOUS = "ambiguous"
STATUS_MATCHED = "matched"
STATUS_STATEMENT_VERIFIED = "statement_verified"
STATUS_UNMATCHED = "unmatched"
STATUS_FEED_ONLY = "feed_only"


def _coverage(db: Session, bank_account_ids: set) -> dict[Any, list[tuple[date, date]]]:
    """Non-voided statement periods per bank account.

    Voiding an import retracts the *claim* that its period was accounted for,
    so its period stops counting as coverage. (Individual lines may survive a
    void when another import also attested them — ``statement_service`` keeps
    those — and a surviving line still pairs. Coverage and line survival are
    separate questions, and this is the coverage one.)
    """
    if not bank_account_ids:
        return {}
    rows = db.execute(
        select(
            BankStatementImport.bank_account_id,
            BankStatementImport.period_start,
            BankStatementImport.period_end,
        ).where(
            BankStatementImport.bank_account_id.in_(bank_account_ids),
            BankStatementImport.voided_at.is_(None),
        )
    ).all()
    out: dict[Any, list[tuple[date, date]]] = {}
    for acct_id, start, end in rows:
        out.setdefault(acct_id, []).append((start, end))
    return out


def _covered(periods: list[tuple[date, date]] | None, when: date) -> bool:
    return bool(periods) and any(s <= when <= e for s, e in periods)


def _solve(
    claimants: list[tuple[date, Any]],
    lines: list[tuple[date, Any]],
) -> tuple[dict[Any, Any], set]:
    """Assign lines to claimants within one ``(account, amount)`` key.

    Returns ``(txn_id -> line_id, ambiguous_txn_ids)``. Both inputs must
    already be sorted, so the result is stable across calls.

    Components are tiny — everything here shares an amount to the cent and
    sits inside a two-day span — so the quadratic edge build is bounded in
    practice by how many identical charges a real account sees in a day.
    """
    edges: dict[Any, list[Any]] = {}
    for c_date, c_id in claimants:
        edges[c_id] = [
            l_id for l_date, l_id in lines
            if abs((l_date - c_date).days) <= DATE_WINDOW_DAYS
        ]

    line_to_claimants: dict[Any, list[Any]] = {}
    for c_id, line_ids in edges.items():
        for l_id in line_ids:
            line_to_claimants.setdefault(l_id, []).append(c_id)

    assigned: dict[Any, Any] = {}
    ambiguous: set = set()
    seen: set = set()

    for _c_date, c_id in claimants:
        if c_id in seen:
            continue
        # Walk the connected component containing this claimant.
        comp_claimants: list[Any] = []
        comp_lines: set = set()
        stack = [c_id]
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            comp_claimants.append(node)
            for l_id in edges.get(node, ()):
                if l_id not in comp_lines:
                    comp_lines.add(l_id)
                    for peer in line_to_claimants.get(l_id, ()):
                        if peer not in seen:
                            stack.append(peer)

        if not comp_lines:
            continue  # nothing to assign; coverage decides these
        if len(comp_claimants) > len(comp_lines):
            # Indistinguishable rows outnumber the evidence. Naming a winner
            # would be a coin flip presented as a bank confirmation.
            ambiguous.update(comp_claimants)
            continue

        order = [c for c in (cid for _d, cid in claimants) if c in set(comp_claimants)]
        used: set = set()
        for cand in order:
            for l_id in edges.get(cand, ()):
                if l_id not in used:
                    used.add(l_id)
                    assigned[cand] = l_id
                    break
            else:
                # Enough lines in the component, but none reachable from this
                # claimant once its neighbours took theirs. Say so rather than
                # calling it a discrepancy.
                ambiguous.add(cand)
    return assigned, ambiguous


def compute_statuses(
    db: Session,
    rows: Iterable[tuple[Any, Any]],
) -> dict[str, dict[str, Any]]:
    """Return ``{feed_txn_id: {...status payload...}}`` for a page of rows.

    ``rows`` is the ``(BankFeedTransaction, BankFeedAccount)`` pairing the
    transactions endpoint already has in hand. Four set-based reads for the
    whole page regardless of its size — no per-row query.
    """
    rows = list(rows)
    if not rows:
        return {}

    linked: dict[Any, Any] = {}
    for _txn, acct in rows:
        bank_account_id = getattr(acct, "bank_account_id", None)
        if bank_account_id is not None:
            linked[acct.id] = bank_account_id

    datable = [
        (t, a)
        for t, a in rows
        if t.posted_date is not None
        and not t.pending
        and t.amount_cents is not None
        and a.id in linked
    ]

    assigned: dict[Any, Any] = {}
    ambiguous: set = set()
    contention: dict[Any, tuple[int, int]] = {}
    confirmed_lines: dict[Any, tuple[Any, str | None]] = {}
    coverage: dict[Any, list[tuple[date, date]]] = {}

    if datable:
        bank_account_ids = {linked[a.id] for _t, a in datable}
        amounts = {t.amount_cents for t, _a in datable}
        dates = [t.posted_date for t, _a in datable]
        window_lo = min(dates) - timedelta(days=DATE_WINDOW_DAYS)
        window_hi = max(dates) + timedelta(days=DATE_WINDOW_DAYS)

        line_rows = db.execute(
            select(
                BankStatementLine.id,
                BankStatementLine.bank_account_id,
                BankStatementLine.amount_cents,
                BankStatementLine.txn_date,
            )
            .where(
                BankStatementLine.bank_account_id.in_(bank_account_ids),
                BankStatementLine.amount_cents.in_(amounts),
                BankStatementLine.txn_date >= window_lo,
                BankStatementLine.txn_date <= window_hi,
            )
            # Deterministic: without this the dict order is the database's
            # return order, and a row could flip between `matched` and
            # `statement_verified` between two loads of the same page.
            .order_by(BankStatementLine.txn_date, BankStatementLine.id)
        ).all()

        lines_by_key: dict[tuple[Any, int], list[tuple[date, Any]]] = {}
        candidate_ids = []
        for line_id, acct_id, amount_cents, txn_date in line_rows:
            lines_by_key.setdefault((acct_id, amount_cents), []).append((txn_date, line_id))
            candidate_ids.append(line_id)

        # Claimants are queried globally rather than read off this page: a
        # status that depended on pagination or the active date filter would
        # be its own kind of lie.
        claim_rows = db.execute(
            select(
                BankFeedTransaction.id,
                BankFeedAccount.bank_account_id,
                BankFeedTransaction.amount_cents,
                BankFeedTransaction.posted_date,
            )
            .join(BankFeedAccount, BankFeedAccount.id == BankFeedTransaction.account_id)
            .where(
                BankFeedAccount.bank_account_id.in_(bank_account_ids),
                BankFeedTransaction.amount_cents.in_(amounts),
                BankFeedTransaction.posted_date >= window_lo,
                BankFeedTransaction.posted_date <= window_hi,
                BankFeedTransaction.pending.is_(False),
                BankFeedTransaction.deleted_at.is_(None),
            )
            .order_by(BankFeedTransaction.posted_date, BankFeedTransaction.id)
        ).all()

        claims_by_key: dict[tuple[Any, int], list[tuple[date, Any]]] = {}
        for txn_id, acct_id, amount_cents, posted in claim_rows:
            claims_by_key.setdefault((acct_id, amount_cents), []).append((posted, txn_id))

        for key, claimants in claims_by_key.items():
            key_lines = lines_by_key.get(key, [])
            key_assigned, key_ambiguous = _solve(claimants, key_lines)
            assigned.update(key_assigned)
            ambiguous.update(key_ambiguous)
            for _d, c_id in claimants:
                contention[c_id] = (len(claimants), len(key_lines))

        if candidate_ids:
            match_rows = db.execute(
                select(BankMatchLine.line_id, BankMatch.id, BankMatch.classification)
                .join(BankMatch, BankMatch.id == BankMatchLine.match_id)
                .where(
                    BankMatchLine.line_id.in_(candidate_ids),
                    BankMatch.status == MATCH_CONFIRMED,
                )
            ).all()
            for line_id, match_id, classification in match_rows:
                confirmed_lines[line_id] = (match_id, classification)

        coverage = _coverage(db, bank_account_ids)

    out: dict[str, dict[str, Any]] = {}
    for txn, acct in rows:
        key = str(txn.id)
        if txn.pending or txn.posted_date is None:
            out[key] = {"match_status": STATUS_PENDING}
            continue
        if txn.amount_cents is None:
            out[key] = {"match_status": STATUS_NO_AMOUNT}
            continue
        bank_account_id = linked.get(acct.id)
        if bank_account_id is None:
            out[key] = {"match_status": STATUS_UNLINKED}
            continue

        if txn.id in ambiguous:
            claimants, lines = contention.get(txn.id, (0, 0))
            out[key] = {
                "match_status": STATUS_AMBIGUOUS,
                "ambiguous_claimants": claimants,
                "ambiguous_lines": lines,
            }
            continue

        paired_line = assigned.get(txn.id)
        if paired_line is None:
            covered = _covered(coverage.get(bank_account_id), txn.posted_date)
            out[key] = {
                "match_status": STATUS_UNMATCHED if covered else STATUS_FEED_ONLY
            }
            continue

        confirmed = confirmed_lines.get(paired_line)
        if confirmed is None:
            out[key] = {
                "match_status": STATUS_STATEMENT_VERIFIED,
                "statement_line_id": str(paired_line),
            }
            continue

        match_id, classification = confirmed
        out[key] = {
            "match_status": STATUS_MATCHED,
            "statement_line_id": str(paired_line),
            "match_id": str(match_id),
            "match_classification": classification,
        }
    return out

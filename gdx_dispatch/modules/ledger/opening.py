"""Evidence-derived opening balances — "figure out a starting balance and
date from the information you have" (Doug, 2026-08-16).

The bank statement archive is the only evidence OF the bank (GL Phase 2
§2.2 — QB's book balance was rejected as a circular anchor). When a
contiguous, balance-chained statement run exists for a bank account, the
run's final ending balance IS that account's opening balance on the first
day after the run — nothing typed, nothing guessed.

Two halves, deliberately split:

- ``propose_opening`` (read-only): per bank account, find the latest
  contiguous chain (each period's beginning equals the prior ending, no
  coverage gap), derive the cutover candidate (the day after the chain
  ends — must be a month boundary, because era membership is BY DATE on
  month-locked periods) and the opening cents; also list any live journal
  entries dated BEFORE that cutover, which contradict the era rule and
  must be reversed by apply.

- ``apply_opening`` (one audited action, ``accounting.close``): reverse
  the pre-cutover live entries (the S10 backfill re-anchors what should
  survive as P8), set ``cutover_month`` (this action is the sanctioned
  writer even though the field is router-locked once entries exist — the
  lock exists to stop casual drift, and THIS is the deliberate
  initialization it was waiting for), map + rename the bank account's GL
  account so the ledger reads as the real account (Doug's other point:
  "the accounts should show up as accounts"), post the opening bank entry
  (debit the bank account's GL account / credit 3950 Opening Balance
  Equity, dated at cutover, content-keyed idempotent), ensure the era
  lock at cutover − 1 day, and stamp the opening-bank attestation with
  the acting user. Re-running is a no-op by construction.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.modules.ledger import service as ledger_service
from gdx_dispatch.modules.ledger.coa import resolve_role_account
from gdx_dispatch.modules.ledger.engine import (
    PostingEvent,
    PostingLine,
    post_for_event,
    reverse_entry,
)
from gdx_dispatch.modules.ledger.models import (
    ENTRY_STATUS_POSTED,
    ROLE_OPENING_EQUITY,
    ROLE_OPERATING_BANK,
    GlJournalEntry,
    GlPeriodLock,
)

log = logging.getLogger(__name__)

# A statement chain link tolerates a small boundary overlap/gap in period
# dating (the real corpus has periods like 02-02→03-01 followed by
# 03-02→03-31) but NEVER a balance discontinuity — the balance equation is
# the actual evidence; dates are labeling.
_MAX_BOUNDARY_GAP_DAYS = 3


class OpeningDerivationError(ValueError):
    """The evidence doesn't support a derivation — surfaced verbatim."""


def _statement_chain(db: Session, bank_account_id) -> list:
    from gdx_dispatch.modules.bank_feeds.statement_models import (
        TIE_OUT_PASSED,
        BankStatementImport,
    )

    # Only tie-out-PASSED imports may anchor an opening balance: the
    # tie-out gate is the parse-integrity verdict (balance equation,
    # recomputed daily balances) — a failed import's numbers are exactly
    # the ones we must not build a book on.
    return list(
        db.scalars(
            select(BankStatementImport)
            .where(
                BankStatementImport.bank_account_id == bank_account_id,
                BankStatementImport.voided_at.is_(None),
                BankStatementImport.tie_out_status == TIE_OUT_PASSED,
            )
            .order_by(
                BankStatementImport.period_start,
                BankStatementImport.period_end,
                BankStatementImport.created_at,
            )
        ).all()
    )


def _latest_contiguous_run(imports: list) -> list:
    """The maximal suffix of the import list whose balances chain: each
    period's beginning equals the previous period's ending, with no
    coverage hole beyond the labeling tolerance."""
    run: list = []
    for imp in imports:
        if run:
            prev = run[-1]
            gap = (imp.period_start - prev.period_end).days
            linked = (
                imp.beginning_balance_cents == prev.ending_balance_cents
                and 0 <= gap <= _MAX_BOUNDARY_GAP_DAYS
            )
            if not linked:
                run = []
        run.append(imp)
    return run


def _month_boundary_after(day: date) -> date | None:
    """day+1 when that lands on the 1st of a month, else None — a cutover
    inside a month would split a lockable period."""
    candidate = day + timedelta(days=1)
    return candidate if candidate.day == 1 else None


def _boundary_candidates(run: list) -> dict:
    """Every month-boundary cutover the chained run can prove, with its
    opening cents. TWO proofs exist (the real bank's periods drift off
    calendar months — July ran 7/1→8/2, which broke the end-only rule):

    - a period ENDING on a month end proves the next day's opening via its
      ending balance;
    - a period STARTING on a month first proves that day's opening via its
      beginning balance.

    Where both prove the same boundary the chain link already guarantees
    they agree (beginning == prior ending). Latest boundary wins later.
    """
    candidates: dict = {}
    for imp in run:
        after = _month_boundary_after(imp.period_end)
        if after is not None:
            candidates[after] = imp.ending_balance_cents
        if imp.period_start.day == 1:
            candidates.setdefault(imp.period_start, imp.beginning_balance_cents)
    return candidates


def _pre_cutover_live_entries(db: Session, company_id: str, cutover: date) -> list:
    return list(
        db.scalars(
            select(GlJournalEntry).where(
                GlJournalEntry.company_id == company_id,
                GlJournalEntry.status == ENTRY_STATUS_POSTED,
                GlJournalEntry.reverses_entry_id.is_(None),
                GlJournalEntry.effective_at < cutover,
            )
        ).all()
    )


def propose_opening(db: Session, company_id: str) -> dict:
    from gdx_dispatch.modules.bank_feeds.statement_models import BankAccount

    settings = ledger_service.get_gl_settings(db, company_id)
    # Deterministic order, checking first: the FIRST usable account claims
    # the Operating Bank role account (gate-audit finding 5 — DB order let
    # a savings account claim it, silently misrouting every payment).
    accounts = sorted(
        db.scalars(select(BankAccount)).all(),
        key=lambda a: (a.kind != "checking", a.name, a.last4),
    )
    if not accounts:
        raise OpeningDerivationError(
            "no bank accounts registered — import a bank statement first"
        )

    staged = []
    for account in accounts:
        imports = _statement_chain(db, account.id)
        entry = {
            "bank_account_id": str(account.id),
            "name": f"{account.name} …{account.last4}",
        }
        if not imports:
            staged.append((entry, None, {"reason": "no statements imported"}))
            continue
        run = _latest_contiguous_run(imports)
        candidates = _boundary_candidates(run)
        if not candidates:
            staged.append((entry, run, {"reason": (
                "no statement in the chain starts on a month first or ends on "
                "a month end — import an adjacent statement"
            )}))
            continue
        staged.append((entry, run, candidates))

    usable = [(e, run, c) for e, run, c in staged if run is not None and "reason" not in c]
    if not usable:
        raise OpeningDerivationError(
            "no bank account has a usable statement chain: "
            + "; ".join(f"{e['name']}: {c['reason']}" for e, _run, c in staged)
        )

    # The cutover must hold for EVERY usable account (one book, one era
    # boundary): intersect each account's provable boundaries, take the
    # latest common one.
    common = set(usable[0][2].keys())
    for _e, _run, candidates in usable[1:]:
        common &= set(candidates.keys())
    if not common:
        detail = "; ".join(
            f"{e['name']}: {', '.join(d.isoformat() for d in sorted(c))}"
            for e, _run, c in usable
        )
        raise OpeningDerivationError(
            f"the accounts' statement chains prove no COMMON month boundary — {detail}"
        )
    cutover = max(common)

    proposals = []
    for entry, run, candidates in staged:
        if run is None or "reason" in candidates:
            proposals.append({**entry, "usable": False, "reason": candidates["reason"]})
            continue
        proposals.append({
            **entry,
            "usable": True,
            "chain_from": run[0].period_start.isoformat(),
            "chain_to": run[-1].period_end.isoformat(),
            "chain_statements": len(run),
            "opening_cents": candidates[cutover],
            "cutover": cutover.isoformat(),
        })
    conflicts = _pre_cutover_live_entries(db, company_id, cutover)
    return {
        "cutover": cutover.isoformat(),
        "accounts": proposals,
        "already_set_cutover": settings.cutover_month.isoformat() if settings and settings.cutover_month else None,
        "entries_to_reverse": [
            {
                "entry_no": e.entry_no,
                "source_type": e.source_type,
                "effective_at": e.effective_at.isoformat(),
            }
            for e in conflicts
        ],
    }


def apply_opening(
    db: Session,
    company_id: str,
    actor: str | None,
    *,
    expected_cutover: date | None = None,
    expected_reversals: int | None = None,
) -> dict:
    """One audited initialization. Safe to re-run: every step is
    existence-checked, content-keyed, or reverses its own stale output.

    ``expected_cutover``/``expected_reversals`` bind the apply to the
    proposal the operator SAW (gate-audit finding 2): a statement imported
    between viewing and clicking changes the derivation, and blind apply
    would silently act on numbers nobody reviewed — refuse instead.
    """
    from gdx_dispatch.core.audit import utcnow
    from gdx_dispatch.modules.bank_feeds.statement_models import BankAccount
    from gdx_dispatch.modules.ledger.models import GlAccount

    proposal = propose_opening(db, company_id)
    cutover = date.fromisoformat(proposal["cutover"])

    if expected_cutover is not None and cutover != expected_cutover:
        raise OpeningDerivationError(
            f"the statement evidence changed since you reviewed it — it now "
            f"derives cutover {cutover.isoformat()}, not "
            f"{expected_cutover.isoformat()}; refresh and review again"
        )
    if expected_reversals is not None and len(proposal["entries_to_reverse"]) != expected_reversals:
        raise OpeningDerivationError(
            f"the set of journal entries to reverse changed since you reviewed "
            f"it ({len(proposal['entries_to_reverse'])} now, {expected_reversals} "
            "when you looked) — refresh and review again"
        )

    settings = ledger_service.ensure_gl_seed(db, company_id)
    if settings.cutover_month is not None and settings.cutover_month != cutover:
        raise OpeningDerivationError(
            f"cutover_month is already {settings.cutover_month.isoformat()} — it "
            f"cannot move to {cutover.isoformat()} under a live book"
        )

    # 1. Reverse era-contradicting live entries FIRST. override_lock so a
    # re-run after the era lock exists cannot 500 (reverse_entry self-audits
    # locked-period postings).
    reversed_entries = []
    for entry in _pre_cutover_live_entries(db, company_id, cutover):
        reverse_entry(db, entry, created_by=actor, override_lock=True)
        reversed_entries.append(entry.entry_no)

    # 2. The policy date.
    settings.cutover_month = cutover

    # 3. Bank accounts SHOW UP AS ACCOUNTS: the first usable account (a
    # checking account, by the proposal's deterministic ordering) claims
    # the Operating Bank role account; additional accounts get their own
    # asset accounts (1010, 1011, …). An ALREADY-mapPED account keeps its
    # GL account and its name — re-apply never clobbers operator renames
    # (gate-audit finding 8).
    operating = resolve_role_account(db, company_id, ROLE_OPERATING_BANK)
    postings = []
    stale_reversed = []
    next_code = 1010
    from uuid import UUID as _UUID

    for idx, acct_prop in enumerate(p for p in proposal["accounts"] if p["usable"]):
        bank_account = db.get(BankAccount, _UUID(acct_prop["bank_account_id"]))
        if bank_account.gl_account_id is not None:
            gl_account = db.get(GlAccount, bank_account.gl_account_id)
        elif idx == 0:
            gl_account = operating
            gl_account.name = acct_prop["name"]
        else:
            existing = db.scalars(select(GlAccount).where(
                GlAccount.company_id == company_id,
                GlAccount.name == acct_prop["name"],
            )).first()
            if existing is None:
                while db.scalars(select(GlAccount).where(
                        GlAccount.company_id == company_id,
                        GlAccount.code == str(next_code))).first() is not None:
                    next_code += 1
                existing = GlAccount(
                    company_id=company_id, code=str(next_code),
                    name=acct_prop["name"], type="asset", active=True,
                )
                db.add(existing)
                db.flush()
            gl_account = existing
        bank_account.gl_account_id = gl_account.id

        # 4. The opening entry: debit the account, credit Opening Balance
        # Equity, dated at cutover. Content-keyed → identical re-runs land
        # on the same entry; a CHANGED archive (corrected import) posts the
        # new number and the stale opening entry is reversed below —
        # without this, corrected evidence would STACK opening entries and
        # overstate the bank (gate-audit finding 1).
        cents = int(acct_prop["opening_cents"])
        fresh_entry = None
        if cents != 0:
            fresh_entry = post_for_event(
                db,
                PostingEvent(
                    company_id=company_id,
                    source_type="bank_account",
                    source_id=str(bank_account.id),
                    event="opening",
                    effective_at=cutover,
                    lines=(
                        PostingLine(amount_cents=cents, account_id=gl_account.id,
                                    memo=f"opening balance per statement chain through {acct_prop['chain_to']}"),
                        PostingLine(amount_cents=-cents, role=ROLE_OPENING_EQUITY,
                                    memo=f"opening balance {acct_prop['name']}"),
                    ),
                    created_by=actor,
                ),
            )
            postings.append({"account": acct_prop["name"], "cents": cents,
                             "entry_no": fresh_entry.entry_no})
        for stale in db.scalars(select(GlJournalEntry).where(
                GlJournalEntry.company_id == company_id,
                GlJournalEntry.source_type == "bank_account",
                GlJournalEntry.source_id == str(bank_account.id),
                GlJournalEntry.status == ENTRY_STATUS_POSTED,
                GlJournalEntry.reverses_entry_id.is_(None),
        )).all():
            if fresh_entry is not None and stale.id == fresh_entry.id:
                continue
            reverse_entry(db, stale, created_by=actor, override_lock=True)
            stale_reversed.append(stale.entry_no)

    # 5. Era lock at cutover − 1: nothing back-posts into the pre-ledger era.
    lock_date = cutover - timedelta(days=1)
    if db.scalars(select(GlPeriodLock).where(
            GlPeriodLock.company_id == company_id,
            GlPeriodLock.lock_date == lock_date)).first() is None:
        db.add(GlPeriodLock(company_id=company_id, lock_date=lock_date))

    # 6. The attestation stamp — evidence-derived, actor-attributed; only
    # stamped fresh when absent (a re-run must not restamp history).
    if settings.opening_bank_attested_at is None:
        settings.opening_bank_attested_at = utcnow()
        settings.opening_bank_attested_by = (actor or "system")[:36]

    # 7. The other half of opening: pre-cutover OPEN invoices anchor as
    # opening receivables (P8) — without this, ledger AR sits understated
    # from apply until someone remembers the backfill (gate-audit
    # finding 7). run_opening is idempotent by construction.
    from gdx_dispatch.modules.ledger.backfill import run_opening

    ar_phase = run_opening(db, company_id)

    db.flush()
    return {
        "cutover": cutover.isoformat(),
        "reversed_entry_nos": reversed_entries,
        "stale_opening_reversed": stale_reversed,
        "opening_entries": postings,
        "lock_date": lock_date.isoformat(),
        "ar_anchors": {
            "posted": ar_phase.posted,
            "skipped": ar_phase.skipped,
            "entries_created": ar_phase.entries_created,
            "locked": len(ar_phase.locked),
            "refused": len(ar_phase.refused),
        },
    }

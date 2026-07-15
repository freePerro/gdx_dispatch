"""Posting engine core (S3, spec §5.6 / §3.6 / §8).

``post_for_event`` and ``reverse_entry`` are the only two ways a journal
entry is ever born. Both are synchronous, in-transaction, and **never
commit** — the caller's operational transaction owns atomicity, so the
operational write and its ledger entry land or roll back together.

Isolated in S3: nothing operational calls this yet. The chokepoint (S4)
wires ``transition_invoice_status`` → posting rules → these two functions,
gated on ``ledger_posting_enabled``.

Two contracts callers (S4+) must honor:

- **Replay is state-derived, never history-derived.** seq counts *reversed*
  entries, so replaying an event whose live entry was reversed REPOSTS it —
  that is the A→B→A edit semantics working as designed. It also means a
  reversal used as a VOID is undone by naive replay: backfill/replay tooling
  must derive events from CURRENT source state (a voided invoice generates no
  "issued" event), exactly how tools/gl_backfill.py (S10) is specified.
- **Keep the enclosing transaction short.** A concurrent duplicate post
  blocks on the winner's uncommitted unique key while holding the caller's
  operational row locks; long transactions turn that into stalls or
  deadlock kills. Single-tenant traffic makes this rare, not impossible.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import AuditLog
from gdx_dispatch.modules.ledger.coa import LedgerConfigError, resolve_role_account
from gdx_dispatch.modules.ledger.keys import (
    compute_seq,
    content_hash,
    idempotency_key,
    key_prefix,
    reversal_key,
)
from gdx_dispatch.modules.ledger.models import (
    ENTRY_STATUS_POSTED,
    ENTRY_STATUS_REVERSED,
    GlAccount,
    GlJournalEntry,
    GlJournalLine,
    GlPeriodLock,
)

# Retries for the reversed-key-collision race (§5.6): recompute seq and try
# again. Bounded — a livelock here means something is reversing entries as
# fast as we post them, which is a bug to surface, not absorb.
_MAX_KEY_ATTEMPTS = 5


class UnbalancedEntryError(ValueError):
    """Lines don't satisfy the balance invariant. Raised in Python BEFORE the
    DB trigger so the caller gets a clear error inside its transaction, not a
    deferred-constraint explosion at commit."""


class PeriodLockedError(RuntimeError):
    """effective_at falls on or before the latest period lock and the caller
    did not assert the accounting.close override."""


@dataclass(frozen=True)
class PostingLine:
    """One leg of an event. Exactly one of ``role`` / ``account_id`` —
    posting rules speak roles (guiding rule 4); explicit account ids exist
    for data-driven maps (expense categories) and tests."""

    amount_cents: int
    role: str | None = None
    account_id: UUID | None = None
    memo: str | None = None
    job_id: UUID | None = None
    customer_id: UUID | None = None


@dataclass(frozen=True)
class PostingEvent:
    """A source-system fact the ledger should record. ``source_type`` /
    ``source_id`` / ``event`` identify WHAT happened; the lines carry the
    money; ``extra_content`` joins the lines in the idempotency hash for
    economic content the lines alone don't capture (rare)."""

    company_id: str
    source_type: str
    source_id: str
    event: str
    effective_at: date
    lines: tuple[PostingLine, ...]
    created_by: str | None = None
    extra_content: dict | None = field(default=None)


def _validate_balance(lines: tuple[PostingLine, ...]) -> None:
    if len(lines) < 2:
        raise UnbalancedEntryError("an entry needs at least 2 lines")
    total = sum(line.amount_cents for line in lines)
    if total != 0:
        raise UnbalancedEntryError(f"lines sum to {total} cents, not 0")
    if not any(line.amount_cents > 0 for line in lines) or not any(
        line.amount_cents < 0 for line in lines
    ):
        raise UnbalancedEntryError("an entry needs at least one debit and one credit")
    for line in lines:
        if line.amount_cents == 0:
            raise UnbalancedEntryError("zero-amount line")
        if not isinstance(line.amount_cents, int) or isinstance(line.amount_cents, bool):
            raise UnbalancedEntryError("amount_cents must be int")
        if (line.role is None) == (line.account_id is None):
            raise UnbalancedEntryError("each line needs exactly one of role / account_id")


def _resolve_lines(
    session: Session, company_id: str, lines: tuple[PostingLine, ...]
) -> list[tuple[PostingLine, UUID]]:
    resolved = []
    for line in lines:
        if line.role is not None:
            account = resolve_role_account(session, company_id, line.role)
            account_id = account.id
        else:
            account = session.get(GlAccount, line.account_id)
            if account is None or account.company_id != company_id:
                raise LedgerConfigError(f"account {line.account_id} not found")
            if not account.active:
                raise LedgerConfigError(
                    f"account {account.code} {account.name} is deactivated"
                )
            account_id = account.id
        resolved.append((line, account_id))
    return resolved


def _check_period_lock(
    session: Session,
    company_id: str,
    effective_at: date,
    *,
    override_lock: bool,
    created_by: str | None,
    context: str,
) -> None:
    """Hard block on effective_at <= latest lock_date (§3.6). Overriding is
    the accounting.close path — the CALLER enforces the capability; the
    engine records every override (`gl_posted_into_locked_period`)."""
    lock_date = session.scalar(
        select(func.max(GlPeriodLock.lock_date)).where(
            GlPeriodLock.company_id == company_id
        )
    )
    if lock_date is None or effective_at > lock_date:
        return
    if not override_lock:
        raise PeriodLockedError(
            f"period locked through {lock_date}; {effective_at} is not postable "
            "(post to the first open day with a memo naming the true date, or "
            "override with accounting.close)"
        )
    session.add(
        AuditLog(
            tenant_id=company_id,
            user_id=created_by,
            action="gl_posted_into_locked_period",
            entity_type="gl_journal_entry",
            details={"effective_at": str(effective_at), "lock_date": str(lock_date), "context": context},
        )
    )


def _next_entry_no(session: Session) -> int | None:
    """Postgres draws from the SEQUENCE; SQLite (tests) needs an explicit
    value (max+1 — single-writer test sessions only)."""
    if session.get_bind().dialect.name == "postgresql":
        return None
    current = session.scalar(select(func.max(GlJournalEntry.entry_no)))
    return (current or 0) + 1


def _current_txid(session: Session) -> int | None:
    """The sealing anchor (§3.5): lines only insert in the transaction that
    created their entry. PG-only; the trigger doesn't exist on SQLite."""
    if session.get_bind().dialect.name == "postgresql":
        return session.execute(text("SELECT txid_current()")).scalar()
    return None


def _entry_by_key(session: Session, company_id: str, key: str) -> GlJournalEntry | None:
    return session.scalars(
        select(GlJournalEntry).where(
            GlJournalEntry.company_id == company_id,
            GlJournalEntry.idempotency_key == key,
        )
    ).first()


def _insert_entry(
    session: Session,
    *,
    company_id: str,
    effective_at: date,
    source_type: str | None,
    source_id: str | None,
    key: str,
    reverses_entry_id: UUID | None,
    created_by: str | None,
    resolved_lines: list[tuple[PostingLine, UUID]],
) -> GlJournalEntry:
    entry = GlJournalEntry(
        effective_at=effective_at,
        source_type=source_type,
        source_id=source_id,
        idempotency_key=key,
        reverses_entry_id=reverses_entry_id,
        created_txid=_current_txid(session),
        created_by=created_by,
        company_id=company_id,
    )
    entry_no = _next_entry_no(session)
    if entry_no is not None:
        entry.entry_no = entry_no
    session.add(entry)
    for line, account_id in resolved_lines:
        session.add(
            GlJournalLine(
                entry=entry,
                account_id=account_id,
                amount_cents=line.amount_cents,
                memo=line.memo,
                job_id=line.job_id,
                customer_id=line.customer_id,
            )
        )
    return entry


def post_for_event(
    session: Session, event: PostingEvent, *, override_lock: bool = False
) -> GlJournalEntry:
    """Post one balanced entry for ``event``. Idempotent: replaying the same
    economic content returns the existing live entry instead of double-
    posting. Never commits; flushes inside a SAVEPOINT so a key collision
    can't abort the caller's transaction.
    """
    _validate_balance(event.lines)
    resolved = _resolve_lines(session, event.company_id, event.lines)
    _check_period_lock(
        session,
        event.company_id,
        event.effective_at,
        override_lock=override_lock,
        created_by=event.created_by,
        context=f"{event.source_type}:{event.source_id}:{event.event}",
    )

    chash = content_hash(
        {
            "effective_at": event.effective_at.isoformat(),
            "lines": [
                {
                    "account_id": str(account_id),
                    "amount_cents": line.amount_cents,
                    "job_id": str(line.job_id) if line.job_id else None,
                    "customer_id": str(line.customer_id) if line.customer_id else None,
                }
                for line, account_id in resolved
            ],
            **(event.extra_content or {}),
        }
    )
    prefix = key_prefix(event.source_type, event.source_id, event.event, chash)

    for _ in range(_MAX_KEY_ATTEMPTS):
        key = idempotency_key(prefix, compute_seq(session, event.company_id, prefix))
        existing = _entry_by_key(session, event.company_id, key)
        if existing is not None and existing.status == ENTRY_STATUS_POSTED:
            return existing  # idempotent success — same content already live
        try:
            with session.begin_nested():
                entry = _insert_entry(
                    session,
                    company_id=event.company_id,
                    effective_at=event.effective_at,
                    source_type=event.source_type,
                    source_id=event.source_id,
                    key=key,
                    reverses_entry_id=None,
                    created_by=event.created_by,
                    resolved_lines=resolved,
                )
            return entry
        except IntegrityError:
            # Lost a race on the unique key. posted → return it; reversed →
            # seq moved under us, recompute and retry.
            collided = _entry_by_key(session, event.company_id, key)
            if collided is None:
                raise  # not a key collision — surface the real error
            if collided.status == ENTRY_STATUS_POSTED:
                return collided
    raise RuntimeError(
        f"could not settle idempotency key for {prefix} after "
        f"{_MAX_KEY_ATTEMPTS} attempts — entries are being reversed concurrently"
    )


def reverse_entry(
    session: Session,
    entry: GlJournalEntry,
    *,
    effective_at: date | None = None,
    created_by: str | None = None,
    override_lock: bool = False,
) -> GlJournalEntry:
    """Reverse a posted entry: mirror entry with negated lines, original
    marked ``reversed`` (the trigger's one permitted transition). Idempotent —
    reversing an already-reversed entry returns the existing reversal.
    ``effective_at`` defaults to the original's date; pass the current period
    date when unwinding into a closed-period situation instead of overriding.
    """
    if entry.status == ENTRY_STATUS_REVERSED:
        existing = session.get(GlJournalEntry, entry.reversed_by_entry_id)
        if existing is None:
            raise LedgerConfigError(
                f"entry {entry.id} is reversed but its reversal is missing"
            )
        return existing

    when = effective_at or entry.effective_at
    _check_period_lock(
        session,
        entry.company_id,
        when,
        override_lock=override_lock,
        created_by=created_by,
        context=f"reversal:{entry.id}",
    )

    lines = session.scalars(
        select(GlJournalLine).where(GlJournalLine.entry_id == entry.id)
    ).all()
    mirrored = [
        (
            PostingLine(
                amount_cents=-line.amount_cents,
                account_id=line.account_id,
                memo=f"reversal of entry #{entry.entry_no}",
                job_id=line.job_id,
                customer_id=line.customer_id,
            ),
            line.account_id,
        )
        for line in lines
    ]

    try:
        with session.begin_nested():
            reversal = _insert_entry(
                session,
                company_id=entry.company_id,
                effective_at=when,
                source_type=entry.source_type,
                source_id=entry.source_id,
                key=reversal_key(entry.id),
                reverses_entry_id=entry.id,
                created_by=created_by,
                resolved_lines=mirrored,
            )
    except IntegrityError:
        existing = _entry_by_key(session, entry.company_id, reversal_key(entry.id))
        if existing is None:
            raise
        return existing

    entry.status = ENTRY_STATUS_REVERSED
    entry.reversed_by_entry_id = reversal.id
    session.flush()
    return reversal

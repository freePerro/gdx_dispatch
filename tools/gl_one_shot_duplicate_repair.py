#!/usr/bin/env python3
"""Reverse duplicated one-shot GL postings that hold AR no invoice owes (GDXA-1238).

A one-shot posting records something that happened once: the P8 opening
anchor of a pre-cutover invoice, the compensator for an opening-era payment
void, a credit memo, a refund, a credit application. Their posters run again
on every replay and recalc, and the engine keys an entry by its CONTENT. Until
GDXA-1238, a re-run after an input moved minted a second live entry beside the
first and reversed neither: a pre-cutover invoice whose total or job changed
and was recalculated carried two anchors, so the AR control held the
invoice's opening balance twice. The posting rules now return the live entry
instead (``rules._live_event_entry``). This tool repairs entries posted
before that.

What this writes: for each (source row, event) with more than one live entry,
the EARLIEST is kept (the one the posting rules now return) and every later
one is reversed through the ledger engine, but only when the invoice's live
drift (ledger AR less what the row owes) still holds that AR. A later entry
computed from the ledger balance can have absorbed a duplicate already (a void
of a pre-cutover invoice clears whatever AR the ledger shows, duplicates
included); reversing it then would push the invoice the other way. Those
groups, and duplicates that hold no AR, are printed as HELD and not touched. Each reversal is a new mirror entry
(key ``reversal:<id>``); the duplicate flips ``posted`` -> ``reversed``, the
one transition the ledger triggers allow. No journal line is edited or
deleted, and no invoice, payment or adjustment row is touched. Every reversal
gets an audit row (``gl_one_shot_duplicate_reversed``) naming the operator,
the kept entry, the duplicate, its reversal and the AR it held. Re-runnable: a
reversed duplicate is no longer live and is not selected twice.

Reversals are dated at each duplicate's own date. A group dated into a locked
period is skipped and named; re-run with ``--effective-date`` set to the first
open day to correct it there instead.

Usage (inside the app container)::

    python tools/gl_one_shot_duplicate_repair.py                       # dry-run
    python tools/gl_one_shot_duplicate_repair.py --apply --operator doug
    python tools/gl_one_shot_duplicate_repair.py --apply --operator doug \\
        --effective-date 2026-10-01                                    # locked era

Rollback: every reversal entry id is printed and audited
(``details.reversal_entry_id``). Reversing a reversal restores the duplicate's
lines to the books, through the same engine::

    python tools/gl_one_shot_duplicate_repair.py --rollback <reversal_entry_id> ... \\
        --operator doug

The duplicate stays marked ``reversed`` (the triggers forbid un-reversing);
its money is back through the reversal-of-reversal, which is audited as
``gl_one_shot_duplicate_restored``.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gdx_dispatch.core.audit import ensure_audit_table, log_audit_event_sync  # noqa: E402
from gdx_dispatch.core.database import SessionLocal  # noqa: E402
from gdx_dispatch.core.tenant import company_id  # noqa: E402
from gdx_dispatch.modules.ledger import backfill  # noqa: E402
from gdx_dispatch.modules.ledger.engine import reverse_entry  # noqa: E402
from gdx_dispatch.modules.ledger.models import (  # noqa: E402
    ENTRY_STATUS_POSTED,
    GlJournalEntry,
)

# gl_journal_entries.created_by is String(36): "cli:" + the operator.
_MAX_OPERATOR = 32


def _dollars(cents: int) -> str:
    return f"{cents / 100:,.2f}"


def _actor(operator: str) -> str:
    operator = operator.strip()
    if not operator:
        raise ValueError("an operator is required")
    if len(operator) > _MAX_OPERATOR:
        raise ValueError(f"operator must be at most {_MAX_OPERATOR} characters")
    return f"cli:{operator}"


def print_plan(groups: list[dict]) -> None:
    if not groups:
        print("No one-shot posting has a duplicate live entry. Nothing to do.")
        return
    print(f"\n{len(groups)} one-shot posting(s) with duplicate live entries:\n")
    print(f"  {'invoice':16} {'event':22} {'kept':>6} {'duplicates':14} {'AR held':>12}")
    for g in groups:
        dups = ",".join(f"#{n}" for n in g["duplicate_entry_nos"])
        print(
            f"  {(g['invoice_number'] or '-'):16} {g['event']:22} "
            f"#{g['keeper_entry_no']:>5} {dups:14} {_dollars(g['ar_cents']):>12}"
            + ("" if g["reversible"] else f"  HELD: {g['hold_reason']}")
        )
    fixable = sum(g["ar_cents"] for g in groups if g["reversible"])
    print(f"\n  AR the reversible duplicates still hold: {_dollars(fixable)}")
    held = [g for g in groups if not g["reversible"]]
    if held:
        print(f"  {len(held)} group(s) HELD and left alone: review those by hand.")


def apply_repair(db, *, operator: str, effective_at: date | None = None) -> backfill.PhaseResult:
    """Reverse every duplicate and audit each reversal. Commits."""
    actor = _actor(operator)
    tenant = company_id()
    ensure_audit_table(db)
    result = backfill.reverse_duplicate_one_shot_entries(
        db, tenant, actor=actor, effective_at=effective_at
    )
    for row in result.rows:
        for dup_id, rev_id in zip(row["duplicate_entry_ids"], row["reversal_entry_ids"], strict=True):
            log_audit_event_sync(
                db, tenant_id=tenant, user_id=actor,
                action="gl_one_shot_duplicate_reversed",
                entity_type="gl_journal_entry", entity_id=dup_id,
                details={
                    "source_type": row["source_type"],
                    "source_id": row["source_id"],
                    "event": row["event"],
                    "invoice_id": row["invoice_id"],
                    "invoice_number": row["invoice_number"],
                    "kept_entry_id": row["keeper_entry_id"],
                    "duplicate_entry_id": dup_id,
                    # The rollback handle: reverse this entry to restore.
                    "reversal_entry_id": rev_id,
                    "reversed_on": row["reversed_on"],
                    "group_ar_cents": row["ar_cents"],
                    "reason": "a one-shot posting re-run minted a second live entry (GDXA-1238)",
                },
            )
    db.commit()
    return result


def rollback(db, reversal_ids: list[str], *, operator: str) -> list[str]:
    """Reverse the given reversal entries (this tool's own only). Commits."""
    actor = _actor(operator)
    tenant = company_id()
    ensure_audit_table(db)
    restored = []
    for rid in reversal_ids:
        rev = db.get(GlJournalEntry, UUID(rid))
        original = (
            db.get(GlJournalEntry, rev.reverses_entry_id)
            if rev is not None and rev.reverses_entry_id is not None
            else None
        )
        parts = (original.idempotency_key or "").split(":") if original else []
        if (
            original is None
            or rev.status != ENTRY_STATUS_POSTED
            or not (rev.created_by or "").startswith("cli:")
            or len(parts) != 5
            or (original.source_type, parts[2]) not in backfill.ONE_SHOT_EVENTS
        ):
            raise ValueError(f"{rid} is not a live reversal of a one-shot posting by this tool")
        back = reverse_entry(db, rev, created_by=actor)
        restored.append(str(back.id))
        log_audit_event_sync(
            db, tenant_id=tenant, user_id=actor,
            action="gl_one_shot_duplicate_restored",
            entity_type="gl_journal_entry", entity_id=str(rev.reverses_entry_id),
            details={
                "reversal_entry_id": rid,
                "restoring_entry_id": str(back.id),
                "reason": "rollback of gl_one_shot_duplicate_reversed (GDXA-1238)",
            },
        )
    db.commit()
    return restored


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--apply", action="store_true", help="write the plan (default: dry-run)")
    ap.add_argument("--operator", default="", help="who is running this (required to write; audited)")
    ap.add_argument("--effective-date", type=date.fromisoformat, default=None,
                    help="date every reversal here (a locked period); default: each duplicate's own date")
    ap.add_argument("--rollback", nargs="+", metavar="REVERSAL_ENTRY_ID",
                    help="reverse these reversal entries, restoring the duplicates")
    args = ap.parse_args()

    if (args.apply or args.rollback) and not args.operator.strip():
        ap.error("--apply and --rollback require --operator")

    db = SessionLocal()
    try:
        if args.rollback:
            restored = rollback(db, args.rollback, operator=args.operator)
            print(f"Restored {len(restored)} duplicate(s): {', '.join(restored)}")
            return 0

        groups = backfill.duplicate_one_shot_entries(db, company_id())
        print_plan(groups)
        if not groups:
            return 0
        if not args.apply:
            print("\nDry run — nothing written. Re-run with --apply --operator <you>.")
            return 0

        result = apply_repair(db, operator=args.operator, effective_at=args.effective_date)
        print(f"\nApplied: {result.entries_created} duplicate(s) reversed in "
              f"{result.posted} group(s). Audit rows: gl_one_shot_duplicate_reversed.")
        for row in result.rows:
            print(f"  {row['invoice_number'] or row['source_id']} {row['event']}: "
                  f"reversal entries {', '.join(row['reversal_entry_ids'])}")
        for msg in result.locked:
            print(f"  LOCKED, not reversed: {msg}")
        for msg in result.refused:
            print(f"  HELD, not reversed: {msg}")
        return 1 if result.locked else 0
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())

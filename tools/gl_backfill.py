#!/usr/bin/env python3
"""GL cutover backfill CLI (S10, spec §5.7 / plan §S10). Re-runnable.

Rollout order (§11):

    # 1. before flipping the flag: post P8 anchors + era lock, then hand-check
    python tools/gl_backfill.py --phase opening
    python tools/gl_backfill.py --phase report        # compare vs QBO aging

    # 2. flip the flag on /accounting-settings, then replay from cutover
    python tools/gl_backfill.py --phase replay
    python tools/gl_backfill.py --phase report

Every phase is idempotent (content-keyed posting; existence-checked lock).
``--dry-run`` rolls the session back instead of committing — the full
computation runs and prints, nothing persists. ``--csv PATH`` writes the
report/opening rows for the QBO aging hand-check.
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gdx_dispatch.core.database import SessionLocal  # noqa: E402
from gdx_dispatch.core.tenant import company_id  # noqa: E402
from gdx_dispatch.modules.ledger import backfill  # noqa: E402
from gdx_dispatch.modules.ledger.coa import LedgerConfigError  # noqa: E402


def _dollars(cents: int) -> str:
    return f"{cents / 100:,.2f}"


def _print_phase(name: str, result: backfill.PhaseResult) -> None:
    print(
        f"\n== {name}: {result.posted} processed, {result.skipped} skipped, "
        f"{result.entries_created} new journal entries =="
    )
    for row in result.rows:
        if "opening_cents" in row:
            mark = "POSTED " if row.get("posted") else "skip   "
            over = "  [OVERPAID at cutover — customer credit stays QBO-era, hand-review]" if row.get("overpaid_at_cutover") else ""
            print(
                f"  {mark} {row['invoice_number']:<20} {row['effective_at']}  "
                f"${_dollars(row['opening_cents'])}{over}"
            )
        else:
            print(f"  {row}")
    if result.locked:
        print(f"\n  ⚠ {len(result.locked)} event(s) refused by the period lock — these are")
        print("    dated into the closed pre-cutover era; review each (fix the business")
        print("    date, or post with accounting.close) and re-run:")
        for line in result.locked:
            print(f"    - {line}")
    if result.refused:
        print(f"\n  ⚠ {len(result.refused)} event(s) won't reconcile (data problems) — fix and re-run:")
        for line in result.refused:
            print(f"    - {line}")


def _print_report(report: dict) -> None:
    totals = report["totals"]
    print("\n== AR reconciliation (compare per-invoice vs QBO aging) ==")
    print(f"  open invoices with AR:            {len(report['rows'])}")
    print(f"  operational AR (Σ balance_due):   ${_dollars(totals['operational_ar_cents'])}")
    print(f"  GL AR attributed to invoices:     ${_dollars(totals['gl_attributed_ar_cents'])}")
    print(f"  GL AR account balance:            ${_dollars(totals['gl_ar_account_cents'])}")
    if report["mismatches"]:
        print(f"\n  ⚠ {len(report['mismatches'])} invoice(s) diverge (operational − GL):")
        for row in report["mismatches"]:
            print(
                f"    {row['invoice_number']:<20} op ${_dollars(row['operational_cents'])}  "
                f"gl ${_dollars(row['gl_cents'])}  Δ ${_dollars(row['delta_cents'])}"
                f"{'  [P8]' if row['anchored'] else ''}"
            )
    else:
        print("  ✓ every open invoice's GL AR matches its operational balance")
    suspects = report.get("legacy_credit_suspects") or []
    if suspects:
        print(f"\n  ⚠ {len(suspects)} invoice(s) carry legacy amount_paid values — pre-GL")
        print("    credit memos lived there and are invisible to BOTH sides of this")
        print("    report (delta reads zero). Hand-check these against QBO aging:")
        for row in suspects:
            print(f"    - {row['invoice_number']}: amount_paid ${_dollars(row['amount_paid_cents'])}")


def _write_csv(path: str, rows: list[dict]) -> None:
    if not rows:
        print(f"(no rows to write to {path})")
        return
    keys = sorted({k for row in rows for k in row})
    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {len(rows)} rows → {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--phase",
        choices=("opening", "replay", "report", "all"),
        default="report",
        help="which phase to run (default: report — read-only)",
    )
    parser.add_argument("--dry-run", action="store_true", help="compute + print, roll back instead of committing")
    parser.add_argument("--csv", metavar="PATH", help="also write the phase rows / report rows as CSV")
    args = parser.parse_args()

    session = SessionLocal()
    company = company_id()
    csv_rows: list[dict] = []

    def _settle_phase() -> None:
        # Commit (or roll back) each phase on its own — a later phase's
        # refusal must never undo an earlier phase's committed work.
        if args.dry_run:
            session.rollback()
        else:
            session.commit()

    exit_code = 0
    try:
        if args.phase in ("opening", "all"):
            result = backfill.run_opening(session, company)
            _print_phase("opening (P8)", result)
            csv_rows.extend(result.rows)
            _settle_phase()
        if args.phase in ("replay", "all"):
            try:
                result = backfill.run_replay(session, company)
                _print_phase("replay", result)
                _settle_phase()
            except LedgerConfigError as exc:
                session.rollback()
                print(f"\nreplay not run: {exc}", file=sys.stderr)
                exit_code = 2
        if args.phase in ("report", "all"):
            report = backfill.reconciliation_report(session, company)
            _print_report(report)
            session.rollback()  # report is read-only
            if not csv_rows:
                csv_rows = report["rows"]
        if args.dry_run:
            print("\n(dry run — rolled back, nothing persisted)")
        if args.csv:
            _write_csv(args.csv, csv_rows)
        return exit_code
    except LedgerConfigError as exc:
        session.rollback()
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 2
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())

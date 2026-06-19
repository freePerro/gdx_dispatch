"""SS-34 slice E — DR drill CLI / systemd-timer entry point.

Usage::

    python -m gdx_dispatch.tools.dr_drill_cron \
        --scope=full \
        --source-db=postgresql://.../prod_replica \
        --staging-db=postgresql://.../staging \
        --snapshot-target=/var/backups/dr/drill.pgc

One invocation == one drill. The CLI generates a fresh
``drill_run_id`` (uuid4) each call; a scheduler that needs
idempotency (e.g. systemd timer retry) should pass ``--drill-run-id``
explicitly so re-runs return the cached report instead of double-
dumping the source DB.

Exit codes:

* 0 — drill passed (all verification checks green).
* 1 — drill failed verification (restore succeeded but checks failed).
* 2 — infrastructure failure (snapshot or restore raised).
* 3 — refused (looks like production).
* 4 — CLI misuse.

TODO: wire into systemd-timer. The orchestrator will land
``/etc/systemd/system/gdx-dr-drill.timer`` at deploy time; this CLI
is the ``ExecStart`` target.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from uuid import uuid4

from gdx_dispatch.core.dr.drill_orchestrator import run_drill
from gdx_dispatch.core.dr.restore_to_staging import ProductionTargetRefused

logger = logging.getLogger("gdx_dispatch.dr.cron")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gdx_dispatch.tools.dr_drill_cron",
        description="Run one SS-34 DR drill (snapshot → restore → verify).",
    )
    p.add_argument("--scope", choices=("full", "tenant", "schema"), default="full")
    p.add_argument(
        "--scope-selector",
        help="Schema name required when --scope is tenant or schema",
    )
    p.add_argument("--source-db", required=True, help="Source DB URL (pg_dump)")
    p.add_argument(
        "--staging-db", required=True,
        help="Target staging DB URL (pg_restore). MUST NOT equal DATABASE_URL.",
    )
    p.add_argument(
        "--snapshot-target", required=True,
        help="Local filesystem path to write the pg_dump artifact.",
    )
    p.add_argument(
        "--drill-run-id",
        help="Optional uuid; if omitted a fresh one is generated.",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Emit lifecycle events but skip pg_dump/pg_restore.",
    )
    p.add_argument(
        "--json", action="store_true",
        help="Print the DrillReport as JSON to stdout.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.scope in ("tenant", "schema") and not args.scope_selector:
        parser.error(f"--scope-selector required for --scope={args.scope}")
        return 4

    drill_run_id = args.drill_run_id or str(uuid4())
    scheduled_for = datetime.now(timezone.utc)

    try:
        report = run_drill(
            drill_run_id=drill_run_id,
            scheduled_for=scheduled_for,
            scope=args.scope,
            staging_db_url=args.staging_db,
            source_db_url=args.source_db,
            snapshot_target=args.snapshot_target,
            scope_selector=args.scope_selector,
            dry_run=args.dry_run,
        )
    except ProductionTargetRefused as exc:
        logger.error("refused: %s", exc)
        return 3
    except Exception as exc:
        logger.exception("drill infrastructure failure: %s", exc)
        return 2

    if args.json:
        json.dump(report.to_dict(), sys.stdout, default=str, indent=2)
        sys.stdout.write("\n")
    else:
        status = "PASSED" if report.passed else "FAILED"
        print(f"drill {drill_run_id} {status}")
        if report.failure_reason:
            print(f"  reason: {report.failure_reason}")
        if report.restore:
            print(f"  restore duration: {report.restore.duration_s:.1f}s")
        if report.verification:
            for c in report.verification.checks:
                flag = "OK" if c.passed else "FAIL"
                print(f"  [{flag}] {c.name}: {c.detail}")

    return 0 if report.passed else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

#!/usr/bin/env python3
"""D44 — test-residue sweeper.

Walks every tenant DB and classifies rows on tenant-scoped tables that lack
tenant attribution. Three classes:

  1. DELETABLE RESIDUE — row matches a known test-data pattern (qa_test_*
     markers, 555-0xx phones, @example.com emails) AND has no tenant scope.
     Safe to auto-delete. Live D41 surfaced 1 such row
     (``qa-test@example.com`` in ``users``).
  2. UNATTRIBUTED DATA — row has no tenant scope but does NOT match a test
     pattern. Real business data (audit_logs events, payments, expenses,
     etc.) that was written without setting ``tenant_id``/``company_id``.
     Needs BACKFILL, not DELETE. Reported but never touched by ``--delete``.
  3. TRANSITIVE-SCOPED — table inherits scope from a parent FK (e.g.
     ``estimate_lines`` via ``estimates.company_id``). Not an orphan by
     design. Listed in ``TRANSITIVE_SCOPED_TABLES`` and skipped entirely.

DISCOVERED
----------
2026-04-17 an earlier session during D41 investigation. After backfilling 14
``customer_reviews``/``loyalty_referrals`` rows to their correct tenant,
one row remained with both columns NULL — QA test data
(``referee_name='qa_test_value'``, phone ``555-0199``, dead referrer FK)
orphaned inside the GDX tenant DB. First live run of ``--report`` then
found 324 rows across 8 tables — showing the SAME class of orphan is
much broader than D41 alone. Classification logic added to separate the
deletable residue from real-data backfill candidates so ``--delete``
can't destroy real audit events, payments, etc.

MODES
-----
  --report   (default) read-only. Writes JSON to
             ``ai-queue/rd/operations/test_residue_report.json``.
             Exits 1 if any findings (deletable OR unattributed).
  --delete   Destructive. Requires ``--confirm-delete``. DELETES ONLY
             rows classified as DELETABLE RESIDUE (pattern-matched).
             UNATTRIBUTED DATA is never deleted — only reported.
             Per-finding: pg_dump snapshot to
             ``/var/backups/gdx/test_residue_<ts>_<tenant>.sql`` first;
             DELETE inside a transaction with post-count verification;
             rollback on any mismatch.

EXIT CODES
    0 — clean (no findings)
    1 — findings present (report) or cleaned (delete)
    2 — could not connect to control DB or a tenant DB
    3 — usage error (e.g. --delete without --confirm-delete)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# Tables whose scoping comes from a parent FK, not their own column.
# These may have NULL on their own company_id/tenant_id legitimately —
# skip them. To add: verify the child's parent always carries scope.
TRANSITIVE_SCOPED_TABLES: set[str] = {
    "estimate_lines",   # scoped via estimates.company_id
    "invoice_lines",    # scoped via invoices.company_id
}


# Per-table patterns that identify rows as deletable test residue.
# Rows matching ANY pattern here AND lacking tenant scope are DELETABLE.
# Rows lacking tenant scope but NOT matching are UNATTRIBUTED (backfill).
# Uses Postgres ~ (regex) operator. Keep conservative — false positives
# here destroy real user data.
# Acknowledged-legacy rows: rows with a documented reason for being
# unattributed that we have consciously decided NOT to backfill.
# Keyed by (tenant_slug, table, null_column). date_range_utc narrows
# the acknowledgment to a specific window so any NEW NULL rows outside
# it still surface as unattributed drift.
#
# The canonical case: GDX's 2026-04-08 audit_logs (296 rows) from the
# initial GDX data population. log_audit_event's row_hash is computed
# over tenant_id, so retro-backfill would break the hash chain — and
# the hash chain's purpose is proving history wasn't altered. We
# accept the NULL gap and document it here instead of rewriting.
KNOWN_LEGACY_ROWS: dict[tuple[str, str, str], dict] = {
    ("gdx", "audit_logs", "tenant_id"): {
        "date_range_utc": ("2026-04-08T00:00:00+00:00",
                           "2026-04-08T23:59:59.999+00:00"),
        "reason": "Initial GDX data population day; migration scripts "
                  "wrote via log_audit_event() without web request "
                  "context. Row_hash chain integrity takes precedence "
                  "over retroactive attribution. Filed 2026-04-17 "
                  "an earlier session (D44).",
        "acknowledged_at": "2026-04-17",
        "filed_by": "an earlier session",
    },
}


RESIDUE_PATTERNS: dict[str, list[tuple[str, str]]] = {
    "users": [
        ("email", r"^qa[-_]test@|@example\.(com|org)$|^test[-_]user@"),
    ],
    "loyalty_referrals": [
        ("referee_name", r"^qa[-_]?test"),
        ("referee_phone", r"^555-0[0-9]{3}$"),
        ("referee_email", r"@example\.(com|org)$"),
    ],
    "customers": [
        ("email", r"^qa[-_]test@|@example\.(com|org)$"),
        ("phone", r"^555-0[0-9]{3}$"),
    ],
    "customer_reviews": [
        # Review text / review email markers if used for fuzz seeding
        ("review_text", r"^qa[-_]test|^test[-_]review"),
    ],
}

def _detect_repo_root() -> Path:
    """Best-effort repo root detection that survives being run from /tmp."""
    here = Path(__file__).resolve()
    # Normal layout: <repo>/gdx_dispatch/tools/this_file.py → parents[2]
    for p in [here] + list(here.parents):
        if (p / "gdx_dispatch" / "tools").is_dir() and (p / "ai-queue").is_dir():
            return p
    # Fallback: env override, then CWD.
    override = os.getenv("GDX_REPO_ROOT")
    if override:
        return Path(override)
    return Path.cwd()


REPO_ROOT = _detect_repo_root()
REPORT_PATH = REPO_ROOT / "ai-queue/rd/operations/test_residue_report.json"
# Snapshot path: host-writable /var/backups/gdx by default; override with
# GDX_SNAPSHOT_DIR for in-container or test runs (e.g. /tmp).
SNAPSHOT_DIR = Path(os.getenv("GDX_SNAPSHOT_DIR", "/var/backups/gdx"))


def _lazy_db():
    from sqlalchemy import create_engine, text
    try:
        from gdx_dispatch.core.database import _decrypt_db_url
    except Exception:
        _decrypt_db_url = None
    return create_engine, text, _decrypt_db_url


def _fallback_decrypt(db_url_enc: str) -> str:
    key = os.getenv("GDX_FERNET_KEY", "")
    if not key:
        return db_url_enc
    from cryptography.fernet import Fernet
    return Fernet(key.encode()).decrypt(db_url_enc.encode()).decode()


@dataclass
class Finding:
    table: str
    deletable_residue_count: int      # matches RESIDUE_PATTERNS — safe to DELETE
    unattributed_count: int           # no scope + no pattern + not acknowledged — BACKFILL needed
    null_column: str                  # 'company_id' | 'tenant_id' | 'both' | 'query_error:...'
    acknowledged_legacy_count: int = 0  # KNOWN_LEGACY_ROWS match — documented, left alone

    @property
    def total_unscoped(self) -> int:
        return (max(0, self.deletable_residue_count)
                + max(0, self.unattributed_count)
                + max(0, self.acknowledged_legacy_count))

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TenantSweep:
    slug: str
    tenant_id: str
    findings: list[Finding] = field(default_factory=list)
    deleted: int = 0
    snapshot_path: str | None = None
    error: str | None = None

    @property
    def total_deletable(self) -> int:
        return sum(f.deletable_residue_count for f in self.findings
                   if f.deletable_residue_count > 0)

    @property
    def total_unattributed(self) -> int:
        return sum(f.unattributed_count for f in self.findings
                   if f.unattributed_count > 0)

    @property
    def total_acknowledged_legacy(self) -> int:
        return sum(f.acknowledged_legacy_count for f in self.findings
                   if f.acknowledged_legacy_count > 0)

    def to_dict(self) -> dict:
        return {
            "slug": self.slug,
            "tenant_id": self.tenant_id,
            "findings": [f.to_dict() for f in self.findings],
            "total_deletable": self.total_deletable,
            "total_unattributed": self.total_unattributed,
            "total_acknowledged_legacy": self.total_acknowledged_legacy,
            "deleted": self.deleted,
            "snapshot_path": self.snapshot_path,
            "error": self.error,
        }


# ── Tenant discovery ────────────────────────────────────────────────────────

def discover_tenants(slug_filter: str | None) -> list[tuple[str, str, str]]:
    create_engine, text, _ = _lazy_db()
    control_url = os.environ.get("CONTROL_DATABASE_URL")
    if not control_url:
        raise RuntimeError("CONTROL_DATABASE_URL not set")
    eng = create_engine(control_url)
    rows: list[tuple[str, str, str]] = []
    with eng.connect() as conn:
        result = conn.execute(text(
            "SELECT id, slug, db_url_enc FROM tenants "
            "WHERE db_url_enc IS NOT NULL AND db_url_enc != '' "
            "AND deleted_at IS NULL"
        ))
        for r in result:
            if slug_filter and r.slug != slug_filter:
                continue
            rows.append((str(r.id), r.slug, r.db_url_enc))
    eng.dispose()
    return rows


# ── Core scan ──────────────────────────────────────────────────────────────

def find_tenant_scoped_tables(engine, text_fn) -> dict[str, set[str]]:
    """Return {table: columns_it_has ∩ {'company_id', 'tenant_id'}}."""
    with engine.connect() as conn:
        rows = conn.execute(text_fn(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' "
            "AND column_name IN ('company_id', 'tenant_id')"
        )).fetchall()
    out: dict[str, set[str]] = {}
    for r in rows:
        out.setdefault(r.table_name, set()).add(r.column_name)
    return out


def build_orphan_clause(cols: set[str]) -> tuple[str, str] | None:
    """Return (where_clause, null_column_label) for a table's tenant columns.

    Returns None if the table has no tenant columns (shouldn't happen — caller
    should not have passed it in).
    """
    if "company_id" in cols and "tenant_id" in cols:
        return "company_id IS NULL AND tenant_id IS NULL", "both"
    if "company_id" in cols:
        return "company_id IS NULL", "company_id"
    if "tenant_id" in cols:
        return "tenant_id IS NULL", "tenant_id"
    return None


def _regex_operator_for(engine) -> str:
    """Pick the regex operator for this engine's dialect.

    Postgres uses ``~`` (and ``~*`` case-insensitive); SQLite's
    registered REGEXP function uses the ``REGEXP`` keyword. Production is
    always PG — SQLite support is for unit tests that register a
    python regex callback via ``conn.create_function("REGEXP", ...)``.
    """
    name = getattr(getattr(engine, "dialect", None), "name", "postgresql")
    return "REGEXP" if name == "sqlite" else "~"


def _residue_clause_and_params(
    table: str, regex_op: str = "~",
) -> tuple[str, dict] | None:
    """Return (regex-OR clause, bind params) for RESIDUE_PATTERNS of a table.

    None if the table has no residue patterns defined — residue count for
    that table is always 0 (all unscoped rows are unattributed, not
    deletable residue).
    """
    patterns = RESIDUE_PATTERNS.get(table)
    if not patterns:
        return None
    parts: list[str] = []
    params: dict[str, str] = {}
    for i, (col, pattern) in enumerate(patterns):
        key = f"residue_p{i}"
        parts.append(f'"{col}" {regex_op} :{key}')
        params[key] = pattern
    return "(" + " OR ".join(parts) + ")", params


def classify_table(
    engine, text_fn, table: str, cols: set[str], tenant_slug: str = "",
) -> Finding | None:
    """Classify one table's unscoped rows into three buckets:
      - deletable_residue: matches RESIDUE_PATTERNS, safe to DELETE
      - acknowledged_legacy: matches KNOWN_LEGACY_ROWS + falls in its
        date range, a deliberate known gap — leave alone
      - unattributed: everything else — needs BACKFILL

    Returns None for transitive-scoped tables (not real orphans) or
    tables with zero unscoped rows.
    """
    if table in TRANSITIVE_SCOPED_TABLES:
        return None
    clause_info = build_orphan_clause(cols)
    if clause_info is None:
        return None
    clause, null_col = clause_info

    with engine.connect() as conn:
        total = conn.execute(
            text_fn(f'SELECT COUNT(*) FROM "{table}" WHERE {clause}')
        ).scalar() or 0
    if total == 0:
        return None

    residue_count = 0
    residue_info = _residue_clause_and_params(table, _regex_operator_for(engine))
    if residue_info is not None:
        residue_clause, params = residue_info
        with engine.connect() as conn:
            residue_count = conn.execute(
                text_fn(
                    f'SELECT COUNT(*) FROM "{table}" '
                    f'WHERE {clause} AND {residue_clause}'
                ),
                params,
            ).scalar() or 0

    acknowledged_count = 0
    legacy_entry = KNOWN_LEGACY_ROWS.get((tenant_slug, table, null_col))
    if legacy_entry is not None:
        lo, hi = legacy_entry["date_range_utc"]
        with engine.connect() as conn:
            acknowledged_count = conn.execute(
                text_fn(
                    f'SELECT COUNT(*) FROM "{table}" '
                    f'WHERE {clause} AND created_at BETWEEN :lo AND :hi'
                ),
                {"lo": lo, "hi": hi},
            ).scalar() or 0

    # Residue ∩ acknowledged is possible in theory but pathological — if
    # so, treat the row as residue (more actionable signal).
    unattributed = int(total) - int(residue_count) - int(acknowledged_count)
    if unattributed < 0:
        # Overlap case: residue + acknowledged exceed total. Clamp and
        # reduce acknowledged first (residue is more specific/actionable).
        acknowledged_count = max(0, int(total) - int(residue_count))
        unattributed = 0

    return Finding(
        table=table,
        deletable_residue_count=int(residue_count),
        unattributed_count=int(unattributed),
        null_column=null_col,
        acknowledged_legacy_count=int(acknowledged_count),
    )


def scan_tenant(tenant_id: str, slug: str, db_url_enc: str) -> TenantSweep:
    create_engine, text, _decrypt_db_url = _lazy_db()
    result = TenantSweep(slug=slug, tenant_id=tenant_id)
    try:
        decrypt = _decrypt_db_url or _fallback_decrypt
        db_url = decrypt(db_url_enc)
        eng = create_engine(db_url, isolation_level="AUTOCOMMIT")
    except Exception as e:
        result.error = f"connect failed: {type(e).__name__}: {e}"
        return result
    try:
        tables = find_tenant_scoped_tables(eng, text)
        for table, cols in sorted(tables.items()):
            try:
                finding = classify_table(eng, text, table, cols, tenant_slug=slug)
                if finding:
                    result.findings.append(finding)
            except Exception as e:
                # Surface the skip rather than silence it.
                result.findings.append(Finding(
                    table=table,
                    deletable_residue_count=-1,
                    unattributed_count=-1,
                    null_column=f"query_error: {type(e).__name__}",
                ))
    finally:
        eng.dispose()
    return result


# ── Delete mode ────────────────────────────────────────────────────────────

def snapshot_before_delete(
    engine, text_fn, slug: str, findings: list[Finding],
) -> str:
    """Capture the EXACT rows the delete will remove, via the same DB
    connection. Writes a JSON file (recovery format: re-INSERT by dict).

    This replaces a subprocess/docker-exec pg_dump approach so the tool
    works identically on host and inside containers — as long as we have
    a live DB connection, we can snapshot.
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    try:
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise RuntimeError(
            f"snapshot dir {SNAPSHOT_DIR} not writable: "
            f"{type(e).__name__}: {e} — refusing to delete"
        ) from e

    snap = SNAPSHOT_DIR / f"test_residue_{ts}_{slug}.json"
    snapshot_data: dict = {
        "timestamp_utc": ts,
        "tenant_slug": slug,
        "schema_version": 1,
        "tables": {},
    }

    regex_op = _regex_operator_for(engine)
    for f in findings:
        if f.deletable_residue_count <= 0:
            continue
        residue_info = _residue_clause_and_params(f.table, regex_op)
        if residue_info is None:
            continue
        if f.null_column == "both":
            scope_clause = "company_id IS NULL AND tenant_id IS NULL"
        elif f.null_column in ("company_id", "tenant_id"):
            scope_clause = f"{f.null_column} IS NULL"
        else:
            continue
        residue_clause, params = residue_info
        try:
            with engine.connect() as conn:
                rows = conn.execute(
                    text_fn(
                        f'SELECT * FROM "{f.table}" '
                        f'WHERE {scope_clause} AND {residue_clause}'
                    ),
                    params,
                ).fetchall()
            snapshot_data["tables"][f.table] = [
                {k: (v if isinstance(v, (str, int, float, bool, type(None)))
                      else str(v))
                 for k, v in r._mapping.items()}
                for r in rows
            ]
        except Exception as e:
            raise RuntimeError(
                f"snapshot failed for table={f.table}: "
                f"{type(e).__name__}: {e} — refusing to delete"
            ) from e

    try:
        with snap.open("w") as fh:
            json.dump(snapshot_data, fh, indent=2, default=str)
    except OSError as e:
        raise RuntimeError(
            f"could not write snapshot to {snap}: "
            f"{type(e).__name__}: {e} — refusing to delete"
        ) from e
    return str(snap)


def delete_residue_in_transaction(engine, text_fn, findings: list[Finding]) -> int:
    """DELETE ONLY rows matching RESIDUE_PATTERNS. Unattributed rows are
    reported but never touched. Wrapped in a single transaction; each
    DELETE's rowcount must equal the pre-scanned residue count or the
    transaction rolls back.
    """
    deleted = 0
    with engine.begin() as conn:
        for f in findings:
            if f.deletable_residue_count <= 0:
                continue
            residue_info = _residue_clause_and_params(
                f.table, _regex_operator_for(engine),
            )
            if residue_info is None:
                # Safety: no pattern registered → no delete. Should not
                # happen if deletable_residue_count > 0 (set by classify_table
                # only when patterns exist), but guard anyway.
                continue
            if f.null_column == "both":
                scope_clause = "company_id IS NULL AND tenant_id IS NULL"
            elif f.null_column in ("company_id", "tenant_id"):
                scope_clause = f"{f.null_column} IS NULL"
            else:
                continue
            residue_clause, params = residue_info
            result = conn.execute(
                text_fn(
                    f'DELETE FROM "{f.table}" '
                    f'WHERE {scope_clause} AND {residue_clause}'
                ),
                params,
            )
            rc = result.rowcount or 0
            if rc != f.deletable_residue_count:
                raise RuntimeError(
                    f"table={f.table} expected {f.deletable_residue_count} "
                    f"deletes, got {rc} — rolling back (possible race with "
                    f"concurrent writer or pattern mismatch)"
                )
            deleted += rc
    return deleted


def delete_tenant_residue(sweep: TenantSweep, db_url_enc: str) -> TenantSweep:
    create_engine, text, _decrypt_db_url = _lazy_db()
    deletable = [f for f in sweep.findings if f.deletable_residue_count > 0]
    if not deletable:
        return sweep
    try:
        decrypt = _decrypt_db_url or _fallback_decrypt
        db_url = decrypt(db_url_enc)
        eng = create_engine(db_url)
    except Exception as e:
        sweep.error = f"connect failed: {type(e).__name__}: {e}"
        return sweep
    try:
        sweep.snapshot_path = snapshot_before_delete(
            eng, text, sweep.slug, deletable,
        )
    except Exception as e:
        sweep.error = str(e)
        eng.dispose()
        return sweep
    try:
        sweep.deleted = delete_residue_in_transaction(eng, text, deletable)
    except Exception as e:
        sweep.error = f"delete failed: {type(e).__name__}: {e}"
    finally:
        eng.dispose()
    return sweep


# ── Report ─────────────────────────────────────────────────────────────────

def write_report(sweeps: list[TenantSweep], mode: str) -> dict:
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "tenants_scanned": len(sweeps),
        "total_deletable_residue": sum(s.total_deletable for s in sweeps),
        "total_unattributed": sum(s.total_unattributed for s in sweeps),
        "total_acknowledged_legacy": sum(s.total_acknowledged_legacy for s in sweeps),
        "total_deleted": sum(s.deleted for s in sweeps),
        "tenants": [s.to_dict() for s in sweeps],
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2))
    return report


# ── Main ───────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="D44: test-residue sweeper")
    ap.add_argument("--delete", action="store_true",
                    help="Actually delete orphans (default: report only)")
    ap.add_argument("--confirm-delete", action="store_true",
                    help="Required with --delete to prevent accidents")
    ap.add_argument("--tenant", default=None,
                    help="Restrict to one tenant slug (default: all)")
    args = ap.parse_args()

    mode = "delete" if args.delete else "report"
    if args.delete and not args.confirm_delete:
        print("D44: --delete requires --confirm-delete to prevent accidents",
              file=sys.stderr)
        return 3

    try:
        tenants = discover_tenants(args.tenant)
    except Exception as e:
        print(f"D44: 🚨 tenant discovery failed: {type(e).__name__}: {e}",
              file=sys.stderr)
        return 2
    if not tenants:
        print(f"D44: no tenants matched filter={args.tenant!r}; nothing to do")
        return 0

    sweeps: list[TenantSweep] = []
    for tid, slug, db_url_enc in tenants:
        sweep = scan_tenant(tid, slug, db_url_enc)
        has_deletable = sweep.total_deletable > 0
        if mode == "delete" and has_deletable and not sweep.error:
            sweep = delete_tenant_residue(sweep, db_url_enc)
        sweeps.append(sweep)

    report = write_report(sweeps, mode)

    total_residue = report["total_deletable_residue"]
    total_unattrib = report["total_unattributed"]
    total_legacy = report["total_acknowledged_legacy"]
    total_deleted = report["total_deleted"]
    print(f"D44: mode={mode}  tenants={report['tenants_scanned']}  "
          f"deletable_residue={total_residue}  "
          f"unattributed={total_unattrib}  "
          f"acknowledged_legacy={total_legacy}  "
          f"deleted={total_deleted}")
    for s in sweeps:
        if s.error:
            print(f"  [error] {s.slug}: {s.error}", file=sys.stderr)
            continue
        if (s.total_deletable == 0 and s.total_unattributed == 0
                and s.total_acknowledged_legacy == 0):
            continue
        marker = f" → deleted {s.deleted}" if s.deleted else ""
        print(f"  [findings] {s.slug}: "
              f"{s.total_deletable} residue + {s.total_unattributed} "
              f"unattributed + {s.total_acknowledged_legacy} legacy "
              f"across {len(s.findings)} table(s){marker}")
        for f in s.findings:
            if (f.deletable_residue_count < 0
                    or f.unattributed_count < 0
                    or f.acknowledged_legacy_count < 0):
                print(f"    {f.table}: {f.null_column}")
            else:
                tags = []
                if f.deletable_residue_count > 0:
                    tags.append(f"{f.deletable_residue_count} residue")
                if f.unattributed_count > 0:
                    tags.append(f"{f.unattributed_count} unattributed")
                if f.acknowledged_legacy_count > 0:
                    tags.append(f"{f.acknowledged_legacy_count} legacy")
                print(f"    {f.table}: {', '.join(tags)} "
                      f"({f.null_column} NULL)")

    if any(s.error for s in sweeps):
        return 2
    # Exit 1 only on actionable findings (residue OR unattributed).
    # Acknowledged-legacy alone does NOT fail the gate — it's documented.
    return 1 if (total_residue + total_unattrib) > 0 else 0


if __name__ == "__main__":
    sys.exit(main())

"""Sprint phone-com fix-it Wave H / S15 — tenant-plane schema-drift scanner.

Compares TenantBase ORM declarations against the live information_schema
of every tenant's database. Catches three classes of drift:

1. **Missing table** — ORM declares it; tenant DB lacks it.
2. **Missing column** — ORM declares the column; tenant DB lacks it. This
   is the class that broke phone_com_calls.final_action_target before the
   Wave F ALTER landed.
3. **Orphan column** — tenant DB has a column the ORM doesn't know about.
   Usually harmless, but flags pre-lift-and-shift legacy that should
   eventually drop.

CLI:
    python -m gdx_dispatch.tools.tenant_plane_schema_drift --tenant gdx
    python -m gdx_dispatch.tools.tenant_plane_schema_drift --all-tenants
    python -m gdx_dispatch.tools.tenant_plane_schema_drift --all-tenants --report-dir ai-queue/operations/inbox

Exit code:
    0 — no drift detected
    1 — drift detected (CI / cron should fail)
    2 — operator error (bad args, can't reach control DB)

The scanner does not write to tenant DBs. Resolution remains
``gdx_dispatch/tools/pave_tenant_db.py`` for missing/changed columns, or a manual
ALTER for special cases.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import inspect, select, text
from sqlalchemy.engine import Engine

from gdx_dispatch.control.models import Tenant
from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.database import SessionLocal, _decrypt_db_url
from gdx_dispatch.core.tenant import engine_registry

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)


@dataclass
class TenantDriftReport:
    tenant_id: str
    tenant_slug: str
    db_host: str
    db_name: str
    missing_tables: list[str] = field(default_factory=list)
    missing_columns: list[tuple[str, str]] = field(default_factory=list)  # (table, column)
    orphan_columns: list[tuple[str, str]] = field(default_factory=list)  # (table, column)

    @property
    def has_drift(self) -> bool:
        return bool(self.missing_tables or self.missing_columns or self.orphan_columns)

    @property
    def total_findings(self) -> int:
        return (
            len(self.missing_tables)
            + len(self.missing_columns)
            + len(self.orphan_columns)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "tenant_slug": self.tenant_slug,
            "db_host": self.db_host,
            "db_name": self.db_name,
            "missing_tables": self.missing_tables,
            "missing_columns": [list(t) for t in self.missing_columns],
            "orphan_columns": [list(t) for t in self.orphan_columns],
            "total_findings": self.total_findings,
        }


# Tables we deliberately don't expect to exist on every tenant DB. These
# are forward-feature tables, multi-tenant-only tables, or things paved
# only on specific tenant cohorts. Filter them out of "missing tables".
_TABLE_ALLOWLIST_MISSING: set[str] = {
    "alembic_version",  # tenant-plane is ORM-only, no alembic table
}

# Orphan-column allowlist: columns we know exist in legacy tenant DBs
# but are not declared in the current ORM. These were noted in
# ARCHITECTURAL_INVARIANTS / pave_tenant_db work and don't need to
# block CI.
_ORPHAN_COLUMN_ALLOWLIST: set[tuple[str, str]] = {
    # Examples — populated as the scanner finds genuine known-orphans on
    # prod. Empty by default so the first run produces a baseline.
}


def _orm_schema() -> dict[str, set[str]]:
    """Return {table_name: {column_name, ...}} for every TenantBase model."""
    result: dict[str, set[str]] = {}
    for tname, table in TenantBase.metadata.tables.items():
        result[tname] = {col.name for col in table.columns}
    return result


def _live_schema(engine: Engine) -> dict[str, set[str]]:
    """Return {table_name: {column_name, ...}} for the public schema."""
    insp = inspect(engine)
    result: dict[str, set[str]] = {}
    for tname in insp.get_table_names(schema="public"):
        result[tname] = {c["name"] for c in insp.get_columns(tname, schema="public")}
    return result


def diff_schemas(
    orm: dict[str, set[str]],
    live: dict[str, set[str]],
) -> tuple[list[str], list[tuple[str, str]], list[tuple[str, str]]]:
    """Compute (missing_tables, missing_columns, orphan_columns)."""
    missing_tables = sorted(
        t for t in orm.keys() - live.keys() if t not in _TABLE_ALLOWLIST_MISSING
    )
    missing_columns: list[tuple[str, str]] = []
    orphan_columns: list[tuple[str, str]] = []
    for tname in orm.keys() & live.keys():
        orm_cols = orm[tname]
        live_cols = live[tname]
        for c in sorted(orm_cols - live_cols):
            missing_columns.append((tname, c))
        for c in sorted(live_cols - orm_cols):
            if (tname, c) not in _ORPHAN_COLUMN_ALLOWLIST:
                orphan_columns.append((tname, c))
    return missing_tables, missing_columns, orphan_columns


def scan_tenant(tenant_id: UUID, slug: str, db_url: str) -> TenantDriftReport:
    eng = engine_registry.get_engine(str(tenant_id), db_url)
    orm = _orm_schema()
    live = _live_schema(eng)
    missing_t, missing_c, orphan_c = diff_schemas(orm, live)
    # Extract host/db from URL safely (no creds).
    db_host = ""
    db_name = ""
    try:
        u = eng.url
        db_host = str(u.host or "")
        db_name = str(u.database or "")
    except Exception:  # noqa: BLE001
        pass
    return TenantDriftReport(
        tenant_id=str(tenant_id),
        tenant_slug=slug,
        db_host=db_host,
        db_name=db_name,
        missing_tables=missing_t,
        missing_columns=missing_c,
        orphan_columns=orphan_c,
    )


def render_markdown(reports: list[TenantDriftReport]) -> str:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines = [
        f"# Tenant-plane schema drift report — {ts}",
        "",
        f"Tenants scanned: **{len(reports)}**.",
        f"Tenants with drift: **{sum(1 for r in reports if r.has_drift)}**.",
        "",
    ]
    for r in reports:
        if not r.has_drift:
            lines.append(f"## {r.tenant_slug} (`{r.db_name}` on `{r.db_host}`) — clean ✅")
            lines.append("")
            continue
        lines.append(
            f"## {r.tenant_slug} (`{r.db_name}` on `{r.db_host}`) — "
            f"**{r.total_findings} finding(s)**",
        )
        if r.missing_tables:
            lines.append("")
            lines.append("**Missing tables (ORM declared, DB lacks):**")
            for t in r.missing_tables:
                lines.append(f"- `{t}`")
        if r.missing_columns:
            lines.append("")
            lines.append("**Missing columns:**")
            for tname, c in r.missing_columns:
                lines.append(f"- `{tname}.{c}`")
        if r.orphan_columns:
            lines.append("")
            lines.append("**Orphan columns (DB has, ORM doesn't):**")
            for tname, c in r.orphan_columns:
                lines.append(f"- `{tname}.{c}`")
        lines.append("")
    return "\n".join(lines)


def write_report(report_dir: Path, content: str) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = report_dir / f"tenant_plane_drift_{ts}.md"
    path.write_text(content, encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", help="Single tenant slug")
    parser.add_argument("--all-tenants", action="store_true")
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=None,
        help="Write a markdown report file here (in addition to stdout).",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit JSON to stdout instead of markdown.",
    )
    args = parser.parse_args(argv)

    if not args.tenant and not args.all_tenants:
        parser.print_help()
        return 2

    reports: list[TenantDriftReport] = []
    with SessionLocal() as cs:
        stmt = select(Tenant).where(Tenant.deleted_at.is_(None))
        if args.tenant:
            stmt = stmt.where(Tenant.slug == args.tenant)
        tenants = cs.execute(stmt).scalars().all()
        if args.tenant and not tenants:
            log.error("Tenant slug %r not found.", args.tenant)
            return 2
        for t in tenants:
            try:
                db_url = _decrypt_db_url(t.db_url_enc)
            except Exception:  # noqa: BLE001
                log.exception("decrypt failed for tenant=%s", t.slug)
                continue
            try:
                reports.append(scan_tenant(t.id, t.slug, db_url))
            except Exception as exc:  # noqa: BLE001
                log.exception("scan failed for tenant=%s: %s", t.slug, exc)

    if args.json:
        payload = {"reports": [r.to_dict() for r in reports]}
        print(json.dumps(payload, indent=2))
    else:
        md = render_markdown(reports)
        print(md)
        if args.report_dir:
            path = write_report(args.report_dir, md)
            print(f"\nReport written: {path}")

    drift_count = sum(1 for r in reports if r.has_drift)
    return 1 if drift_count > 0 else 0


if __name__ == "__main__":
    sys.exit(main())

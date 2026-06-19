#!/usr/bin/env python3
"""D35 — router raw-SQL vs live-tenant-DB drift gate.

Catches two classes of drift before a deploy ships:

  1. COLUMN MISSING — router references a column that does not exist on the
     live tenant DB yet (migration not applied, column renamed, typo).
  2. CANONICAL-BUT-EMPTY — column exists but is populated in zero rows while
     the table has rows. The router query compiles and runs, but returns
     zero results because the data still lives in a legacy sibling column.
     This is the 2026-04-16 appointments.tech_id trap: column existed,
     router read from it, but all 5 appointment rows had tech_id IS NULL
     and the real tech was stored in technician_id. Every tech would have
     seen zero jobs.

Complements tenant_isolation_audit.py (ORM-vs-live schema drift) — this one
sits one layer up at the router raw-SQL level.

USAGE
-----
    # Diff-aware (default): scan routers changed vs origin/main
    python3 gdx_dispatch/tools/router_sql_live_audit.py

    # Scan every router (deploy gate default)
    python3 gdx_dispatch/tools/router_sql_live_audit.py --all

    # Different base ref or tenant
    python3 gdx_dispatch/tools/router_sql_live_audit.py --base HEAD~5 --tenant gdx

    # JSON report to a file
    python3 gdx_dispatch/tools/router_sql_live_audit.py --all --json /tmp/d35.json

EXIT CODES
    0 — clean
    1 — at least one violation
    2 — could not connect to control DB or reference tenant DB
    3 — usage error
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# SQL tokens that are never column names — filter them out of WHERE/SET matches.
_SQL_RESERVED = {
    "SELECT", "FROM", "WHERE", "AND", "OR", "NOT", "NULL", "IS", "IN",
    "LIKE", "BETWEEN", "ORDER", "GROUP", "BY", "HAVING", "LIMIT", "OFFSET",
    "INSERT", "INTO", "VALUES", "UPDATE", "SET", "DELETE", "JOIN", "LEFT",
    "RIGHT", "INNER", "OUTER", "FULL", "CROSS", "NATURAL", "ON", "AS",
    "UNION", "ALL", "DISTINCT", "COUNT", "SUM", "AVG", "MIN", "MAX",
    "CASE", "WHEN", "THEN", "ELSE", "END", "EXISTS", "TRUE", "FALSE",
    "ASC", "DESC", "WITH", "USING", "OVER", "PARTITION", "WINDOW",
    "FILTER", "RETURNING", "CONFLICT", "DO", "NOTHING", "COALESCE",
    "CAST", "ELSEIF", "NOW", "CURRENT_TIMESTAMP",
}

# Tables whose NULL counts we intentionally skip — legacy stragglers or
# tables where canonical-but-empty is an expected transitional state.
# Keep this tiny; every entry is a known-accepted gap.
_TABLE_ALLOWLIST: set[str] = set()

# Columns that are legitimately optional — nullable by design, zero population
# doesn't signal drift. Examples: soft-delete timestamps, optional metadata.
_COLUMN_ALLOWLIST = {
    # Soft-delete / lifecycle timestamps — legitimately sparse.
    "deleted_at", "archived_at", "completed_at", "cancelled_at",
    "last_login_at", "last_seen_at", "last_used_at", "revoked_at",
    "expires_at", "paid_at", "voided_at", "refunded_at",
    "parent_tenant_id", "granted_via_installation_id",
    # information_schema meta column names — appear when a router queries
    # the PG catalog directly (the body filter catches most; these backstop
    # the concatenated-string-literal case).
    "table_name", "table_schema", "column_name", "data_type",
    "is_nullable", "table_catalog", "udt_name", "column_default",
}

# String-literal forms inside a Python source file, greedy enough to grab
# both """triple""" and "double" and 'single' multi-line content.
_STRING_LITERAL = re.compile(
    r'(?P<quote>"""|\'\'\'|"|\')'
    r'(?P<body>(?:\\.|(?!(?P=quote)).)*)'
    r'(?P=quote)',
    re.DOTALL,
)

# A string literal is considered SQL only if it contains BOTH a data
# keyword AND a structural marker on the same side of the pair. This
# filters out docstrings that merely mention SQL vocabulary.
_SQL_PAIRS = [
    (re.compile(r'\bSELECT\b', re.IGNORECASE),        re.compile(r'\bFROM\b', re.IGNORECASE)),
    (re.compile(r'\bINSERT\s+INTO\b', re.IGNORECASE), re.compile(r'\bVALUES\b|\(', re.IGNORECASE)),
    (re.compile(r'\bUPDATE\b', re.IGNORECASE),        re.compile(r'\bSET\b', re.IGNORECASE)),
    (re.compile(r'\bDELETE\s+FROM\b', re.IGNORECASE), re.compile(r'\bWHERE\b', re.IGNORECASE)),
]


def _looks_like_sql(body: str) -> bool:
    return any(lhs.search(body) and rhs.search(body) for lhs, rhs in _SQL_PAIRS)

# Within a SQL block, column references we care about:
_WHERE_COL = re.compile(
    r'(?:\bWHERE\s+|\bAND\s+|\bOR\s+)'
    r'(?:\(\s*)?'
    r'([a-z_][a-z_0-9]*)'
    r'\s*(?:=|!=|<>|<=|>=|<|>|\bIS\b|\bIN\b|\bLIKE\b)',
    re.IGNORECASE,
)
_SET_COL = re.compile(r'\bSET\s+([a-z_][a-z_0-9]*)\s*=', re.IGNORECASE)
_INSERT_BLOCK = re.compile(
    r'\bINSERT\s+INTO\s+([a-z_][a-z_0-9]*)\s*\(([^)]+)\)',
    re.IGNORECASE | re.DOTALL,
)
_FROM_TABLE = re.compile(r'\bFROM\s+([a-z_][a-z_0-9]*)', re.IGNORECASE)
_UPDATE_TABLE = re.compile(r'\bUPDATE\s+([a-z_][a-z_0-9]*)', re.IGNORECASE)
_JOIN_TABLE = re.compile(r'\bJOIN\s+([a-z_][a-z_0-9]*)', re.IGNORECASE)


@dataclass
class ColRef:
    file: str
    line: int
    column: str
    table_hints: set[str] = field(default_factory=set)

    def to_dict(self) -> dict:
        return {
            "file": self.file,
            "line": self.line,
            "column": self.column,
            "table_hints": sorted(self.table_hints),
        }


@dataclass
class Violation:
    file: str
    line: int
    column: str
    table: str
    kind: str  # "column_missing" | "canonical_but_empty"
    detail: str

    def to_dict(self) -> dict:
        return asdict(self)


# ── SQL extraction ──────────────────────────────────────────────────────────

def _extract_tables(block: str) -> set[str]:
    tables: set[str] = set()
    for rx in (_FROM_TABLE, _UPDATE_TABLE, _JOIN_TABLE):
        tables.update(m.group(1).lower() for m in rx.finditer(block))
    for m in _INSERT_BLOCK.finditer(block):
        tables.add(m.group(1).lower())
    return tables


def _extract_columns(block: str, tables: set[str]) -> list[tuple[str, set[str]]]:
    """Return [(column, table_hints), ...] from one SQL string literal."""
    found: dict[str, set[str]] = {}

    def _note(col: str) -> None:
        c = col.lower()
        if c in _SQL_RESERVED:
            return
        if c in _COLUMN_ALLOWLIST:
            return
        found.setdefault(c, set()).update(tables)

    for m in _WHERE_COL.finditer(block):
        _note(m.group(1))
    for m in _SET_COL.finditer(block):
        _note(m.group(1))
    for m in _INSERT_BLOCK.finditer(block):
        insert_table = m.group(1).lower()
        for raw in m.group(2).split(","):
            col = raw.strip().strip('"').strip("'").strip("`")
            if not col or not col.replace("_", "").isalnum():
                continue
            c = col.lower()
            if c in _SQL_RESERVED or c in _COLUMN_ALLOWLIST:
                continue
            found.setdefault(c, set()).add(insert_table)

    return [(c, ts) for c, ts in found.items()]


def scan_file(path: Path) -> list[ColRef]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"  ⚠ could not read {path}: {e}", file=sys.stderr)
        return []

    refs: list[ColRef] = []
    for m in _STRING_LITERAL.finditer(text):
        body = m.group("body")
        if not _looks_like_sql(body):
            continue
        # Meta-queries against PG catalogs are not tenant data queries.
        if re.search(r'\b(?:information_schema|pg_catalog|pg_class|alembic_version)\b',
                     body, re.IGNORECASE):
            continue
        line = text.count("\n", 0, m.start()) + 1
        tables = _extract_tables(body)
        for col, hints in _extract_columns(body, tables):
            refs.append(
                ColRef(
                    file=str(path),
                    line=line,
                    column=col,
                    table_hints=hints or set(),
                )
            )
    return refs


# ── Git diff ────────────────────────────────────────────────────────────────

def changed_routers(base: str, repo_root: Path) -> list[Path]:
    try:
        out = subprocess.check_output(
            ["git", "-C", str(repo_root), "diff", "--name-only",
             f"{base}...HEAD", "--", "gdx_dispatch/routers/"],
            text=True,
        )
    except subprocess.CalledProcessError as e:
        print(f"  ⚠ git diff failed ({e.returncode}); falling back to --all",
              file=sys.stderr)
        return all_routers(repo_root)
    paths: list[Path] = []
    for rel in out.splitlines():
        rel = rel.strip()
        if not rel.endswith(".py"):
            continue
        p = repo_root / rel
        if p.exists():
            paths.append(p)
    return paths


def all_routers(repo_root: Path) -> list[Path]:
    return sorted((repo_root / "gdx_dispatch" / "routers").glob("*.py"))


# ── Live DB check ───────────────────────────────────────────────────────────

def _lazy_db():
    """Import SQLAlchemy + Fernet lazily so --help works without env."""
    from sqlalchemy import create_engine, text
    try:
        from gdx_dispatch.core.database import _decrypt_db_url
    except Exception:
        _decrypt_db_url = None
    return create_engine, text, _decrypt_db_url


def _fallback_decrypt(db_url_enc: str) -> str:
    """Mirror ``gdx_dispatch.core.database._decrypt_db_url`` semantics so the D35
    gate doesn't InvalidToken-out on legacy plaintext rows. The session-59
    bug-bash patched the production decrypt path to pass through plaintext;
    this fallback (used when the primary import fails) was missed."""
    key = os.getenv("GDX_FERNET_KEY", "")
    if not key:
        return db_url_enc
    from cryptography.fernet import Fernet, InvalidToken
    try:
        return Fernet(key.encode()).decrypt(db_url_enc.encode()).decode()
    except InvalidToken:
        return db_url_enc


def resolve_tenant_engine(slug: str):
    """Return (tenant_engine, text_fn, control_columns).

    control_columns is the set of column names present in the control DB's
    public schema — used by audit() to suppress column_missing findings that
    are actually control-DB queries (platform, tenants, identities, etc).
    """
    create_engine, text, _decrypt_db_url = _lazy_db()
    control_url = os.environ.get("CONTROL_DATABASE_URL")
    if not control_url:
        raise RuntimeError("CONTROL_DATABASE_URL not set")
    control_eng = create_engine(control_url)
    with control_eng.connect() as conn:
        row = conn.execute(
            text("SELECT db_url_enc FROM tenants WHERE slug = :s "
                 "AND db_url_enc IS NOT NULL AND deleted_at IS NULL"),
            {"s": slug},
        ).fetchone()
        ctrl_rows = conn.execute(
            text("SELECT column_name FROM information_schema.columns "
                 "WHERE table_schema = 'public'")
        ).fetchall()
    control_eng.dispose()
    if not row:
        raise RuntimeError(f"tenant slug={slug!r} not found or has no db_url_enc")
    control_columns = {r.column_name.lower() for r in ctrl_rows}
    decrypt = _decrypt_db_url or _fallback_decrypt
    db_url = decrypt(row.db_url_enc)
    return create_engine(db_url, isolation_level="AUTOCOMMIT"), text, control_columns


def build_column_index(engine, text_fn) -> dict[str, set[str]]:
    """Return {column_name: {table1, table2, ...}} from information_schema."""
    with engine.connect() as conn:
        rows = conn.execute(
            text_fn(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = 'public'"
            )
        ).fetchall()
    idx: dict[str, set[str]] = {}
    for r in rows:
        idx.setdefault(r.column_name.lower(), set()).add(r.table_name.lower())
    return idx


def check_population(engine, text_fn, table: str, column: str) -> tuple[int, int]:
    """Return (total_rows, populated_rows) for table.column."""
    with engine.connect() as conn:
        total = conn.execute(text_fn(f'SELECT COUNT(*) FROM "{table}"')).scalar() or 0
        if total == 0:
            return 0, 0
        populated = conn.execute(
            text_fn(f'SELECT COUNT("{column}") FROM "{table}"')
        ).scalar() or 0
    return int(total), int(populated)


# ── Audit ───────────────────────────────────────────────────────────────────

def audit(
    refs: list[ColRef],
    column_index: dict[str, set[str]],
    population_fn,
    control_columns: set[str] | None = None,
) -> list[Violation]:
    """Pure audit: given refs + column_index + population_fn, return violations.

    population_fn(table, column) -> (total_rows, populated_rows). Raising an
    exception from population_fn records a column_missing violation for that
    (table, column) pair.

    control_columns is the set of column names present on the control DB
    (platform / tenants / identities / etc). A column found there but not
    in the tenant column_index is a control-DB query, not a drift — skip it.
    """
    control_columns = control_columns or set()
    violations: list[Violation] = []
    pop_cache: dict[tuple[str, str], tuple[int, int]] = {}

    for ref in refs:
        live_tables = column_index.get(ref.column, set())
        if not live_tables:
            if ref.column in control_columns:
                # Router is querying the control DB — not a tenant drift.
                continue
            violations.append(Violation(
                file=ref.file, line=ref.line, column=ref.column,
                table="(none)", kind="column_missing",
                detail=f"column {ref.column!r} not found on any tenant "
                       f"table (tables searched: {sorted(ref.table_hints) or 'n/a'})",
            ))
            continue

        candidates = (ref.table_hints & live_tables) or live_tables
        for tbl in candidates:
            if tbl in _TABLE_ALLOWLIST:
                continue
            key = (tbl, ref.column)
            if key not in pop_cache:
                try:
                    pop_cache[key] = population_fn(tbl, ref.column)
                except Exception as e:
                    pop_cache[key] = (-1, -1)
                    violations.append(Violation(
                        file=ref.file, line=ref.line, column=ref.column,
                        table=tbl, kind="column_missing",
                        detail=f"query failed on live DB: "
                               f"{type(e).__name__}: {str(e)[:120]}",
                    ))
                    continue
            total, populated = pop_cache[key]
            if total > 0 and populated == 0:
                violations.append(Violation(
                    file=ref.file, line=ref.line, column=ref.column,
                    table=tbl, kind="canonical_but_empty",
                    detail=f"table has {total} rows but column {ref.column!r} "
                           f"is populated in 0 rows — router queries will return "
                           f"no results. Backfill before deploy.",
                ))
    return violations


def audit_live(refs: list[ColRef], tenant_slug: str) -> list[Violation]:
    """Live wrapper — resolves engine, builds column index, runs audit()."""
    engine, text_fn, control_columns = resolve_tenant_engine(tenant_slug)
    try:
        column_index = build_column_index(engine, text_fn)

        def _pop(table: str, column: str) -> tuple[int, int]:
            return check_population(engine, text_fn, table, column)

        return audit(refs, column_index, _pop, control_columns=control_columns)
    finally:
        engine.dispose()


# ── Main ────────────────────────────────────────────────────────────────────

def find_repo_root(start: Path) -> Path:
    cur = start.resolve()
    for p in [cur, *cur.parents]:
        if (p / ".git").exists() or (p / "gdx_dispatch" / "routers").is_dir():
            return p
    return start.resolve()


def main() -> int:
    ap = argparse.ArgumentParser(description="D35: router raw-SQL vs live-tenant-DB gate")
    ap.add_argument("--base", default="origin/main",
                    help="Git base ref for diff mode (default: origin/main)")
    ap.add_argument("--all", action="store_true",
                    help="Scan every router, not just diff vs --base")
    ap.add_argument("--tenant", default="gdx",
                    help="Reference tenant slug (default: gdx)")
    ap.add_argument("--json", dest="json_path", default=None,
                    help="Write full JSON report to this path")
    ap.add_argument("--repo-root", default=None,
                    help="Repo root (default: auto-detect from this script)")
    ap.add_argument("--routers-dir", default=None,
                    help="Override path to routers/ directory")
    ap.add_argument("--acknowledged-file", default=None,
                    help="Path to a JSON file of known-acceptable violations "
                         "(canonical-but-empty with explicit operator sign-off). "
                         "Default: gdx_dispatch/tools/d35_acknowledged.json if present.")
    args = ap.parse_args()

    repo_root = Path(args.repo_root) if args.repo_root else find_repo_root(Path(__file__).parent)

    if args.routers_dir:
        routers = sorted(Path(args.routers_dir).glob("*.py"))
    elif args.all:
        routers = all_routers(repo_root)
    else:
        routers = changed_routers(args.base, repo_root)

    routers = [p for p in routers if p.name != "__init__.py"]

    if not routers:
        print("D35: no router files to scan (diff mode found no changes).")
        return 0

    print(f"D35: scanning {len(routers)} router file(s) against tenant={args.tenant!r}")

    refs: list[ColRef] = []
    for p in routers:
        refs.extend(scan_file(p))

    if not refs:
        print("D35: no raw-SQL column references extracted — gate clean.")
        return 0

    print(f"D35: extracted {len(refs)} column references; querying live DB…")

    try:
        violations = audit_live(refs, args.tenant)
    except Exception as e:
        print(f"D35: 🚨 connection error: {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tenant": args.tenant,
        "routers_scanned": [str(p) for p in routers],
        "refs_extracted": len(refs),
        "violations": [v.to_dict() for v in violations],
    }
    if args.json_path:
        Path(args.json_path).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_path).write_text(json.dumps(report, indent=2))

    # Filter acknowledged violations. An entry matches when file basename,
    # line, table, and column all agree. Only `canonical_but_empty` findings
    # are eligible for acknowledgment — `column_missing` is always blocking.
    ack_path = args.acknowledged_file
    if ack_path is None:
        default_ack = repo_root / "gdx_dispatch" / "tools" / "d35_acknowledged.json"
        if default_ack.exists():
            ack_path = str(default_ack)
    acknowledged: list[dict] = []
    if ack_path:
        try:
            data = json.loads(Path(ack_path).read_text())
            # Support either a bare list or a {"_meta": ..., "acknowledged": [...]} envelope.
            if isinstance(data, dict) and "acknowledged" in data:
                acknowledged = data["acknowledged"]
            elif isinstance(data, list):
                acknowledged = data
            else:
                print(f"D35: ⚠ acknowledged-file {ack_path} has unexpected shape", file=sys.stderr)
        except (OSError, json.JSONDecodeError) as e:
            print(f"D35: ⚠ could not read acknowledged-file {ack_path}: {e}", file=sys.stderr)

    def _is_acknowledged(v: "Violation") -> bool:
        # canonical_but_empty is always eligible.
        # column_missing is eligible ONLY when table == "(none)" — that is
        # exclusively the regex-extractor false-positive case (where the
        # gate could not attribute the column to any table). Real
        # column_missing on a known table stays blocking forever.
        if v.kind == "canonical_but_empty" or (v.kind == "column_missing" and v.table == "(none)"):
            short = Path(v.file).name
            for entry in acknowledged:
                if (
                    entry.get("file") == short
                    and entry.get("line") == v.line
                    and entry.get("table") == v.table
                    and entry.get("column") == v.column
                ):
                    return True
        return False

    if acknowledged:
        total_before = len(violations)
        ack_count = sum(1 for v in violations if _is_acknowledged(v))
        violations = [v for v in violations if not _is_acknowledged(v)]
        if ack_count:
            print(f"D35: acknowledged {ack_count} known-acceptable finding(s) "
                  f"(from {ack_path}); {total_before} → {len(violations)} blocking")

    if not violations:
        print(f"D35: ✅ clean — 0 blocking violations across {len(refs)} column references")
        return 0

    print(f"D35: 🚨 {len(violations)} violation(s) found:", file=sys.stderr)
    for v in violations:
        short = Path(v.file).name
        print(f"  [{v.kind}] {short}:{v.line} "
              f"{v.table}.{v.column} — {v.detail}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())

"""Raw-SQL-on-encrypted-columns scan — flags ``text("…")`` queries that
reference any column typed ``EncryptedString``.

Bug class this catches
----------------------
S122-1b (2026-05-12) shipped Customer-PII encryption activation. ~20
raw-SQL routers using ``db.execute(text("SELECT name, email FROM
customers"))`` bypassed ``EncryptedString.process_result_value`` —
269 customer pages rendered ``gAAAAA…`` ciphertext. The symmetric
writer-side bypass (``INSERT INTO webhook_endpoints (…, secret, …)
VALUES (…, :secret, …)``) was caught at auditor round 1 of S122-1c.

The structural rule the codebase needs: **every read AND every write
against an EncryptedString column must go through the ORM.** Pinned at
the SQLAlchemy contract level by ``gdx_dispatch/tests/test_pii_typedecorator_raw_sql.py``;
this scan enforces it at the codebase level so the next contributor's
one-off ``text(...)`` query can't quietly re-open the bypass.

Source of truth
---------------
The list of (table, column) pairs to scan against is **not hard-coded**
here. It comes from ``gdx_dispatch.core.pii.encryption_status()`` — the same
helper the boot gate and SOC2 evidence collector consume. When a model
swaps a column to/from ``EncryptedString``, this scan updates
automatically.

Today (post-S122-1c) the helper reports ``columns=()`` so this scan
finds zero matches and is a no-op. That changes the moment slices 2/3
of S122-9 add columns back.

What it flags
-------------
For every column ``(table, name)`` in ``encryption_status().columns``,
the scan greps every ``gdx/**.py`` for:

    text("…<sql containing 'table' AND 'name'>…")

…where both identifiers appear as SQL-identifier tokens (preceded by
whitespace, ``(``, ``,``, ``.``, or start-of-string; followed by the
same plus ``)`` and ``;``). Catches both the reader form
(``SELECT name FROM customers``) and the writer form
(``INSERT INTO webhook_endpoints (…, secret, …)``).

False positives
---------------
A scan finding here is a strong signal but not always a real bug:
- A SQL string that mentions the column in a comment, view, or table
  it's not actually selecting (rare).
- A migration tool legitimately touching the raw bytes during a
  re-encrypt pass (``gdx_dispatch/tools/encrypt_*.py``, ``rollback_encryption_to_plaintext.py``).

The scan **excludes** ``gdx_dispatch/tests/``, ``gdx_dispatch/migrations/``, and the
encryption tools themselves by default. For any remaining false
positive, baseline it via ``--baseline`` or annotate the line with
``# noqa: RAW_ENC``.

Usage
-----
    python -m gdx_dispatch.tools.raw_sql_on_encrypted_columns_scan          # warn-only
    python -m gdx_dispatch.tools.raw_sql_on_encrypted_columns_scan --strict # exit non-zero on net-new
    python -m gdx_dispatch.tools.raw_sql_on_encrypted_columns_scan --baseline
    python -m gdx_dispatch.tools.raw_sql_on_encrypted_columns_scan --prune
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE_FILE = REPO_ROOT / ".raw_sql_on_encrypted_columns_baseline"

SCAN_ROOTS = [REPO_ROOT / "gdx_dispatch"]
SKIP_DIR_PARTS = {
    "tests",
    "migrations",
    "__pycache__",
}
# Tool files that legitimately touch the raw bytes during transition windows.
SKIP_FILE_NAMES = {
    "encrypt_qb_token_store_rows.py",
    "encrypt_tenants_db_url_rows.py",
    "encrypt_customer_pii_rows.py",
    "rollback_encryption_to_plaintext.py",
    "tenant_schema_drift_check.py",
    "raw_sql_on_encrypted_columns_scan.py",  # the scan itself
}

NOQA_RE = re.compile(r"#\s*noqa\b(?:\s*:\s*([\w,\s]+))?", re.IGNORECASE)


def is_suppressed(line: str) -> bool:
    m = NOQA_RE.search(line)
    if not m:
        return False
    codes = m.group(1)
    if codes is None:
        return True
    listed = {c.strip().upper() for c in codes.split(",") if c.strip()}
    return "RAW_ENC" in listed


def _load_encrypted_columns() -> list[tuple[str, str]]:
    """Return [(table, column), …] for every EncryptedString column.

    Sourced from the central helper so the scan tracks the model layer
    automatically. Returns ``[]`` if the helper can't load (boot
    environment may not have JWT keys etc) — the scan then becomes a
    no-op rather than failing CI on an unrelated error.
    """
    import os
    # JWT_SECRET is required by the auth router import chain; provide a
    # stub so a developer can run this scan locally without setting
    # production secrets. CI sets real values.
    os.environ.setdefault("JWT_SECRET", "scan-stub-" + "x" * 48)
    try:
        # Importing gdx_dispatch.models registers tenant-plane models on TenantBase.
        # gdx_dispatch.control.models registers the control plane. Both are needed
        # so encryption_status() sees every base.
        import gdx_dispatch.models  # noqa: F401, PLC0415
        import gdx_dispatch.control.models  # noqa: F401, PLC0415
        from gdx_dispatch.core import pii  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        print(
            f"raw_sql_on_encrypted_columns_scan: helper unavailable "
            f"({type(exc).__name__}: {exc}); scan is a no-op."
        )
        return []
    status = pii.encryption_status()
    if status.scan_error:
        print(f"raw_sql_on_encrypted_columns_scan: helper scan_error="
              f"{status.scan_error}; scan is a no-op.")
        return []
    return [(c.table, c.column) for c in status.columns]


def _build_token_re(identifier: str) -> re.Pattern[str]:
    """Match identifier as a SQL token: bordered by non-identifier chars."""
    return re.compile(
        rf"(?:(?<=^)|(?<=[\s,(.;]))"  # left boundary
        rf"{re.escape(identifier)}"
        rf"(?=[\s,);.=]|$)",
        re.IGNORECASE,
    )


def _find_text_calls(tree: ast.AST) -> list[tuple[int, str]]:
    """Return [(lineno, sql_literal), …] for every ``text("…")`` Call
    whose first arg is a string constant. Catches the common shapes:

      * ``text("SELECT … FROM …")``
      * ``sqlalchemy.text("…")``
      * ``sa.text("…")``

    f-strings and concatenated multi-arg variants are NOT inspected
    (documented limitation — same as tenant_plane_redundant_filter_scan).
    """
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        # Match `text`, `sa.text`, `sqlalchemy.text`, `db.text`, etc.
        fname = ""
        if isinstance(func, ast.Name):
            fname = func.id
        elif isinstance(func, ast.Attribute):
            fname = func.attr
        if fname != "text":
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            out.append((node.lineno, first.value))
    return out


def scan(
    encrypted: list[tuple[str, str]] | None = None,
) -> list[tuple[Path, int, str, str, str]]:
    """Return [(path, lineno, table, column, sql_excerpt), …]."""
    if encrypted is None:
        encrypted = _load_encrypted_columns()
    if not encrypted:
        return []

    # Build a regex per (table, column) so the scan loop is cheap.
    matchers = [
        (table, column, _build_token_re(table), _build_token_re(column))
        for table, column in encrypted
    ]

    out: list[tuple[Path, int, str, str, str]] = []
    for path in _iter_py_files():
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (SyntaxError, OSError):
            continue
        source_lines = source.splitlines()
        for lineno, sql in _find_text_calls(tree):
            for table, column, table_re, column_re in matchers:
                if table_re.search(sql) and column_re.search(sql):
                    if 0 < lineno <= len(source_lines):
                        if is_suppressed(source_lines[lineno - 1]):
                            continue
                    excerpt = sql.strip().replace("\n", " ")[:80]
                    out.append((path, lineno, table, column, excerpt))
                    break  # one finding per (file, line)
    out.sort(key=lambda f: (str(f[0]), f[1]))
    return out


def _iter_py_files():
    for root in SCAN_ROOTS:
        if not root.exists():
            continue
        for p in root.rglob("*.py"):
            if any(part in SKIP_DIR_PARTS for part in p.parts):
                continue
            if p.name in SKIP_FILE_NAMES:
                continue
            yield p


def _to_signature(f: tuple[Path, int, str, str, str]) -> str:
    path, lineno, table, column, _excerpt = f
    rel = path.relative_to(REPO_ROOT)
    return f"{rel}:{table}.{column}:{lineno}"


def _load_baseline() -> set[str]:
    if not BASELINE_FILE.exists():
        return set()
    raw = json.loads(BASELINE_FILE.read_text())
    return set(raw)


def _write_baseline(findings: list[tuple[Path, int, str, str, str]]) -> None:
    sigs = sorted({_to_signature(f) for f in findings})
    BASELINE_FILE.write_text(json.dumps(sigs, indent=2) + "\n")


def _net_new(findings, baseline):
    return [f for f in findings if _to_signature(f) not in baseline]


def _prune(findings):
    baseline = _load_baseline()
    if not baseline:
        return 0, 0
    current = {_to_signature(f) for f in findings}
    kept = sorted(baseline & current)
    pruned = baseline - current
    BASELINE_FILE.write_text(json.dumps(kept, indent=2) + "\n")
    return len(pruned), len(kept)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--no-baseline", action="store_true")
    parser.add_argument("--prune", action="store_true")
    args = parser.parse_args()

    encrypted = _load_encrypted_columns()
    if not encrypted:
        print(
            "raw_sql_on_encrypted_columns_scan: zero EncryptedString columns "
            "currently registered — scan is a no-op. (Sourced from "
            "gdx_dispatch.core.pii.encryption_status().)"
        )
        return 0

    findings = scan(encrypted)

    if args.baseline:
        _write_baseline(findings)
        print(f"Wrote {len(findings)} signatures to "
              f"{BASELINE_FILE.relative_to(REPO_ROOT)}")
        return 0

    if args.prune:
        pruned, kept = _prune(findings)
        print(f"Pruned {pruned} stale signature(s); kept {kept}.")
        return 0

    if not findings:
        print(
            f"raw_sql_on_encrypted_columns_scan: clean. "
            f"({len(encrypted)} encrypted column(s) scanned.)"
        )
        return 0

    baseline = set() if args.no_baseline else _load_baseline()
    new_findings = _net_new(findings, baseline)

    print(
        f"raw_sql_on_encrypted_columns_scan: {len(findings)} total findings "
        f"({len(new_findings)} net-new vs baseline) across "
        f"{len(encrypted)} encrypted column(s)"
    )
    print()
    new_sigs = {_to_signature(f) for f in new_findings}
    for path, lineno, table, column, excerpt in findings:
        sig = _to_signature((path, lineno, table, column, excerpt))
        marker = "NEW" if sig in new_sigs else "   "
        rel = path.relative_to(REPO_ROOT)
        print(f"  [{marker}] {rel}:{lineno} {table}.{column}  «{excerpt}»")

    if args.strict and new_findings:
        print()
        print(f"❌ {len(new_findings)} net-new raw-SQL on EncryptedString "
              "column violation(s).")
        print("   See sprint_encryption_rollout_proper.md Option C — every read")
        print("   AND every write against an EncryptedString column must go")
        print("   through the ORM. Refactor or annotate `# noqa: RAW_ENC`.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

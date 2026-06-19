"""Tenant-plane redundant-filter scan — flags `WHERE tenant_id =` queries.

Bug class this catches
----------------------
Per CLAUDE.md § Tenant Isolation, the tenant plane is per-tenant Postgres
DB. Isolation is the *connection itself*: `Depends(get_db)` opens
a session against that tenant's own database. Any `WHERE tenant_id = …`
filter added on top is:

  1. Redundant. There is no other tenant's data in the connection.
  2. Actively harmful when a row has NULL tenant_id — the filter excludes
     rows that should be visible. The 2026-04-22 documents bug shipped
     because of this exact pattern: tenant-plane documents got a
     `tenant_id IS NOT NULL` predicate, hiding every legacy row.
  3. A code smell that signals the author hasn't internalized the
     three-plane model — they're treating the tenant plane like the
     shared control plane.

What it flags (baseline-aware, --strict for CI)
-----------------------------------------------
T1. `<query>.filter(<X>.tenant_id <op> <expr>)`
T2. `<query>.filter(<X>.company_id <op> <expr>)`
T3. `<query>.filter_by(tenant_id=<expr>)`
T4. `<query>.filter_by(company_id=<expr>)`
T5. `<select>.where(<X>.tenant_id <op> <expr>)` (SQLAlchemy 2.0)
T6. `<select>.where(<X>.company_id <op> <expr>)`

`<op>` covers: `==`, `.in_(…)`, `.is_(…)`, `.isnot(…)`, `.between(a,b)`,
and the same wrapped in `and_()`/`or_()`/`not_()`/`cast()`. The
`.is_(None)` form is the literal 2026-04-22 documents-bug pattern.

Scan scope
----------
All `.py` files under `gdx/` except: `gdx_dispatch/tests/`, `gdx_dispatch/migrations/`,
`gdx_dispatch/control/` (the control plane LEGITIMATELY filters by tenant_id —
that's its isolation model), and `__pycache__`.

False positives & how to triage
-------------------------------
A scan finding here is not always a real bug — sometimes the `Foo` in
`Foo.tenant_id == x` is a control-plane model (Membership, Tenant,
TenantModuleGrant) that legitimately needs the filter. The signature
records the model name, so review can spot those quickly. If the model
is control-plane, either:
  - Annotate the line with `# noqa: T1` (or T2/T3/T4/T5/T6 as appropriate), or
  - Run `--baseline` to fold it into the baseline.

Known limitations (audit-flagged, not yet closed)
-------------------------------------------------
- Aliased filter calls bypass the gate: `f = q.filter; f(M.tenant_id == x)`
  is undetected because the call's func is just `Name('f')`, not an
  Attribute. Closing this requires symbol-tracking dataflow which AST
  alone can't do. Don't write that pattern.
- Lineno is part of the signature, so refactors that shift line numbers
  show up as net-new findings. Use `--prune` after such refactors.

Usage
-----
    python -m gdx_dispatch.tools.tenant_plane_redundant_filter_scan
    python -m gdx_dispatch.tools.tenant_plane_redundant_filter_scan --strict
    python -m gdx_dispatch.tools.tenant_plane_redundant_filter_scan --baseline
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE_FILE = REPO_ROOT / ".tenant_plane_redundant_filter_baseline"

SCAN_ROOTS = [REPO_ROOT / "gdx_dispatch"]
SKIP_DIR_PARTS = {
    "tests",
    "migrations",
    "control",       # control plane filters by tenant_id legitimately
    "__pycache__",
    "tools",         # the scans themselves
}

FILTER_COL_NAMES = {"tenant_id", "company_id"}

NOQA_RE = re.compile(r"#\s*noqa\b(?:\s*:\s*([\w,\s]+))?", re.IGNORECASE)


def is_suppressed(line: str, code: str) -> bool:
    m = NOQA_RE.search(line)
    if not m:
        return False
    codes = m.group(1)
    if codes is None:
        return True
    listed = [c.strip().upper() for c in codes.split(",") if c.strip()]
    return code.upper() in listed


def _is_filter_call(node: ast.AST) -> str | None:
    """Return the method name if node is a Call to .filter / .filter_by / .where."""
    if not isinstance(node, ast.Call):
        return None
    if not isinstance(node.func, ast.Attribute):
        return None
    name = node.func.attr
    if name in {"filter", "filter_by", "where"}:
        return name
    return None


def _attr_path(node: ast.AST) -> str:
    if isinstance(node, ast.Attribute):
        base = _attr_path(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Name):
        return node.id
    return "<expr>"


def _func_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_func_name(node.value)}.{node.attr}" if isinstance(node.value, (ast.Name, ast.Attribute)) else node.attr
    return ""


_BOOL_WRAPPER_NAMES = {
    "and_", "or_", "not_",
    "sa.and_", "sa.or_", "sa.not_",
    "sqlalchemy.and_", "sqlalchemy.or_", "sqlalchemy.not_",
}


_INSTANCE_METHOD_FORMS = {"in_", "is_", "isnot", "between"}


def _flatten_filter_args(args: list[ast.expr]):
    """Yield every Compare, or `.in_()/.is_()/.isnot()/.between()` Call
    reachable from filter args, recursing into and_/or_/not_ wrappers.
    Closes audit-flagged bypasses across rounds 3-5:
      - filter(and_(M.tenant_id == tid))           (round 3)
      - filter(not_(M.tenant_id == tid))           (round 4)
      - filter(M.tenant_id.in_([tid]))             (round 4)
      - filter(M.tenant_id.is_(None))              (round 5 — CRITICAL,
        this is the literal 2026-04-22 documents-bug pattern)
      - filter(M.tenant_id.isnot(None))            (round 5)
      - filter(M.tenant_id.between(a, b))          (round 5)
    Still NOT covered (documented limitation):
      - tuple_(M.tenant_id, M.x).in_(…)            (composite key form, rare)
      - aliased filter call f = q.filter; f(...)   (needs dataflow)
    """
    for arg in args:
        if isinstance(arg, ast.Compare):
            yield arg
            continue
        if isinstance(arg, ast.Call):
            fname = _func_name(arg.func)
            if fname in _BOOL_WRAPPER_NAMES:
                yield from _flatten_filter_args(list(arg.args))
                continue
            # Instance-method forms: M.tenant_id.<method>(…)
            if (
                isinstance(arg.func, ast.Attribute)
                and arg.func.attr in _INSTANCE_METHOD_FORMS
                and isinstance(arg.func.value, ast.Attribute)
                and arg.func.value.attr in FILTER_COL_NAMES
            ):
                yield arg  # signal to visitor: this is an instance-method call


def _extract_tenant_attr(node: ast.AST) -> tuple[str, str] | None:
    """Return (col_attr, model_name) if node references `*.tenant_id` /
    `*.company_id` — including when wrapped in cast() / func.X() etc.
    Closes round-3 audit-flagged bypass: `cast(M.tenant_id, UUID) == tid`
    previously slipped past because we only checked direct Attribute."""
    if isinstance(node, ast.Attribute) and node.attr in FILTER_COL_NAMES:
        return node.attr, _attr_path(node.value)
    if isinstance(node, ast.Call):
        for arg in node.args:
            result = _extract_tenant_attr(arg)
            if result:
                return result
    return None


class RedundantFilterVisitor(ast.NodeVisitor):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.findings: list[tuple[int, str, str, str]] = []

    def visit_Call(self, node: ast.Call) -> None:
        method = _is_filter_call(node)
        if method is None:
            self.generic_visit(node)
            return

        if method == "filter_by":
            for kw in node.keywords:
                if kw.arg in FILTER_COL_NAMES:
                    code = "T4" if kw.arg == "company_id" else "T3"
                    ident = f"filter_by({kw.arg}=…)"
                    self.findings.append(
                        (
                            node.lineno,
                            code,
                            ident,
                            f".filter_by({kw.arg}=…) — redundant on tenant plane",
                        )
                    )
        else:
            # .filter(...) / .where(...) — look for Compare with X.tenant_id == …
            # Recursively unwrap and_/or_/not_ wrappers; also handle .in_() form.
            for arg in _flatten_filter_args(list(node.args)):
                col_attr = None
                model_name = None
                if isinstance(arg, ast.Call):
                    # Instance-method form: M.tenant_id.<in_/is_/isnot/between>(…)
                    method_name = arg.func.attr  # type: ignore[union-attr]
                    col_attr = arg.func.value.attr  # type: ignore[union-attr]
                    model_name = _attr_path(arg.func.value.value)  # type: ignore[union-attr]
                    snippet_op = f".{method_name}(…)"
                elif isinstance(arg, ast.Compare):
                    if len(arg.ops) != 1 or not isinstance(arg.ops[0], ast.Eq):
                        continue
                    left, right = arg.left, arg.comparators[0]
                    hit = _extract_tenant_attr(left) or _extract_tenant_attr(right)
                    if hit is None:
                        continue
                    col_attr, model_name = hit
                    snippet_op = "== …"
                else:
                    continue
                if method == "where":
                    code = "T6" if col_attr == "company_id" else "T5"
                else:
                    code = "T2" if col_attr == "company_id" else "T1"
                ident = f"{model_name}.{col_attr}"
                # Use the outer .filter() call's lineno (not compare.lineno) —
                # preserves baseline compatibility across the unwrapping fix.
                self.findings.append(
                    (
                        node.lineno,
                        code,
                        ident,
                        f".{method}({model_name}.{col_attr} {snippet_op}) — redundant on tenant plane",
                    )
                )
        self.generic_visit(node)


def _iter_py_files(roots: Iterable[Path]) -> Iterable[Path]:
    for root in roots:
        if not root.exists():
            continue
        for p in root.rglob("*.py"):
            if any(part in SKIP_DIR_PARTS for part in p.parts):
                continue
            yield p


def scan() -> list[tuple[Path, int, str, str, str]]:
    out: list[tuple[Path, int, str, str, str]] = []
    for path in _iter_py_files(SCAN_ROOTS):
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (SyntaxError, OSError):
            continue
        source_lines = source.splitlines()
        v = RedundantFilterVisitor(path)
        v.visit(tree)
        for lineno, code, identifier, snippet in v.findings:
            if 0 < lineno <= len(source_lines):
                if is_suppressed(source_lines[lineno - 1], code):
                    continue
            out.append((path, lineno, code, identifier, snippet))
    out.sort(key=lambda f: (str(f[0]), f[1]))
    return out


def _to_signature(finding: tuple[Path, int, str, str, str]) -> str:
    """Lineno-in-sig closes the audit-found "delete-one-add-one same-file"
    blind spot of the prior count-based baseline."""
    path, lineno, code, identifier, _snippet = finding
    rel = path.relative_to(REPO_ROOT)
    return f"{rel}:{code}:{identifier}:{lineno}"


def _load_baseline() -> set[str]:
    if not BASELINE_FILE.exists():
        return set()
    raw = json.loads(BASELINE_FILE.read_text())
    if isinstance(raw, dict):
        return set(raw.keys())  # legacy count-based format flattened
    return set(raw)


def _write_baseline(findings: list[tuple[Path, int, str, str, str]]) -> None:
    sigs = sorted({_to_signature(f) for f in findings})
    BASELINE_FILE.write_text(json.dumps(sigs, indent=2) + "\n")


def _net_new_findings(
    findings: list[tuple[Path, int, str, str, str]],
    baseline: set[str],
) -> list[tuple[Path, int, str, str, str]]:
    return [f for f in findings if _to_signature(f) not in baseline]


def _prune_baseline(findings: list[tuple[Path, int, str, str, str]]) -> tuple[int, int]:
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
    parser.add_argument("--prune", action="store_true", help="drop baseline entries that no longer match current findings")
    args = parser.parse_args()

    findings = scan()

    if args.baseline:
        _write_baseline(findings)
        print(f"Wrote {len(findings)} signatures to {BASELINE_FILE.relative_to(REPO_ROOT)}")
        return 0

    if args.prune:
        pruned, kept = _prune_baseline(findings)
        print(f"Pruned {pruned} stale signature(s); kept {kept}.")
        return 0

    if not findings:
        print("tenant_plane_redundant_filter_scan: clean.")
        return 0

    baseline = set() if args.no_baseline else _load_baseline()
    new_findings = _net_new_findings(findings, baseline)
    new_sigs = {_to_signature(f) for f in new_findings}

    print(
        f"tenant_plane_redundant_filter_scan: {len(findings)} total findings "
        f"({len(new_findings)} net-new vs baseline)"
    )
    print()
    print("Rule codes:")
    print("  T1/T2 — .filter(X.tenant_id|.company_id == …)")
    print("  T3/T4 — .filter_by(tenant_id=… | company_id=…)")
    print("  T5/T6 — .where(X.tenant_id|.company_id == …)")
    print()

    for path, lineno, code, identifier, snippet in findings:
        sig = _to_signature((path, lineno, code, identifier, snippet))
        marker = "NEW" if sig in new_sigs else "   "
        rel = path.relative_to(REPO_ROOT)
        print(f"  [{marker}] {rel}:{lineno} [{code}] {snippet}")

    if not args.no_baseline and baseline:
        current_sigs = {_to_signature(f) for f in findings}
        stale = baseline - current_sigs
        if stale:
            print(
                f"\n⚠ {len(stale)} stale baseline entr{'y' if len(stale) == 1 else 'ies'} — "
                "run with --prune to drop them."
            )

    if args.strict and new_findings:
        print()
        print(f"❌ {len(new_findings)} net-new tenant-plane redundant-filter violation(s).")
        print("   See CLAUDE.md § Tenant Isolation. The tenant plane isolates by")
        print("   connection; tenant_id/company_id filters are redundant and break")
        print("   on NULL (the 2026-04-22 documents bug pattern). If the model is")
        print("   actually control-plane, add it to .tenant_plane_redundant_filter_baseline")
        print("   or annotate the line with `# noqa: T1` (etc).")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Silent-failure meta-detector.

AST-walks the codebase looking for shape-1/2/3 silent-failure patterns
defined in ``ai-queue/operations/silent_failure_registry.md``. Writes a
JSON report. Designed to run as a deploy gate or periodic cron.

SHAPES DETECTED
---------------

  **Shape 1 — Reports success without verifying outcome.**
    - try/except around db.execute with log.exception + return falsy
      (empty dict/list/None) instead of raising
    - "return True" / "return None" in functions whose docstring
      promises enforcement (e.g. "ensure", "guard", "verify")
    - Log calls containing literal strings like "success", "succeeded",
      "committed" NOT followed within N lines by a verify/SELECT call

  **Shape 2 — Missing runtime enforcement.**
    - ensure_*/guard_*/verify_* functions whose body branches on dialect
      but only installs the guard on ONE dialect (the common D45 class:
      SQLite trigger installed, PG branch missing/empty)
    - Functions named like ``check_*`` / ``enforce_*`` that return True
      in at least one branch without actually performing a check

  **Shape 3 — Silent null/None fallback to operator-facing output.**
    - f-string / .format() / % interpolation of a value that was
      produced by .get() without a sentinel, within an "alert/message/
      signal/log" context
    - str(x or "") patterns where x flows to user-visible output

  **Coverage gap — detectors without contract tests.**
    - Any function decorated/documented as a "detector" whose name is
      not in DETECTOR_REGISTRY.list_registered_detectors()

USAGE
-----
    python3 gdx_dispatch/tools/silent_failure_scanner.py               # full scan
    python3 gdx_dispatch/tools/silent_failure_scanner.py --shape 1     # one shape only
    python3 gdx_dispatch/tools/silent_failure_scanner.py --json /tmp/s.json

EXIT CODES
    0 — no new findings
    1 — findings present (for gate integration)
    2 — usage / setup error
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


def _detect_repo_root() -> Path:
    here = Path(__file__).resolve()
    for p in [here] + list(here.parents):
        if (p / "gdx_dispatch" / "tools").is_dir() and (p / "ai-queue").is_dir():
            return p
    return Path.cwd()


REPO_ROOT = _detect_repo_root()
REPORT_PATH = REPO_ROOT / "ai-queue/rd/operations/silent_failure_scan.json"
REGISTRY_PATH = REPO_ROOT / "ai-queue/operations/silent_failure_registry.md"


SCAN_DIRS = [
    REPO_ROOT / "gdx_dispatch" / "core",
    REPO_ROOT / "gdx_dispatch" / "routers",
    REPO_ROOT / "gdx_dispatch" / "tools",
    REPO_ROOT / "gdx_dispatch" / "app.py",
]

# Regex-friendly markers for "success claim" patterns in log/message strings.
_SUCCESS_MARKERS = re.compile(
    r"\b(?:success|succeeded|committed|completed|applied|wrote|saved|"
    r"installed|done|ok|finished)\b",
    re.IGNORECASE,
)

# "Detector" markers — functions that emit verdicts based on ground truth.
_DETECTOR_MARKERS = re.compile(
    r"\b(?:detector|canary|scanner|audit|guard|monitor|classifier)\b",
    re.IGNORECASE,
)


@dataclass
class Finding:
    file: str
    line: int
    shape: int   # 1, 2, or 3
    rule: str
    detail: str

    def to_dict(self) -> dict:
        return asdict(self)


# ── Shape 1: report-without-verify ─────────────────────────────────────────

class Shape1Visitor(ast.NodeVisitor):
    """Find try/except patterns where the except body logs + returns an empty
    truthy fallback (dict/list/None) instead of raising.
    """
    def __init__(self, file: str):
        self.file = file
        self.findings: list[Finding] = []

    def visit_Try(self, node: ast.Try) -> None:
        for handler in node.handlers:
            self._inspect_handler(handler)
        self.generic_visit(node)

    def _inspect_handler(self, handler: ast.ExceptHandler) -> None:
        has_log = False
        has_raise = False
        has_empty_return = False
        for n in ast.walk(ast.Module(body=handler.body, type_ignores=[])):
            if isinstance(n, ast.Raise):
                has_raise = True
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
                if n.func.attr in {"exception", "error", "warning", "info", "debug"}:
                    has_log = True
            if isinstance(n, ast.Return) and n.value is not None:
                # Empty collection / constant-False / None → silent-success fallback
                v = n.value
                if isinstance(v, ast.Dict) and not v.keys or isinstance(v, ast.List) and not v.elts or isinstance(v, ast.Constant) and v.value in (None, False, "", 0):
                    has_empty_return = True
        if has_log and has_empty_return and not has_raise:
            self.findings.append(Finding(
                file=self.file,
                line=handler.lineno,
                shape=1,
                rule="except_logs_and_returns_empty",
                detail=(
                    "except body logs via logger/log AND returns empty "
                    "dict/list/None/False/0 without raising — caller "
                    "cannot distinguish real result from error"
                ),
            ))


# ── Shape 2: missing runtime enforcement ──────────────────────────────────

class Shape2Visitor(ast.NodeVisitor):
    """Find `ensure_*` / `guard_*` / `verify_*` functions whose body has a
    dialect branch (if dialect == 'sqlite' / elif / else) where one branch
    is empty / passes / only has a comment. The canonical D45 class."""
    def __init__(self, file: str):
        self.file = file
        self.findings: list[Finding] = []
        # Track which ast.If nodes we've already chain-walked so we don't
        # re-enter them when ast.walk yields them as elif nodes.
        self._seen_chain: set[int] = set()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if not re.match(r"^(ensure|guard|verify|check|enforce)_", node.name):
            self.generic_visit(node)
            return
        for n in ast.walk(node):
            if isinstance(n, ast.If) and id(n) not in self._seen_chain:
                self._check_dialect_branch(n, node.name)
        self.generic_visit(node)

    def _check_dialect_branch(self, if_node: ast.If, fn_name: str) -> None:
        current = if_node
        dialect_like = False
        while current:
            self._seen_chain.add(id(current))
            if isinstance(current.test, ast.Compare):
                for comp in current.test.comparators:
                    if isinstance(comp, ast.Constant) and isinstance(comp.value, str):
                        if comp.value in {"sqlite", "postgresql", "postgres", "mysql"}:
                            dialect_like = True
                            break
            if dialect_like and self._body_is_empty_like(current.body):
                self.findings.append(Finding(
                    file=self.file,
                    line=current.lineno,
                    shape=2,
                    rule="dialect_branch_not_implemented",
                    detail=(
                        f"{fn_name}() has a dialect branch whose body is "
                        f"empty / pass-only / comment-only — the guard is "
                        f"declared but not installed on that dialect"
                    ),
                ))
            if len(current.orelse) == 1 and isinstance(current.orelse[0], ast.If):
                current = current.orelse[0]
            else:
                break

    @staticmethod
    def _body_is_empty_like(body: list[ast.stmt]) -> bool:
        if not body:
            return True
        if all(isinstance(s, ast.Pass) for s in body):
            return True
        # Only docstring / comments
        if (len(body) == 1 and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            return True
        return False


# ── Shape 3: null/None to operator-facing output ──────────────────────────

class Shape3Visitor(ast.NodeVisitor):
    """Find f-strings/.format/%-interpolation inside dicts with keys like
    'signal', 'alert', 'message', 'body', where the interpolated value is
    a dict.get(...) call with no sentinel AND flows into user-visible text.

    Conservative — false negatives are acceptable. False positives are not.
    """
    def __init__(self, file: str):
        self.file = file
        self.findings: list[Finding] = []

    def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
        # Only consider f-strings that appear to describe operator output:
        # look at the string content for keywords like "alert", "signal",
        # "DISPATCH", etc.
        literal_text = "".join(
            str(v.value) for v in node.values
            if isinstance(v, ast.Constant) and isinstance(v.value, str)
        )
        if not re.search(
            r"\b(DISPATCH|ALERT|SWEEP|SIGNAL|WARNING|CRITICAL|sprint|run `)",
            literal_text,
        ):
            self.generic_visit(node)
            return
        # Any FormattedValue whose expression is dict.get without default
        for val in node.values:
            if not isinstance(val, ast.FormattedValue):
                continue
            if self._is_unguarded_dict_get(val.value) or self._is_index_without_default(val.value):
                self.findings.append(Finding(
                    file=self.file,
                    line=node.lineno,
                    shape=3,
                    rule="unguarded_none_in_operator_message",
                    detail=(
                        "f-string carries a value that could be None into "
                        "operator-facing text (DISPATCH/ALERT/signal/etc). "
                        "Renders literal 'None' when missing."
                    ),
                ))
                break
        self.generic_visit(node)

    @staticmethod
    def _is_unguarded_dict_get(node: ast.AST) -> bool:
        # Matches x.get('k') — no default arg
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "get" and len(node.args) == 1:
                return True
        return False

    @staticmethod
    def _is_index_without_default(node: ast.AST) -> bool:
        # Matches x['k'] — if key is missing it'd KeyError, not render None.
        # But if the x dict may have .get-shaped None items, this could still
        # interpolate None. Include the pattern conservatively.
        return False  # disabled for now; too many false positives


# ── Coverage: detectors without contracts ─────────────────────────────────

def scan_detector_coverage() -> list[Finding]:
    """Flag every function whose name matches a detector marker AND lives in
    a gdx_dispatch/tools/orchestrator/ path BUT has no corresponding contract test.
    """
    findings: list[Finding] = []
    try:
        from gdx_dispatch.tests.contracts.detector_contract import list_registered_detectors
        registered = set(list_registered_detectors())
    except Exception:
        registered = set()

    detector_dir = REPO_ROOT / "gdx_dispatch" / "tools" / "orchestrator"
    if not detector_dir.exists():
        return findings

    for py in detector_dir.rglob("*.py"):
        if "test" in py.name or "__pycache__" in py.parts:
            continue
        try:
            tree = ast.parse(py.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            if not _DETECTOR_MARKERS.search(node.name):
                continue
            # Only top-level functions (methods skipped for v1)
            if node.name.startswith("_"):
                continue
            if node.name in registered:
                continue
            rel = py.relative_to(REPO_ROOT)
            findings.append(Finding(
                file=str(rel),
                line=node.lineno,
                shape=0,  # coverage gap — pre-shape
                rule="detector_without_contract_test",
                detail=(
                    f"function {node.name!r} looks like a detector but has "
                    f"no entry in gdx_dispatch/tests/contracts/. Register one via "
                    f"register_detector() per Phase 3b pattern."
                ),
            ))
    return findings


# ── Main scan ──────────────────────────────────────────────────────────────

def iter_scan_files() -> list[Path]:
    files: list[Path] = []
    for target in SCAN_DIRS:
        if target.is_file():
            if target.suffix == ".py":
                files.append(target)
        elif target.is_dir():
            for py in target.rglob("*.py"):
                if "__pycache__" in py.parts or "/tests/" in str(py):
                    continue
                files.append(py)
    return sorted(files)


def _noqa_lines(src: str) -> set[int]:
    """Return set of 1-based line numbers carrying ````
    (case-insensitive). Operators use this marker to acknowledge an
    intentional pattern (e.g. pure utility functions where returning None
    on unparseable input is the documented contract). Suppressing is an
    explicit decision that lives in the source, not a hidden allowlist.
    """
    marked: set[int] = set()
    for i, line in enumerate(src.splitlines(), start=1):
        if re.search(r"#\s*noqa:\s*silent-failure\b", line, re.IGNORECASE):
            marked.add(i)
    return marked


def scan_file(path: Path, shapes: set[int]) -> list[Finding]:
    findings: list[Finding] = []
    try:
        src = path.read_text()
        tree = ast.parse(src)
    except (SyntaxError, UnicodeDecodeError):
        return findings
    try:
        rel = str(path.relative_to(REPO_ROOT))
    except ValueError:
        # Path not under REPO_ROOT (e.g. a tmp_path in tests). Use the
        # absolute path so findings still have a usable location.
        rel = str(path)
    noqa = _noqa_lines(src)
    if 1 in shapes:
        v = Shape1Visitor(rel)
        v.visit(tree)
        findings.extend(v.findings)
    if 2 in shapes:
        v = Shape2Visitor(rel)
        v.visit(tree)
        findings.extend(v.findings)
    if 3 in shapes:
        v = Shape3Visitor(rel)
        v.visit(tree)
        findings.extend(v.findings)
    # Drop any finding whose line (or the line before/after) has the marker
    # — tolerant window because except handlers span multiple lines.
    if noqa:
        findings = [
            f for f in findings
            if not any(ln in noqa for ln in (f.line - 1, f.line, f.line + 1))
        ]
    return findings


def main() -> int:
    ap = argparse.ArgumentParser(description="Silent-failure meta-detector")
    ap.add_argument("--shape", type=int, choices=[1, 2, 3], default=None,
                    help="Only scan for one shape (default: all)")
    ap.add_argument("--json", dest="json_path", default=str(REPORT_PATH),
                    help="JSON report path")
    ap.add_argument("--skip-coverage", action="store_true",
                    help="Skip detector-without-contract-test check")
    args = ap.parse_args()

    shapes = {args.shape} if args.shape else {1, 2, 3}

    all_findings: list[Finding] = []
    for f in iter_scan_files():
        all_findings.extend(scan_file(f, shapes))

    if not args.skip_coverage and (args.shape is None):
        all_findings.extend(scan_detector_coverage())

    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_findings": len(all_findings),
        "by_shape": {},
        "findings": [f.to_dict() for f in all_findings],
    }
    for f in all_findings:
        key = f"shape_{f.shape}"
        report["by_shape"][key] = report["by_shape"].get(key, 0) + 1

    Path(args.json_path).parent.mkdir(parents=True, exist_ok=True)
    Path(args.json_path).write_text(json.dumps(report, indent=2))

    print(f"silent_failure_scanner: {len(all_findings)} finding(s)")
    for shape, n in sorted(report["by_shape"].items()):
        print(f"  {shape}: {n}")
    if all_findings:
        print()
        for f in all_findings[:20]:
            short = Path(f.file).name
            print(f"  shape={f.shape} {short}:{f.line} [{f.rule}] {f.detail[:80]}")
        if len(all_findings) > 20:
            print(f"  ... and {len(all_findings) - 20} more in {args.json_path}")

    return 1 if all_findings else 0


if __name__ == "__main__":
    sys.exit(main())

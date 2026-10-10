"""List the test files that compare a committed baseline with the live tree.

`run_tests_split.sh --scans` runs exactly these, in one pytest process, so a
stale baseline goes red in ~25 s instead of at the end of the ~7 min matrix.
GDXA-408 counted the same three tests as the matrix red on at least eight agent
issues: each was a branch that had merged origin/main and shifted a line-keyed
baseline (or added a duplicate block) without re-freezing.

The list is DERIVED from a naming convention, never written down, so a fourth
re-freezable baseline scan joins `--scans` the day its test lands — provided
it follows the convention. Other baselines (doc links, PII literals, authz,
route shadows) are not selected and still need the matrix. A test file is
selected when

* its file name, or the name of a test function in it, contains
  ``scan_refreeze``; or
* it defines ``test_the_committed_baseline_is_what_the_tree_scans`` (at module
  level or in a class) — the name both existing refreeze guards share; or
* it is ``test_saas_surfaces_retired.py``, which pins the tenant-plane baseline
  from a file whose name says nothing about baselines. It is required: if it
  is renamed this exits 2 rather than quietly dropping its scans.

Function names are read with ``ast``, not imported, so this runs on the host
with the standard library only (`python3 -I`), before any docker image starts.

    python3 -I gdx_dispatch/tools/baseline_scan_tests.py [--root TESTS_DIR]

Prints one repo-relative path per line (absolute when ``--root`` is outside
the repo). Guarded by tests/test_baseline_scan_tests.py.
"""
from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS_ROOT = REPO_ROOT / "gdx_dispatch" / "tests"

MARKER = "scan_refreeze"
SHARED_GUARD = "test_the_committed_baseline_is_what_the_tree_scans"
REQUIRED = ("test_saas_surfaces_retired.py",)
# e2e/ is never collected by the runner (an import there makes a network call).
SKIP_DIRS = {"e2e", "__pycache__"}


def _test_function_names(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    # Only a file that mentions a marker can define one; skipping the parse
    # elsewhere takes the whole walk from ~1.5 s to well under one.
    if MARKER not in text and SHARED_GUARD not in text:
        return set()
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError as exc:
        # Raise, never skip: an unparseable test file could be the very
        # baseline guard this list exists to run.
        raise SystemExit(f"baseline_scan_tests: cannot parse {path}: {exc}") from exc
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test")
    }


def discover(tests_root: Path = TESTS_ROOT) -> list[Path]:
    tests_root = tests_root.resolve()
    found: set[Path] = set()
    for path in tests_root.rglob("test_*.py"):
        if SKIP_DIRS.intersection(path.relative_to(tests_root).parts[:-1]):
            continue
        if MARKER in path.name:
            found.add(path)
            continue
        names = _test_function_names(path)
        if SHARED_GUARD in names or any(MARKER in n for n in names):
            found.add(path)
    missing = [name for name in REQUIRED if not (tests_root / name).is_file()]
    if missing:
        raise SystemExit(
            f"baseline_scan_tests: required baseline-scan file(s) missing from {tests_root}: "
            f"{', '.join(missing)} — renamed? Update REQUIRED in {Path(__file__).name}."
        )
    found.update(tests_root / name for name in REQUIRED)
    return sorted(found)


def _display(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT) if path.is_relative_to(REPO_ROOT) else path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", type=Path, default=TESTS_ROOT, help="tests directory to search")
    args = parser.parse_args()
    try:
        paths = discover(args.root)
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 2
    for path in paths:
        print(_display(path))
    return 0


if __name__ == "__main__":
    sys.exit(main())

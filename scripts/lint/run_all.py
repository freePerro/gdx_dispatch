"""
scripts/lint/run_all.py

Orchestrates all GDX template linters.

Usage:
    python scripts/lint/run_all.py                   # lint all templates
    python scripts/lint/run_all.py path/to/file.html  # lint specific files
    python scripts/lint/run_all.py --strict           # warnings become errors

Exit codes:
    0  clean
    1  errors found (or warnings in --strict mode)
    2  warnings only (non-strict mode)
"""

import argparse
import glob
import sys
from pathlib import Path

# Allow running from repo root: python scripts/lint/run_all.py
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.lint import lint_js, lint_html


def main() -> int:
    parser = argparse.ArgumentParser(description="GDX template linter")
    parser.add_argument(
        "paths", nargs="*",
        help="HTML files to lint (default: all archive/dispatch_flask/templates/**/*.html)",
    )
    parser.add_argument(
        "--strict", action="store_true",
        help="Treat warnings as errors (exit 1 instead of 2)",
    )
    args = parser.parse_args()

    # Resolve file list
    if args.paths:
        paths = args.paths
    else:
        repo_root = Path(__file__).resolve().parent.parent.parent
        pattern = str(repo_root / "archive/dispatch_flask" / "templates" / "**" / "*.html")
        paths = glob.glob(pattern, recursive=True)

    if not paths:
        print("lint: no HTML files found", file=sys.stderr)
        return 0

    # Run linters
    issues = lint_js.lint_files(paths) + lint_html.lint_files(paths)

    if not issues:
        print(f"lint: ✓ {len(paths)} file(s) clean")
        return 0

    # Sort by file + line for readable output
    issues.sort(key=lambda i: (i["file"], i["line"]))

    errors = [i for i in issues if i["level"] == "error"]
    warnings = [i for i in issues if i["level"] == "warning"]

    for issue in issues:
        level = issue["level"].upper()
        print(f"{level:7}  {issue['file']}:{issue['line']}  {issue['message']}")

    print(f"\nlint: {len(errors)} error(s), {len(warnings)} warning(s) in {len(paths)} file(s)")

    if errors:
        return 1
    if warnings:
        return 1 if args.strict else 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Duplicate-block scan — flags identical 5+ line blocks across the codebase.

Bug class this catches
----------------------
Per the Sonar / GitClear analyses (2024–2026), the dominant quality
signal in AI-accelerated codebases is a 4–8× rise in duplicated code:
the model's preference for "local correctness over global coherence"
manifests as the same five lines copied into a third file rather than
extracted into a shared helper.

This scan finds those clones at the source: any contiguous run of 5+
canonicalized lines (whitespace and comments stripped) that occurs in
two or more places — the same file, different files, doesn't matter.

What it flags (baseline-aware, --strict for CI)
-----------------------------------------------
D1. A canonical 5-line block whose content hash appears 2+ times across
    the scanned files.

How "canonical" is computed
---------------------------
Each line: strip leading/trailing whitespace, strip "# …" trailing
comments, normalize internal whitespace to single space. Lines that
become empty (blank lines, lines that were ONLY a comment) are dropped
before windowing. The resulting line stream is hashed in 5-line sliding
windows.

What it doesn't flag
--------------------
- Tests (`gdx_dispatch/tests/`) — fixture boilerplate is duplicate by design.
- Migrations (`gdx_dispatch/migrations/`) — alembic revisions repeat ops.
- Blocks composed entirely of imports — caught at the canonicalization
  step (we drop the trailing-comment, but a block of 5 imports is still
  legitimately flagged; in practice the baseline absorbs them).
- `__pycache__`.

The baseline mode is the same expand-contract pattern used by
`tenant_id_shape_scan.py` — capture the current state, gate fails only
on NET-NEW duplicate hashes.

Usage
-----
    python -m gdx_dispatch.tools.duplicate_block_scan
    python -m gdx_dispatch.tools.duplicate_block_scan --strict
    python -m gdx_dispatch.tools.duplicate_block_scan --baseline
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import tokenize
from collections import defaultdict
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE_FILE = REPO_ROOT / ".duplicate_block_baseline"

WINDOW = 5  # minimum block length to report
SCAN_ROOTS = [REPO_ROOT / "gdx_dispatch"]
SKIP_DIR_PARTS = {
    "tests",
    "migrations",
    "__pycache__",
    "node_modules",
    ".venv",
    "frontend",  # JS/Vue, not py
}


_WS_RE = re.compile(r"\s+")


def _canonicalize_via_tokenize(text: str) -> list[tuple[int, str]]:
    """Return [(original_lineno, canonical_text)] for non-blank lines.

    Uses `tokenize` to drop comment tokens (so `#` inside string literals
    is preserved) and collapse the remaining tokens line-by-line. This
    fixes the audit-found bug where a regex `#.*$` was mangling URLs and
    f-strings — same hash for structurally-different lines.
    """
    try:
        toks = list(tokenize.tokenize(io.BytesIO(text.encode("utf-8")).readline))
    except (tokenize.TokenizeError, SyntaxError, IndentationError):
        return []

    by_line: dict[int, list[str]] = defaultdict(list)
    drop = {
        tokenize.COMMENT,
        tokenize.NL,
        tokenize.NEWLINE,
        tokenize.ENCODING,
        tokenize.ENDMARKER,
        tokenize.INDENT,
        tokenize.DEDENT,
    }
    for tok in toks:
        if tok.type in drop:
            continue
        # Multi-line strings (triple-quoted, or backslash-continued) span
        # several source lines. Distribute their content per source line so
        # a 6-line copied docstring still produces 6 hashable canonical
        # lines (audit-flagged coverage hole — collapse-to-one-line hid
        # docstring clones).
        if tok.start[0] != tok.end[0]:
            chunks = tok.string.split("\n")
            for i, chunk in enumerate(chunks):
                by_line[tok.start[0] + i].append(chunk)
        else:
            by_line[tok.start[0]].append(tok.string)

    out: list[tuple[int, str]] = []
    for lineno in sorted(by_line):
        canonical = _WS_RE.sub(" ", " ".join(by_line[lineno])).strip()
        if canonical:
            out.append((lineno, canonical))
    return out


def _iter_py_files(roots: Iterable[Path]) -> Iterable[Path]:
    for root in roots:
        if not root.exists():
            continue
        for p in root.rglob("*.py"):
            if any(part in SKIP_DIR_PARTS for part in p.parts):
                continue
            yield p


def _hash(lines: list[str]) -> str:
    h = hashlib.sha256()
    for line in lines:
        h.update(line.encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()[:16]


def scan() -> dict[str, list[tuple[Path, int]]]:
    """Return {block_hash: [(path, lineno_start), …]} for blocks with 2+ occurrences."""
    # First pass: collect all 5-line windows from every file.
    # Window keyed by hash; value is list of (path, original_lineno_start).
    windows: dict[str, list[tuple[Path, int]]] = defaultdict(list)

    for path in _iter_py_files(SCAN_ROOTS):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue

        kept = _canonicalize_via_tokenize(text)  # [(lineno, canonical), …]
        if len(kept) < WINDOW:
            continue

        for i in range(len(kept) - WINDOW + 1):
            chunk = kept[i : i + WINDOW]
            block = [c for _, c in chunk]
            block_hash = _hash(block)
            start_line = chunk[0][0]
            windows[block_hash].append((path, start_line))

    # Filter to multi-occurrence groups
    return {h: locs for h, locs in windows.items() if len(locs) > 1}


def _load_baseline() -> set[str]:
    if not BASELINE_FILE.exists():
        return set()
    return set(json.loads(BASELINE_FILE.read_text()))


def _write_baseline(groups: dict[str, list[tuple[Path, int]]]) -> None:
    sigs = sorted(groups.keys())
    BASELINE_FILE.write_text(json.dumps(sigs, indent=2) + "\n")


def _prune_baseline(groups: dict[str, list[tuple[Path, int]]]) -> tuple[int, int]:
    """Drop baseline hashes that no longer appear in current scan results."""
    baseline = _load_baseline()
    if not baseline:
        return 0, 0
    current = set(groups.keys())
    kept = sorted(baseline & current)
    pruned = baseline - current
    BASELINE_FILE.write_text(json.dumps(kept, indent=2) + "\n")
    return len(pruned), len(kept)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--no-baseline", action="store_true")
    parser.add_argument("--prune", action="store_true", help="drop baseline hashes that no longer appear in current findings")
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="cap on groups to print (default 50)",
    )
    args = parser.parse_args()

    groups = scan()

    if args.baseline:
        _write_baseline(groups)
        print(f"Wrote {len(groups)} duplicate-block hashes to {BASELINE_FILE.relative_to(REPO_ROOT)}")
        return 0

    if args.prune:
        pruned, kept = _prune_baseline(groups)
        print(f"Pruned {pruned} stale hash(es); kept {kept}.")
        return 0

    if not groups:
        print("duplicate_block_scan: clean.")
        return 0

    baseline = set() if args.no_baseline else _load_baseline()
    new_hashes = [h for h in groups if h not in baseline]

    print(
        f"duplicate_block_scan: {len(groups)} duplicate group(s) "
        f"({len(new_hashes)} net-new vs baseline)"
    )
    print()

    # Print the new ones first, then a few existing ones for context
    sorted_new = sorted(new_hashes, key=lambda h: -len(groups[h]))
    sorted_old = sorted([h for h in groups if h in baseline], key=lambda h: -len(groups[h]))
    to_print = (sorted_new + sorted_old)[: args.limit]

    for h in to_print:
        locs = groups[h]
        marker = "NEW" if h not in baseline else "   "
        print(f"  [{marker}] hash={h} ({len(locs)} copies)")
        for path, lineno in locs[:8]:
            rel = path.relative_to(REPO_ROOT)
            print(f"        {rel}:{lineno}")
        if len(locs) > 8:
            print(f"        … and {len(locs) - 8} more")
        print()

    if not args.no_baseline and baseline:
        current_hashes = set(groups.keys())
        stale = baseline - current_hashes
        if stale:
            print(
                f"\n⚠ {len(stale)} stale baseline hash(es) — "
                "run with --prune to drop them."
            )

    if args.strict and new_hashes:
        print(f"❌ {len(new_hashes)} net-new duplicate-block group(s).")
        print("   Per Sonar/GitClear: AI-accelerated codebases see a 4–8× rise in")
        print("   clones; new clones almost always indicate a missing helper.")
        print("   Extract the shared block, or fold it into the baseline.")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Doc link scan — backticked file paths in docs that no longer resolve.

Bug class this catches
----------------------
Docs name files. Files move and get deleted; the doc keeps naming them. The
2026-09-01 doc audit found 168 dead backticked paths across 47 documents,
and while most were benign, a handful sent readers at things that were never
going to be there:

  * `BUILD_RULES.md` gave `python -m gdx_dispatch.tools.sync_tenant_db --apply`
    as the mandatory step after any tenant-plane model change. No such module.
  * `BUILD_RULES.md` and `encryption_at_rest.md` both cited
    `ARCHITECTURAL_STATE.md` as "the canonical picture". It has never existed.
  * `ARCHITECTURAL_INVARIANTS.md` named `tools/pollution_check.py` as the
    detection for an invariant it marked **enforced**.
  * `dr_drill_first.md` gave `tools/dr_drill_cron.py` as a runnable command.

Every one of those was written when it was true, or believed to be. Nothing
noticed when it stopped being true. That is the whole gap: a link-check is
cheap and no one had one.

Why the baseline here is plaintext
----------------------------------
Unlike a PII baseline — where a list of matched values would itself be the
disclosure — **a dead path is not sensitive**. Recording `doc -> path` in
the clear costs nothing and buys a reviewable artifact: you can read the
baseline and see what is rotting. Do not copy the hashing pattern from a
privacy scanner into a scanner that has nothing to protect.

What counts as dead
-------------------
A backticked token ending in a known source extension that resolves against
neither the repo root, `gdx_dispatch/`, `gdx_dispatch/frontend/`,
`gdx_dispatch/frontend/src/`, nor any file of that basename anywhere in the
tree. Deliberately excluded, because they are not decay:

  * Anything under a path this repo does not own (`ai-queue/`, `plans/`,
    `memory/`, `~/…`) — outside the tree by design.
  * Documentation placeholders (`your_module.py`, `path/to/file.py`).
  * Third-party or hypothetical filenames (`setup.py`, `Stripe.js`).

The ratchet actually ratchets
-----------------------------
`--strict` fails on any dead reference not in the baseline. Separately,
`tests/test_doc_link_scan.py` asserts the baseline **count never rises** —
the 2026-09-01 adversarial audit of a sibling scanner found its "ratchet"
had nothing enforcing direction, so re-freezing a regression went green.
Re-freezing here is caught by that test, not by good intentions.

Usage
-----
    python -m gdx_dispatch.tools.doc_link_scan             # report
    python -m gdx_dispatch.tools.doc_link_scan --strict    # CI: fail on new
    python -m gdx_dispatch.tools.doc_link_scan --write     # re-freeze (shrink only)

Escape hatch: `link-ok` in the same line.
"""
from __future__ import annotations

import argparse
import os
import posixpath
import re
import subprocess
import sys
from pathlib import Path

from gdx_dispatch.tools.tracked_files import tracked_or_none

REPO_ROOT = Path(__file__).resolve().parents[2]

BASELINE_PATH = REPO_ROOT / ".doc_link_baseline"

DOC_GLOBS = ("docs/**/*.md", "gdx_dispatch/docs/**/*.md", "*.md")
SELF_EXEMPT = {"gdx_dispatch/tools/doc_link_scan.py", "gdx_dispatch/tests/test_doc_link_scan.py"}

REF = re.compile(r"`([A-Za-z0-9_./-]+\.(?:py|vue|js|ts|sh|json|txt|ini|yml|yaml|sql|md))`")
SKIP_LINE = re.compile(r"link-ok")

# Paths this repo does not own — absent by design, not by decay.
FOREIGN_PREFIXES = ("ai-queue/", "plans/", "memory/", "~", "orchestrator/", "dispatch/")
# Placeholders and third-party names that are not meant to resolve.
NOT_REAL = {
    "your_module.py", "test_your_module.py", "gdx_dispatch/routers/your_module.py",
    "gdx_dispatch/tests/test_your_module.py", "path/to/file.py", "setup.py",
    "Stripe.js", "catalog.json", "vendored.json", "config.json", "render.yaml",
    "elestio.yml", "package.json", "swagger.yaml", "openapi.json",
}

_index: dict[str, bool] | None = None


def _basenames() -> dict[str, bool]:
    """Basenames of every file that ships, for the last-resort resolve.

    Tracked-only, for the same reason the doc enumeration is (audit 2026-09-12):
    `os.walk` here made a reference RESOLVE because a gitignored path happened
    to exist locally. Measured that day: 157 dead refs locally vs 159 in a
    tracked-only tree — the two extras resolved only via `gdx_dispatch/docker/
    demo/` and `.claude/`. That is the worse direction, green locally and red in
    CI, and `--write` would have frozen the local 157 into the baseline.
    """
    global _index
    if _index is None:
        tracked = tracked_or_none(REPO_ROOT)
        if tracked is not None:
            _index = {posixpath.basename(rel): True for rel in tracked}
        else:
            _index = {}
            for dp, dn, fs in os.walk(REPO_ROOT):
                dn[:] = [d for d in dn if d not in
                         (".git", "node_modules", "__pycache__", ".pytest_cache", "dist", ".venv")]
                for f in fs:
                    _index[f] = True
    return _index


def resolves(ref: str) -> bool:
    # Exact-path check against the tracked set, not the filesystem: a
    # gitignored file that exists locally must not make a reference resolve.
    tracked = tracked_or_none(REPO_ROOT)
    for cand in (ref, f"gdx_dispatch/{ref}", f"gdx_dispatch/frontend/{ref}",
                 f"gdx_dispatch/frontend/src/{ref}"):
        if tracked is not None:
            if cand in tracked:
                return True
        elif (REPO_ROOT / cand).exists():
            return True
    return os.path.basename(ref) in _basenames()


def interesting(ref: str) -> bool:
    """False for references that were never expected to resolve."""
    if ref.startswith(FOREIGN_PREFIXES) or ref.startswith("/"):
        return False
    return ref not in NOT_REAL and os.path.basename(ref) not in NOT_REAL


def docs() -> list[Path]:
    """Tracked docs matching DOC_GLOBS.

    Tracked-only because this scan is baseline-gated: an UNTRACKED doc changes
    the verdict on one machine and not in CI. Measured 2026-09-12, the globs
    picked up two files that are gitignored by design — `FOUND_NOT_FILED.md`
    (the local findings ledger CLAUDE.md requires, and which by its nature names
    source files) and `VERIFICATION_MANIFEST.md`. Neither produced a hit that
    day, so this was latent rather than broken; it would have fired the first
    time a ledger entry named a file that no longer exists, turning the gate red
    locally and leaving it green in CI.

    Same class as the demo-directory failures that made three other guards
    permanently red on a maintainer's checkout — where a standing red went on to
    mask a real regression.
    """
    # None when REPO_ROOT is not a git checkout (a scratch dir in a test);
    # raises when it IS a checkout whose index cannot be read, because a silent
    # fallback to globbing the tree restores the defect. Same rule, and same
    # reasoning, as tenant_plane_redundant_filter_scan._tracked_or_none.
    tracked = tracked_or_none(REPO_ROOT)
    out: list[Path] = []
    for g in DOC_GLOBS:
        for p in REPO_ROOT.glob(g):
            if not p.is_file():
                continue
            rel = p.relative_to(REPO_ROOT).as_posix()
            if rel in SELF_EXEMPT:
                continue
            if tracked is not None and rel not in tracked:
                continue
            out.append(p)
    return sorted(set(out))


def scan() -> list[str]:
    """`doc|path` for each distinct dead reference, sorted and deduplicated."""
    hits: set[str] = set()
    for path in docs():
        rel = path.relative_to(REPO_ROOT).as_posix()
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if SKIP_LINE.search(line):
                continue
            for ref in REF.findall(line):
                if interesting(ref) and not resolves(ref):
                    hits.add(f"{rel}|{ref}")
    return sorted(hits)


def load_baseline() -> set[str]:
    if not BASELINE_PATH.exists():
        return set()
    return {ln.strip() for ln in BASELINE_PATH.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--strict", action="store_true", help="exit 1 on a dead ref not in the baseline")
    ap.add_argument("--write", action="store_true", help="re-freeze the baseline (may only shrink)")
    args = ap.parse_args(argv)

    hits = scan()
    baseline = load_baseline()

    if args.write:
        if len(hits) > len(baseline) and baseline:
            print(f"REFUSING to re-freeze: {len(hits)} dead refs vs {len(baseline)} in the "
                  f"baseline. A baseline may only shrink — fix the new reference instead.",
                  file=sys.stderr)
            return 2
        BASELINE_PATH.write_text(
            "# Dead backticked file references, frozen. A RATCHET: this count may only\n"
            "# fall. tests/test_doc_link_scan.py asserts that, and --write refuses to\n"
            "# grow it. Fix the doc; do not re-freeze around a new one.\n"
            "# Plaintext on purpose — a dead path is not sensitive, and a readable\n"
            "# baseline is one you can actually work through.\n"
            + "\n".join(hits) + "\n", encoding="utf-8")
        print(f"wrote {len(hits)} entries to {BASELINE_PATH.name}")
        return 0

    new = [h for h in hits if h not in baseline]
    for h in new:
        doc, ref = h.split("|", 1)
        print(f"{doc}: {ref}")
    print(f"\n{len(hits)} dead references, {len(new)} not in baseline "
          f"(baseline holds {len(baseline)})")
    if args.strict and new:
        print("\nA doc names a file that does not resolve. Either fix the path, delete the\n"
              "reference, or — if it is a placeholder or lives outside this repo — add\n"
              "'link-ok' to the line. Do not re-freeze the baseline.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

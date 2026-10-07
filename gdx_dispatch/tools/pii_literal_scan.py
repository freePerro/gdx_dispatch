"""PII-literal scan — real email addresses and phone numbers in a public repo.

Bug class this catches
----------------------
This repository is **public**. On 2026-09-01 a doc audit found a design doc
carrying a named customer, and a sibling sweep found the same class in source:
customer- and vendor-shaped values used as docstring examples, comments and
test fixtures. Nothing in the repo could have caught it. GitGuardian covers
*secrets*; it does not know a customer's mailbox from a fictional one.

Ported, trimmed, from the parked ``docs-guard-lane-parked`` branch (GDXA-327,
GDXA-354). Kept: the two rules a regex can decide with a reserved fiction
space to point people at. Dropped: P1, the person-name regex — its own
docstring called a regex the wrong tool for NER, and its ~2,100-entry baseline
was mostly UI labels, a ratchet nobody could review.

Why this scan holds no denylist
-------------------------------
A list of real customer emails to grep for **is itself the leak**: committing
it publishes exactly what it protects. So the scan holds only

  * the **reserved fiction spaces** — RFC 2606 / RFC 6761 email domains and
    the NANP ``555-0100..555-0199`` block — safe to publish because they can
    never be anybody's, and
  * a **frozen baseline** of ``RULE|sha256(value)[:16]`` for what already
    matched when it was frozen.

Anything matching E1 or T1 below that is neither reserved nor baselined fails.
A reviewer then answers the one question the tool cannot: *is that real?*

The baseline hashes values so it is not a tidy aggregation of every address in
the tree. The hash is not secrecy — a phone number is ten digits and
brute-forces in seconds — and it does not need to be: the baseline only ever
records values present in the same commit's public tree. It discloses nothing
that commit does not already show, in a form that is not a ready-made list.

Rules
-----
E1. An email address whose domain is not reserved (``example.com|org|net|edu``,
    ``*.example``, ``*.test``, ``*.invalid``, ``localhost``).
T1. A North-American phone number outside the ``555-01xx`` fiction block.

Known gaps, deliberate
----------------------
* A real value present when the baseline was frozen is accepted. The baseline
  is a ratchet over that debt, not an approval of it.
* Keyed on the value, not the path: moving a file, or copying an
  already-public value into a second file, passes. Only a value this tree has
  never held fails.
* Names, street addresses and prose are not detected.
* T1 matches E.164 (``+1NNNNNNNNNN``) and separated forms only. A bare
  ``6125558841`` is not matched — ten digits is also an epoch timestamp — and
  neither is a number inside an extensionless file such as ``.test_durations``.
* The baseline only shrinks if someone works it down; the test pins a ceiling
  so it cannot silently grow.

Scan scope
----------
Every **tracked** file with a scannable extension, read from ``.git/index``
via ``tracked_files`` — the test image has no ``git`` binary, and a
``.gitignore`` re-implementation would only approximate the tracked set.
Migrations are in scope: a leaked comment in an immutable migration cannot be
edited away later.

Usage
-----
    python -m gdx_dispatch.tools.pii_literal_scan            # report new values
    python -m gdx_dispatch.tools.pii_literal_scan --strict   # exit 1 on new
    python -m gdx_dispatch.tools.pii_literal_scan --write    # re-freeze baseline

Escape hatch: put ``pii-ok`` in a trailing comment on the line.

The default suite gates this through ``tests/test_pii_literal_scan.py``.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

from gdx_dispatch.tools.tracked_files import read_tracked_files

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE_PATH = REPO_ROOT / ".pii_literal_baseline"

SCANNABLE_SUFFIXES = {".py", ".vue", ".js", ".ts", ".jsx", ".tsx", ".md", ".json",
                      ".sql", ".sh", ".yml", ".yaml", ".html", ".txt", ".toml", ".ini"}

# This scanner and its fixtures describe the patterns, so they match themselves.
SELF_EXEMPT = {
    "gdx_dispatch/tools/pii_literal_scan.py",
    "gdx_dispatch/tests/test_pii_literal_scan.py",
    ".pii_literal_baseline",
}

# RFC 2606 / RFC 6761 reserved — can never resolve to a real mailbox.
RESERVED_EMAIL_DOMAIN = re.compile(
    r"@(?:[a-z0-9-]+\.)*(?:example\.(?:com|org|net|edu)|example|test|invalid|localhost)$")

# NANP reserves 555-0100..555-0199 for fictional use.
FICTION_PHONE = re.compile(r"555[-.\s]?01\d\d$")

EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
# Two shapes, both NANP-valid (area code and exchange start 2-9):
#   * E.164 ``+1NNNNNNNNNN`` — what Phone.com payloads and SMS fixtures carry;
#   * separated ``612-555-8841`` / ``(612) 555-8841`` / ``(612)555-8841`` (the
#     PhoneInput mask) with an optional ``1``/``+1`` prefix.
# Bare unseparated digits are deliberately NOT matched: ten digits is also an
# epoch timestamp, an order number, a QB id.
PHONE = re.compile(
    r"(?<![\d.])(?:\+1[2-9]\d{2}[2-9]\d{6}"
    r"|(?:\+?1[-.\s])?\(?[2-9]\d{2}\)?[-.\s]?[2-9]\d{2}[-.\s]\d{4})(?![\d.])")

SKIP_LINE = re.compile(r"pii-ok")


def tracked_files() -> list[Path]:
    files = []
    for rel in sorted(read_tracked_files(REPO_ROOT)):
        if rel in SELF_EXEMPT:
            continue
        p = REPO_ROOT / rel
        if p.suffix in SCANNABLE_SUFFIXES and p.is_file():
            files.append(p)
    return files


def is_reserved_email(addr: str) -> bool:
    return bool(RESERVED_EMAIL_DOMAIN.search(addr.lower()))


def normalize(rule: str, value: str) -> str:
    """The identity behind a literal: a lowercased address, a bare 10-digit
    number. Without it ``(320) 555-7777`` and ``1-320-555-7777`` are two
    values, so reformatting a baselined number would read as new."""
    if rule == "T1":
        digits = re.sub(r"\D", "", value)
        return digits[1:] if len(digits) == 11 and digits.startswith("1") else digits
    return value.lower()


def fingerprint(rule: str, value: str) -> str:
    """``RULE|sha256(rule|normalized value)[:16]`` — path deliberately
    excluded, so a rename or move never reads as a new value (the parked
    branch's hook and CI disagreed on exactly that until a7287d37)."""
    digest = hashlib.sha256(f"{rule}|{normalize(rule, value)}".encode()).hexdigest()[:16]
    return f"{rule}|{digest}"


def scan_text(rel: str, text: str) -> list[tuple[str, str]]:
    """Return ``(signature, shown)`` pairs. The signature is baseline-safe;
    ``shown`` carries the value and is for console review only."""
    hits: list[tuple[str, str]] = []
    for line in text.splitlines():
        if SKIP_LINE.search(line):
            continue
        for m in EMAIL.finditer(line):
            if not is_reserved_email(m.group(0)):
                hits.append((fingerprint("E1", m.group(0)), f"E1 {rel}: {m.group(0)}"))
        for m in PHONE.finditer(line):
            if not FICTION_PHONE.search(m.group(0)):
                hits.append((fingerprint("T1", m.group(0)), f"T1 {rel}: {m.group(0)}"))
    return hits


def scan_repo() -> list[tuple[str, str]]:
    hits: list[tuple[str, str]] = []
    for path in tracked_files():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        hits.extend(scan_text(path.relative_to(REPO_ROOT).as_posix(), text))
    return sorted(set(hits))


def load_baseline() -> set[str]:
    if not BASELINE_PATH.exists():
        return set()
    return {
        ln.strip() for ln in BASELINE_PATH.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.startswith("#")
    }


BASELINE_HEADER = (
    "# RULE|sha256(value)[:16] for every email/phone literal present when this\n"
    "# baseline was frozen. Written by gdx_dispatch/tools/pii_literal_scan.py --write.\n"
    "#\n"
    "# Hashed so this file is not a list of every address in the tree. Every\n"
    "# value here is also in the same commit's public tree; run the scan without\n"
    "# --strict to see them locally.\n"
    "#\n"
    "# A RATCHET, not an approval list: the count should only ever fall. Never\n"
    "# re-freeze to make the test green — use an example.com address or a\n"
    "# 555-01xx number, or append 'pii-ok' to a line that is neither.\n"
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--strict", action="store_true", help="exit 1 on any hit not in the baseline")
    ap.add_argument("--write", action="store_true", help="re-freeze the baseline from the current tree")
    args = ap.parse_args(argv)

    hits = scan_repo()

    if args.write:
        sigs = sorted({sig for sig, _ in hits})
        BASELINE_PATH.write_text(BASELINE_HEADER + "".join(f"{s}\n" for s in sigs), encoding="utf-8")
        print(f"wrote {len(sigs)} entries to {BASELINE_PATH.name}")
        return 0

    baseline = load_baseline()
    new = [shown for sig, shown in hits if sig not in baseline]
    for shown in new:
        print(shown)
    print(f"\n{len(hits)} hits in tracked files, {len(new)} not in baseline")
    if args.strict and new:
        print("\nNew email/phone literal(s). Is it a real address or number?\n"
              "  - Real or invented → use an example.com address or a 555-01xx number.\n"
              "  - Not an address/phone at all → append 'pii-ok' to the line.\n"
              "Do not re-freeze the baseline to make this pass.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Vendor statement line classifier — slice 2.

Pure function: given a parsed line's description text, return one of
`"job"`, `"inventory"`, or `"unknown"`. The router/service write the
result into `VendorStatementLine.classification`. Unknown lines surface
in the UI with a "Mark as job/inventory" override.

Heuristics (order matters — first match wins):
    1. inventory  — bare "Stock", any "Op's …", any NxN dimension token,
                    or a description that is itself a bare date.
    2. unknown    — bare "Add-on(s)" / "add on" variants.
    3. job        — 2+ capitalized name-shaped words (handles
                    "LYNN & JIM LIEPOLD", "Trevor Johnson", "RUSS BISCHOFF",
                    "Schwatz Jenson", "Ybuilt Kramer", etc.).
    4. unknown    — everything else (single-word names like "Wilke",
                    builder-code-only rows like "89 CLIFF" — human marks).
"""
from __future__ import annotations

import re

JOB = "job"
INVENTORY = "inventory"
UNKNOWN = "unknown"

_STOCK_RE = re.compile(r"^\s*stock\s*$", re.IGNORECASE)
_OPS_RE = re.compile(r"\bop'?s\b", re.IGNORECASE)
_DIMENSION_RE = re.compile(r"\d+\s*x\s*\d+", re.IGNORECASE)
_BARE_DATE_RE = re.compile(r"^\s*\d{1,2}[./]\d{1,2}[./]\d{2,4}\s*$")
_ADDON_RE = re.compile(r"^\s*add[\s\-]?ons?\s*$", re.IGNORECASE)
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'&]+")


def classify_line(description: str | None) -> str:
    if not description:
        return UNKNOWN
    text = description.strip()
    if not text:
        return UNKNOWN

    if (
        _STOCK_RE.match(text)
        or _OPS_RE.search(text)
        or _DIMENSION_RE.search(text)
        or _BARE_DATE_RE.match(text)
    ):
        return INVENTORY

    if _ADDON_RE.match(text):
        return UNKNOWN

    cap_words = [w for w in _WORD_RE.findall(text) if w[0].isupper() and len(w) >= 2]
    if len(cap_words) >= 2:
        return JOB

    return UNKNOWN


VALID_CLASSIFICATIONS = {JOB, INVENTORY, UNKNOWN}

"""GL Phase 1 (S4) — writer inventory + money-write lint. THE enforcement.

Static complement to the runtime flush guard: every code path that writes
Invoice.status, births a non-draft Invoice, or issues raw Core writes to
money tables is inventoried here with a disposition. A new writer changes
the counts → this test goes red → the author must either route through
``transition_invoice_status`` / the engine, or consciously extend the
inventory in review. That friction is the point (spec §10, plan §S4).

Line numbers are deliberately NOT asserted (they drift on unrelated edits);
per-file COUNTS are.
"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]  # gdx_dispatch/

# Paths outside enforcement: tests (fixtures write freely), the ledger module
# itself (it IS the chokepoint), migrations (schema, not behavior), demo
# seeding (not shipped behavior), frontend.
_EXCLUDE = ("tests/", "modules/ledger/", "migrations/", "docker/", "frontend/")


def _py_files():
    for path in sorted(PKG.rglob("*.py")):
        rel = path.relative_to(PKG).as_posix()
        if any(rel.startswith(x) or f"/{x}" in rel for x in _EXCLUDE):
            continue
        yield rel, path.read_text(errors="replace")


def _scan(pattern: str) -> Counter:
    rx = re.compile(pattern)
    found: Counter = Counter()
    for rel, text in _py_files():
        n = sum(1 for line in text.splitlines() if rx.search(line))
        if n:
            found[rel] = n
    return found


# ---------------------------------------------------------------------------
# 1. Invoice.status assignment writers (spec §5 inventory)
# ---------------------------------------------------------------------------

# file → (count, disposition). Dispositions come from the implementation
# plan's slice map — every writer is either retrofitted through the
# chokepoint (S5/S6/S7) or disabled under the flag (S9) or deleted.
STATUS_WRITERS: dict[str, tuple[int, str]] = {
    # S5 retrofitted the issuance writers; S6 rewrote _mark_invoice_paid and
    # /refund; S7 rebuilt /credit-memo on invoice_adjustments. Every live
    # operational writer now routes through transition_invoice_status. S9
    # deleted the dead core/quickbooks.py legacy pull and runtime-gates the
    # live sync pull (_assert_money_pull_allowed raises when the flag is on).
    "modules/quickbooks/sync.py": (1, "QB pull — S9 gate raises when ledger on"),
}

# Matches `invoice.status = x` / `the_invoice.status = x` — NOT `inv.status`
# (distributor invitations) or `invite.status`. Assignment only (excludes ==).
_STATUS_WRITE_RX = r"(?:^|[^\w.])(?:\w+_)?invoice\.status\s*=[^=]"


def test_invoice_status_writer_inventory_is_exact():
    found = _scan(_STATUS_WRITE_RX)
    expected = Counter({f: c for f, (c, _) in STATUS_WRITERS.items()})
    new = {f: n for f, n in found.items() if n > expected.get(f, 0)}
    gone = {f: n for f, n in expected.items() if n > found.get(f, 0)}
    assert not new, (
        f"NEW Invoice.status writer(s) outside the chokepoint: {new}. "
        "Route the write through gdx_dispatch.modules.ledger.service."
        "transition_invoice_status, or (only with a reviewed reason) extend "
        "STATUS_WRITERS in this test."
    )
    assert not gone, (
        f"Inventoried Invoice.status writer(s) vanished: {gone}. "
        "If a slice retrofitted them, shrink STATUS_WRITERS to match — "
        "the inventory must always mirror reality."
    )


# ---------------------------------------------------------------------------
# 2. Non-draft Invoice births (issuance skipping P1)
# ---------------------------------------------------------------------------

NON_DRAFT_BIRTHS: dict[str, tuple[int, str]] = {
    "routers/onboarding.py": (1, "sample invoice during onboarding — S5 revisit"),
    "modules/quickbooks/sync.py": (1, "QB pull creation — S9 gate raises when ledger on"),
}

# A `status=` kwarg with a non-draft value within an Invoice( constructor.
# Heuristic: any status= line whose literal/expression is not exactly "draft"
# inside files, counted per file; constructors are the only place a
# `status="sent"`-style kwarg appears at column indent inside a call.
_BIRTH_RX = r'status\s*=\s*(?!["\']draft["\'])["\'\w]'


def _call_span(text: str, open_paren: int) -> str:
    """The argument text of a call, by paren balance (audit round 1: a fixed
    window + blank-line split silently under-scanned long constructors)."""
    depth, i = 1, open_paren + 1
    while i < len(text) and depth:
        depth += {"(": 1, ")": -1}.get(text[i], 0)
        i += 1
    return text[open_paren + 1 : i - 1]


def _invoice_constructor_births() -> Counter:
    found: Counter = Counter()
    for rel, text in _py_files():
        n = 0
        for m in re.finditer(r"\bInvoice\(", text):
            if re.search(_BIRTH_RX, _call_span(text, m.end() - 1)):
                n += 1
        if n:
            found[rel] = n
    return found


def test_non_draft_invoice_birth_inventory_is_exact():
    found = _invoice_constructor_births()
    expected = Counter({f: c for f, (c, _) in NON_DRAFT_BIRTHS.items()})
    assert found == expected, (
        f"Non-draft Invoice() births changed: found {dict(found)}, expected "
        f"{dict(expected)}. A non-draft birth is an issuance that skips P1 — "
        "create as draft and transition through the chokepoint, or extend "
        "NON_DRAFT_BIRTHS with a reviewed disposition."
    )


# ---------------------------------------------------------------------------
# 3. Raw Core writes to money tables (the CI lint, plan §S4)
# ---------------------------------------------------------------------------

_MONEY_MODELS = (
    "Invoice", "InvoiceLine", "Payment", "Expense", "ExpenseLine",
    "GlAccount", "GlJournalEntry", "GlJournalLine", "GlSettings", "GlPeriodLock",
)

RAW_CORE_WRITES: dict[str, tuple[int, str]] = {
    "modules/quickbooks/sync.py": (
        1, "InvoiceLine resync bulk delete — S9 gate raises when ledger on"
    ),
}

_MONEY_ALT = "|".join(_MONEY_MODELS)
# Four write forms, all flush-guard-invisible (audit round 1 added the last
# two): Model.__table__.update/delete, execute(update(Model)), bare
# update/delete(Model) statement construction (two-line execute), and
# query(Model).update/delete.
_RAW_WRITE_RX = (
    rf"\b(?:{_MONEY_ALT})\.__table__\.(?:update|delete)\("
    rf"|\b(?:sa\.|sqlalchemy\.)?(?:update|delete)\(\s*(?:{_MONEY_ALT})\b"
    rf"|query\(\s*(?:{_MONEY_ALT})\s*\)[\s\S]{{0,40}}?\.(?:update|delete)\("
)


def test_no_raw_core_writes_to_money_tables():
    # Full-text (not per-line) scan: query(Model)…, then .update( on the
    # next line, must still match.
    rx = re.compile(_RAW_WRITE_RX)
    found: Counter = Counter()
    for rel, text in _py_files():
        n = len(rx.findall(text))
        if n:
            found[rel] = n
    expected = Counter({f: c for f, (c, _) in RAW_CORE_WRITES.items()})
    assert found == expected, (
        f"Raw Core write to a money table changed: found {dict(found)}, "
        f"expected {dict(expected)}. ORM writes route through the session "
        "(the flush guard sees them); raw Core writes are invisible to the "
        "ledger. Use ORM mutations or the ledger engine."
    )


# ---------------------------------------------------------------------------
# 4. Floats are banned inside modules/ledger/ (spec §8)
# ---------------------------------------------------------------------------

def test_no_float_conversion_in_ledger_module():
    ledger = PKG / "modules" / "ledger"
    offenders = []
    for path in sorted(ledger.glob("*.py")):
        for i, line in enumerate(path.read_text().splitlines(), 1):
            if re.search(r"\bfloat\(", line):
                offenders.append(f"{path.name}:{i}")
    assert not offenders, (
        f"float() conversion on a money path: {offenders} — ledger money is "
        "integer cents end-to-end (isinstance rejection of float is fine; "
        "conversion is not)."
    )

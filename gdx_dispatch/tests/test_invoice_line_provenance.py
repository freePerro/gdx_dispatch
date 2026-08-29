"""Guards for invoice-line pricing provenance.

The defect these exist to prevent, measured on the live tenant 2026-08-29:
832 invoice lines, `cost_snapshot` on 63, `margin_pct_snapshot` on **zero** —
and every one of those 63 had `unit_price > 0`, so all 63 were exactly
derivable and none were derived. The amount was always kept; the reason never
was.

Each test below names the input that turns it red. A guard that cannot fail for
its own defect proves nothing.
"""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path
from typing import get_args
from uuid import UUID

import pytest

from gdx_dispatch.core.pricing_provenance import (
    COST_FORBIDDEN,
    PRICING_SOURCES,
    build_invoice_line,
    derive_margin_pct,
)
from gdx_dispatch.services.pricing_engine import PricingSource

_REPO = Path(__file__).resolve().parents[1]

# The class definition and the helper. Nowhere else may construct one, because
# a bare InvoiceLine() is how an unprovenanced money row gets written.
_MAY_CONSTRUCT = {
    Path("models/tenant_models.py"),
    Path("core/pricing_provenance.py"),
}
_SKIP_DIRS = {"tests", "migrations", "__pycache__"}
# A different model with its own table; not an InvoiceLine.
_SKIP_PARTS = {"vendor_invoices"}


def _python_files():
    for path in _REPO.rglob("*.py"):
        rel = path.relative_to(_REPO)
        if set(rel.parts) & _SKIP_DIRS or set(rel.parts) & _SKIP_PARTS:
            continue
        yield rel, path


def _invoice_line_calls(path: Path):
    """Every `InvoiceLine(...)` CALL NODE in a file.

    Matching the call node rather than grepping for a keyword is deliberate: a
    `pricing_source=` presence check is defeated by `InvoiceLine(**kwargs)`, and
    a text grep passes on a comment that merely mentions the word. Asserting a
    string appears in source proves authorship, not correctness.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:  # pragma: no cover - not our problem to report here
        return []
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "id", None) or getattr(func, "attr", None)
        if name == "InvoiceLine":
            hits.append(node.lineno)
    return hits


# Sites still to convert, pinned by FILE and COUNT. This is a ratchet: it may
# only ever shrink. Line numbers are deliberately not pinned — they move for
# unrelated edits — but the count is, so an EXTRA bare construction in a file
# that already has some still fails.
#
# Converted so far: all 7 in routers/invoices.py (the ~98% of live volume).
_PENDING_CONVERSION = {
    Path("routers/mobile_invoicing.py"): 5,
    Path("core/closeout_billing.py"): 3,
    Path("modules/deposits/service.py"): 2,
    Path("modules/quickbooks/sync.py"): 2,
    Path("routers/sub_resources.py"): 1,
}


def test_no_new_invoice_line_is_constructed_without_provenance():
    """G1 — the sweep that catches a future write path.

    Fails on a bare `InvoiceLine(...)` in any file that is not on the pending
    ratchet, and on an EXTRA one in a file that is.

    Counterfactual, run before trusting this: paste a bare `InvoiceLine(...)`
    into any router and confirm it names that file. It does — while the seven
    routers/invoices.py sites were still unconverted, this listed every one of
    them by file:line.
    """
    found: dict[Path, list[int]] = {}
    for rel, path in _python_files():
        if rel in _MAY_CONSTRUCT:
            continue
        hits = _invoice_line_calls(path)
        if hits:
            found[rel] = hits

    problems = []
    for rel, hits in sorted(found.items()):
        allowed = _PENDING_CONVERSION.get(rel, 0)
        if len(hits) > allowed:
            lines = ", ".join(str(n) for n in hits)
            problems.append(
                f"{rel}: {len(hits)} bare InvoiceLine() (allowed {allowed}) at lines {lines}"
            )
    assert not problems, (
        "InvoiceLine() constructed directly — these lines carry no pricing "
        "provenance:\n  " + "\n  ".join(problems)
        + "\n\nUse core.pricing_provenance.build_invoice_line(pricing_source=...) "
        "so the lane that produced the price is recorded with it."
    )


def test_the_pending_ratchet_only_shrinks():
    """A file that has been fully converted must be removed from the ratchet,
    so it can never silently regain an unprovenanced construction."""
    stale = []
    for rel, allowed in _PENDING_CONVERSION.items():
        actual = len(_invoice_line_calls(_REPO / rel))
        if actual < allowed:
            stale.append(f"{rel}: ratchet says {allowed}, file now has {actual}")
    assert not stale, (
        "the pending ratchet is out of date — lower these counts:\n  "
        + "\n  ".join(stale)
    )


def test_pricing_source_is_required_and_has_no_default():
    """A new site that forgets the lane must die immediately, not write a row."""
    with pytest.raises(TypeError):
        build_invoice_line(  # type: ignore[call-arg]
            description="x", quantity=1, unit_price=Decimal("1"), line_total=Decimal("1")
        )


def test_an_unknown_lane_cannot_be_invented():
    """A typo must not silently truncate into VARCHAR(32) and become a lane
    nobody can ever query for."""
    with pytest.raises(ValueError, match="unknown pricing_source"):
        build_invoice_line(
            pricing_source="clientcost",  # typo
            description="x", quantity=1, unit_price=Decimal("1"), line_total=Decimal("1"),
        )


@pytest.mark.parametrize("lane", sorted(COST_FORBIDDEN))
def test_a_cost_on_a_not_priced_line_is_unwriteable(lane):
    """cost_snapshot=0 on a discount/deposit/import derives (p-0)/p = 100%,
    which would mint a fake full-margin row in every profit report."""
    with pytest.raises(ValueError, match="have no cost"):
        build_invoice_line(
            pricing_source=lane,
            cost_snapshot=Decimal("0"),
            description="x", quantity=1, unit_price=Decimal("50"), line_total=Decimal("50"),
        )


def test_margin_is_derived_from_cost_and_price():
    """THE assertion that was red for all 63 live rows.

    Counterfactual: change the helper to (sell-cost)/cost and this goes red.
    """
    line = build_invoice_line(
        pricing_source="client_cost",
        cost_snapshot=Decimal("60"),
        description="x", quantity=1, unit_price=Decimal("100"), line_total=Decimal("100"),
    )
    assert line.cost_snapshot == Decimal("60.00")
    assert line.margin_pct_snapshot == Decimal("0.4000")


def test_an_unknown_cost_yields_null_never_an_invented_one():
    """The guard must forbid inventing a cost as firmly as it requires
    recording one. A fabricated margin is worse than an honest blank."""
    line = build_invoice_line(
        pricing_source="manual",
        description="x", quantity=1, unit_price=Decimal("100"), line_total=Decimal("100"),
    )
    assert line.cost_snapshot is None
    assert line.margin_pct_snapshot is None


def test_a_negative_price_derives_no_margin_and_does_not_overflow():
    """A materialised discount is a negative line. Numeric(6,4) would reject a
    derived -199.0 on Postgres while SQLite silently accepts it, so the guard
    lives in the formula."""
    line = build_invoice_line(
        pricing_source="operator_discount",
        description="Discount", quantity=1,
        unit_price=Decimal("-50"), line_total=Decimal("-50"),
    )
    assert line.margin_pct_snapshot is None
    # And the formula itself refuses the fat-fingered case directly.
    assert derive_margin_pct(Decimal("1000"), Decimal("5")) is None


def test_labor_may_legitimately_carry_a_margin():
    """Regression guard against a rule that was proposed and rejected.

    "Labor is never marked up" is about the ENGINE marking labor up. The
    estimate side deliberately stores a margin on labor-matrix lines so they
    appear in the profit panel, and 101 live estimate lines do. A hard failure
    on (labor_source + margin) would 500 the next estimate->invoice conversion
    of a labor-matrix quote.
    """
    line = build_invoice_line(
        pricing_source="labor_matrix",
        cost_snapshot=Decimal("300"),
        labor_source="matrix",
        description="Labor", quantity=1,
        unit_price=Decimal("850"), line_total=Decimal("850"),
    )
    assert line.margin_pct_snapshot is not None
    assert line.labor_source == "matrix"


def test_the_helper_never_touches_authorship():
    """`source` is a different axis — closeout_billing.is_untouched_autodraft
    reads it to decide whether a machine draft may be rebuilt. Conflating the
    two would make the rebuild delete human work, or refuse to run."""
    line = build_invoice_line(
        pricing_source="manual",
        source="autodraft",
        description="x", quantity=1, unit_price=Decimal("10"), line_total=Decimal("10"),
    )
    assert line.source == "autodraft", "the helper rewrote authorship"

    untagged = build_invoice_line(
        pricing_source="manual",
        description="x", quantity=1, unit_price=Decimal("10"), line_total=Decimal("10"),
    )
    assert untagged.source is None, "the helper invented an authorship value"


def test_every_engine_lane_is_accepted():
    """The lane set must cover everything the ENGINE can stamp.

    `customer_override` was missing from a hand-transcribed list. It is a real
    PricingSource (set a customer's margin_override_pct), it reaches
    EstimateLine.pricing_source, and the estimate->invoice copy forwards it —
    so an unknown-lane ValueError inside a request handler was a 500 on a path
    that worked before. Deriving the set from the engine's own Literal is what
    makes that unrepeatable; this asserts the derivation actually happened.
    """
    from typing import get_args

    from gdx_dispatch.services.pricing_engine import PricingSource

    engine_lanes = set(get_args(PricingSource))
    assert engine_lanes, "PricingSource is not a Literal any more — re-check this"
    missing = engine_lanes - PRICING_SOURCES
    assert not missing, f"engine lanes the invoice side would 500 on: {missing}"
    assert COST_FORBIDDEN <= PRICING_SOURCES


def test_a_forwarded_engine_lane_does_not_raise():
    """The 500, reproduced at the boundary that would have raised it."""
    for lane in sorted(set(get_args(PricingSource))):
        line = build_invoice_line(
            pricing_source=lane,
            description="x", quantity=1,
            unit_price=Decimal("100"), line_total=Decimal("100"),
        )
        assert line.pricing_source == lane


def test_the_margin_formula_is_the_estimate_sides_formula():
    """Rounding is money. The original quantized with ROUND_HALF_UP and guarded
    the QUANTIZED value; a version that guards first and quantizes with the
    default HALF_EVEN returns 0.9062 where the estimate side returns 0.9063,
    and the two would disagree on the same line.
    """
    assert derive_margin_pct(Decimal("3"), Decimal("32")) == Decimal("0.9063")
    assert derive_margin_pct(Decimal("7"), Decimal("32")) == Decimal("0.7813")
    assert derive_margin_pct(Decimal("11"), Decimal("32")) == Decimal("0.6563")


# ── Behavioural: assert the PERSISTED value, not the constructor's arguments ──
# A unit test on the helper proves the arguments were right. Only a route test
# proves the row on disk carries them, which is the claim that was false for all
# 63 live rows.

from gdx_dispatch.routers.invoices import (  # noqa: E402
    InvoiceCreateIn,
    InvoiceLineCreateIn,
    create_invoice,
)
from gdx_dispatch.tests.test_invoices import (  # noqa: E402
    _current_user,
    _seed_job,
    tenant_db_session,  # noqa: F401  — pytest fixture, used by name
)


def test_a_hand_composed_line_persists_its_margin(tenant_db_session):
    """THE regression. A cost + a price on the 98% path must persist a margin.

    Red before this change for every one of the 63 live rows that carried a
    cost: all were exactly derivable and none were derived.
    """
    db = tenant_db_session
    job = _seed_job(db)
    result = create_invoice(
        payload=InvoiceCreateIn(
            job_id=str(job.id),
            customer_id=str(job.customer_id),
            line_items=[
                InvoiceLineCreateIn(
                    description="Bracket", quantity=1,
                    unit_price=100.0, cost=60.0, taxable=True,
                ),
                InvoiceLineCreateIn(
                    description="No cost recorded", quantity=1,
                    unit_price=80.0, taxable=True,
                ),
            ],
        ),
        _=_current_user(),
        db=db,
    )
    from gdx_dispatch.models.tenant_models import InvoiceLine
    rows = {
        r.description: r
        for r in db.query(InvoiceLine).filter(
            InvoiceLine.invoice_id == UUID(str(result["id"]))
        ).all()
    }

    priced = rows["Bracket"]
    assert priced.pricing_source == "client_cost"
    assert priced.cost_snapshot == Decimal("60.00")
    assert priced.margin_pct_snapshot == Decimal("0.4000"), (
        "a cost and a price were both stored and the margin was not derived"
    )

    # And the honest blank survives the round trip — no invented cost.
    blank = rows["No cost recorded"]
    assert blank.pricing_source == "manual"
    assert blank.cost_snapshot is None
    assert blank.margin_pct_snapshot is None

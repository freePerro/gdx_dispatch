"""#560: a stored quantity of 0 is 0, not 1.

``quantity or 1`` re-rated a recorded zero as one — a zero-quantity invoice,
change-order, estimate or parts line billed, ordered or scheduled one unit.
The owner's rule (2026-09-11, and job costing's since #469): a blank
quantity reads as 1, a recorded 0 reads as 0. Prod carries no zero or NULL
quantity in any quantity column (census 2026-09-11), so no stored amount moves.
"""
from __future__ import annotations

import ast
import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.quantities import recorded_quantity
from gdx_dispatch.models.tenant_models import (
    ChangeOrderLine,
    Customer,
    Invoice,
    InvoiceLine,
    Job,
    JobCloseout,
    JobPartNeeded,
    Payment,
)
from gdx_dispatch.modules.proposals.models import Estimate, EstimateLine
from gdx_dispatch.modules.tax.models import TaxConfig, TaxExemption
from gdx_dispatch.routers.change_orders import ChangeOrder, ChangeOrderIn, create_change_order
from gdx_dispatch.routers.invoices import InvoiceCreateIn, create_invoice

PACKAGE = Path(__file__).resolve().parents[1]
USER = {"user_id": "00000000-0000-0000-0000-000000000560", "tenant_id": "tenant-1", "role": "admin"}


def test_a_blank_quantity_reads_as_one_and_a_recorded_zero_as_zero():
    assert recorded_quantity(None) == 1
    assert recorded_quantity(0) == 0
    assert recorded_quantity(Decimal("0")) == 0
    assert recorded_quantity(3) == 3


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    for tbl in (Job, Customer, Estimate, EstimateLine, Invoice, InvoiceLine, Payment, JobPartNeeded,
                JobCloseout, ChangeOrder, ChangeOrderLine, TaxConfig, TaxExemption):
        tbl.__table__.create(bind=engine, checkfirst=True)
    TenantBase.metadata.create_all(bind=engine, checkfirst=True)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    session.add(TaxConfig(default_rate=Decimal("0")))
    session.commit()
    yield session
    session.close()
    engine.dispose()


def _job(db) -> Job:
    job = Job(customer_id=uuid4(), title="Door repair", description="t", lifecycle_stage="completed",
              dispatch_status="done", billing_status="unbilled", company_id="tenant-1")
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def test_a_zero_quantity_part_is_not_billed_and_stays_on_the_checklist(db):
    """Closeout billing priced a part as unit × quantity, so a recorded 0 billed
    a whole unit. It is now neither billed as 1 nor minted as a $0 line the
    invoice API would refuse (owner 2026-09-11): the row is left unclaimed for
    the office. No writer stores a 0 here today — every input requires >= 1 —
    so this pins the rule rather than reproducing a live row."""
    from gdx_dispatch.core.closeout_billing import build_closeout_lines

    job = _job(db)
    invoice = Invoice(id=uuid4(), customer_id=job.customer_id, job_id=job.id, invoice_number="INV-560",
                      status="draft", subtotal=Decimal("0"), total=Decimal("0"), company_id="tenant-1",
                      public_token=uuid4().hex)
    closeout = JobCloseout(job_id=job.id, hours_worked=Decimal("0"), parts_used=[], no_parts_used=False,
                           closed_by_user_id=USER["user_id"], closed_at=datetime.now(UTC))
    db.add_all([invoice, closeout])
    for qty, name in ((0, "Spring (none used)"), (None, "Seal (quantity unstated)")):
        db.add(JobPartNeeded(id=str(uuid4()), company_id="tenant-1", job_id=str(job.id), part_name=name,
                             quantity=qty, status="used", source="closeout", unit_price=Decimal("45.00")))
    db.commit()

    build_closeout_lines(db, tenant_id="tenant-1", invoice=invoice, closeout=closeout, job_type=None,
                         job_id=str(job.id))
    db.commit()
    lines = {ln.description: ln for ln in db.execute(
        select(InvoiceLine).where(InvoiceLine.invoice_id == invoice.id)).scalars()}
    assert "Spring (none used)" not in lines
    assert (lines["Seal (quantity unstated)"].quantity, lines["Seal (quantity unstated)"].line_total) == (
        1, Decimal("45.00"))
    unbilled = {p.part_name: p.billed_invoice_id for p in db.execute(select(JobPartNeeded)).scalars()}
    assert unbilled["Spring (none used)"] is None, "the zero row must stay on the checklist"


def _co_with_a_zero_line(db, job) -> ChangeOrder:
    out = create_change_order(payload=ChangeOrderIn(
        job_id=str(job.id), customer_id=str(job.customer_id), title="Extra", status="approved",
        line_items=[{"description": "Opener unit", "quantity": 1, "unit_price": 300.0},
                    {"description": "Trim kit", "quantity": 1, "unit_price": 0.0}],
    ), user=USER, db=db)
    co = db.get(ChangeOrder, UUID(out["id"]))
    trim = db.execute(select(ChangeOrderLine).where(ChangeOrderLine.co_id == co.id,
                                                    ChangeOrderLine.description == "Trim kit")).scalar_one()
    trim.qty = 0  # a recorded zero (the create form requires > 0; legacy or direct data can hold one)
    db.commit()
    return co


def test_a_zero_quantity_change_order_line_shows_as_zero_and_is_not_billed(db):
    """It read as 1 on the change order and was copied onto the invoice as one
    unit. Now it shows 0 and the copy leaves it out."""
    from gdx_dispatch.routers.change_orders import get_change_order

    job = _job(db)
    co = _co_with_a_zero_line(db, job)
    shown = get_change_order(co_id=co.id, _=USER, db=db)
    assert {ln["description"]: ln["quantity"] for ln in shown["line_items"]}["Trim kit"] == 0

    create_invoice(payload=InvoiceCreateIn(job_id=job.id, customer_id=job.customer_id,
                                           from_change_order_ids=[co.id]), _=USER, db=db)
    copied = [ln.description for ln in db.execute(select(InvoiceLine)).scalars()]
    assert [d for d in copied if "Trim kit" in d] == []
    assert [d for d in copied if "Opener unit" in d] != [], "the priced line still bills"


def test_a_zero_quantity_estimate_line_is_not_billed(db):
    """The estimate→invoice copy writes InvoiceLine directly, past the API's
    own quantity check, so a zero line would have billed one unit."""
    job = _job(db)
    est = Estimate(id=uuid4(), job_id=job.id, customer_id=job.customer_id, estimate_number="EST-560",
                   status="accepted", company_id="tenant-1", public_token=uuid4().hex)
    db.add(est)
    db.flush()
    db.add_all([
        EstimateLine(id=uuid4(), estimate_id=est.id, company_id="tenant-1", description="Opener unit", quantity=1,
                     unit_price=Decimal("300.00"), line_total=Decimal("300.00")),
        EstimateLine(id=uuid4(), estimate_id=est.id, company_id="tenant-1", description="Trim kit", quantity=0,
                     unit_price=Decimal("50.00"), line_total=Decimal("0.00")),
    ])
    db.commit()

    create_invoice(payload=InvoiceCreateIn(job_id=job.id, customer_id=job.customer_id, estimate_id=est.id),
                   _=USER, db=db)
    billed = [ln.description for ln in db.execute(select(InvoiceLine)).scalars()]
    assert "Opener unit" in billed
    assert "Trim kit" not in billed


def test_a_zero_quantity_change_order_line_that_carries_money_is_refused(db):
    """A line recorded 0 that still carries a signed amount is neither skipped
    (that loses the money — a $350 change order invoiced at $300 and stamped
    billed, audit round 5) nor copied (an invoice line with quantity 0 and an
    amount is a shape the app destroys on the next PATCH, audit round 6). The
    copy stops and names the line so a person fixes it at the source."""
    job = _job(db)
    out = create_change_order(payload=ChangeOrderIn(
        job_id=str(job.id), customer_id=str(job.customer_id), title="Extra", status="approved",
        line_items=[{"description": "Opener unit", "quantity": 1, "unit_price": 300.0},
                    {"description": "Haul-away", "quantity": 1, "unit_price": 50.0}],
    ), user=USER, db=db)
    co = db.get(ChangeOrder, UUID(out["id"]))
    haul = db.execute(select(ChangeOrderLine).where(ChangeOrderLine.co_id == co.id,
                                                    ChangeOrderLine.description == "Haul-away")).scalar_one()
    haul.qty = 0  # recorded 0, and the $50 stays on the line
    db.commit()

    with pytest.raises(HTTPException) as refused:
        create_invoice(payload=InvoiceCreateIn(job_id=job.id, customer_id=job.customer_id,
                                               from_change_order_ids=[co.id]), _=USER, db=db)
    assert refused.value.status_code == 409
    assert "Haul-away" in str(refused.value.detail), "names the line to fix"
    assert list(db.execute(select(InvoiceLine)).scalars()) == [], "nothing half-billed"
    db.rollback()
    assert db.get(ChangeOrder, co.id).billed_invoice_id is None, "and the CO is not stamped"


def test_a_zero_quantity_estimate_line_that_carries_money_is_refused(db):
    """Same shape on the estimate copy, which writes InvoiceLine directly."""
    job = _job(db)
    est = Estimate(id=uuid4(), job_id=job.id, customer_id=job.customer_id, estimate_number="EST-562",
                   status="accepted", company_id="tenant-1", public_token=uuid4().hex)
    db.add(est)
    db.flush()
    db.add_all([
        EstimateLine(id=uuid4(), estimate_id=est.id, company_id="tenant-1", description="Opener unit", quantity=1,
                     unit_price=Decimal("300.00"), line_total=Decimal("300.00")),
        EstimateLine(id=uuid4(), estimate_id=est.id, company_id="tenant-1", description="Haul-away", quantity=0,
                     unit_price=Decimal("50.00"), line_total=Decimal("50.00")),
    ])
    db.commit()

    with pytest.raises(HTTPException) as refused:
        create_invoice(payload=InvoiceCreateIn(job_id=job.id, customer_id=job.customer_id, estimate_id=est.id),
                       _=USER, db=db)
    assert refused.value.status_code == 409
    assert "Haul-away" in str(refused.value.detail)
    assert list(db.execute(select(InvoiceLine)).scalars()) == []


def test_an_unrelated_line_edit_does_not_rewrite_a_stored_amount(db):
    """The PATCH handler recomputed line_total = quantity × unit_price on EVERY
    edit, so toggling `taxable` re-priced any line whose stored total was never
    that product — a QuickBooks-imported row, or a lump sum. Round 6 reproduced
    a $350 invoice falling to $300 on a taxable-only edit."""
    from gdx_dispatch.routers.invoices import InvoiceLinePatchIn, patch_invoice_line

    job = _job(db)
    invoice = Invoice(id=uuid4(), customer_id=job.customer_id, job_id=job.id, invoice_number="INV-563",
                      status="draft", subtotal=Decimal("500.00"), total=Decimal("500.00"),
                      company_id="tenant-1", public_token=uuid4().hex)
    db.add(invoice)
    db.flush()
    line = InvoiceLine(id=uuid4(), company_id="tenant-1", invoice_id=invoice.id, description="Imported lump sum",
                       quantity=1, unit_price=Decimal("0.00"), line_total=Decimal("500.00"), taxable=True)
    db.add(line)
    db.commit()

    patch_invoice_line(invoice_id=invoice.id, line_id=line.id,
                       payload=InvoiceLinePatchIn(taxable=False), user=USER, db=db)
    db.commit()
    assert db.get(InvoiceLine, line.id).line_total == Decimal("500.00"), "the stored amount survives"


def test_an_estimate_of_empty_lines_bills_nothing_and_does_not_refuse(db):
    """Every line recorded 0 AND $0: there is nothing to bill, so nothing is
    billed. It is NOT refused — refusing was an invention of this fix, and the
    same invention broke the office's own 'generate empty invoice' path."""
    job = _job(db)
    est = Estimate(id=uuid4(), job_id=job.id, customer_id=job.customer_id, estimate_number="EST-561",
                   status="accepted", company_id="tenant-1", public_token=uuid4().hex)
    db.add(est)
    db.flush()
    db.add(EstimateLine(id=uuid4(), estimate_id=est.id, company_id="tenant-1", description="Trim kit",
                        quantity=0, unit_price=Decimal("50.00"), line_total=Decimal("0.00")))
    db.commit()

    out = create_invoice(payload=InvoiceCreateIn(job_id=job.id, customer_id=job.customer_id, estimate_id=est.id),
                         _=USER, db=db)
    assert float(out["total"]) == 0.0
    assert [ln.description for ln in db.execute(select(InvoiceLine)).scalars()] == []


def test_a_change_order_with_no_billable_line_still_bills_the_signed_amount(db):
    """Skipping its only line must not mark the CO billed for nothing: it falls
    to the signed-amount fallback, the guard that exists for that money-loser."""
    job = _job(db)
    out = create_change_order(payload=ChangeOrderIn(
        job_id=str(job.id), customer_id=str(job.customer_id), title="Signed extra", status="approved",
        line_items=[{"description": "Opener unit", "quantity": 1, "unit_price": 300.0}],
    ), user=USER, db=db)
    co = db.get(ChangeOrder, UUID(out["id"]))
    line = db.execute(select(ChangeOrderLine).where(ChangeOrderLine.co_id == co.id)).scalar_one()
    # Emptied entirely: no quantity AND no money on the line, while the change
    # order still carries the $300 the customer signed.
    line.qty, line.unit_price, line.line_total = 0, Decimal("0"), Decimal("0")
    db.commit()
    assert Decimal(str(co.amount)) == Decimal("300.00")

    create_invoice(payload=InvoiceCreateIn(job_id=job.id, customer_id=job.customer_id,
                                           from_change_order_ids=[co.id]), _=USER, db=db)
    lines = list(db.execute(select(InvoiceLine)).scalars())
    assert [ln.line_total for ln in lines] == [Decimal("300.00")], "the signed amount, not a guessed unit"
    assert "Opener unit" not in (lines[0].description or "")


# ── the guard: no stored quantity read with `or 1` ────────────────────────────

QUANTITY_NAMES = {"quantity", "qty", "qty_used", "quantity_used", "quantity_ordered"}
# (path, line text) -> why it is not a read of a stored quantity.
ALLOWED = {
    ("modules/proposals/service.py", "qty = max(1, int(quantity or 1))"): (
        "write-side: a new proposal tier line is clamped to at least 1 when saved"
    ),
    ("modules/proposals/service.py", 'row.quantity = max(1, int(fields["quantity"] or 1))'): (
        "write-side: the same clamp when a tier line is edited"
    ),
}


def _names_a_quantity(node: ast.AST) -> bool:
    """Any spelling: `ln.qty`, `quantity`, `line["quantity"]`, getattr(…, "quantity", 1)."""
    for child in ast.walk(node):
        if isinstance(child, ast.Attribute) and child.attr in QUANTITY_NAMES:
            return True
        if isinstance(child, ast.Name) and child.id in QUANTITY_NAMES:
            return True
        if isinstance(child, ast.Constant) and child.value in QUANTITY_NAMES:
            return True
    return False


def _quantity_or_one(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or) and len(node.values) == 2:
            left, right = node.values
            if _names_a_quantity(left) and isinstance(right, ast.Constant) and right.value in (1, 1.0):
                yield node.lineno


# Every quantity written onto an invoice line must come from recorded_quantity,
# a literal, or one of these. Keyed by (file, function, expression) WITH a count:
# round 2's defect spelled itself `line.quantity` in the same file as a legitimate
# payload copy, so a file-level entry would have excused it (CLAUDE.md's green-
# ratchet edge). A second copy of an allowed spelling raises the count and fails.
INVOICE_LINE_ALLOWED = {
    ("routers/invoices.py", "create_invoice", "line.quantity"): (
        1, "the payload line_items branch — validated gt=0 by InvoiceLineCreateIn"),
    ("routers/invoices.py", "add_invoice_line", "payload.quantity"): (
        1, "payload — validated gt=0 by InvoiceLineCreateIn"),
    ("core/closeout_billing.py", "build_closeout_lines", "labor.quantity"): (
        1, "a computed service labor line (rates x attested hours), not a stored quantity"),
    ("core/closeout_billing.py", "build_closeout_lines", "_install.quantity"): (
        1, "a computed install labor line (a picked matrix row), not a stored quantity"),
    ("routers/sub_resources.py", "create_job_line_item", "int(_raw_qty)"): (
        1, "raw-dict body, validated just above: 422 unless a whole number >= 1"),
}


def _invoice_line_quantities(tree: ast.AST):
    """(function, expression, line) per quantity= written to an invoice line,
    with a local variable resolved back to what it was assigned.

    Cannot see a **splat (`InvoiceLine(**row)`); none exists today.
    """
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        assigned = {
            t.id: node.value
            for node in ast.walk(fn) if isinstance(node, ast.Assign)
            for t in node.targets if isinstance(t, ast.Name)
        }
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            if (getattr(node.func, "id", None) or getattr(node.func, "attr", None)) not in (
                    "InvoiceLine", "build_invoice_line"):
                continue
            for kw in node.keywords:
                if kw.arg != "quantity":
                    continue
                value = kw.value
                if isinstance(value, ast.Name) and value.id in assigned:
                    value = assigned[value.id]
                yield fn.name, ast.unparse(value), node.lineno


def test_every_invoice_line_quantity_is_recorded_or_declared():
    """#560 round 2: two copies wrote `quantity=line.quantity` straight from a
    stored row, past the API's own check — a shape no `or 1` scan can see."""
    from collections import Counter

    counts: Counter = Counter()
    where: dict = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(PACKAGE).as_posix()
        if rel.startswith(("tests/", "migrations/", "frontend/")):
            continue
        for func, expr, lineno in _invoice_line_quantities(ast.parse(path.read_text(errors="replace"))):
            if "recorded_quantity" in expr or expr.isdigit():
                continue
            counts[(rel, func, expr)] += 1
            where.setdefault((rel, func, expr), []).append(lineno)

    undeclared = [f"{k[0]}:{where[k]}: in {k[1]}(), quantity={k[2]}" for k in counts if k not in INVOICE_LINE_ALLOWED]
    assert undeclared == [], "\n".join(undeclared)
    grew = [f"{k[0]} {k[1]}() quantity={k[2]}: {counts[k]} now, {INVOICE_LINE_ALLOWED[k][0]} declared"
            for k in counts if counts[k] != INVOICE_LINE_ALLOWED[k][0]]
    assert grew == [], "\n".join(grew)
    assert sorted(set(INVOICE_LINE_ALLOWED) - set(counts)) == [], "an INVOICE_LINE_ALLOWED entry matches nothing now"


_JINJA_IF = re.compile(r"{%-?\s*(?:el)?if\s+(.+?)\s*-?%}")
_QUANTITY_WORD = re.compile(r"\b(quantity|qty)\b")
_COMPARISON = re.compile(r"(==|!=|<=|>=|<|>|\bis\s+(?:not\s+)?(?:none|defined|undefined)\b|\bin\b)")
_HIDES_ZERO = re.compile(r"\b(quantity|qty)\s*>\s*[01]\b")


def _template_clauses_hiding_a_zero(condition: str):
    """Clauses of a Jinja `{% if %}` that read a recorded 0 as 'no quantity'."""
    for clause in re.split(r"\s+(?:and|or)\s+", condition):
        if not _QUANTITY_WORD.search(clause):
            continue
        if _HIDES_ZERO.search(clause):
            yield clause          # `qty > 1` / `qty > 0`: a 0 is omitted, so it reads as 1
        elif not _COMPARISON.search(clause):
            yield clause          # bare truthiness: 0 is falsy


def test_no_template_hides_a_recorded_zero_quantity():
    """The sweep's blind spot (audit round 5): the same shape lives in Jinja,
    which no scanner here looked at. `{% if door.quantity and door.quantity > 1 %}`
    printed nothing for a door recorded 0, so the install sheet read as one door.
    """
    hits = []
    for path in sorted((PACKAGE / "templates").rglob("*.html")):
        rel = path.relative_to(PACKAGE).as_posix()
        for lineno, line in enumerate(path.read_text(errors="replace").splitlines(), start=1):
            for condition in _JINJA_IF.findall(line):
                for clause in _template_clauses_hiding_a_zero(condition):
                    hits.append(f"{rel}:{lineno}: `{clause.strip()}` — a recorded 0 disappears here")
    assert hits == [], "\n".join(hits)


def test_the_template_guard_fails_for_the_shape_it_is_meant_to_catch():
    """CLAUDE.md: a green ratchet proves nothing unless it can fail. These are
    the two spellings that were live, plus the fixed form that must stay green."""
    assert list(_template_clauses_hiding_a_zero("door.quantity and door.quantity > 1"))
    assert list(_template_clauses_hiding_a_zero("part.qty"))
    assert not list(_template_clauses_hiding_a_zero("door.quantity is not none and door.quantity != 1"))


def test_no_stored_quantity_is_read_with_or_one():
    """Every `quantity or 1` in the package, except the ALLOWED write-side clamp."""
    hits = []
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(PACKAGE).as_posix()
        if rel.startswith(("tests/", "migrations/", "frontend/")):
            continue
        source = path.read_text(errors="replace")
        lines = source.splitlines()
        for lineno in _quantity_or_one(ast.parse(source)):
            text = lines[lineno - 1].strip()
            if (rel, text) not in ALLOWED:
                hits.append(f"{rel}:{lineno}: {text}")
    assert hits == [], "\n".join(hits)
    stale = [key for key in ALLOWED
             if key[1] not in (line.strip() for line in (PACKAGE / key[0]).read_text().splitlines())]
    assert stale == [], f"ALLOWED entries that match nothing now — remove them: {stale}"

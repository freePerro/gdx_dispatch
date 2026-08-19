"""invoice_lines.includes_labor — "the install price is in the part price".

Doug 2026-08-19: some catalog items are priced WITH the installation. Billing
one of those alongside an hourly labor line charges the customer for the
install twice, and nothing in the data distinguishes a bundled item from a
bare one — the tenant's catalog carries both variants of the same opener,
separable only by the words in a free-text name. Money code may not guess
from prose, so the office ticks a box at billing and the invoice records it.

Contract pinned here:

1. The column defaults FALSE, so every pre-existing line keeps today's
   behaviour and the warning stays silent until a human ticks something.
2. The create contract accepts and stores it.
3. The patch contract can flip it after the fact.
4. It round-trips through the serializer, including for rows that predate
   the column.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models.tenant_models import Customer, Invoice, InvoiceLine

TENANT = "tenant-incl-labor"


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    for tbl in [Customer.__table__, Invoice.__table__, InvoiceLine.__table__]:
        tbl.create(bind=engine, checkfirst=True)
    TenantBase.metadata.create_all(bind=engine, checkfirst=True)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _invoice(db) -> Invoice:
    cust = Customer(id=uuid4(), name="Bundled Co", company_id=TENANT)
    db.add(cust)
    db.commit()
    inv = Invoice(
        id=uuid4(),
        customer_id=cust.id,
        invoice_number=f"INV-{uuid4().hex[:6]}",
        subtotal=0, tax_amount=0, total=0, balance_due=0,
        status="draft",
        public_token=uuid4().hex,
        company_id=TENANT,
    )
    db.add(inv)
    db.commit()
    return inv


def _line(db, inv, **kw) -> InvoiceLine:
    line = InvoiceLine(
        id=uuid4(),
        invoice_id=inv.id,
        description=kw.pop("description", "2220L opener with install"),
        quantity=1,
        unit_price=602,
        line_total=602,
        company_id=TENANT,
        **kw,
    )
    db.add(line)
    db.commit()
    db.refresh(line)
    return line


def test_defaults_false_so_existing_lines_are_unchanged(db) -> None:
    """The load-bearing default: nothing changes until a human ticks it."""
    inv = _invoice(db)
    line = _line(db, inv)
    assert line.includes_labor is False


def test_flag_persists_when_set(db) -> None:
    inv = _invoice(db)
    line = _line(db, inv, includes_labor=True)

    fetched = db.execute(
        select(InvoiceLine).where(InvoiceLine.id == line.id)
    ).scalar_one()
    assert fetched.includes_labor is True


def test_flag_is_per_line_not_per_invoice(db) -> None:
    """A bundled opener and a bare part can share one invoice."""
    inv = _invoice(db)
    bundled = _line(db, inv, description="opener with install", includes_labor=True)
    bare = _line(db, inv, description="keypad", includes_labor=False)

    assert bundled.includes_labor is True
    assert bare.includes_labor is False


def test_serializer_round_trips_the_flag(db) -> None:
    from gdx_dispatch.routers.invoices import _serialize_line

    inv = _invoice(db)
    bundled = _line(db, inv, includes_labor=True)
    bare = _line(db, inv, description="keypad", includes_labor=False)

    assert _serialize_line(bundled)["includes_labor"] is True
    assert _serialize_line(bare)["includes_labor"] is False


def test_serializer_reads_false_for_a_row_predating_the_column(db) -> None:
    """Older rows have no value; they must not read as bundled."""
    from gdx_dispatch.routers.invoices import _serialize_line

    class _Legacy:
        description = "old line"
        quantity = 1
        unit_price = 10
        line_total = 10
        taxable = True
        category = None
        cost_snapshot = None
        margin_pct_snapshot = None
        margin_pct_override = None
        part_id = None
        sort_order = 1
        id = uuid4()
        invoice_id = uuid4()
        created_at = None
        deleted_at = None
        # includes_labor deliberately absent

    assert _serialize_line(_Legacy())["includes_labor"] is False


def test_create_contract_accepts_the_flag_and_defaults_it_false() -> None:
    from gdx_dispatch.routers.invoices import InvoiceLineCreateIn

    assert InvoiceLineCreateIn(description="x").includes_labor is False
    assert InvoiceLineCreateIn(description="x", includes_labor=True).includes_labor is True


def test_patch_contract_can_flip_the_flag() -> None:
    from gdx_dispatch.routers.invoices import InvoiceLinePatchIn

    # Absent stays absent — exclude_unset semantics mean "leave it alone".
    assert "includes_labor" not in InvoiceLinePatchIn().model_dump(exclude_unset=True)
    flipped = InvoiceLinePatchIn(includes_labor=True).model_dump(exclude_unset=True)
    assert flipped["includes_labor"] is True
    cleared = InvoiceLinePatchIn(includes_labor=False).model_dump(exclude_unset=True)
    assert cleared["includes_labor"] is False


# ---------------------------------------------------------------------------
# add_invoice_line: the part must be CLAIMED, not just billed.
#
# The office adds a recorded part to a draft, the money is charged -- and if
# the part row is never stamped, every unbilled-parts surface keeps reporting
# it as missing. A warning that doing the right thing cannot clear is how a
# checklist becomes wallpaper.
# ---------------------------------------------------------------------------
def test_add_line_contract_accepts_part_id_and_includes_labor() -> None:
    from gdx_dispatch.routers.invoices import InvoiceLineCreateIn

    payload = InvoiceLineCreateIn(
        description="2220L chain (7ft door)",
        quantity=1,
        unit_price=536.00,
        part_id="a" * 36,
        includes_labor=True,
    )
    assert payload.part_id == "a" * 36
    assert payload.includes_labor is True


def test_add_line_handler_stores_part_id_and_claims_the_part() -> None:
    """Pins the two fields the handler silently dropped.

    Source-level because the handler needs the full request stack; the
    round-trip itself was walked in a browser (banner -> edit -> save ->
    reload -> banner gone).
    """
    import inspect

    from gdx_dispatch.routers import invoices

    src = inspect.getsource(invoices.add_invoice_line)
    assert "part_id=payload.part_id" in src, "line loses its part linkage"
    assert "includes_labor" in src, "contract field ignored by the handler"
    # The claim must be guarded on still-unbilled, or a concurrent create
    # could double-bill the same part.
    assert "billed_invoice_id.is_(None)" in src, "unguarded claim"
    assert "billed_invoice_id=invoice.id" in src, "part never claimed"


def test_add_line_claim_is_job_scoped_and_409s_when_it_cannot_claim() -> None:
    """Audit round 2: the create path earned both guards the hard way.

    Without the job scope a line on a counter sale or another job's invoice
    claims a part it has no business claiming. Without the 409, a part
    already billed elsewhere is CHARGED here while its stamp stays on the
    other invoice -- billing the customer twice and silencing the
    unbilled-parts banner in the same request.
    """
    import inspect

    from gdx_dispatch.routers import invoices

    src = inspect.getsource(invoices.add_invoice_line)
    assert "JobPartNeeded.job_id ==" in src, "claim is not job-scoped"
    assert "status_code=409" in src, "a failed claim must not bill silently"
    assert "db.rollback()" in src, "a refused claim must not leave the line"

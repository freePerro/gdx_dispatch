"""The arithmetic-check verdict is a column, not prose (money-audit M26).

The check guards untrusted LLM-extracted money. Its verdict lived only in the
free-text ``notes`` field it shares with the LLM marker — which is how a
substring contract silently reported PASS for failing bills. The read fix
(``not in``) shipped 2026-08-23; this suite pins the prescribed second half:
``vendor_invoices.invariant_ok`` (migration 078), written at creation,
preferred at read, with the substring surviving ONLY as the fallback for
pre-column (NULL) rows — where it reproduces the historical inference exactly.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import types

import pytest
from sqlalchemy import create_engine, inspect, text

MIGRATION = (
    pathlib.Path(__file__).resolve().parents[1]
    / "migrations/versions/078_vendor_invariant_column.py"
)


def _load(conn):
    spec = importlib.util.spec_from_file_location("m078", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    fake = types.ModuleType("alembic")

    class _Op:
        def get_bind(self):
            return conn

    fake.op = _Op()
    saved = sys.modules.get("alembic")
    sys.modules["alembic"] = fake
    try:
        spec.loader.exec_module(mod)
    finally:
        if saved is not None:
            sys.modules["alembic"] = saved
        else:
            sys.modules.pop("alembic", None)
    return mod


SEED = "CREATE TABLE vendor_invoices (id TEXT PRIMARY KEY, total REAL, notes TEXT)"


@pytest.fixture
def conn(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'm078.db'}", future=True)
    with eng.begin() as c:
        c.exec_driver_sql(SEED)
    with eng.begin() as c:
        yield c
    eng.dispose()


def _cols(conn):
    return {c["name"] for c in inspect(conn).get_columns("vendor_invoices")}


# ── the migration ──────────────────────────────────────────────────────────


def test_upgrade_adds_the_column(conn):
    m = _load(conn)
    assert "invariant_ok" not in _cols(conn)
    m.upgrade()
    assert "invariant_ok" in _cols(conn)


def test_backfill_is_partial_and_honest(conn):
    """Marker-present rows → FALSE (the marker is written iff the check
    failed — that direction is proven). Marker-absent rows stay NULL rather
    than asserting "passed" for eras this migration cannot vouch for."""
    conn.exec_driver_sql(
        "INSERT INTO vendor_invoices (id, total, notes) VALUES "
        "('failed', 100, 'LLM_EXTRACTED (llm:x): verify; INVARIANT_MISMATCH: header off by 3'),"
        "('clean',  200, 'LLM_EXTRACTED (llm:x): verify'),"
        "('bare',   300, NULL)"
    )
    _load(conn).upgrade()
    rows = dict(conn.execute(text("SELECT id, invariant_ok FROM vendor_invoices")).all())
    assert rows["failed"] == 0, "a marker row must backfill FALSE"
    assert rows["clean"] is None, "no marker is not proof of passing — stay NULL"
    assert rows["bare"] is None


def test_re_runnable_and_reversible(conn):
    m = _load(conn)
    m.upgrade()
    m.upgrade()
    assert "invariant_ok" in _cols(conn)
    m.downgrade()
    assert "invariant_ok" not in _cols(conn)
    m.upgrade()
    assert "invariant_ok" in _cols(conn)


def test_downgrade_keeps_the_bill(conn):
    conn.exec_driver_sql(
        "INSERT INTO vendor_invoices (id, total, notes) VALUES ('b1', 842.50, 'x')"
    )
    m = _load(conn)
    m.upgrade()
    m.downgrade()
    assert conn.execute(text("SELECT total FROM vendor_invoices WHERE id='b1'")).scalar() == 842.50


def test_on_postgres(pg_test_engine):
    """The other engine — including the %% escape in the backfill LIKE.
    Skips when no Postgres is reachable."""
    with pg_test_engine.begin() as c:
        c.exec_driver_sql("DROP TABLE IF EXISTS vendor_invoices CASCADE")
        c.exec_driver_sql(
            "CREATE TABLE vendor_invoices (id TEXT PRIMARY KEY, total NUMERIC(12,2), notes TEXT)"
        )
        c.exec_driver_sql(
            "INSERT INTO vendor_invoices VALUES ('f', 10, 'LLM_EXTRACTED; INVARIANT_MISMATCH: x'), ('c', 20, 'fine')"
        )
        m = _load(c)
        m.upgrade()
        assert "invariant_ok" in _cols(c)
        rows = dict(c.execute(text("SELECT id, invariant_ok FROM vendor_invoices")).all())
        assert rows["f"] is False and rows["c"] is None
        m.upgrade()
        m.downgrade()
        assert "invariant_ok" not in _cols(c)


# ── the write path ─────────────────────────────────────────────────────────


@pytest.fixture
def vendor_db(tmp_path, monkeypatch):
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from gdx_dispatch.core.audit import TenantBase, ensure_audit_table

    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TenantBase.metadata.create_all(eng, checkfirst=True)
    db = sessionmaker(bind=eng, autoflush=False, autocommit=False)()
    ensure_audit_table(db)
    yield db
    db.close()
    eng.dispose()


def _upload(db, *, header_off: bool, tag: str | None = None):
    """Drive the REAL shared persistence entry (`_persist_parsed_invoice`) —
    the same function both the parser and LLM rungs call — with a bill whose
    header arithmetic passes or fails."""
    from decimal import Decimal

    from gdx_dispatch.modules.vendor_invoices import service as svc
    from gdx_dispatch.modules.vendor_invoices.parsers.midwest_invoice import (
        ParsedInvoice,
        ParsedInvoiceLine,
    )

    tag = tag or ("BAD" if header_off else "OK")
    parsed = ParsedInvoice(
        invoice_number=f"MW-{tag}-1",
        invoice_date=None,
        po_reference=None,
        terms=None,
        net_days=None,
        due_date=None,
        tax=Decimal("0"),
        shipping=Decimal("0"),
        # lines sum to $100; a $150 header is the invariant failure
        total=Decimal("150.00") if header_off else Decimal("100.00"),
        credits_pending=Decimal("0"),
        amount_due=None,
        lines=[ParsedInvoiceLine(
            line_no=1, item_label="P1", description="Torsion spring",
            quantity=Decimal("1"), package=None,
            unit_price=Decimal("100.00"), line_total=Decimal("100.00"),
        )],
    )
    return svc._persist_parsed_invoice(
        db,
        pdf_bytes=b"%PDF-fake-" + tag.encode(),
        content_hash=f"hash-{tag}",
        existing_doc=None,
        parsed=parsed,
        vendor_name_raw="Midwest Door Co",
        extraction_method="llm",
        extractor_label="llm:test",
        original_filename=f"{tag}.pdf",
        content_type="application/pdf",
        uploaded_by="tester",
        source="upload",
    )


def test_a_failing_bill_writes_false(vendor_db):
    r = _upload(vendor_db, header_off=True)
    assert r.invariant_ok is False
    assert r.invoice.invariant_ok is False, "the verdict must be stored, not just returned"


def test_a_passing_bill_writes_true(vendor_db):
    r = _upload(vendor_db, header_off=False)
    assert r.invariant_ok is True
    assert r.invoice.invariant_ok is True


# ── the read path ──────────────────────────────────────────────────────────


def test_the_column_beats_the_prose(vendor_db):
    """A FALSE column with scrubbed notes still reports FALSE — the verdict no
    longer depends on prose surviving edits."""
    r = _upload(vendor_db, header_off=True)
    r.invoice.notes = "office cleaned this up"
    vendor_db.flush()

    from gdx_dispatch.routers.vendor_invoices import _detail

    out = _detail(vendor_db, r.invoice)
    assert out.invariant_ok is False


def test_a_pre_column_row_without_the_marker_reads_true(vendor_db):
    """THE polarity that catches a dropped fallback. bool(None) is False, so a
    reader that ignores NULL coincidentally agrees with the marker case — a
    counterfactual proved the marker-polarity test alone was vacuous. A NULL
    row with clean notes must read True (the historical inference), which
    bool(None) cannot produce."""
    r = _upload(vendor_db, header_off=False)
    r.invoice.invariant_ok = None
    r.invoice.notes = "LLM_EXTRACTED (x): verify against the PDF"
    vendor_db.flush()

    from gdx_dispatch.routers.vendor_invoices import _detail

    out = _detail(vendor_db, r.invoice)
    assert out.invariant_ok is True


def test_a_pre_column_row_falls_back_to_the_substring(vendor_db):
    """NULL means "row predates the column"; the reader reproduces the
    historical inference exactly."""
    r = _upload(vendor_db, header_off=False)
    r.invoice.invariant_ok = None
    r.invoice.notes = "LLM_EXTRACTED (x): verify; INVARIANT_MISMATCH: header off"
    vendor_db.flush()

    from gdx_dispatch.routers.vendor_invoices import _detail

    out = _detail(vendor_db, r.invoice)
    assert out.invariant_ok is False


def test_the_review_queue_rows_carry_the_verdict(vendor_db):
    """The adversarial find that mattered: the office looks at the LIST, and
    the list schema never carried the field — the new column had zero UI
    consumers while both Vue views still parsed prose with the original
    startsWith bug. A failing bill must arrive at the queue as
    invariant_ok=false; a pre-column row as null (the client then falls back
    to the substring)."""
    import asyncio

    from gdx_dispatch.routers.vendor_invoices import list_invoices

    bad = _upload(vendor_db, header_off=True)
    ok = _upload(vendor_db, header_off=False)
    # A distinct number: the (vendor, invoice_number) dedup layer collapses a
    # same-number upload into the existing row — the first draft of this test
    # mutated the OK row through the dedup return and asserted a fiction.
    legacy = _upload(vendor_db, header_off=False, tag="LEGACY")
    legacy.invoice.invariant_ok = None
    vendor_db.flush()

    rows = asyncio.run(
        list_invoices(status=None, needs_review=False, _={"id": "t"}, db=vendor_db)
    )
    by_num = {r.invoice_number: r.invariant_ok for r in rows}
    assert by_num[bad.invoice.invoice_number] is False
    assert by_num[ok.invoice.invoice_number] is True
    assert by_num[legacy.invoice.invoice_number] is None, (
        "a pre-column row must say UNKNOWN, not guess"
    )


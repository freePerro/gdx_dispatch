"""Labor billed through a point: a labor line names the day rows it bills
(``time_entry_ids``), the server claims them in the same transaction, and a
second line or a second invoice can never bill the same days again.

Every test drives the real route functions against an ORM-built SQLite
database and reads back what landed: the invoice lines, the
``time_entries.billed_invoice_id`` stamps, and the audit rows. The race of
two claims has a Postgres arm, which skips without a postgres URL.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import threading
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core.audit import AuditLog
from gdx_dispatch.core.closeout_billing import claim_day_rows
from gdx_dispatch.core.job_taxonomy import INSTALLATION, SERVICE_CALL
from gdx_dispatch.models.pricing_engine import PricingSettings
from gdx_dispatch.models.tenant_models import (
    Customer,
    Invoice,
    InvoiceLine,
    Job,
    JobCloseout,
    TimeEntry,
)
from gdx_dispatch.routers.invoices import (
    InvoiceCreateIn,
    InvoiceLineCreateIn,
    InvoiceLinePatchIn,
    add_invoice_line,
    create_invoice,
    delete_invoice,
    delete_invoice_line,
    patch_invoice_line,
    void_invoice,
)
from gdx_dispatch.routers.jobs import closeout_billing_suggestion
from gdx_dispatch.tests.conftest import make_fresh_db

TENANT = "tenant-1"
ACTOR = "00000000-0000-0000-0000-0000000000b1"
USER = {"user_id": ACTOR, "tenant_id": TENANT, "role": "admin"}
DAY1 = _dt.datetime(2026, 11, 2, 15, 0, tzinfo=_dt.UTC)
DAY2 = DAY1 + _dt.timedelta(days=1)
DAY3 = DAY1 + _dt.timedelta(days=2)


@pytest.fixture
def db():
    engine = make_fresh_db()
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _rates(db, first: str, hourly: str) -> None:
    db.add(PricingSettings(service_call_first_hour_price=Decimal(first),
                           service_call_hourly_rate=Decimal(hourly)))
    db.commit()


def _job(db, *, job_type=SERVICE_CALL, stage="completed") -> Job:
    cust = Customer(id=uuid4(), name="Acme", phone="555", email="a@x.com",
                    address="11 Main", company_id=TENANT)
    db.add(cust)
    db.flush()
    job = Job(id=uuid4(), company_id=TENANT, customer_id=cust.id, title="Spring",
              description="", scheduled_at=DAY1, status="Completed", priority="Normal",
              job_type=job_type, lifecycle_stage=stage, dispatch_status="assigned",
              billing_status="unbilled", is_demo=False, is_return_visit=False)
    db.add(job)
    db.commit()
    return job


def _row(db, job, *, at, minutes) -> TimeEntry:
    te = TimeEntry(id=uuid4(), job_id=job.id, tech_id=str(uuid4()), company_id=TENANT,
                   entry_type="job", clock_in=at, clock_out=at + _dt.timedelta(minutes=minutes),
                   duration_minutes=minutes, day_closed_at=at + _dt.timedelta(hours=9))
    db.add(te)
    db.commit()
    return te


def _closeout(db, job, *, hours, techs=1, matrix=None) -> JobCloseout:
    co = JobCloseout(id=uuid4(), job_id=job.id, hours_worked=hours, techs_on_site=techs,
                     labor_matrix_item_id=matrix, closed_by_user_id=str(uuid4()), closed_at=DAY3)
    db.add(co)
    db.commit()
    return co


def _suggest(db, job, invoice_id=None) -> tuple[int, dict]:
    resp = closeout_billing_suggestion(
        job_id=str(job.id), request=SimpleNamespace(headers={}, client=None, state=SimpleNamespace()),
        invoice_id=None if invoice_id is None else str(invoice_id), current_user=USER, db=db,
    )
    return resp.status_code, json.loads(resp.body)


def _as_line(s: dict, **over) -> InvoiceLineCreateIn:
    """A suggested line as the browser sends it back."""
    body = {
        "description": s["description"], "quantity": Decimal(str(s["quantity"])),
        "unit_price": s["unit_price"], "taxable": False, "category": "Labor",
        "labor_source": "attested", "estimated_man_hours": s.get("estimated_man_hours"),
        "time_entry_ids": s.get("time_entry_ids"),
    }
    body.update(over)
    return InvoiceLineCreateIn(**body)


def _create(db, job, lines, *, force=False) -> dict:
    return create_invoice(
        payload=InvoiceCreateIn(job_id=job.id, customer_id=job.customer_id,
                                line_items=lines, force=force),
        _=USER, db=db,
    )


def _holders(db, *rows) -> list:
    db.expire_all()
    return [db.get(TimeEntry, r.id).billed_invoice_id for r in rows]


def _lines(db, inv_id) -> list[InvoiceLine]:
    db.expire_all()
    return list(db.execute(
        select(InvoiceLine).where(InvoiceLine.invoice_id == UUID(str(inv_id)),
                                  InvoiceLine.deleted_at.is_(None)).order_by(InvoiceLine.sort_order)
    ).scalars())


def _audits(db, action: str, entity_id) -> list[AuditLog]:
    return list(db.execute(
        select(AuditLog).where(AuditLog.action == action, AuditLog.entity_id == str(entity_id))
    ).scalars())


def _code(exc: pytest.ExceptionInfo) -> tuple[int, object]:
    return exc.value.status_code, exc.value.detail


# ── the suggestion → the create, end to end ──────────────────────────


def test_the_suggestion_names_the_rows_and_the_create_claims_them(db):
    job = _job(db)
    r1 = _row(db, job, at=DAY1, minutes=180)
    r2 = _row(db, job, at=DAY2, minutes=252)
    _closeout(db, job, hours=2)

    code, body = _suggest(db, job)
    assert code == 200
    [line] = body["labor_lines"]  # $100/$100 by default: one line
    assert line["quantity"] == 9.5
    assert line["line_total"] == 950.0
    assert sorted(line["time_entry_ids"]) == sorted([str(r1.id), str(r2.id)])

    inv = _create(db, job, [_as_line(line)])
    assert _holders(db, r1, r2) == [UUID(inv["id"])] * 2
    [stored] = _lines(db, inv["id"])
    assert stored.pricing_source == "labor_attested"
    assert stored.labor_source == "attested"
    assert Decimal(str(stored.quantity)) == Decimal("9.5")
    [audit] = _audits(db, "invoice_created", inv["id"])
    assert audit.details["claimed_time_entries"] == 2
    assert audit.user_id == ACTOR

    # Reloaded afterwards, the suggestion offers no days a second time.
    _code_, again = _suggest(db, job)
    assert again["labor_lines"][0]["time_entry_ids"] == []
    assert again["labor_lines"][0]["quantity"] == 2.0  # the closeout's hours only


def test_the_zero_hour_final_day_and_close_without_work_still_offer_the_days(db):
    job = _job(db)
    row = _row(db, job, at=DAY1, minutes=360)
    # Close-without-work: a completed job with no closeout at all.
    _c, body = _suggest(db, job)
    [line] = body["labor_lines"]
    assert (line["quantity"], line["time_entry_ids"]) == (6.0, [str(row.id)])
    # And a 0 h "Yes" closeout offers the same days.
    _closeout(db, job, hours=0)
    _c, body = _suggest(db, job)
    [line] = body["labor_lines"]
    assert (line["quantity"], line["time_entry_ids"]) == (6.0, [str(row.id)])


def test_an_in_progress_job_is_not_offered_its_day_rows(db):
    job = _job(db, stage="in_progress")
    _row(db, job, at=DAY1, minutes=360)
    _c, body = _suggest(db, job)
    assert body["labor_lines"] == []


def test_an_install_lane_suggestion_still_reaches_the_picker(db):
    from gdx_dispatch.models.labor_pricing import LaborPriceItem

    item = LaborPriceItem(id=uuid4(), description="16x7 Sectional Install", service_type="install",
                          flat_price=Decimal("650"), assumed_man_hours=Decimal("6.5"),
                          default_crew_size=1, min_wall_clock_minutes=15, active=True,
                          effective_from=_dt.date(2026, 1, 1), sort_order=1)
    db.add(item)
    db.commit()
    job = _job(db, job_type=INSTALLATION)
    _row(db, job, at=DAY1, minutes=480)
    _closeout(db, job, hours=4, matrix=str(item.id))
    _c, body = _suggest(db, job)
    [line] = body["labor_lines"]
    assert line["source"] == "matrix"
    assert line["labor_price_item_id"] == str(item.id)
    assert line["line_total"] == 650.0
    assert "time_entry_ids" not in line


# ── the two 409s ─────────────────────────────────────────────────────


def test_picker_plus_prefill_in_one_post_is_labor_already_on_invoice(db):
    job = _job(db)
    row = _row(db, job, at=DAY1, minutes=180)
    _closeout(db, job, hours=1)
    _c, body = _suggest(db, job)
    [line] = body["labor_lines"]
    before = db.execute(select(Invoice.id)).all()
    with pytest.raises(HTTPException) as exc:
        _create(db, job, [_as_line(line), _as_line(line)])
    assert _code(exc) == (409, {
        "code": "labor_already_on_invoice",
        "message": "This invoice already has these days' labor — remove one of the labor lines.",
    })
    # Nothing landed: no invoice, no stamp.
    assert db.execute(select(Invoice.id)).all() == before
    assert _holders(db, row) == [None]


def test_another_invoice_holding_the_rows_is_labor_already_billed(db):
    job = _job(db)
    row = _row(db, job, at=DAY1, minutes=180)
    _closeout(db, job, hours=1)
    _c, body = _suggest(db, job)
    [line] = body["labor_lines"]
    first = _create(db, job, [_as_line(line)])
    # A stale page sends the same suggestion again, past the job-level
    # "already billed" prompt.
    with pytest.raises(HTTPException) as exc:
        _create(db, job, [_as_line(line)], force=True)
    assert _code(exc) == (409, {
        "code": "labor_already_billed",
        "message": f"These days' labor is already billed on invoice {first['invoice_number']}"
                   " — reload the labor suggestion.",
    })
    assert _holders(db, row) == [UUID(first["id"])]


def test_mixed_holders_answer_labor_already_billed(db):
    job = _job(db)
    r1 = _row(db, job, at=DAY1, minutes=60)
    r2 = _row(db, job, at=DAY2, minutes=60)
    other = _create(db, job, [InvoiceLineCreateIn(description="Labor", quantity=Decimal("1"),
                                                  unit_price=100, time_entry_ids=[r1.id])])
    with pytest.raises(HTTPException) as exc:
        _create(db, job, [
            InvoiceLineCreateIn(description="Labor A", unit_price=100, time_entry_ids=[r2.id]),
            InvoiceLineCreateIn(description="Labor B", unit_price=100, time_entry_ids=[r1.id, r2.id]),
        ], force=True)
    status, detail = _code(exc)
    assert status == 409 and detail["code"] == "labor_already_billed"
    assert other["invoice_number"] in detail["message"]
    assert _holders(db, r1, r2) == [UUID(other["id"]), None]


def test_an_edited_invoice_keeping_its_old_line_is_labor_already_on_invoice(db):
    job = _job(db)
    row = _row(db, job, at=DAY1, minutes=180)
    _closeout(db, job, hours=1)
    _c, body = _suggest(db, job)
    [line] = body["labor_lines"]
    inv = _create(db, job, [_as_line(line)])
    # The edit page asks for the suggestion as this invoice: its own rows
    # count as unbilled for it, so they are offered again ...
    _c, body = _suggest(db, job, invoice_id=inv["id"])
    [again] = body["labor_lines"]
    assert again["time_entry_ids"] == [str(row.id)]
    # ... and adding them beside the line that already bills them is refused.
    with pytest.raises(HTTPException) as exc:
        add_invoice_line(invoice_id=UUID(inv["id"]), payload=_as_line(again), current_user=USER, db=db)
    assert _code(exc)[1]["code"] == "labor_already_on_invoice"
    assert len(_lines(db, inv["id"])) == 1


def test_delete_first_then_add_replaces_the_line(db):
    """The edit save's delete-first order: the old line goes (releasing the
    rows), then the suggestion's line claims them again."""
    job = _job(db)
    row = _row(db, job, at=DAY1, minutes=180)
    _closeout(db, job, hours=1)
    _c, body = _suggest(db, job)
    inv = _create(db, job, [_as_line(body["labor_lines"][0])])
    inv_id = UUID(inv["id"])
    [old] = _lines(db, inv_id)
    delete_invoice_line(invoice_id=inv_id, line_id=old.id, user=USER, db=db)
    assert _holders(db, row) == [None]
    _c, body = _suggest(db, job, invoice_id=inv_id)
    add_invoice_line(invoice_id=inv_id, payload=_as_line(body["labor_lines"][0]), current_user=USER, db=db)
    assert _holders(db, row) == [inv_id]


# ── what is never refused, and what is ──────────────────────────────


def test_a_hand_built_split_pair_claims_once_and_both_lines_are_attested(db):
    _rates(db, "125", "100")
    job = _job(db)
    row = _row(db, job, at=DAY1, minutes=150)
    _closeout(db, job, hours=0)
    _c, body = _suggest(db, job)
    first, hourly = body["labor_lines"]
    assert "time_entry_ids" not in first
    assert (first["quantity"], first["unit_price"], hourly["quantity"]) == (1.0, 125.0, 1.5)
    inv = _create(db, job, [_as_line(first), _as_line(hourly)])
    assert Decimal(inv["total"] if not isinstance(inv["total"], float) else str(inv["total"])) == Decimal("275")
    lines = _lines(db, inv["id"])
    assert [ln.pricing_source for ln in lines] == ["labor_attested", "labor_attested"]
    assert _holders(db, row) == [UUID(inv["id"])]


@pytest.mark.parametrize(("over", "want"), [
    ({"time_entry_ids": []}, "labor_attested"),            # [] is "from the suggestion"
    ({"time_entry_ids": None}, "labor_attested"),          # absent, but attested
    ({"time_entry_ids": None, "labor_source": None}, "manual"),
    ({"time_entry_ids": [], "labor_source": "manual"}, "labor_attested"),  # hours edited before save
])
def test_pricing_source_of_a_hand_built_line(db, over, want):
    job = _job(db)
    s = {"description": "Labor", "quantity": 2, "unit_price": 100.0, "time_entry_ids": None}
    inv = _create(db, job, [_as_line(s, **over)])
    [line] = _lines(db, inv["id"])
    assert line.pricing_source == want


def test_an_id_that_is_not_this_jobs_closed_day_row_is_a_422(db):
    job = _job(db)
    other_job = _job(db)
    foreign = _row(db, other_job, at=DAY1, minutes=60)
    consumed = _row(db, job, at=DAY1, minutes=0)  # a day-close stamped it to 0
    good = _row(db, job, at=DAY2, minutes=60)
    for bad in ([foreign.id], [consumed.id], [uuid4()], [good.id, good.id]):
        with pytest.raises(HTTPException) as exc:
            _create(db, job, [InvoiceLineCreateIn(description="Labor", unit_price=100, time_entry_ids=bad)])
        assert exc.value.status_code == 422, bad
    assert _holders(db, foreign, consumed, good) == [None, None, None]


def test_a_two_and_a_half_hour_line_at_33_33_adds_to_the_cent(db):
    job = _job(db)
    row = _row(db, job, at=DAY1, minutes=150)
    inv = _create(db, job, [])
    out = add_invoice_line(
        invoice_id=UUID(inv["id"]),
        payload=InvoiceLineCreateIn(description="Service labor", quantity=Decimal("2.5"), unit_price=33.33,
                                    taxable=False, category="Labor", labor_source="attested",
                                    time_entry_ids=[row.id]),
        current_user=USER, db=db,
    )
    [line] = _lines(db, inv["id"])
    assert Decimal(str(line.line_total)) == Decimal("83.33")
    assert line.pricing_source == "labor_attested"
    assert _holders(db, row) == [UUID(inv["id"])]
    [audit] = _audits(db, "add_invoice_line", inv["id"])
    assert audit.details == {"claimed_time_entries": 1}
    assert out is not None


# ── an attested line edited ──────────────────────────────────────────


def _attested_invoice(db):
    job = _job(db)
    row = _row(db, job, at=DAY1, minutes=180)
    inv = _create(db, job, [InvoiceLineCreateIn(description="Service labor", quantity=Decimal("3"),
                                                unit_price=100, taxable=False, category="Labor",
                                                labor_source="attested", time_entry_ids=[row.id])])
    [line] = _lines(db, inv["id"])
    return UUID(inv["id"]), line


def _patch(db, inv_id, line, **fields):
    return patch_invoice_line(invoice_id=inv_id, line_id=line.id,
                              payload=InvoiceLinePatchIn(**fields), user=USER, db=db)


def test_a_description_edit_keeps_the_line_attested(db):
    inv_id, line = _attested_invoice(db)
    # The detail view sends quantity and price on every PATCH.
    _patch(db, inv_id, line, description="Labor, as agreed", quantity=Decimal("3"), unit_price=100)
    [after] = _lines(db, inv_id)
    assert (after.labor_source, after.pricing_source) == ("attested", "labor_attested")


@pytest.mark.parametrize("fields", [
    {"quantity": Decimal("2.5")},
    {"unit_price": 90},
    {"quantity": Decimal("2.5"), "labor_source": "attested"},  # the downgrade wins
])
def test_changing_hours_or_rate_makes_the_line_manual(db, fields):
    inv_id, line = _attested_invoice(db)
    _patch(db, inv_id, line, **fields)
    [after] = _lines(db, inv_id)
    assert after.labor_source == "manual"
    assert after.pricing_source == "labor_attested", "where the line started stays recorded"


# ── releases ─────────────────────────────────────────────────────────


def test_deleting_the_last_attested_line_releases_with_an_audit_row(db):
    _rates(db, "125", "100")
    job = _job(db)
    row = _row(db, job, at=DAY1, minutes=150)
    _c, body = _suggest(db, job)
    first, hourly = body["labor_lines"]
    inv = _create(db, job, [_as_line(first), _as_line(hourly)])
    inv_id = UUID(inv["id"])
    l_first, l_hourly = _lines(db, inv_id)

    # One line of a split pair: the claim stays (the other still bills it).
    delete_invoice_line(invoice_id=inv_id, line_id=l_hourly.id, user=USER, db=db)
    assert _holders(db, row) == [inv_id]
    assert _audits(db, "labor_day_rows_released", inv_id) == []

    delete_invoice_line(invoice_id=inv_id, line_id=l_first.id, user=USER, db=db)
    assert _holders(db, row) == [None]
    [audit] = _audits(db, "labor_day_rows_released", inv_id)
    assert audit.user_id == ACTOR
    assert audit.details["released_time_entries"] == 1
    assert audit.details["why"] == "last_labor_line_deleted"
    [deleted] = _audits(db, "invoice_line_deleted", l_first.id)
    assert deleted.details["released_time_entries"] == 1


def test_void_releases_and_the_re_invoice_bills_the_days_again(db):
    job = _job(db)
    row = _row(db, job, at=DAY1, minutes=180)
    _closeout(db, job, hours=1)
    _c, body = _suggest(db, job)
    inv = _create(db, job, [_as_line(body["labor_lines"][0])])
    inv_id = UUID(inv["id"])

    void_invoice(invoice_id=inv_id, _=USER, db=db)
    assert _holders(db, row) == [None]
    [audit] = _audits(db, "labor_day_rows_released", inv_id)
    assert (audit.user_id, audit.details["why"], audit.details["released_time_entries"]) == (
        ACTOR, "invoice_voided", 1)
    [voided] = _audits(db, "invoice_voided", inv_id)
    assert voided.details["released_time_entries"] == 1

    # The void no longer counts as the first hour charged, and the days are free.
    _c, body = _suggest(db, job)
    [line] = body["labor_lines"]
    assert (line["quantity"], line["time_entry_ids"]) == (4.0, [str(row.id)])
    again = _create(db, job, [_as_line(line)])
    assert _holders(db, row) == [UUID(again["id"])]


def test_deleting_the_invoice_releases_with_an_audit_row(db):
    job = _job(db)
    row = _row(db, job, at=DAY1, minutes=180)
    _closeout(db, job, hours=1)
    _c, body = _suggest(db, job)
    inv = _create(db, job, [_as_line(body["labor_lines"][0])])
    inv_id = UUID(inv["id"])
    delete_invoice(invoice_id=inv_id, current_user=USER, db=db)
    assert _holders(db, row) == [None]
    [audit] = _audits(db, "labor_day_rows_released", inv_id)
    assert audit.details["why"] == "invoice_deleted"
    assert audit.user_id == ACTOR


def test_reopen_then_re_invoice_offers_only_new_days_and_no_second_first_hour(db):
    _rates(db, "125", "100")
    job = _job(db)
    r1 = _row(db, job, at=DAY1, minutes=180)
    co = _closeout(db, job, hours=1)
    _c, body = _suggest(db, job)
    first_inv = _create(db, job, [_as_line(s) for s in body["labor_lines"]])
    assert Decimal(str(first_inv["total"])) == Decimal("425")  # 4 h: 125 + 3 × 100

    # Reopened: another day, then a new closeout.
    r2 = _row(db, job, at=DAY2, minutes=60)
    co.superseded_at = DAY3
    db.commit()
    _closeout(db, job, hours=0.25)
    _c, body = _suggest(db, job)
    [line] = body["labor_lines"]
    assert line["description"] != "Service labor — first hour"
    assert (line["quantity"], line["unit_price"], line["time_entry_ids"]) == (1.5, 100.0, [str(r2.id)])
    second = _create(db, job, [_as_line(line)], force=True)
    assert _holders(db, r1, r2) == [UUID(first_inv["id"]), UUID(second["id"])]


def test_the_suggestion_refuses_an_invoice_from_another_job(db):
    job = _job(db)
    other = _job(db)
    inv = _create(db, other, [])
    assert _suggest(db, job, invoice_id=inv["id"])[0] == 404
    assert _suggest(db, job, invoice_id="not-a-uuid")[0] == 422


# ── a race of two claims ─────────────────────────────────────────────


def test_two_claims_on_sqlite_one_wins(db):
    job = _job(db)
    row = _row(db, job, at=DAY1, minutes=60)
    a, b = uuid4(), uuid4()
    assert claim_day_rows(db, a, [row.id]) == 1
    assert claim_day_rows(db, b, [row.id]) == 0
    db.rollback()


_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""


def test_these_tests_actually_run_in_ci() -> None:
    if os.environ.get("CI"):
        assert "postgresql" in _URL, "CI is set but no postgres URL — the Postgres race would skip."


@pytest.mark.skipif("postgresql" not in _URL,
                    reason="the claim race needs a real Postgres; set DATABASE_URL or TEST_DATABASE_URL")
def test_two_concurrent_claims_on_postgres_one_wins():
    """Session A claims and holds its transaction open; session B's claim of
    the same row blocks on the row lock, then re-reads ``billed_invoice_id``
    once A commits and claims nothing."""
    eng = create_engine(_URL, future=True)
    schema = "claim_race_test"  # a constant: the statements below name it literally
    row = uuid4()
    with eng.begin() as c:
        c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        c.exec_driver_sql(f"CREATE SCHEMA {schema}")
        c.exec_driver_sql(
            f"CREATE TABLE {schema}.time_entries (id uuid PRIMARY KEY, billed_invoice_id uuid, "
            "updated_at timestamptz)"
        )
        c.exec_driver_sql("INSERT INTO claim_race_test.time_entries (id) VALUES (%s)", (str(row),))
    scoped = eng.execution_options(schema_translate_map={None: schema})
    Session = sessionmaker(bind=scoped)
    a, b = Session(), Session()
    try:
        inv_a, inv_b = uuid4(), uuid4()
        assert claim_day_rows(a, inv_a, [row]) == 1   # A holds the row lock
        result: dict = {}

        def _b():
            result["b"] = claim_day_rows(b, inv_b, [row])
            b.commit()

        t = threading.Thread(target=_b)
        t.start()
        t.join(timeout=1.0)
        assert t.is_alive(), "B must wait on A's row lock"
        a.commit()
        t.join(timeout=10)
        assert result == {"b": 0}
        with eng.connect() as c:
            assert c.exec_driver_sql(
                "SELECT billed_invoice_id FROM claim_race_test.time_entries").scalar() == inv_a
    finally:
        a.close()
        b.close()
        with eng.begin() as c:
            c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        eng.dispose()

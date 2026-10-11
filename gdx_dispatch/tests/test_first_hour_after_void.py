"""GDXA-489: the first hour comes back with the re-bill after its carrier dies.

``first_hour_charged_elsewhere`` is read at build time, so an invoice built
beside a live carrier goes out hourly-only. It used to count ANY attested
line as "charged elsewhere": void the carrier and that hourly-only sibling
then stood in for it, so no later build charged the first hour either.

Doug's ruling (2026-10-10, design A): only a live invoice that actually
carries the first hour counts; the re-bill takes it; a sibling invoice is
never re-priced. Hours on a non-carrying sibling shrink the floor, so the job
bills at least 1 h and never more. These drive the real route functions and
add up what landed.
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core.billing_lanes import FIRST_HOUR_DESCRIPTION, first_hour_elsewhere
from gdx_dispatch.models.pricing_engine import PricingSettings
from gdx_dispatch.models.tenant_models import Invoice
from gdx_dispatch.routers.invoices import (
    InvoiceLineCreateIn,
    delete_invoice,
    delete_invoice_line,
    void_invoice,
)
from gdx_dispatch.tests.conftest import make_fresh_db
from gdx_dispatch.tests.test_labor_billed_through_a_point import (
    USER,
    _as_line,
    _closeout,
    _create,
    _job,
    _lines,
    _rates,
    _suggest,
)


@pytest.fixture
def db():
    engine = make_fresh_db()
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _attested(db, job, description: str, quantity: str, unit_price: str) -> UUID:
    inv = _create(db, job, force=True, lines=[InvoiceLineCreateIn(
        description=description, quantity=Decimal(quantity), unit_price=Decimal(unit_price),
        taxable=False, category="Labor", labor_source="attested",
    )])
    return UUID(inv["id"])


def _hourly_only(db, job, hours: str = "0.5") -> UUID:
    """A sibling built while another invoice carried the first hour: attested
    hours at the hourly rate, no floor, in the text the builder writes."""
    return _attested(db, job, f"Service labor — {Decimal(hours):.2f} man-hours at $100.00/hr",
                     hours, "100")


def _rebill(db, job) -> UUID:
    _c, body = _suggest(db, job)
    return UUID(_create(db, job, [_as_line(ln) for ln in body["labor_lines"]], force=True)["id"])


def _job_total(db, job) -> Decimal:
    db.expire_all()
    return sum((Decimal(str(i.total)) for i in db.execute(
        select(Invoice).where(Invoice.job_id == job.id, Invoice.deleted_at.is_(None),
                              Invoice.status != "void")).scalars()), Decimal(0))


def _void(db, inv_id):
    void_invoice(invoice_id=inv_id, _=USER, db=db)


def _delete(db, inv_id):
    delete_invoice(invoice_id=inv_id, current_user=USER, db=db)


def _strip(db, inv_id):
    for ln in _lines(db, inv_id):
        delete_invoice_line(invoice_id=inv_id, line_id=ln.id, user=USER, db=db)


def _change_rates(db, first: str, hourly: str) -> None:
    settings = db.execute(select(PricingSettings)).scalars().first()
    settings.service_call_first_hour_price = Decimal(first)
    settings.service_call_hourly_rate = Decimal(hourly)
    db.commit()


def _carrier_and_sibling(db, *, first="125", hourly="100", closeout_hours=3.0):
    _rates(db, first, hourly)
    job = _job(db)
    co = _closeout(db, job, hours=closeout_hours)
    carrier = _rebill(db, job)
    assert _lines(db, carrier)[0].description == FIRST_HOUR_DESCRIPTION
    sibling = _hourly_only(db, job)
    return job, co, carrier, sibling


@pytest.mark.parametrize("remove", [_void, _delete, _strip])
def test_void_and_reissue_bills_the_first_hour_once(db, remove):
    # 3 h + 0.5 h at 125/100 is $375. The specified re-price-the-sibling fix
    # made this $425; before any fix it was $350.
    job, _co, carrier, sibling = _carrier_and_sibling(db)
    assert _job_total(db, job) == Decimal("375.00")
    sibling_before = [(ln.id, ln.quantity, ln.unit_price) for ln in _lines(db, sibling)]

    remove(db, carrier)
    reissued = _rebill(db, job)

    assert [ln.description for ln in _lines(db, reissued)][0] == FIRST_HOUR_DESCRIPTION
    assert _job_total(db, job) == Decimal("375.00")
    assert [(ln.id, ln.quantity, ln.unit_price) for ln in _lines(db, sibling)] == sibling_before, \
        "a sibling invoice is never re-priced"


def test_a_build_after_the_reissue_is_not_charged_again(db):
    job, _co, carrier, _sibling = _carrier_and_sibling(db)
    _void(db, carrier)
    reissued = _rebill(db, job)
    assert first_hour_elsewhere(db, job.id)[0], "the re-issue now carries it"
    assert not first_hour_elsewhere(db, job.id, invoice_id=reissued)[0], \
        "the hourly-only sibling alone does not"


def test_at_split_rates_hourly_only_siblings_never_carry_it(db):
    _rates(db, "125", "100")
    job = _job(db)
    _hourly_only(db, job, "0.5")
    assert first_hour_elsewhere(db, job.id) == (False, Decimal("0.5"))
    _hourly_only(db, job, "1.5")
    assert first_hour_elsewhere(db, job.id) == (False, Decimal("2.0")), \
        "hours alone never stand in for the first-hour price"


def test_at_equal_rates_an_hour_elsewhere_carries_it(db):
    _rates(db, "100", "100")
    job = _job(db)
    _hourly_only(db, job, "1.0")
    assert first_hour_elsewhere(db, job.id) == (True, Decimal("1.0"))


def test_a_sibling_of_an_hour_or_more_does_not_keep_the_premium_after_a_void(db):
    # 3 h carrier + 2 h hourly-only sibling at 125/100 is $525. Counting the
    # sibling's 2 h as the carrier made the re-bill hourly-only: $500.
    _rates(db, "125", "100")
    job = _job(db)
    _closeout(db, job, hours=3.0)
    carrier = _rebill(db, job)
    _hourly_only(db, job, "2.0")
    assert _job_total(db, job) == Decimal("525.00")
    _void(db, carrier)
    reissued = _rebill(db, job)
    assert _lines(db, reissued)[0].description == FIRST_HOUR_DESCRIPTION
    assert _job_total(db, job) == Decimal("525.00")


@pytest.mark.parametrize(("description", "quantity", "unit_price"), [
    # Pre-#926: one line, the premium folded into the price (migration 110
    # backfilled these to labor_attested so they would count as carriers).
    ("Service labor — 3.00 man-hours (1 tech × 3.00 h on site; first hour $125, then $100/hr)",
     "1", "325"),
    # A first-hour line whose text staff edited.
    ("First hour, garage call", "1", "125"),
])
def test_a_carrier_not_named_first_hour_still_carries_it(db, description, quantity, unit_price):
    # 3 h carrier + 2 h re-bill at 125/100 is $525; charging the premium a
    # second time made it $550.
    _rates(db, "125", "100")
    job = _job(db)
    _attested(db, job, description, quantity, unit_price)
    assert first_hour_elsewhere(db, job.id)[0]
    _closeout(db, job, hours=2.0)
    rebill = _rebill(db, job)
    assert [ln.description for ln in _lines(db, rebill)][0] != FIRST_HOUR_DESCRIPTION
    assert _job_total(db, job) == Decimal(unit_price) * Decimal(quantity) + Decimal("200.00")


def test_a_carrier_from_before_a_rate_change_still_carries_it(db):
    # Built at $100/$100 the carrier is one line at the hourly rate. Once the
    # office splits the rates, its price no longer tells it from an
    # hourly-only sibling; it must not lose the first hour to that.
    _rates(db, "100", "100")
    job = _job(db)
    co = _closeout(db, job, hours=3.0)
    carrier = _rebill(db, job)
    [line] = _lines(db, carrier)
    assert Decimal(str(line.unit_price)) == Decimal("100")
    settings = db.execute(select(PricingSettings)).scalars().first()
    settings.service_call_first_hour_price = Decimal("125")
    db.commit()
    assert first_hour_elsewhere(db, job.id)[0]
    co.hours_worked = 2.0
    db.commit()
    rebill = _rebill(db, job)
    assert [ln.description for ln in _lines(db, rebill)][0] != FIRST_HOUR_DESCRIPTION
    assert _job_total(db, job) == Decimal("500.00")


def test_an_unrelated_settings_save_does_not_make_a_short_sibling_a_carrier(db):
    # Any Save on the pricing panel moves updated_at, even one that changes no
    # rate. The 0.5 h sibling is now "older than the rates" but under an hour,
    # so it cannot have been a carrier: void-and-reissue stays at $375.
    job, _co, carrier, _sibling = _carrier_and_sibling(db)
    settings = db.execute(select(PricingSettings)).scalars().first()
    settings.updated_at = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=1)
    db.commit()
    _void(db, carrier)
    reissued = _rebill(db, job)
    assert _lines(db, reissued)[0].description == FIRST_HOUR_DESCRIPTION
    assert _job_total(db, job) == Decimal("375.00")


def test_a_new_hourly_rate_does_not_make_an_hourly_only_sibling_a_carrier(db):
    # The 0.5 h sibling bills $100; after 135/110 that is not the hourly rate,
    # but it was priced on the old rates. 0.5 @ 100 + 1 @ 135 + 2 @ 110.
    job, _co, carrier, _sibling = _carrier_and_sibling(db)
    _change_rates(db, "135", "110")
    _void(db, carrier)
    reissued = _rebill(db, job)
    assert _lines(db, reissued)[0].description == FIRST_HOUR_DESCRIPTION
    assert _job_total(db, job) == Decimal("405.00")


def test_a_first_hour_split_by_the_floor_still_carries_after_the_rates_split(db):
    # At 100/100 a 0.5 h sibling and a 0.5 h floor-shrunk build share the one
    # first hour. Neither is an hour on its own; splitting the rates must not
    # make the next build charge the first-hour price a second time.
    _rates(db, "100", "100")
    job = _job(db)
    _hourly_only(db, job, "0.5")
    _closeout(db, job, hours=0.25)
    _rebill(db, job)
    assert first_hour_elsewhere(db, job.id) == (True, Decimal("1.00"))
    _change_rates(db, "125", "100")
    assert first_hour_elsewhere(db, job.id)[0]


def test_the_floor_counts_hours_on_the_sibling(db):
    # Equal rates: 0.5 h already billed on a non-carrier, 0.25 h attested now
    # (rounds to 0.5). The job bills 1 h, not 0.5 + a fresh 1 h floor.
    _rates(db, "100", "100")
    job = _job(db)
    _hourly_only(db, job, "0.5")
    _closeout(db, job, hours=0.25)
    rebill = _rebill(db, job)
    [line] = _lines(db, rebill)
    assert Decimal(str(line.quantity)) == Decimal("0.5")
    # Not the tenant template: it would say "first hour $100" on a half hour.
    assert line.description == "Service labor — 0.50 man-hours at $100.00/hr"
    assert _job_total(db, job) == Decimal("100.00")


def test_split_rates_floor_never_bills_past_one_hour(db):
    # 125/100: the sibling holds 0.5 h; the re-bill's first-hour line covers
    # only the rest of the floor. Under-bills the premium on the sibling's
    # half hour (accepted), never bills 1.5 h for 1 h.
    _rates(db, "125", "100")
    job = _job(db)
    _hourly_only(db, job, "0.5")
    _closeout(db, job, hours=0.25)
    rebill = _rebill(db, job)
    [line] = _lines(db, rebill)
    assert line.description == FIRST_HOUR_DESCRIPTION
    assert Decimal(str(line.quantity)) == Decimal("0.5")
    assert _job_total(db, job) == Decimal("112.50")

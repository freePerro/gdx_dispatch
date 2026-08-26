"""Pay-period arithmetic — the one definition three surfaces count on.

Doug 2026-08-26: "it is bi weekly and this friday is when we get paid",
period Mon 2026-08-10 – Sun 2026-08-23. That real configuration is pinned
below as `test_dougs_configuration_lands_on_his_payday`, because every
other test here is only calendar theory until one of them matches the shop.

What these tests are built to catch, stated as the failure not the feature:

* A fortnight computed one week out of phase. Biweekly is the only cadence
  the calendar cannot derive, and being off by one period emails two weeks
  nobody worked. Hence the anchor, hence the raise when it is missing.
* Floor-vs-truncate on days BEFORE the anchor. `int()` truncation toward
  zero pulls those days forward onto the anchor's own period.
* Fixed-span stepping across semi-monthly periods, which are 13/14/15/16
  days long and drift the moment you subtract a constant.
* Bucketing a clock stamp by its UTC date. A 7pm-Central shift is stored
  with tomorrow's UTC day; two of this tenant's thirty-nine rows already
  did that when the Timesheets page was written.
* A scheduled send firing on a day no period actually closed.
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from gdx_dispatch.core.pay_periods import (
    CADENCES,
    DEFAULT_CADENCE,
    PayPeriod,
    PayPeriodUnconfigured,
    invalid_recipient_emails,
    is_close_day,
    next_period,
    normalize_cadence,
    parse_recipient_emails,
    pay_date,
    period_containing,
    previous_period,
    settings_config,
    shop_day_of,
    shop_today,
)

ANCHOR = date(2026, 8, 10)  # a Monday


class _Settings:
    """Stands in for an AppSettings row."""

    def __init__(self, **kw):
        self.timezone = kw.get("timezone", "America/Chicago")
        self.pay_period_cadence = kw.get("cadence", "biweekly")
        self.pay_period_anchor_start = kw.get("anchor", ANCHOR)
        self.pay_period_pay_lag_days = kw.get("lag", 5)


# ---------------------------------------------------------------------------
# The shop's real configuration
# ---------------------------------------------------------------------------

def test_dougs_configuration_lands_on_his_payday():
    """Period Mon 8/10 – Sun 8/23, paid Friday 8/28. All three, or it's wrong."""
    current = period_containing(date(2026, 8, 26), cadence="biweekly", anchor_start=ANCHOR)
    last = previous_period(current, cadence="biweekly", anchor_start=ANCHOR)

    assert (last.start, last.end) == (date(2026, 8, 10), date(2026, 8, 23))
    assert last.days == 14
    payday = pay_date(last, 5)
    assert payday == date(2026, 8, 28)
    assert payday.weekday() == 4, "payday must be a Friday"


def test_the_period_being_worked_now_is_not_the_one_being_paid():
    """The live period and the one on Friday's check are different fortnights.

    Sending the *current* period on payday is the obvious wrong answer, and
    it looks right on any single day you happen to test.
    """
    current = period_containing(date(2026, 8, 26), cadence="biweekly", anchor_start=ANCHOR)
    last = previous_period(current, cadence="biweekly", anchor_start=ANCHOR)
    assert current.start == date(2026, 8, 24)
    assert current != last


# ---------------------------------------------------------------------------
# Biweekly phase
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "day,expected_start",
    [
        (date(2026, 8, 10), date(2026, 8, 10)),   # the anchor itself
        (date(2026, 8, 23), date(2026, 8, 10)),   # last day of that period
        (date(2026, 8, 24), date(2026, 8, 24)),   # first day of the next
        (date(2026, 9, 6), date(2026, 8, 24)),
        (date(2026, 9, 7), date(2026, 9, 7)),
    ],
)
def test_biweekly_boundaries_are_inclusive(day, expected_start):
    p = period_containing(day, cadence="biweekly", anchor_start=ANCHOR)
    assert p.start == expected_start
    assert p.contains(day)


@pytest.mark.parametrize(
    "day,expected_start",
    [
        (date(2026, 8, 9), date(2026, 7, 27)),    # one day before the anchor
        (date(2026, 7, 27), date(2026, 7, 27)),
        (date(2026, 7, 26), date(2026, 7, 13)),
    ],
)
def test_days_before_the_anchor_fall_in_earlier_fortnights(day, expected_start):
    """Truncation toward zero would drag all of these onto the anchor period."""
    p = period_containing(day, cadence="biweekly", anchor_start=ANCHOR)
    assert p.start == expected_start
    assert p.contains(day)
    assert p.end < ANCHOR, "a pre-anchor day must not land in the anchor's own period"


def test_biweekly_without_an_anchor_refuses_rather_than_guessing():
    with pytest.raises(PayPeriodUnconfigured):
        period_containing(date(2026, 8, 26), cadence="biweekly", anchor_start=None)


def test_every_biweekly_period_is_exactly_fourteen_days():
    p = period_containing(date(2026, 1, 1), cadence="biweekly", anchor_start=ANCHOR)
    for _ in range(30):
        assert p.days == 14
        nxt = next_period(p, cadence="biweekly", anchor_start=ANCHOR)
        assert (nxt.start - p.end).days == 1, "periods must abut with no gap or overlap"
        p = nxt


# ---------------------------------------------------------------------------
# Weekly cadences
# ---------------------------------------------------------------------------

def test_weekly_mon_runs_monday_to_sunday():
    p = period_containing(date(2026, 8, 26), cadence="weekly_mon")
    assert (p.start, p.end) == (date(2026, 8, 24), date(2026, 8, 30))
    assert p.start.weekday() == 0 and p.end.weekday() == 6


def test_weekly_sun_runs_sunday_to_saturday():
    p = period_containing(date(2026, 8, 26), cadence="weekly_sun")
    assert (p.start, p.end) == (date(2026, 8, 23), date(2026, 8, 29))
    assert p.start.weekday() == 6 and p.end.weekday() == 5


def test_the_two_weekly_cadences_disagree_by_a_day():
    """If these ever returned the same range, the setting would be cosmetic."""
    mon = period_containing(date(2026, 8, 26), cadence="weekly_mon")
    sun = period_containing(date(2026, 8, 26), cadence="weekly_sun")
    assert mon.start != sun.start


# ---------------------------------------------------------------------------
# Semi-monthly
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "day,expected",
    [
        (date(2026, 8, 1), (date(2026, 8, 1), date(2026, 8, 15))),
        (date(2026, 8, 15), (date(2026, 8, 1), date(2026, 8, 15))),
        (date(2026, 8, 16), (date(2026, 8, 16), date(2026, 8, 31))),
        (date(2026, 8, 31), (date(2026, 8, 16), date(2026, 8, 31))),
        (date(2026, 2, 20), (date(2026, 2, 16), date(2026, 2, 28))),
        (date(2028, 2, 20), (date(2028, 2, 16), date(2028, 2, 29))),  # leap
        (date(2026, 4, 20), (date(2026, 4, 16), date(2026, 4, 30))),
    ],
)
def test_semimonthly_halves(day, expected):
    p = period_containing(day, cadence="semimonthly")
    assert (p.start, p.end) == expected


def test_semimonthly_steps_across_a_month_without_drifting():
    """Fixed-span arithmetic breaks here; re-deriving from the day does not."""
    march_first_half = period_containing(date(2026, 3, 5), cadence="semimonthly")
    prev = previous_period(march_first_half, cadence="semimonthly")
    assert (prev.start, prev.end) == (date(2026, 2, 16), date(2026, 2, 28))

    p = period_containing(date(2026, 1, 3), cadence="semimonthly")
    for _ in range(40):
        nxt = next_period(p, cadence="semimonthly")
        assert (nxt.start - p.end).days == 1, "no gap or overlap across months"
        p = nxt


# ---------------------------------------------------------------------------
# Cadence handling
# ---------------------------------------------------------------------------

def test_unknown_cadence_degrades_to_the_default():
    assert normalize_cadence("nonsense") == DEFAULT_CADENCE
    assert normalize_cadence(None) == DEFAULT_CADENCE
    assert normalize_cadence("  BIWEEKLY  ") == "biweekly"


def test_every_advertised_cadence_actually_produces_a_period():
    """A value the settings API accepts must not blow up the arithmetic."""
    for cadence in CADENCES:
        p = period_containing(date(2026, 8, 26), cadence=cadence, anchor_start=ANCHOR)
        assert isinstance(p, PayPeriod)
        assert p.start <= date(2026, 8, 26) <= p.end


def test_pay_date_of_zero_lag_is_the_period_end():
    p = period_containing(date(2026, 8, 26), cadence="weekly_mon")
    assert pay_date(p, 0) == p.end
    assert pay_date(p, None) == p.end


# ---------------------------------------------------------------------------
# Shop-local days
# ---------------------------------------------------------------------------

def test_an_evening_shift_is_not_filed_under_tomorrow():
    """02:30 UTC is 21:30 the previous evening in Chicago.

    Slicing the first ten characters of the stored text — which is what a
    naive query does — answers 2026-08-26 and moves the shift a day, and on
    a period boundary a whole fortnight.
    """
    stamp = "2026-08-26T02:30:00+00:00"
    assert stamp[:10] == "2026-08-26"
    assert shop_day_of(stamp, "America/Chicago") == date(2026, 8, 25)


def test_shop_day_handles_what_the_clock_actually_stores():
    assert shop_day_of("2026-08-25T13:06:01.988050+00:00", "America/Chicago") == date(2026, 8, 25)
    assert shop_day_of("2026-08-25 13:06:01+00:00", "America/Chicago") == date(2026, 8, 25)
    assert shop_day_of(datetime(2026, 8, 25, 13, 6), "America/Chicago") == date(2026, 8, 25)
    assert shop_day_of(date(2026, 8, 25), "America/Chicago") == date(2026, 8, 25)
    assert shop_day_of(None, "America/Chicago") is None
    assert shop_day_of("", "America/Chicago") is None


def test_an_unusable_timezone_falls_back_instead_of_raising():
    assert shop_day_of("2026-08-25T13:06:01+00:00", "Not/AZone") == date(2026, 8, 25)
    assert isinstance(shop_today("Not/AZone"), date)


# ---------------------------------------------------------------------------
# Close day — what the scheduled send asks
# ---------------------------------------------------------------------------

def test_close_day_fires_the_morning_after_a_period_ends():
    settings = _Settings()
    # 8/23 is the last day of Doug's period; the send runs on 8/24.
    period = is_close_day(date(2026, 8, 24), settings)
    assert period is not None
    assert (period.start, period.end) == (date(2026, 8, 10), date(2026, 8, 23))


@pytest.mark.parametrize("day", [date(2026, 8, 23), date(2026, 8, 25), date(2026, 8, 26)])
def test_close_day_stays_silent_on_every_other_day(day):
    """The counterfactual: if this returned truthy mid-period the send would
    mail an unfinished fortnight every morning."""
    assert is_close_day(day, _Settings()) is None


def test_close_day_never_fires_on_the_last_day_of_the_period():
    """Clock-outs for that day have not happened yet."""
    assert is_close_day(date(2026, 8, 23), _Settings()) is None


# ---------------------------------------------------------------------------
# Settings plumbing
# ---------------------------------------------------------------------------

def test_settings_config_reads_a_row():
    cfg = settings_config(_Settings())
    assert cfg == {"cadence": "biweekly", "anchor_start": ANCHOR, "lag_days": 5}


def test_settings_config_survives_a_row_that_predates_the_columns():
    class Bare:
        pass

    cfg = settings_config(Bare())
    assert cfg["cadence"] == DEFAULT_CADENCE
    assert cfg["anchor_start"] is None
    assert cfg["lag_days"] == 0


def test_settings_config_accepts_an_iso_string_anchor():
    cfg = settings_config(_Settings(anchor="2026-08-10"))
    assert cfg["anchor_start"] == ANCHOR


def test_a_datetime_anchor_becomes_a_date():
    """datetime subclasses date; returning it unconverted mixes types in
    arithmetic later and raises somewhere far away from here."""
    cfg = settings_config(_Settings(anchor=datetime(2026, 8, 10, 6, 30)))
    assert cfg["anchor_start"] == ANCHOR
    assert not isinstance(cfg["anchor_start"], datetime)


# ---------------------------------------------------------------------------
# Recipients
# ---------------------------------------------------------------------------

def test_recipients_split_on_what_people_actually_type():
    assert parse_recipient_emails("a@b.com, c@d.com") == ["a@b.com", "c@d.com"]
    assert parse_recipient_emails("a@b.com; c@d.com") == ["a@b.com", "c@d.com"]
    assert parse_recipient_emails("a@b.com\nc@d.com") == ["a@b.com", "c@d.com"]


def test_recipients_deduplicate_case_insensitively():
    assert parse_recipient_emails("a@b.com, A@B.com") == ["a@b.com"]


def test_blank_recipient_list_is_empty_not_a_blank_address():
    assert parse_recipient_emails("") == []
    assert parse_recipient_emails(None) == []
    assert parse_recipient_emails(" , ; ") == []


def test_a_typo_is_reported_and_a_real_address_is_not():
    assert invalid_recipient_emails("bookkeeper@example.com") == []
    assert invalid_recipient_emails("bookkeeper@example, ok@x.com") == ["bookkeeper@example"]
    assert invalid_recipient_emails("no-at-sign") == ["no-at-sign"]

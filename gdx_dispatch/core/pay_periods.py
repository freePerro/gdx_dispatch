"""Pay-period math — ONE definition of "which two weeks is this".

Three surfaces need to agree on the answer: the office Timesheets screen
(its This/Last pay period presets), the CSV+PDF export, and the scheduled
send that fires after a period closes. They agree because they all call
here. A second implementation of this arithmetic anywhere else is a bug:
a screen that shows one fortnight while the emailed file covers another is
the exact failure this module exists to prevent.

Cadence is a tenant setting (`AppSettings.pay_period_cadence`), not a
constant, because the shop that runs this is not the only shop:

  weekly_mon    Mon..Sun    — what both timesheet screens assumed before
                              pay periods existed at all
  weekly_sun    Sun..Sat
  biweekly      14 days, counted from `pay_period_anchor_start`
  semimonthly   1st..15th and 16th..end-of-month (uneven by design)

`biweekly` is the only cadence that cannot be derived from the calendar
alone — two shops both paying every other Friday can be a week out of
phase — so it REQUIRES an anchor and raises without one rather than
guessing a fortnight and quietly emailing the wrong hours.

Everything here is pure calendar arithmetic on `datetime.date`. No
timezone conversion happens in this module: callers hand in a date that is
already the day *as the shop sees it*, because a clock entry's pay period
is decided by the shop's calendar, never the server's UTC one. A 10 PM
Central clock-in is stored with tomorrow's UTC date, and bucketing it by
that UTC date files two of this tenant's real shifts into the wrong
fortnight.
"""
from __future__ import annotations

import re
from calendar import monthrange
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

CADENCE_WEEKLY_MON = "weekly_mon"
CADENCE_WEEKLY_SUN = "weekly_sun"
CADENCE_BIWEEKLY = "biweekly"
CADENCE_SEMIMONTHLY = "semimonthly"

#: Every cadence the settings surface may store. Kept as a tuple so the
#: Pydantic pattern, the migration's CHECK-free validation and the Vue
#: dropdown all read from one list.
CADENCES: tuple[str, ...] = (
    CADENCE_WEEKLY_MON,
    CADENCE_WEEKLY_SUN,
    CADENCE_BIWEEKLY,
    CADENCE_SEMIMONTHLY,
)

#: Shown in the UI and in the emailed PDF's header.
CADENCE_LABELS: dict[str, str] = {
    CADENCE_WEEKLY_MON: "Weekly (Monday–Sunday)",
    CADENCE_WEEKLY_SUN: "Weekly (Sunday–Saturday)",
    CADENCE_BIWEEKLY: "Every two weeks",
    CADENCE_SEMIMONTHLY: "Twice a month (1st–15th, 16th–end)",
}

#: The cadence a tenant that has never opened the setting gets. Deliberately
#: NOT this shop's biweekly: the column default ships to every install, and
#: weekly_mon is what the timesheet screens already did, so the default
#: changes nobody's existing view.
DEFAULT_CADENCE = CADENCE_WEEKLY_MON

#: Only cadences in here need `pay_period_anchor_start`.
ANCHORED_CADENCES = frozenset({CADENCE_BIWEEKLY})


class PayPeriodUnconfigured(ValueError):
    """The tenant's cadence cannot produce a period from what is stored.

    Raised, never defaulted. A biweekly shop with no anchor has a 50/50
    chance of being handed the wrong fortnight, and the cost of guessing is
    a payroll file covering two weeks nobody worked.
    """


@dataclass(frozen=True)
class PayPeriod:
    """A closed date range, both ends inclusive, in shop-local calendar days."""

    start: date
    end: date

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def contains(self, day: date) -> bool:
        return self.start <= day <= self.end

    def label(self) -> str:
        """Human range for a PDF header or a button tooltip."""
        return f"{self.start.isoformat()} – {self.end.isoformat()}"

    def as_dict(self) -> dict[str, str]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "label": self.label(),
        }


def _coerce_date(value: Any) -> date | None:
    """Accept a date, a datetime, or an ISO 'YYYY-MM-DD[...]' string."""
    if value is None or value == "":
        return None
    # datetime subclasses date, so it must be tested FIRST — otherwise a
    # datetime returns itself and later date arithmetic mixes the two types.
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def normalize_cadence(value: Any) -> str:
    """Unknown/blank cadence degrades to the default rather than exploding.

    A cadence string only ever reaches here from our own settings column,
    and an install whose row predates this feature holds NULL. Falling back
    is safe *because the fallback is calendar-derivable* — unlike a missing
    biweekly anchor, which is not, and does raise.
    """
    text = str(value or "").strip().lower()
    return text if text in CADENCES else DEFAULT_CADENCE


def period_containing(
    day: date,
    *,
    cadence: str = DEFAULT_CADENCE,
    anchor_start: Any = None,
) -> PayPeriod:
    """The pay period that `day` falls inside.

    `day` must already be a shop-local calendar day.
    """
    cadence = normalize_cadence(cadence)
    anchor = _coerce_date(anchor_start)

    if cadence == CADENCE_WEEKLY_MON:
        start = day - timedelta(days=day.weekday())
        return PayPeriod(start, start + timedelta(days=6))

    if cadence == CADENCE_WEEKLY_SUN:
        # date.weekday() is Mon=0..Sun=6; shift so Sunday is 0.
        start = day - timedelta(days=(day.weekday() + 1) % 7)
        return PayPeriod(start, start + timedelta(days=6))

    if cadence == CADENCE_BIWEEKLY:
        if anchor is None:
            raise PayPeriodUnconfigured(
                "Biweekly pay periods need a start date to count from — "
                "set one in Settings → Pay periods."
            )
        # Floor division, and Python floors toward negative infinity, so a
        # day BEFORE the anchor lands in the correct earlier fortnight
        # instead of being pulled forward onto the anchor itself.
        index = (day - anchor).days // 14
        start = anchor + timedelta(days=14 * index)
        return PayPeriod(start, start + timedelta(days=13))

    if cadence == CADENCE_SEMIMONTHLY:
        if day.day <= 15:
            return PayPeriod(day.replace(day=1), day.replace(day=15))
        last = monthrange(day.year, day.month)[1]
        return PayPeriod(day.replace(day=16), day.replace(day=last))

    raise PayPeriodUnconfigured(f"unknown pay period cadence {cadence!r}")


def previous_period(period: PayPeriod, *, cadence: str, anchor_start: Any = None) -> PayPeriod:
    """The period immediately before `period`.

    Derived by stepping one day back off the start and re-deriving, rather
    than subtracting a fixed span. Semi-monthly periods are 13, 14, 15 or
    16 days long, so fixed-span arithmetic drifts; this cannot.
    """
    return period_containing(
        period.start - timedelta(days=1), cadence=cadence, anchor_start=anchor_start
    )


def next_period(period: PayPeriod, *, cadence: str, anchor_start: Any = None) -> PayPeriod:
    """The period immediately after `period` (same reasoning as above)."""
    return period_containing(
        period.end + timedelta(days=1), cadence=cadence, anchor_start=anchor_start
    )


def pay_date(period: PayPeriod, lag_days: int) -> date:
    """The day the money lands, `lag_days` after the period closes.

    Doug's shop: a period ending Sunday is paid the Friday 5 days later.
    Zero lag means the period ends on payday.
    """
    return period.end + timedelta(days=max(0, int(lag_days or 0)))


def settings_config(settings: Any) -> dict[str, Any]:
    """Pull the cadence triple off an AppSettings row (or anything shaped
    like one) with the same defaults every caller should be using.

    getattr with a default throughout: an install whose app_settings row
    predates migration 081 has the attributes but NULL values, and a
    plugin or test may pass a stub.
    """
    return {
        "cadence": normalize_cadence(getattr(settings, "pay_period_cadence", None)),
        "anchor_start": _coerce_date(getattr(settings, "pay_period_anchor_start", None)),
        "lag_days": int(getattr(settings, "pay_period_pay_lag_days", 0) or 0),
    }


def current_period(day: date, settings: Any) -> PayPeriod:
    """Convenience: the period containing `day` for this tenant's settings."""
    cfg = settings_config(settings)
    return period_containing(day, cadence=cfg["cadence"], anchor_start=cfg["anchor_start"])


def is_close_day(day: date, settings: Any) -> PayPeriod | None:
    """If a period closed YESTERDAY relative to `day`, return that period.

    This is what the scheduled send asks once a day. It deliberately looks
    backward at a *finished* period rather than forward at one about to
    end: a period that ends tonight still has clock-outs coming.
    """
    cfg = settings_config(settings)
    yesterday = day - timedelta(days=1)
    period = period_containing(
        yesterday, cadence=cfg["cadence"], anchor_start=cfg["anchor_start"]
    )
    return period if period.end == yesterday else None


# ---------------------------------------------------------------------------
# Recipient list
# ---------------------------------------------------------------------------
# The finished timesheet goes to a person — a bookkeeper — not to a payroll
# provider's API, so there is no account to model and no provider-specific
# import spec to satisfy. A stored string of addresses is the whole contract.
# It lives here rather than in the send code because Settings validates the
# same string on the way in, and two parsers would eventually disagree about
# whether "a@b.com; c@d.com" is one address or two.

_EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")

#: Comma, semicolon or newline — whatever the operator actually types.
_RECIPIENT_SPLIT_RE = re.compile(r"[,;\n]+")


def parse_recipient_emails(raw: Any) -> list[str]:
    """Stored recipient string → de-duplicated address list, order kept."""
    seen: set[str] = set()
    out: list[str] = []
    for chunk in _RECIPIENT_SPLIT_RE.split(str(raw or "")):
        addr = chunk.strip()
        if not addr:
            continue
        key = addr.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(addr)
    return out


def invalid_recipient_emails(raw: Any) -> list[str]:
    """The addresses in `raw` that are not shaped like an email address.

    Shape only — deliverability is Outlook's business. The point is to
    reject a typo at the Settings screen, where someone can fix it, rather
    than at 7am on a Monday inside a Celery task nobody is watching.
    """
    return [a for a in parse_recipient_emails(raw) if not _EMAIL_RE.match(a)]


# ---------------------------------------------------------------------------
# Shop-local calendar days
# ---------------------------------------------------------------------------
# `timeclock_entries_router.clock_in_at` is TEXT holding an ISO-8601 instant
# in UTC ("2026-08-25T13:06:01.988050+00:00"). Slicing the first 10
# characters — which is what a naive query does — buckets a shift by its UTC
# calendar day. For this shop (America/Chicago) an evening shift starting
# after 7pm CDT is stamped with TOMORROW's UTC date, so date-slicing files it
# into the wrong day and, on a boundary, into the wrong pay period entirely.
# Two of thirty-nine rows already did exactly that when the office Timesheets
# page was built, which is why that page carries a day of slack on each end
# and re-filters in shop time. Same rule here, one implementation.

def resolve_zone(tz_name: Any) -> ZoneInfo:
    """IANA name → ZoneInfo, falling back to UTC on anything unusable."""
    try:
        return ZoneInfo(str(tz_name)) if tz_name else ZoneInfo("UTC")
    except Exception:  # noqa: BLE001 — ZoneInfoNotFoundError, ValueError, OSError
        return ZoneInfo("UTC")


def shop_today(tz_name: Any, *, now: datetime | None = None) -> date:
    """Today's date as the shop sees it."""
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(resolve_zone(tz_name)).date()


def shop_day_of(value: Any, tz_name: Any) -> date | None:
    """The shop-local calendar day an instant belongs to.

    Accepts the TEXT the clock stores, a datetime, or None. A naive value is
    read as UTC, matching how every writer in this app stores it.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, date):
        return value
    else:
        raw = str(value).strip().replace(" ", "T")
        try:
            moment = datetime.fromisoformat(raw)
        except ValueError:
            # Last resort: a bare date, or something we cannot parse at all.
            return _coerce_date(raw)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(resolve_zone(tz_name)).date()

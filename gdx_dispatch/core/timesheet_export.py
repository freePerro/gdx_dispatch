"""Turning a pay period's hours into the two files that get emailed.

A readable PDF for the person who checks it, and a CSV for whoever keys it
in. Both are built from the SAME `PeriodTimesheet`, so they cannot disagree
about what anybody worked — which is the only reason it is safe to send
two files at all.

Neither file computes money. There is no rate column, no gross, no overtime
split: the hours are the deliverable and the bookkeeper applies the rates.
An overtime split in particular would be this app inventing a fact the clock
never recorded.

Paid time off (vacation, holiday — core/time_off.py) is listed with its own
type and its own hours column, never folded into worked hours, and the PDF
and the email state the shop's setting on whether it counts toward overtime.
The CSV carries no such column on purpose: it is an hours file, and the
sentence belongs to the reader, not the spreadsheet.

Each person's TOTAL row also carries their vacation balance for the calendar
year — allowance, used and remaining hours through the period's last day, and
the hours already booked after it — when the caller built the sheet
`with_vacation`. That is a statement of hours
booked against a number the office set, not a payout: nothing here pays out
unused vacation or carries it over.

Both files state their own unresolved rows rather than dropping them. A
timesheet that silently omits an open shift reads as complete and quietly
under-pays somebody; one that names it costs a phone call.
"""
from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from typing import Any

from gdx_dispatch.core.pay_periods import PayPeriod, resolve_zone
from gdx_dispatch.core.timesheet_hours import FLAG_LABELS, PeriodTimesheet, Shift, Timecard

CSV_HEADER = [
    "employee",
    "employee_id",
    "period_start",
    "period_end",
    "date",
    "type",
    "clock_in",
    "clock_out",
    "break_minutes",
    "worked_hours",
    "time_off_hours",
    "needs_review",
    "note",
    # Filled on the per-person TOTAL row only: the calendar year of the
    # period's last day, as of that day (core/time_off.vacation_balances).
    # Used and remaining count vacation through the period's last day, so a
    # past period's file says the same thing when downloaded again; days
    # already booked after it are their own column.
    "vacation_year",
    "vacation_allowance_hours",
    "vacation_used_hours",
    "vacation_remaining_hours",
    "vacation_booked_after_period_hours",
]

#: What a balance cell says when there is no number to print. A blank would
#: read as zero or as "no allowance" — two different facts.
NO_ALLOWANCE = "not set"
UNAVAILABLE = "unavailable"


def _clock(value: str | None, tz_name: str) -> str:
    """A stored UTC instant rendered as the wall clock the shop saw."""
    if not value:
        return ""
    try:
        moment = datetime.fromisoformat(str(value).strip().replace(" ", "T"))
    except ValueError:
        return str(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(resolve_zone(tz_name)).strftime("%-I:%M %p")


def _row_note(shift: Shift) -> str:
    if shift.flag:
        return FLAG_LABELS.get(shift.flag, shift.flag)
    return shift.notes or ""


def _vacation_cells(sheet: PeriodTimesheet, card: Timecard) -> list[Any]:
    """The five balance cells of a TOTAL row. Empty when the caller did not
    ask for balances; "unavailable" when it asked and the read failed."""
    if sheet.vacation_year is None:
        return ["", "", "", "", ""]
    if sheet.vacation_unreadable or card.vacation is None:
        return [sheet.vacation_year, *[UNAVAILABLE] * 4]
    bal = card.vacation
    taken, ahead = f"{bal.taken_hours:.2f}", f"{bal.scheduled_hours:.2f}"
    if bal.allowance_hours is None:
        return [sheet.vacation_year, NO_ALLOWANCE, taken, NO_ALLOWANCE, ahead]
    return [
        sheet.vacation_year,
        f"{float(bal.allowance_hours):.2f}",
        taken,
        f"{bal.remaining_at_as_of_hours:.2f}",
        ahead,
    ]


def build_csv(sheet: PeriodTimesheet) -> str:
    """One row per person per shift, plus a total row per person.

    Per-shift rather than per-day: a person who clocks out for a parts run
    and back in has two shifts, and collapsing them hides the gap that a
    bookkeeper may need to ask about. The per-person TOTAL row is what gets
    keyed in, and it is labelled so nobody keys the detail rows by mistake.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_HEADER)

    for card in sheet.timecards:
        for shift in card.shifts:
            if shift.is_time_off:
                # No clock stamps: the span is synthetic (core/time_off.py)
                # and printing "8:00 AM – 4:00 PM" on a vacation day would
                # read as a shift somebody clocked.
                kind, clock_in, clock_out, breaks = shift.entry_type, "", "", 0
                worked, off = "", f"{shift.time_off_hours:.2f}"
            else:
                kind = "shift"
                clock_in = _clock(shift.clock_in, sheet.timezone)
                clock_out = _clock(shift.clock_out, sheet.timezone)
                breaks = shift.break_minutes or 0
                worked, off = f"{shift.worked_hours:.2f}", ""
            writer.writerow(
                [
                    card.name,
                    card.tech_id,
                    sheet.period.start.isoformat(),
                    sheet.period.end.isoformat(),
                    shift.day.isoformat(),
                    kind,
                    clock_in,
                    clock_out,
                    breaks,
                    worked,
                    off,
                    "yes" if shift.flag else "",
                    _row_note(shift),
                    # The balance is a per-person fact: TOTAL row only.
                    "", "", "", "", "",
                ]
            )
        shifts = len(card.shifts) - card.time_off_days
        summary = f"{shifts} shifts"
        if card.time_off_days:
            summary += f", {card.time_off_days} {'day' if card.time_off_days == 1 else 'days'} off"
        writer.writerow(
            [
                card.name,
                card.tech_id,
                sheet.period.start.isoformat(),
                sheet.period.end.isoformat(),
                "TOTAL",
                "",
                "",
                "",
                card.break_minutes or 0,
                f"{card.worked_hours:.2f}",
                f"{card.time_off_hours:.2f}" if card.time_off_days else "",
                "yes" if card.flagged else "",
                summary,
                *_vacation_cells(sheet, card),
            ]
        )
    return buffer.getvalue()


def csv_filename(period: PayPeriod) -> str:
    return f"timesheet_{period.start.isoformat()}_{period.end.isoformat()}.csv"


def pdf_filename(period: PayPeriod) -> str:
    return f"timesheet_{period.start.isoformat()}_{period.end.isoformat()}.pdf"


def pdf_context(
    sheet: PeriodTimesheet,
    *,
    branding: dict[str, Any] | None = None,
    pay_date: str | None = None,
    prepared_at: str | None = None,
) -> dict[str, Any]:
    """Everything `templates/timesheet_pdf.html` renders.

    Split out from `build_pdf` so the shape can be asserted in tests without
    running WeasyPrint, which is slow and whose output is a binary blob you
    cannot make a meaningful assertion about.
    """
    cards = []
    for card in sheet.timecards:
        cards.append(
            {
                "name": card.name,
                "hours": card.worked_hours,
                "time_off_hours": card.time_off_hours,
                "time_off_days": card.time_off_days,
                "paid_hours": card.paid_hours,
                # The person's balance for `vacation_year` as of the period's
                # last day (VacationBalance.as_dict), or None — see
                # `vacation_unreadable` for which None it is.
                "vacation": card.vacation.as_dict() if card.vacation else None,
                "rows": [
                    {
                        "date": shift.day.strftime("%a %b %-d"),
                        "type_label": shift.type_label,
                        "is_time_off": shift.is_time_off,
                        "clock_in": "" if shift.is_time_off else _clock(shift.clock_in, sheet.timezone),
                        "clock_out": "" if shift.is_time_off else _clock(shift.clock_out, sheet.timezone),
                        "break_minutes": shift.break_minutes or 0,
                        # The row's paid hours: worked for a shift, the day's
                        # value for time off. The card-level split is above.
                        "hours": shift.time_off_hours if shift.is_time_off else shift.worked_hours,
                        "notes": shift.notes or "",
                        "flag_label": FLAG_LABELS.get(shift.flag) if shift.flag else "",
                    }
                    for shift in card.shifts
                ],
            }
        )

    flagged = [
        (
            card.name,
            shift.day.strftime("%a %b %-d"),
            FLAG_LABELS.get(shift.flag, shift.flag or ""),
        )
        for card, shift in sheet.flagged
    ]

    return {
        "period": sheet.period.as_dict(),
        "pay_date": pay_date or "",
        "timezone": sheet.timezone,
        "prepared_at": prepared_at or "",
        "total_hours": sheet.worked_hours,
        "total_time_off_hours": sheet.time_off_hours,
        "total_paid_hours": sheet.paid_hours,
        "time_off_statement": sheet.time_off_statement,
        "vacation_year": sheet.vacation_year,
        "vacation_unreadable": sheet.vacation_unreadable,
        "people": sheet.people,
        "cards": cards,
        "flagged": flagged,
        "branding": branding or {},
    }


def build_pdf(
    sheet: PeriodTimesheet,
    *,
    branding: dict[str, Any] | None = None,
    pay_date: str | None = None,
    prepared_at: str | None = None,
) -> bytes:
    """Render the timesheet PDF.

    WeasyPrint is imported inside the function on purpose: it pulls a large
    native stack, and `build_csv` — the path the CSV-only caller takes —
    must not pay for it.
    """
    from weasyprint import HTML

    from gdx_dispatch.core.pdf_generator import _JINJA_ENV, _TEMPLATES_DIR

    html = _JINJA_ENV.get_template("timesheet_pdf.html").render(
        **pdf_context(
            sheet, branding=branding, pay_date=pay_date, prepared_at=prepared_at
        )
    )
    return HTML(string=html, base_url=str(_TEMPLATES_DIR)).write_pdf()

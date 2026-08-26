"""Turning a pay period's hours into the two files that get emailed.

A readable PDF for the person who checks it, and a CSV for whoever keys it
in. Both are built from the SAME `PeriodTimesheet`, so they cannot disagree
about what anybody worked — which is the only reason it is safe to send
two files at all.

Neither file computes money. There is no rate column, no gross, no overtime
split: the hours are the deliverable and the bookkeeper applies the rates.
An overtime split in particular would be this app inventing a fact the clock
never recorded.

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
from gdx_dispatch.core.timesheet_hours import FLAG_LABELS, PeriodTimesheet, Shift

CSV_HEADER = [
    "employee",
    "employee_id",
    "period_start",
    "period_end",
    "date",
    "clock_in",
    "clock_out",
    "break_minutes",
    "worked_hours",
    "needs_review",
    "note",
]


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
            writer.writerow(
                [
                    card.name,
                    card.tech_id,
                    sheet.period.start.isoformat(),
                    sheet.period.end.isoformat(),
                    shift.day.isoformat(),
                    _clock(shift.clock_in, sheet.timezone),
                    _clock(shift.clock_out, sheet.timezone),
                    shift.break_minutes or 0,
                    f"{shift.worked_hours:.2f}",
                    "yes" if shift.flag else "",
                    _row_note(shift),
                ]
            )
        writer.writerow(
            [
                card.name,
                card.tech_id,
                sheet.period.start.isoformat(),
                sheet.period.end.isoformat(),
                "TOTAL",
                "",
                "",
                card.break_minutes or 0,
                f"{card.worked_hours:.2f}",
                "yes" if card.flagged else "",
                f"{len(card.shifts)} shifts",
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
                "rows": [
                    {
                        "date": shift.day.strftime("%a %b %-d"),
                        "clock_in": _clock(shift.clock_in, sheet.timezone),
                        "clock_out": _clock(shift.clock_out, sheet.timezone),
                        "break_minutes": shift.break_minutes or 0,
                        "hours": shift.worked_hours,
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

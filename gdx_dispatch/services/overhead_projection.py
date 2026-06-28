"""Forward overhead projection engine (ADR-016).

Pure, DB-free, month-by-month projection. Given a set of recurring obligations
(each with an amount, cadence, start, optional end/term, and optional scheduled
step-changes), produce the monthly-equivalent overhead for each month in a
horizon. Obligations that end stop contributing, so a loan paying off makes the
projected total step *down* — the headline of the feature.

Design notes (see ADR-016):

* **Cash-basis, outflow-only.** This models "what you pay". It is NOT a
  runway/net-cash figure — there is no inflow side here on purpose.
* **Monthly-equivalent normalization.** Every cadence is smoothed to a
  per-month figure (annual / 12, weekly * 52/12, …). The projection answers
  "what is my monthly overhead", not "what cash clears the bank in March".
* **Variable cost_type is flagged but treated flat in v1** — a later slice can
  swap in a run-rate / seasonal index without touching callers.

The engine is duck-typed: it reads attributes off whatever objects it's given
(ORM ``OverheadObligation`` rows in prod, simple stand-ins in tests).
"""
from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

_CENTS = Decimal("0.01")

# How many times a cadence occurs per year — normalizes any cadence to a
# monthly-equivalent amount.
_OCCURRENCES_PER_YEAR: dict[str, Decimal] = {
    "weekly": Decimal(52),
    "biweekly": Decimal(26),
    "monthly": Decimal(12),
    "quarterly": Decimal(4),
    "semiannual": Decimal(2),
    "annual": Decimal(1),
}

# Whole months between occurrences — used to derive an effective end date from a
# fixed number of payments (``term_total_occurrences``) when no explicit
# ``end_date`` is set. Sub-monthly cadences round up to 1 so a short-term weekly
# obligation still occupies at least its starting month.
_MONTHS_PER_OCCURRENCE: dict[str, int] = {
    "weekly": 1,
    "biweekly": 1,
    "monthly": 1,
    "quarterly": 3,
    "semiannual": 6,
    "annual": 12,
}


def _q(value: Decimal) -> Decimal:
    """Round to cents, half-up (currency rounding)."""
    return value.quantize(_CENTS, rounding=ROUND_HALF_UP)


def _to_decimal(value) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _to_date(value) -> date | None:
    if value is None or isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _month_index(year: int, month: int) -> int:
    """Absolute month number so months are comparable/addable as ints."""
    return year * 12 + (month - 1)


def add_months(year: int, month: int, n: int) -> tuple[int, int]:
    idx = _month_index(year, month) + n
    return idx // 12, idx % 12 + 1


def monthly_equivalent(amount, cadence: str) -> Decimal:
    """Smooth one obligation's amount to a per-month figure for its cadence."""
    per_year = _OCCURRENCES_PER_YEAR.get(cadence)
    if per_year is None:
        raise ValueError(f"unknown cadence: {cadence!r}")
    return _to_decimal(amount) * per_year / Decimal(12)


def effective_end_date(obligation) -> date | None:
    """Resolve when an obligation stops, from end_date or a fixed term.

    ``end_date`` wins if present. Otherwise, if ``term_total_occurrences`` is
    set, the last payment lands at ``start_date + (term - 1) * interval`` — we
    return the month of that last payment (approximate by whole months; good
    enough for a month-granular projection). Open-ended → ``None``.
    """
    end = _to_date(getattr(obligation, "end_date", None))
    if end is not None:
        return end
    term = getattr(obligation, "term_total_occurrences", None)
    if not term:
        return None
    start = _to_date(getattr(obligation, "start_date", None))
    if start is None:
        return None
    cadence = getattr(obligation, "cadence", "monthly")
    step = _MONTHS_PER_OCCURRENCE.get(cadence, 1)
    y, m = add_months(start.year, start.month, (int(term) - 1) * step)
    return date(y, m, 1)


def _scheduled_amount(obligation, year: int, month: int) -> Decimal:
    """The base amount in effect for (year, month), applying step-changes.

    The latest scheduled change whose ``effective_date`` falls on/before the end
    of the target month wins; before any change, the obligation's base ``amount``
    applies.
    """
    base = _to_decimal(getattr(obligation, "amount", 0))
    changes = getattr(obligation, "scheduled_changes", None) or []
    # End of target month, expressed as the first of the *next* month minus a day
    # is unnecessary — comparing effective_date <= last instant of month is the
    # same as effective month_index <= target month_index.
    target_idx = _month_index(year, month)
    best_idx: int | None = None
    for change in changes:
        eff = _to_date(change.get("effective_date") if isinstance(change, dict) else None)
        if eff is None:
            continue
        eff_idx = _month_index(eff.year, eff.month)
        if eff_idx <= target_idx and (best_idx is None or eff_idx > best_idx):
            best_idx = eff_idx
            base = _to_decimal(change.get("amount"))
    return base


def is_active_in_month(obligation, year: int, month: int) -> bool:
    """True if the obligation contributes overhead in (year, month)."""
    start = _to_date(getattr(obligation, "start_date", None))
    if start is None:
        return False
    target_idx = _month_index(year, month)
    if _month_index(start.year, start.month) > target_idx:
        return False  # hasn't started yet
    end = effective_end_date(obligation)
    if end is not None and _month_index(end.year, end.month) < target_idx:
        return False  # already ended (e.g. loan paid off)
    return True


def amount_for_month(obligation, year: int, month: int) -> Decimal:
    """Monthly-equivalent overhead this obligation adds in (year, month)."""
    if not is_active_in_month(obligation, year, month):
        return Decimal("0")
    base = _scheduled_amount(obligation, year, month)
    cadence = getattr(obligation, "cadence", "monthly")
    return monthly_equivalent(base, cadence)


def project(obligations, anchor_year: int, anchor_month: int, horizon_months: int) -> dict:
    """Build a month-by-month overhead projection.

    Returns a dict with:
      * ``months``: per-month [{year, month, label, total, by_category}]
      * ``current_monthly_total``: total for the anchor (first) month
      * ``horizon_total``: total for the final month in the horizon
      * ``categories``: sorted list of categories present
      * ``step_downs``: months where the total falls vs. the prior month, with
        which obligations ended — i.e. the "a loan paid off here" markers.
    """
    obligations = list(obligations)
    months: list[dict] = []
    prev_active_ids: set | None = None
    prev_total: Decimal | None = None
    step_downs: list[dict] = []
    categories: set[str] = set()

    for offset in range(max(horizon_months, 1)):
        y, m = add_months(anchor_year, anchor_month, offset)
        by_category: dict[str, Decimal] = {}
        total = Decimal("0")
        active_ids: set = set()
        for ob in obligations:
            amt = amount_for_month(ob, y, m)
            if amt == 0:
                continue
            active_ids.add(getattr(ob, "id", id(ob)))
            cat = getattr(ob, "category", "other") or "other"
            categories.add(cat)
            by_category[cat] = by_category.get(cat, Decimal("0")) + amt
            total += amt

        months.append({
            "year": y,
            "month": m,
            "label": f"{y}-{m:02d}",
            "total": _q(total),
            "by_category": {k: _q(v) for k, v in sorted(by_category.items())},
        })

        if prev_total is not None and total < prev_total and prev_active_ids is not None:
            ended = prev_active_ids - active_ids
            if ended:
                ended_labels = [
                    getattr(ob, "label", "?")
                    for ob in obligations
                    if getattr(ob, "id", id(ob)) in ended
                ]
                step_downs.append({
                    "year": y,
                    "month": m,
                    "label": f"{y}-{m:02d}",
                    "drop": _q(prev_total - total),
                    "ended": sorted(ended_labels),
                })
        prev_active_ids = active_ids
        prev_total = total

    return {
        "anchor": {"year": anchor_year, "month": anchor_month},
        "horizon_months": max(horizon_months, 1),
        "months": months,
        "current_monthly_total": months[0]["total"] if months else _q(Decimal("0")),
        "horizon_total": months[-1]["total"] if months else _q(Decimal("0")),
        "categories": sorted(categories),
        "step_downs": step_downs,
    }

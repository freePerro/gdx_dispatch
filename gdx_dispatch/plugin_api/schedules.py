"""Plugin schedules — cron matching + the run a schedule callable receives.

Stdlib only, like the rest of plugin_api: the manifest validates a declared cron
with ``parse_cron`` at declaration time (inside the plugin-host), and the core
driver (``core/plugin_schedules.py``) evaluates it with ``cron_matches`` once a
minute. One parser for both, so a cron the manifest accepts is a cron the
driver can run.

The dialect is standard five-field cron (minute hour day-of-month month
day-of-week), evaluated in UTC like every other beat entry in this app:
``*``, ``N``, ``A-B``, ``*/S``, ``A-B/S``, ``N/S`` and comma lists of those;
month names (jan..dec) and weekday names (sun..sat); weekday 0 and 7 are both
Sunday. When BOTH day-of-month and day-of-week are restricted a day matches
if EITHER does — the Vixie-cron rule every crontab(5) documents. No ``@hourly``
aliases, no seconds field, no ``L``/``W``/``#``: a cron outside the dialect is
refused by ``parse_cron`` (and stripped by the manifest), never half-understood.

Prior art considered (2026-10-10): croniter (pallets-eco, maintained) — not
taken because plugin_api must stay stdlib-only and it is not in the image.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}
_DAYS = {d: i for i, d in enumerate(("sun", "mon", "tue", "wed", "thu", "fri", "sat"))}

# (low, high, names) per field, in cron order.
_FIELDS = (
    (0, 59, None),      # minute
    (0, 23, None),      # hour
    (1, 31, None),      # day of month
    (1, 12, _MONTHS),   # month
    (0, 7, _DAYS),      # day of week (7 == Sunday)
)


@dataclass(frozen=True)
class CronSpec:
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]  # 0..6, Sunday = 0
    dom_restricted: bool
    dow_restricted: bool

    def matches(self, when: datetime) -> bool:
        if when.minute not in self.minutes or when.hour not in self.hours:
            return False
        if when.month not in self.months:
            return False
        dom = when.day in self.days
        dow = (when.isoweekday() % 7) in self.weekdays
        if self.dom_restricted and self.dow_restricted:
            return dom or dow
        return dom and dow


def _value(tok: str, low: int, high: int, names) -> int:
    t = tok.strip().lower()
    if names and t in names:
        return names[t]
    if not t.isdigit():
        raise ValueError(f"not a number: {tok!r}")
    n = int(t)
    if not low <= n <= high:
        raise ValueError(f"{n} outside {low}-{high}")
    return n


def _field(expr: str, low: int, high: int, names) -> frozenset[int]:
    out: set[int] = set()
    for part in expr.split(","):
        if not part:
            raise ValueError("empty list element")
        rng, _, step_s = part.partition("/")
        step = 1
        if step_s:
            if not step_s.isdigit() or int(step_s) < 1:
                raise ValueError(f"bad step: {part!r}")
            step = int(step_s)
        if rng == "*":
            a, b = low, high
        elif "-" in rng:
            a_s, _, b_s = rng.partition("-")
            a, b = _value(a_s, low, high, names), _value(b_s, low, high, names)
            if a > b:
                raise ValueError(f"descending range: {part!r}")
        else:
            a = _value(rng, low, high, names)
            # "N/S" means N through the field's end, every S (crontab(5)).
            b = high if step_s else a
        out.update(range(a, b + 1, step))
    return frozenset(out)


def parse_cron(expr: str) -> CronSpec:
    """Parse a five-field cron string. Raises ValueError when it is outside the
    dialect described in the module docstring."""
    if not isinstance(expr, str):
        raise ValueError(f"cron must be a string: {expr!r}")
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError(f"cron needs 5 fields, got {len(parts)}: {expr!r}")
    sets = [_field(p, lo, hi, nm) for p, (lo, hi, nm) in zip(parts, _FIELDS, strict=True)]
    weekdays = frozenset(d % 7 for d in sets[4])
    return CronSpec(
        minutes=sets[0], hours=sets[1], days=sets[2], months=sets[3],
        weekdays=weekdays,
        # Vixie cron: a field that starts with "*" (even "*/2") counts as
        # unrestricted for the either-day rule.
        dom_restricted=not parts[2].startswith("*"),
        dow_restricted=not parts[4].startswith("*"),
    )


def cron_matches(expr: str, when: datetime) -> bool:
    """Does ``when`` (a UTC minute) fall on ``expr``? False for an unparseable
    cron — the manifest already strips those, so this is belt-and-braces."""
    try:
        return parse_cron(expr).matches(when)
    except ValueError:
        return False


@dataclass(frozen=True)
class PluginScheduleRun:
    """What a schedule callable receives as its first positional argument, when
    that parameter is required or named ``run`` (a defaulted ``limit=50`` gets
    nothing).

    At-least-once: a run can be delivered twice (a retried dispatch after a
    plugin-host restart). ``run_id`` is deterministic — ``<key>:<name>:<minute>``
    — so a callable that writes should dedupe on it."""

    plugin_key: str
    name: str
    scheduled_for: str  # ISO-8601 UTC minute the cron matched
    run_id: str

    @classmethod
    def from_wire(cls, body: dict) -> PluginScheduleRun:
        return cls(
            plugin_key=str(body.get("key") or ""),
            name=str(body.get("name") or ""),
            scheduled_for=str(body.get("scheduled_for") or ""),
            run_id=str(body.get("run_id") or ""),
        )


def schedule_run_id(key: str, name: str, scheduled_for: str) -> str:
    return f"{key}:{name}:{scheduled_for}"

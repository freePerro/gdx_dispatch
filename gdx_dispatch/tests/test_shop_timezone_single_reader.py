"""The shop's timezone has one reader and one default (GDXA-1618).

Five readers of ``AppSettings.timezone`` had grown three behaviours for a
missing row: America/New_York in three of them, ``GDX_BUSINESS_TZ`` then
America/Chicago in two, one of which never read the setting at all. Prod
holds a row, so they agreed there by luck; a fresh install or a blank zone
put the planner and the payroll calendar on different days.

``core.pay_periods`` owns the read (``shop_tz_name_from_settings`` for a
session, ``shop_tz_name`` for a row already loaded) and the default
(``DEFAULT_SHOP_TZ``). This file rejects every other shape of it:

- ``AppSettings.timezone`` named anywhere but pay_periods;
- a zone read with a fallback beside it (``x.timezone or "..."``,
  ``getattr(x, "timezone") or ...``, ``x.timezone if x else "..."``);
- ``getattr(x, "timezone", <default>)``, the fallback in the third argument;
- raw SQL naming ``app_settings`` and ``timezone``;
- the ``GDX_BUSINESS_TZ`` environment override;
- either rival default's name as a literal (America/New_York, America/Chicago).

It scans source, not behaviour, so a reader spelled another way gets past it:
``tz = row.timezone`` with the fallback on a later line, a bare
``ZoneInfo(row.timezone)``, ``AppSettings`` imported under another name.
Telling a settings row's ``.timezone`` from a location's needs types the AST
does not have. ``test_the_scan_catches_each_shape`` feeds it each spelling it
does claim to catch.
"""
from __future__ import annotations

import ast
import logging
import re
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from gdx_dispatch.core import pay_periods
from gdx_dispatch.core.pay_periods import (
    DEFAULT_SHOP_TZ,
    shop_tz_name,
    shop_tz_name_from_settings,
)

PKG = Path(__file__).resolve().parents[1]
OWNER = "core/pay_periods.py"
SKIP_DIRS = {"tests", "migrations", "frontend", "node_modules", "__pycache__"}

#: Readers that other owners are rerouting in sibling issues of GDXA-1616.
#: INTERIM: each line is deleted when its fix lands, and
#: ``test_the_allowlist_is_not_stale`` fails until it is, so the list only
#: shrinks. Never add a line to make a new reader pass; route it through
#: pay_periods instead.
ALLOWLIST: dict[str, str] = {
    "services/customer_statements.py": "money-billing, GDXA-1616 child (_shop_tz)",
    "modules/bank_feeds/service.py": "back-office-books, GDXA-1616 child (tenant_zoneinfo)",
    "routers/ai.py": "ai-mcp, GDXA-1616 child (_business_tz)",
    "services/planner_today.py": "jobs-dispatch, GDXA-1616 child (GDX_BUSINESS_TZ)",
    # Not in the triage; found by this scan and deferred to platform-core:
    # the settings GET serializes `row.timezone or "America/New_York"` and the
    # create path writes the literal.
    "routers/settings.py": "platform-core, deferred (found, not filed)",
    # Not in the triage; money-billing: `else "America/New_York"` when no session.
    "modules/billing_terms/service.py": "money-billing, deferred (found, not filed)",
    # Column defaults: the Python-side default a new row is written with.
    # ``test_default_is_the_column_default`` pins AppSettings' to DEFAULT_SHOP_TZ.
    "models/tenant_models.py": "AppSettings.timezone column default",
    "core/tenant_settings.py": "Tenant.timezone column default (control plane)",
    # customers-crm: a service location's own zone, defaulting to Chicago.
    # Not the shop setting, but a third default; deferred (found, not filed).
    "core/locations.py": "customers-crm, ServiceLocation.timezone default (found, not filed)",
}

RIVAL_DEFAULTS = {DEFAULT_SHOP_TZ, "America/Chicago"}
_RAW_SQL = re.compile(r"\bapp_settings\b.*\btimezone\b|\btimezone\b.*\bapp_settings\b", re.I | re.S)


def _is_timezone_read(node: ast.AST) -> bool:
    if isinstance(node, ast.Attribute) and node.attr == "timezone":
        return True
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value == "timezone"
    )


def findings(source: str) -> list[tuple[int, str]]:
    """(line, shape) for every shop-zone read or default in ``source``."""
    out: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "timezone"
            and isinstance(node.value, ast.Name)
            and node.value.id == "AppSettings"
        ):
            out.append((node.lineno, "AppSettings.timezone"))
        elif isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            if _is_timezone_read(node.values[0]):
                out.append((node.lineno, "zone read `or` fallback"))
        elif isinstance(node, ast.IfExp) and _is_timezone_read(node.body):
            out.append((node.lineno, "zone read `if … else` fallback"))
        elif (
            _is_timezone_read(node)
            and isinstance(node, ast.Call)
            and len(node.args) == 3
            # None is "no fallback"; a bool is DateTime's `timezone=` flag.
            and not (isinstance(node.args[2], ast.Constant) and node.args[2].value in (None, True, False))
        ):
            out.append((node.lineno, "getattr timezone default"))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value == "GDX_BUSINESS_TZ":
                out.append((node.lineno, "GDX_BUSINESS_TZ"))
            elif node.value in RIVAL_DEFAULTS:
                out.append((node.lineno, f"{node.value!r} literal"))
            elif _RAW_SQL.search(node.value):
                out.append((node.lineno, "raw SQL app_settings.timezone"))
    return sorted(out)


def _sources() -> dict[str, list[tuple[int, str]]]:
    hits: dict[str, list[tuple[int, str]]] = {}
    for path in PKG.rglob("*.py"):
        rel = path.relative_to(PKG)
        if SKIP_DIRS.intersection(rel.parts):
            continue
        key = rel.as_posix()
        if key == OWNER:
            continue
        found = findings(path.read_text(encoding="utf-8"))
        if found:
            hits[key] = found
    return hits


def test_the_shop_zone_is_read_only_in_pay_periods():
    stray = {k: v for k, v in _sources().items() if k not in ALLOWLIST}
    assert not stray, (
        "Read the shop timezone through core.pay_periods "
        "(shop_tz_name_from_settings(db) or shop_tz_name(row)) and take the "
        f"default from DEFAULT_SHOP_TZ. Found: {stray}"
    )


def test_the_allowlist_is_not_stale():
    hits = _sources()
    stale = sorted(k for k in ALLOWLIST if k not in hits)
    assert not stale, f"These no longer read the zone themselves; delete their ALLOWLIST lines: {stale}"


@pytest.mark.parametrize(
    "snippet",
    [
        "x = db.query(AppSettings.timezone).scalar()",
        'tz = row.timezone or "UTC"',
        'tz = getattr(settings, "timezone", None) or fallback',
        'tz = row.timezone if row and row.timezone else "UTC"',
        'tz = os.getenv("GDX_BUSINESS_TZ", "UTC")',
        'def f(tz="America/New_York"): ...',
        'tz = ZoneInfo("America/Chicago")',
        'tz = getattr(settings, "timezone", "UTC")',
        'db.execute(text("SELECT timezone FROM app_settings LIMIT 1"))',
    ],
)
def test_the_scan_catches_each_shape(snippet):
    assert findings(snippet), snippet


def test_the_scan_leaves_unrelated_timezones_alone():
    clean = (
        "from datetime import timezone\n"
        "a = datetime.now(timezone.utc)\n"
        "b = DateTime(timezone=True)\n"
        "c = loc.timezone\n"
        "d = shop_tz_name(row)\n"
        'e = getattr(settings, "timezone", None)\n'
        'f = getattr(col.type, "timezone", False)\n'
    )
    assert findings(clean) == []


def test_default_is_the_column_default():
    """Settings creates a missing row with the column default; a fallback that
    differed would move the shop calendar the moment the row appeared."""
    from gdx_dispatch.models.tenant_models import AppSettings

    assert AppSettings.__table__.c.timezone.default.arg == DEFAULT_SHOP_TZ


@pytest.fixture(autouse=True)
def _fresh_warnings(monkeypatch):
    monkeypatch.setattr(pay_periods, "_WARNED", set())


def test_shop_tz_name_from_a_row(caplog):
    class Row:
        timezone = "America/Chicago"

    class Blank:
        timezone = ""

    assert shop_tz_name(Row()) == "America/Chicago"
    with caplog.at_level(logging.WARNING, logger=pay_periods.__name__):
        assert shop_tz_name(None) == DEFAULT_SHOP_TZ
        assert shop_tz_name(Blank()) == DEFAULT_SHOP_TZ
    messages = [r.getMessage() for r in caplog.records if r.name == pay_periods.__name__]
    assert any("no settings row" in m for m in messages)
    assert any("blank zone" in m for m in messages)


def test_an_unusable_stored_zone_gets_the_default_not_utc_or_a_crash(caplog):
    """Settings refused a non-IANA name only from GDXA-1640; an older row can
    hold one. The labor report passes the name to bare ZoneInfo, so before
    this it would have raised."""
    from gdx_dispatch.modules.timeclock.service import daily_labor_report

    class Legacy:
        timezone = "Central Standard Time"

    with caplog.at_level(logging.WARNING, logger=pay_periods.__name__):
        name = shop_tz_name(Legacy())
    assert name == DEFAULT_SHOP_TZ
    assert any("unusable zone" in r.getMessage() for r in caplog.records)

    class _NoRows:
        def execute(self, _stmt):
            class _R:
                def scalars(self):
                    return self

                def all(self):
                    return []

            return _R()

    assert daily_labor_report(date(2026, 9, 14), name, _NoRows()) == []


def test_the_fallback_warns_once_per_reason(caplog):
    with caplog.at_level(logging.WARNING, logger=pay_periods.__name__):
        for _ in range(5):
            shop_tz_name(None)
    assert sum("shop_timezone_unset" in r.getMessage() for r in caplog.records) == 1


def test_shop_tz_name_from_settings_real_session(caplog):
    """A real session, not a mock: the missing row logs and defaults, and a
    row is read."""
    from sqlalchemy.orm import sessionmaker

    from gdx_dispatch.models.tenant_models import AppSettings
    from gdx_dispatch.tests.conftest import make_fresh_db

    engine = make_fresh_db()
    Session = sessionmaker(bind=engine)
    try:
        with Session() as db, caplog.at_level(logging.WARNING, logger=pay_periods.__name__):
            assert shop_tz_name_from_settings(db) == DEFAULT_SHOP_TZ
            assert any("shop_timezone_unset" in r.getMessage() for r in caplog.records)
            db.add(AppSettings(timezone="America/Chicago"))
            db.commit()
        with Session() as db:
            assert shop_tz_name_from_settings(db) == "America/Chicago"
            # 04:30 UTC 14 Sep is still the 13th in Chicago.
            moment = datetime(2026, 9, 14, 4, 30, tzinfo=UTC)
            assert pay_periods.shop_today_from_settings(db, now=moment) == date(2026, 9, 13)
    finally:
        engine.dispose()

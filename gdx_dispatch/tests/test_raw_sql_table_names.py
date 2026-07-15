"""Guard: every table named in a raw SQL string must be a real ORM table.

The 2026-07-15 full-app walk found three endpoints whose raw ``text("... FROM
<table> ...")`` referenced tables that don't exist — ``timeclock_entries``
(→ ``timeclocks``), ``parts_needed`` (→ ``job_parts_needed``), and
``recurring_jobs`` (→ ``recurring_job_schedules``). Each was wrapped in a
try/except that swallowed the ``UndefinedTable`` and returned an empty/zero
fallback, so the bug was invisible: the tech's mobile day-summary silently
reported 0 hours and 0 parts, and every customer showed "No recurring jobs".

Raw SQL bypasses the ORM, so a typo'd table name compiles fine and only
fails at query time — exactly the blind spot unit tests on SQLite miss when
the query is defensively caught. This static scan closes the worst of it: it
greps the router sources for ``FROM``/``JOIN`` targets inside ``text(...)`` /
``_text(...)`` literals (plain and f-string) and asserts each resolves to a
table registered on ``TenantBase.metadata``.

**Scope, honestly:** this is a NAME-EXISTENCE smoke check, not a semantic
validator. It does NOT verify columns exist, that the table is the *right*
one (a real-but-empty table passes — see the timeclocks/time_entries case
in the same walk), or SQL built via ``.format()`` / string concatenation /
a variable passed to ``text(var)``. It is a cheap tripwire for the specific
"typo'd table name, swallowed at runtime" class, which is common enough to
be worth catching automatically. Column/target correctness still needs a
behavioral test with seeded data.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from gdx_dispatch.core.audit import TenantBase

# Import the full model registry so metadata is populated.
import gdx_dispatch.models  # noqa: F401  (side-effect registration)

ROUTERS_DIR = Path(__file__).resolve().parents[1] / "routers"

# Tables that live outside TenantBase metadata but genuinely EXIST (verified
# in the DB during the 2026-07-15 walk) — control-plane or plugin-host owned.
# Add here with a reason if a scan flags a real table this test can't see.
_KNOWN_EXTERNAL: set[str] = {
    "tenants",          # control plane (ControlBase), used by stripe_connect
    "plugin_registry",  # plugin-host managed (raw DDL, not ORM)
    "plugin_artifact",  # plugin-host managed (raw DDL, not ORM)
    "tenant_settings",  # raw-DDL table (exists in DB, not ORM), session_policy
}

# Matches the table(s) after FROM / JOIN: the first name, plus any
# comma-joined follow-ons (FROM a, b). Schema qualifier stripped in the
# scan. information_schema/pg_catalog system views are skipped there.
_TABLE_RX = re.compile(
    r"\b(?:FROM|JOIN)\s+([a-zA-Z_][\w.]*(?:\s*,\s*[a-zA-Z_][\w.]*)*)", re.IGNORECASE
)
# text( or _text(, optionally f-string, single/double/triple quoted.
_TEXT_RX = re.compile(
    r"""_?text\(\s*f?(?:"([^"]*)"|'([^']*)'|\"\"\"(.*?)\"\"\"|'''(.*?)''')""",
    re.DOTALL,
)


def _iter_text_sql():
    for path in sorted(ROUTERS_DIR.rglob("*.py")):
        src = path.read_text(errors="replace")
        for m in _TEXT_RX.finditer(src):
            sql = next(g for g in m.groups() if g is not None)
            yield path.name, sql


def _referenced_tables():
    tables: dict[str, str] = {}  # table -> "file: sql-snippet"
    for fname, sql in _iter_text_sql():
        for tm in _TABLE_RX.finditer(sql):
            # tm.group(1) may be "a, b" (comma join) and each may be
            # schema-qualified (public.x) — split and take the last segment.
            for raw in tm.group(1).split(","):
                raw = raw.strip().lower()
                # Skip system catalogs by their FULL (possibly schema-
                # qualified) name before stripping to the last segment.
                if raw.startswith(("information_schema", "pg_")):
                    continue
                name = raw.split(".")[-1]
                if not name:
                    continue
                tables.setdefault(
                    name, f"{fname}: ...{sql[max(0, tm.start()-10):tm.start()+40].strip()}..."
                )
    return tables


def test_raw_sql_from_join_tables_are_real_orm_tables():
    real = set(TenantBase.metadata.tables.keys()) | _KNOWN_EXTERNAL
    referenced = _referenced_tables()
    unknown = {t: where for t, where in referenced.items() if t not in real}
    assert not unknown, (
        "raw SQL references table(s) not registered on TenantBase.metadata "
        "(a typo here fails only at query time and is often swallowed by a "
        "try/except → silent wrong data):\n"
        + "\n".join(f"  {t!r} — {where}" for t, where in sorted(unknown.items()))
    )


def test_scan_actually_found_raw_sql():
    """Sanity: the scan must be seeing SQL, else a regex change silently
    disables the guard above."""
    assert len(list(_iter_text_sql())) > 5
    assert _referenced_tables(), "no FROM/JOIN tables parsed — regex broken"
